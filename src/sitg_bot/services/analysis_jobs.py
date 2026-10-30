"""Durable analysis execution and persistent artifact caching for training mode.

Agent C module (see `docs/training-mode-plan.md`). Analysis requests are
owner-scoped and idempotent; computation is coalesced per immutable artifact
and executed by durable ``training.analysis`` jobs claimed only by analysis
workers. Artifacts are never mutated after success, and failures are never
cached as successful empty data. Provider calls happen outside transactions
under compute leases; stale completions are fenced by lease tokens.
"""

import asyncio
import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import (
    case,
    delete,
    func,
    literal,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.game_settings import announcement_tokens
from sitg_bot.services.analysis_cache import public_base, run_cached_dissection
from sitg_bot.services.entitlements import (
    FEATURE_BEST_BUZZ,
    FEATURE_COMMON_KNOWLEDGE,
    FEATURE_DISSECTION,
    EntitlementService,
    FeatureDisabledError,
    QuotaExceededError,
)
from sitg_bot.services.reliable_delivery import DurableJobQueue
from sitg_bot.services.training_ai import (
    ANALYSIS_BEST_BUZZ,
    ANALYSIS_COMMON_KNOWLEDGE,
    ANALYSIS_DISSECTION,
    PROVIDER_CALL_METADATA,
    BuzzBoundary,
    BuzzRequest,
    CommonKnowledgeRequest,
    ExtractionRequest,
    FactCandidate,
    ProviderOutcomeUncertain,
    ProviderSettings,
    ProviderTimeout,
    QuestionText,
    fact_candidate_id,
    instruction_digest,
    packet_chunks,
    source_descriptor,
    source_fingerprint_of,
    source_from_descriptor,
    source_kind,
    text_fingerprint,
    validate_buzz_boundaries,
    validate_common_knowledge,
    validate_fact_candidates,
)
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    Base,
    DurableJobRecord,
    PacketVersionRecord,
    PlayerRecord,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
    TrainingBestBuzzCompletionRecord,
)

#: Durable job kind claimed only by analysis workers (kind-filtered claims).
TRAINING_ANALYSIS_JOB_KIND = "training.analysis"

FEATURE_BY_KIND = {
    ANALYSIS_DISSECTION: FEATURE_DISSECTION,
    ANALYSIS_BEST_BUZZ: FEATURE_BEST_BUZZ,
    ANALYSIS_COMMON_KNOWLEDGE: FEATURE_COMMON_KNOWLEDGE,
}

REQUEST_ACTIVE_STATUSES = ("queued", "running")


ANALYSIS_METADATA = Base.metadata
AnalysisBase = Base


class AnalysisRequestError(Exception):
    """Analysis request failure with a stable, adapter-facing error code."""

    code = "analysis_request_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AnalysisIdempotencyConflictError(AnalysisRequestError):
    code = "idempotency_conflict"


class AnalysisFeatureDisabledError(AnalysisRequestError):
    code = "feature_disabled"


class AnalysisQuotaExceededError(AnalysisRequestError):
    code = "quota_exceeded"


class AnalysisQueuedCapExceededError(AnalysisRequestError):
    code = "queued_cap_exceeded"


@dataclass(frozen=True, slots=True)
class AnalysisRequestView:
    """Owner-scoped projection of one analysis request and its job state."""

    request_id: str
    kind: str
    status: str
    progress: float | None
    error: str | None
    retryable: bool
    created_at: datetime
    completed_at: datetime | None


class AnalysisSourceLoader(Protocol):
    """Owner-scoped resolution of authorized question revisions for analysis.

    Implementations (wired by agents A and B) must verify packet access for
    the requesting trainee and return immutable revision texts; a cached
    artifact never grants access on its own.
    """

    async def load_question_texts(
        self, *, owner_player_id: UUID, source: Mapping[str, Any]
    ) -> tuple[QuestionText, ...]: ...


def analysis_artifact_key(
    kind: str,
    *,
    source_fingerprint: str,
    instruction_digest: str,
    language: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    tokenizer_version: str,
    owner_player_id: UUID | None,
) -> str:
    """Derive the artifact key: shared only for empty-instruction base work."""
    return text_fingerprint(
        "artifact",
        kind,
        source_fingerprint,
        instruction_digest,
        language,
        model,
        prompt_version,
        schema_version,
        tokenizer_version,
        "" if owner_player_id is None else str(owner_player_id),
    )


def analysis_job_key(artifact_id: UUID, generation: int) -> str:
    """Durable job deduplication key for one artifact computation generation."""
    return f"training.analysis:{artifact_id}:{generation}"


class AnalysisService:
    """Owner-scoped analysis requests with coalesced durable execution."""

    def __init__(
        self,
        database: Database,
        jobs: DurableJobQueue,
        provider: Any,
        settings: ProviderSettings,
        *,
        entitlements: EntitlementService | None = None,
        source_loader: AnalysisSourceLoader | None = None,
        lease_duration: timedelta = timedelta(minutes=10),
        max_job_attempts: int = 12,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Analysis lease duration must be positive")
        if max_job_attempts < 1:
            raise ValueError("Analysis job max attempts must be positive")
        self.database = database
        self.jobs = jobs
        self.provider = provider
        self.settings = settings
        self.entitlements = entitlements
        self.source_loader = source_loader
        self.lease_duration = lease_duration
        self.max_job_attempts = max_job_attempts
        self._provider_metadata: ContextVar[list | None] = ContextVar(
            "training_job_usage", default=None
        )

    async def request(
        self,
        session: AsyncSession,
        *,
        owner_player_id: UUID,
        source: Any,
        instructions: str = "",
        idempotency_key: str,
        private: bool = False,
        content_fingerprint: str | None = None,
        source_context: dict[str, Any] | None = None,
        units: int = 1,
        now: datetime | None = None,
    ) -> AnalysisRequestView:
        """Authorize, reserve quota, persist and enqueue within one transaction."""
        if not idempotency_key:
            raise ValueError("Analysis idempotency key is required")
        if type(units) is not int or units < 1:
            raise ValueError("Analysis quota units must be a positive integer")
        kind = source_kind(source)
        moment = now or datetime.now(UTC)
        normalized = instructions
        descriptor = source_descriptor(source)
        descriptor["private"] = private
        descriptor.update(source_context or {})
        descriptor["quota_units"] = units
        if content_fingerprint is not None:
            descriptor["content_fingerprint"] = content_fingerprint
        if self.source_loader is not None and hasattr(self.source_loader, "authorize_in_session"):
            await self.source_loader.authorize_in_session(session, owner_player_id, descriptor)
        if self.entitlements is not None:
            await session.get(
                PlayerRecord, owner_player_id,
                with_for_update={"key_share": True}, populate_existing=True,
            )
        existing = await self._request_row(session, idempotency_key)
        if existing is not None:
            if existing.owner_player_id != owner_player_id:
                raise AnalysisIdempotencyConflictError(
                    "Analysis idempotency key already belongs to another trainee"
                )
            if existing.instructions != instructions or existing.source != descriptor:
                raise AnalysisIdempotencyConflictError("Analysis request content changed")
            return self._view(existing, artifact=None)
        fingerprint = text_fingerprint(source_fingerprint_of(source), content_fingerprint or "")
        digest = instruction_digest(normalized)
        if kind == ANALYSIS_DISSECTION:
            fingerprint = text_fingerprint(
                "dissection-cache-v2", self.settings.provider, fingerprint
            )
            digest = text_fingerprint(digest, *sorted(descriptor.get("tag_names", [])))
        if private:
            # Same-request checkpoints are not a reusable owner cache.
            digest = text_fingerprint(digest, idempotency_key)
        shared = not normalized and not private
        artifact_owner = None if shared else owner_player_id
        key = analysis_artifact_key(
            kind,
            source_fingerprint=fingerprint,
            instruction_digest=digest,
            language=descriptor["language"],
            model=self.settings.model,
            prompt_version=self.settings.prompt_version,
            schema_version=self.settings.schema_version,
            tokenizer_version=self.settings.tokenizer_version,
            owner_player_id=artifact_owner,
        )
        reusable = None
        if kind == ANALYSIS_DISSECTION and shared:
            reusable = await public_base(
                self, session, fingerprint, descriptor.get("tag_names", []), require_coverage=True
            )
        artifact = reusable or await self._artifact_for_key(
            session,
            key,
            kind=kind,
            shared=shared,
            owner_player_id=artifact_owner,
            source_fingerprint=fingerprint,
            instruction_digest=digest,
            language=descriptor["language"],
            model=self.settings.model,
            prompt_version=self.settings.prompt_version,
            schema_version=self.settings.schema_version,
            tokenizer_version=self.settings.tokenizer_version,
            created_at=moment,
        )
        cache_hit = artifact.status in ("succeeded", "partially_succeeded")
        await self._authorize_request(
            session,
            owner_player_id=owner_player_id,
            kind=kind,
            idempotency_key=idempotency_key,
            cache_hit=cache_hit,
            units=units,
            moment=moment,
        )
        if artifact.status in ("failed", "cancelled"):
            artifact.generation += 1
            artifact.status = "pending"
            artifact.payload = None
            artifact.checkpoint = None
            artifact.last_error = None
            artifact.lease_token = None
            artifact.lease_expires_at = None
            artifact.completed_at = None
            artifact.failed_at = None
        row = TrainingAnalysisRequestRecord(
            request_id=idempotency_key,
            owner_player_id=owner_player_id,
            kind=kind,
            artifact_id=artifact.id,
            instructions=normalized,
            source=descriptor,
            created_at=moment,
        )
        session.add(row)
        if cache_hit:
            row.status = artifact.status
            row.completed_at = moment
            await self._record_packet_completion(session, row, moment)
        else:
            await DurableJobQueue.schedule_in_session(
                session,
                TRAINING_ANALYSIS_JOB_KIND,
                analysis_job_key(artifact.id, artifact.generation),
                {"artifact_id": str(artifact.id), "generation": artifact.generation},
                priority=200,
                max_attempts=self.max_job_attempts,
            )
        await session.flush()
        return self._view(row, artifact=artifact)

    async def _authorize_request(
        self,
        session: AsyncSession,
        *,
        owner_player_id: UUID,
        kind: str,
        idempotency_key: str,
        cache_hit: bool,
        units: int,
        moment: datetime,
    ) -> None:
        """Feature authorization, queued caps, quota and cache-hit charging."""
        if self.entitlements is None:
            return
        feature = FEATURE_BY_KIND[kind]
        policy = await self.entitlements.policy_for(session, owner_player_id, feature)
        if not policy.enabled:
            raise AnalysisFeatureDisabledError(f"Feature {feature} is disabled")
        queued = await session.scalar(
            select(func.count())
            .select_from(TrainingAnalysisRequestRecord)
            .where(
                TrainingAnalysisRequestRecord.owner_player_id == owner_player_id,
                TrainingAnalysisRequestRecord.kind == kind,
                TrainingAnalysisRequestRecord.status.in_(REQUEST_ACTIVE_STATUSES),
            )
        )
        if (
            not cache_hit
            and policy.queued_request_cap is not None
            and (queued or 0) >= policy.queued_request_cap
        ):
            raise AnalysisQueuedCapExceededError(
                f"Feature {feature} has too much queued or in-flight work"
            )
        if cache_hit and not policy.cache_hit_charging:
            return
        try:
            await self.entitlements.authorize(
                session,
                player_id=owner_player_id,
                feature=feature,
                request_id=idempotency_key,
                units=units,
                now=moment,
            )
        except FeatureDisabledError as error:
            raise AnalysisFeatureDisabledError(error.message) from error
        except QuotaExceededError as error:
            raise AnalysisQuotaExceededError(error.message) from error
        if cache_hit:
            await self.entitlements.settle(
                session,
                player_id=owner_player_id,
                feature=feature,
                request_id=idempotency_key,
                used_units=units,
                provider_usage=self._usage_payload(cache_hit=True),
                now=moment,
            )

    async def status(
        self, *, owner_player_id: UUID, request_id: str, now: datetime | None = None
    ) -> AnalysisRequestView | None:
        """Owner-scoped request status with lazy stale-lease reconciliation."""
        moment = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            row = await self._request_row(session, request_id)
            if row is None or row.owner_player_id != owner_player_id:
                return None
            if self.source_loader is not None and hasattr(
                self.source_loader, "authorize_in_session"
            ):
                await self.source_loader.authorize_in_session(session, owner_player_id, row.source)
            artifact = None
            if row.status in REQUEST_ACTIVE_STATUSES:
                artifact = await session.get(TrainingAnalysisArtifactRecord, row.artifact_id)
                if artifact is not None:
                    await self._reconcile_artifact(session, artifact, moment)
                    await session.refresh(row)
            return self._view(row, artifact=artifact)

    async def result(
        self, *, owner_player_id: UUID, request_id: str
    ) -> dict[str, Any] | None:
        """Return the validated artifact payload for a terminal request."""
        async with self.database.transaction() as session:
            row = await self._request_row(session, request_id)
            if row is None or row.owner_player_id != owner_player_id:
                return None
            if self.source_loader is not None and hasattr(
                self.source_loader, "authorize_in_session"
            ):
                await self.source_loader.authorize_in_session(session, owner_player_id, row.source)
            if row.status not in ("succeeded", "partially_succeeded"):
                return None
            artifact = await session.get(TrainingAnalysisArtifactRecord, row.artifact_id)
            if artifact is None or artifact.payload is None:
                return None
            return dict(artifact.payload)

    async def cancel(
        self, *, owner_player_id: UUID, request_id: str, now: datetime | None = None
    ) -> bool:
        """Cancel one trainee's request without cancelling work others need."""
        moment = now or datetime.now(UTC)
        job_id: UUID | None = None
        async with self.database.transaction() as session:
            row = await self._request_row(session, request_id)
            if row is None or row.owner_player_id != owner_player_id:
                return False
            await session.get(
                PlayerRecord, owner_player_id,
                with_for_update={"key_share": True}, populate_existing=True,
            )
            artifact = await session.get(
                TrainingAnalysisArtifactRecord, row.artifact_id,
                with_for_update={"key_share": True}, populate_existing=True,
            )
            await session.refresh(row)
            if row.status not in REQUEST_ACTIVE_STATUSES:
                return False
            artifact = await session.get(
                TrainingAnalysisArtifactRecord, row.artifact_id, with_for_update=True
            )
            await session.refresh(row)
            if row.status not in REQUEST_ACTIVE_STATUSES:
                return False
            row.status = "cancelled"
            row.completed_at = moment
            if self.entitlements is not None:
                await self.entitlements.release(
                    session,
                    player_id=owner_player_id,
                    feature=FEATURE_BY_KIND[row.kind],
                    request_id=request_id,
                    now=moment,
                )
            if artifact is not None and artifact.status in ("pending", "running"):
                remaining = await session.scalar(
                    select(func.count())
                    .select_from(TrainingAnalysisRequestRecord)
                    .where(
                        TrainingAnalysisRequestRecord.artifact_id == artifact.id,
                        TrainingAnalysisRequestRecord.status.in_(REQUEST_ACTIVE_STATUSES),
                    )
                )
                if not remaining:
                    artifact.status = "cancelled"
                    artifact.lease_token = None
                    artifact.lease_expires_at = None
                    job_id = await session.scalar(
                        select(DurableJobRecord.id).where(
                            DurableJobRecord.deduplication_key
                            == analysis_job_key(artifact.id, artifact.generation)
                        )
                    )
        if job_id is not None:
            await self.jobs.cancel(job_id)
        return True

    async def execute_job(self, payload: Mapping[str, Any]) -> None:
        """Execute one durable analysis job: lease, compute, validate, persist."""
        artifact_id = UUID(payload["artifact_id"])
        generation = int(payload.get("generation", 1))
        async with self.database.transaction() as session:
            artifact = await session.get(
                TrainingAnalysisArtifactRecord, artifact_id,
                with_for_update={"key_share": True}, populate_existing=True,
            )
            if artifact is None:
                raise LookupError(f"Analysis artifact {artifact_id} is missing")
            if artifact.generation != generation:
                return
            if artifact.status not in ("pending", "running"):
                return
            moment = datetime.now(UTC)
            if (
                artifact.status == "running"
                and artifact.lease_expires_at is not None
                and artifact.lease_expires_at > moment
                and artifact.lease_token is not None
            ):
                raise RuntimeError("Analysis compute lease is still held by another worker")
            if any(
                getattr(artifact, field) != getattr(self.settings, field)
                for field in ("model", "prompt_version", "schema_version", "tokenizer_version")
            ):
                await self._fail_artifact(
                    session, artifact, "Analysis execution profile changed", moment
                )
                return
            artifact.status = "running"
            artifact.lease_token = uuid4()
            artifact.lease_expires_at = moment + self.lease_duration
            lease_token = artifact.lease_token
            rows = (
                (
                    await session.execute(
                        select(TrainingAnalysisRequestRecord)
                        .where(
                            TrainingAnalysisRequestRecord.artifact_id == artifact_id,
                            TrainingAnalysisRequestRecord.status.in_(REQUEST_ACTIVE_STATUSES),
                        )
                        .order_by(TrainingAnalysisRequestRecord.created_at)
                    )
                )
                .scalars()
                .all()
            )
            authorized = []
            for row in rows:
                if self.entitlements is not None:
                    # Serialize worker admissions without changing packet/account lock order.
                    await session.execute(
                        select(
                            func.pg_advisory_xact_lock(
                                int(text_fingerprint(str(row.owner_player_id), row.kind)[:15], 16)
                            )
                        )
                    )
                    policy = await self.entitlements.policy_for(
                        session, row.owner_player_id, FEATURE_BY_KIND[row.kind]
                    )
                    if policy.in_flight_request_cap is not None:
                        running = await session.scalar(
                            select(
                                func.count(func.distinct(TrainingAnalysisRequestRecord.artifact_id))
                            ).where(
                                TrainingAnalysisRequestRecord.owner_player_id
                                == row.owner_player_id,
                                TrainingAnalysisRequestRecord.kind == row.kind,
                                TrainingAnalysisRequestRecord.status == "running",
                                TrainingAnalysisRequestRecord.artifact_id != artifact_id,
                            )
                        )
                        if (running or 0) >= policy.in_flight_request_cap:
                            raise AnalysisQueuedCapExceededError("In-flight analysis cap reached")
                if self.source_loader is not None and hasattr(
                    self.source_loader, "authorize_in_session"
                ):
                    try:
                        await self.source_loader.authorize_in_session(
                            session, row.owner_player_id, row.source
                        )
                    except (PermissionError, LookupError):
                        row.status, row.completed_at, row.error = (
                            "cancelled",
                            moment,
                            "Source access revoked",
                        )
                        if self.entitlements is not None:
                            await self.entitlements.release(
                                session,
                                player_id=row.owner_player_id,
                                feature=FEATURE_BY_KIND[row.kind],
                                request_id=row.request_id,
                                now=moment,
                            )
                        continue
                row.status = "running"
                authorized.append(row)
            rows = authorized
            if not rows:
                await self._abandon(session, artifact_id, lease_token)
                return
            instructions = rows[0].instructions
            descriptor = dict(rows[0].source)
            initiating_owner = rows[0].owner_player_id
            kind = artifact.kind
            previous_calls = list((artifact.provider_usage or {}).get("calls", []))
        usage_token = self._provider_metadata.set(previous_calls)
        try:
            if kind == ANALYSIS_COMMON_KNOWLEDGE:
                await self._run_common_knowledge(
                    artifact_id, lease_token, descriptor, initiating_owner
                )
            else:
                await self._run_packet_scan(
                    artifact_id, lease_token, kind, instructions, descriptor, initiating_owner
                )
        except ProviderOutcomeUncertain as error:
            await self._record_uncertain(artifact_id, lease_token, error)
            raise
        except Exception:
            async with self.database.transaction() as session:
                artifact = await session.get(
                    TrainingAnalysisArtifactRecord, artifact_id,
                    with_for_update={"key_share": True}, populate_existing=True,
                )
                if artifact is not None and artifact.lease_token == lease_token:
                    artifact.provider_request_ids = self._provider_request_ids()
                    artifact.provider_usage = self._usage_payload()
            raise
        finally:
            self._provider_metadata.reset(usage_token)

    async def _run_common_knowledge(
        self,
        artifact_id: UUID,
        lease_token: UUID,
        descriptor: Mapping[str, Any],
        initiating_owner: UUID,
    ) -> None:
        _ = initiating_owner
        source = source_from_descriptor(descriptor)
        request = CommonKnowledgeRequest(
            fact_content=source.fact_content,
            language=source.language,
            population=source.population,
        )
        estimate = await self._provider_call(self.provider.estimate_common_knowledge, request)
        value = validate_common_knowledge(estimate.value)
        await self._write_checkpoint(
            artifact_id, lease_token, {"chunks_done": 1, "chunks_total": 1}
        )
        await self._write_terminal(
            artifact_id,
            lease_token,
            status="succeeded",
            payload={
                "value": value,
                "language": source.language,
                "population": source.population,
            },
        )

    async def _run_packet_scan(
        self,
        artifact_id: UUID,
        lease_token: UUID,
        kind: str,
        instructions: str,
        descriptor: Mapping[str, Any],
        initiating_owner: UUID,
    ) -> None:
        if self.source_loader is None:
            raise RuntimeError("An analysis source loader is required for packet scans")
        if kind == ANALYSIS_DISSECTION and await run_cached_dissection(
            self, artifact_id, lease_token, instructions, descriptor
        ):
            return
        questions = await self.source_loader.load_question_texts(
            owner_player_id=initiating_owner, source=descriptor
        )
        if not questions:
            raise RuntimeError("The analysis source has no authorized question revisions")
        chunks = packet_chunks(questions, max_chunk_characters=self.settings.max_chunk_characters)
        total = len(chunks)
        if total > 16:
            raise ValueError("Analysis accepts at most 16 source chunks per packet")
        async with self.database.transaction() as session:
            artifact = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
            assert artifact is not None
            checkpoint = dict(artifact.checkpoint or {})
        chunks_done = int(checkpoint.get("chunks_done", 0))
        partial: list[dict[str, Any]] = list(checkpoint.get("partial", []))
        language = str(descriptor.get("language", "en"))
        for index in range(chunks_done, total):
            async with self.database.transaction() as session:
                active = await session.scalar(
                    select(func.count())
                    .select_from(TrainingAnalysisRequestRecord)
                    .where(
                        TrainingAnalysisRequestRecord.artifact_id == artifact_id,
                        TrainingAnalysisRequestRecord.status.in_(REQUEST_ACTIVE_STATUSES),
                    )
                )
                if not active:
                    await self._abandon(session, artifact_id, lease_token)
                    return
            chunk = chunks[index]
            if kind == ANALYSIS_DISSECTION:
                result = await self._provider_call(
                    self.provider.extract_facts,
                    ExtractionRequest((chunk,), instructions, language,
                                      tuple(descriptor.get("tag_names", []))),
                )
                result = validate_fact_candidates(
                    result,
                    allowed_question_revision_ids=chunk.question_revision_ids,
                    max_output_facts=self.settings.max_output_facts - len(partial),
                    allowed_tag_names=descriptor.get("tag_names", []),
                )
                partial.extend(
                    {
                        "candidate_id": candidate.candidate_id,
                        "title": candidate.title,
                        "description": candidate.description,
                        "links": list(candidate.links),
                        "question_revision_id": str(candidate.question_revision_id),
                        "tags": list(candidate.tags),
                    }
                    for candidate in result
                )
            else:
                result = await self._provider_call(
                    self.provider.best_buzz_boundaries,
                    BuzzRequest(
                        (chunk,), language, int(descriptor.get("question_token_target_chars", 10))
                    ),
                )
                target_chars = int(descriptor.get("question_token_target_chars", 10))
                chunk_questions_by_id = {
                    question.question_revision_id: question
                    for question in questions
                    if question.question_revision_id in chunk.question_revision_ids
                }
                result = validate_buzz_boundaries(
                    result,
                    token_counts={
                        key: len(announcement_tokens(question.text, target_chars))
                        for key, question in chunk_questions_by_id.items()
                    },
                    text_digests={
                        key: text_fingerprint(question.text)
                        for key, question in chunk_questions_by_id.items()
                    },
                )
                partial.extend(
                    {
                        "question_revision_id": str(boundary.question_revision_id),
                        "text_digest": boundary.text_digest,
                        "token_index": boundary.token_index,
                    }
                    for boundary in result
                )
            await self._write_checkpoint(
                artifact_id,
                lease_token,
                {
                    "chunks_done": index + 1,
                    "chunks_total": total,
                    "partial": partial,
                    "cache_route": "full",
                },
            )
        await self._finalize_packet_scan(
            artifact_id, lease_token, kind, questions, partial, descriptor
        )

    async def _finalize_packet_scan(
        self,
        artifact_id: UUID,
        lease_token: UUID,
        kind: str,
        questions: tuple[QuestionText, ...],
        partial: list[dict[str, Any]],
        descriptor: Mapping[str, Any],
    ) -> None:
        allowed = {question.question_revision_id for question in questions}
        if kind == ANALYSIS_DISSECTION:
            candidates = tuple(
                FactCandidate(
                    item["candidate_id"],
                    item["title"],
                    item["description"],
                    tuple(item["links"]),
                    UUID(item["question_revision_id"]),
                    tuple(item.get("tags", [])),
                )
                for item in partial
            )
            validated = validate_fact_candidates(
                candidates,
                allowed_question_revision_ids=allowed,
                max_output_facts=self.settings.max_output_facts,
                allowed_tag_names=descriptor.get("tag_names", []),
            )
            references = {question.question_revision_id: set(re.findall(
                r"https?://[^\s<>\[\]\"']+", question.text
            )) for question in questions}
            safe = []
            for candidate in validated:
                links = tuple(url for url in candidate.links
                              if url in references[candidate.question_revision_id])
                safe.append(replace(candidate, links=links, candidate_id=fact_candidate_id(
                    candidate.question_revision_id, candidate.title, candidate.description, links
                )))
            safe = list({value.candidate_id: value for value in safe}.values())
            payload: dict[str, Any] = {
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "title": candidate.title,
                        "description": candidate.description,
                        "links": list(candidate.links),
                        "question_revision_id": str(candidate.question_revision_id),
                        "tags": list(candidate.tags),
                    }
                    for candidate in safe
                ],
                "cache_version": 2,
                "tag_coverage": sorted(descriptor.get("tag_names", [])),
                "route": "full",
            }
        else:
            boundaries = tuple(
                BuzzBoundary(
                    UUID(item["question_revision_id"]),
                    item["text_digest"],
                    item["token_index"],
                )
                for item in partial
            )
            target_chars = int(descriptor.get("question_token_target_chars", 10))
            token_counts = {
                question.question_revision_id: len(
                    announcement_tokens(question.text, target_chars)
                )
                for question in questions
            }
            text_digests = {
                question.question_revision_id: text_fingerprint(question.text)
                for question in questions
            }
            validated_boundaries = validate_buzz_boundaries(
                boundaries, token_counts=token_counts, text_digests=text_digests
            )
            payload = {
                "boundaries": [
                    {
                        "question_revision_id": str(boundary.question_revision_id),
                        "text_digest": boundary.text_digest,
                        "token_index": boundary.token_index,
                        "token_count": token_counts[boundary.question_revision_id],
                    }
                    for boundary in validated_boundaries
                ],
                "tokenizer_version": self.settings.tokenizer_version,
                "question_token_target_chars": target_chars,
            }
        await self._write_terminal(artifact_id, lease_token, status="succeeded", payload=payload)

    async def _write_checkpoint(
        self, artifact_id: UUID, lease_token: UUID, checkpoint: Mapping[str, Any]
    ) -> None:
        """Persist a chunk checkpoint and renew the compute lease, fenced."""
        async with self.database.transaction() as session:
            updated = await session.execute(
                update(TrainingAnalysisArtifactRecord)
                .where(
                    TrainingAnalysisArtifactRecord.id == artifact_id,
                    TrainingAnalysisArtifactRecord.lease_token == lease_token,
                    TrainingAnalysisArtifactRecord.status == "running",
                )
                .values(
                    checkpoint=dict(checkpoint),
                    provider_request_ids=self._provider_request_ids(),
                    provider_usage=self._usage_payload(),
                    lease_expires_at=datetime.now(UTC) + self.lease_duration,
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("Stale analysis compute lease")

    async def _write_terminal(
        self,
        artifact_id: UUID,
        lease_token: UUID,
        *,
        status: str,
        payload: Mapping[str, Any] | None,
        error: str | None = None,
    ) -> None:
        """Fenced immutable terminal write plus request and ledger settlement."""
        moment = datetime.now(UTC)
        async with self.database.transaction() as session:
            updated = await session.execute(
                update(TrainingAnalysisArtifactRecord)
                .where(
                    TrainingAnalysisArtifactRecord.id == artifact_id,
                    TrainingAnalysisArtifactRecord.lease_token == lease_token,
                    TrainingAnalysisArtifactRecord.status == "running",
                )
                .values(
                    status=status,
                    payload=dict(payload) if payload is not None else None,
                    last_error=error,
                    completed_at=(
                        moment if status in ("succeeded", "partially_succeeded") else None
                    ),
                    failed_at=moment if status == "failed" else None,
                    lease_token=None,
                    lease_expires_at=None,
                    provider_request_ids=self._provider_request_ids(),
                    provider_usage=self._usage_payload(),
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("Stale analysis compute lease")
            await self._complete_requests(session, artifact_id, status, error, moment)

    @staticmethod
    async def _record_packet_completion(session, row, moment):
        if row.kind != ANALYSIS_BEST_BUZZ or row.status != "succeeded":
            return
        # The analysis service also supports virtual fixture sources. Only
        # persisted catalogue versions have a corresponding card/history row.
        await session.execute(pg_insert(TrainingBestBuzzCompletionRecord).from_select(
            ["owner_id", "packet_version_id", "calculated_at"],
            select(literal(row.owner_player_id), PacketVersionRecord.id, literal(moment)).where(
                PacketVersionRecord.id == UUID(row.source["packet_revision_id"])
            ),
        ).on_conflict_do_nothing())

    async def _complete_requests(
        self,
        session: AsyncSession,
        artifact_id: UUID,
        status: str,
        error: str | None,
        moment: datetime,
    ) -> None:
        """Move active requests to a terminal state and settle or refund."""
        rows = (
            (
                await session.execute(
                    select(TrainingAnalysisRequestRecord).where(
                        TrainingAnalysisRequestRecord.artifact_id == artifact_id,
                        TrainingAnalysisRequestRecord.status.in_(REQUEST_ACTIVE_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        settle = status in ("succeeded", "partially_succeeded")
        for row in rows:
            allowed = True
            if self.source_loader is not None and hasattr(
                self.source_loader, "authorize_in_session"
            ):
                try:
                    await self.source_loader.authorize_in_session(
                        session, row.owner_player_id, row.source
                    )
                except (PermissionError, LookupError):
                    allowed = False
            row.status = status if allowed else "cancelled"
            row.error = error[:2000] if error else None
            row.completed_at = moment
            await self._record_packet_completion(session, row, moment)
            if self.entitlements is None:
                continue
            feature = FEATURE_BY_KIND[row.kind]
            if settle and allowed:
                await self.entitlements.settle(
                    session,
                    player_id=row.owner_player_id,
                    feature=feature,
                    request_id=row.request_id,
                    used_units=row.source.get("quota_units", 1),
                    provider_usage=self._usage_payload(),
                    now=moment,
                )
            else:
                await self.entitlements.release(
                    session,
                    player_id=row.owner_player_id,
                    feature=feature,
                    request_id=row.request_id,
                    now=moment,
                )

    async def _reconcile_artifact(
        self,
        session: AsyncSession,
        artifact: TrainingAnalysisArtifactRecord,
        moment: datetime,
    ) -> None:
        """Fence stale computations whose durable job can no longer finish."""
        artifact = await session.scalar(
            select(TrainingAnalysisArtifactRecord)
            .where(TrainingAnalysisArtifactRecord.id == artifact.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if artifact is None:
            return
        if artifact.status not in ("pending", "running"):
            await self._complete_requests(
                session, artifact.id, artifact.status, artifact.last_error, moment
            )
            return
        job = await session.scalar(
            select(DurableJobRecord).where(
                DurableJobRecord.deduplication_key
                == analysis_job_key(artifact.id, artifact.generation)
            )
        )
        if artifact.status == "pending":
            if job is None or job.status in ("failed", "cancelled"):
                await self._fail_artifact(session, artifact, "Analysis job is missing", moment)
            return
        lease_expired = artifact.lease_expires_at is None or artifact.lease_expires_at <= moment
        if (
            job is None
            or job.status in ("failed", "cancelled")
            or (lease_expired and job.status == "succeeded")
        ):
            error = (job.last_error if job is not None else None) or (
                "Analysis execution ended without a terminal artifact"
            )
            await self._fail_artifact(session, artifact, error, moment)

    async def _fail_artifact(
        self,
        session: AsyncSession,
        artifact: TrainingAnalysisArtifactRecord,
        error: str,
        moment: datetime,
    ) -> None:
        """Mark the artifact failed and refund the affected requests."""
        artifact.status = "failed"
        artifact.failed_at = moment
        artifact.last_error = error[:2000]
        artifact.lease_token = None
        artifact.lease_expires_at = None
        await self._complete_requests(session, artifact.id, "failed", error, moment)

    async def _abandon(
        self, session: AsyncSession, artifact_id: UUID, lease_token: UUID
    ) -> None:
        """Abandon computation after every waiter cancelled; fenced by lease."""
        updated = await session.execute(
            update(TrainingAnalysisArtifactRecord)
            .where(
                TrainingAnalysisArtifactRecord.id == artifact_id,
                TrainingAnalysisArtifactRecord.lease_token == lease_token,
                TrainingAnalysisArtifactRecord.status == "running",
            )
            .values(status="cancelled", lease_token=None, lease_expires_at=None)
        )
        if updated.rowcount != 1:
            raise RuntimeError("Stale analysis compute lease")

    async def _record_uncertain(
        self, artifact_id: UUID, lease_token: UUID, error: ProviderOutcomeUncertain
    ) -> None:
        """Record possibly-billed upstream request IDs before retrying."""
        async with self.database.transaction() as session:
            model = TrainingAnalysisArtifactRecord
            request_ids = [error.provider_request_id]
            await session.execute(
                update(model).where(
                    model.id == artifact_id,
                    model.lease_token == lease_token,
                    model.status == "running",
                ).values(
                    provider_request_ids=case(
                        (model.provider_request_ids.contains(request_ids),
                         model.provider_request_ids),
                        else_=model.provider_request_ids + request_ids,
                    ),
                    last_error=(
                        f"Uncertain provider outcome for request {error.provider_request_id}"
                    ),
                )
            )

    def _view(
        self,
        row: TrainingAnalysisRequestRecord,
        *,
        artifact: TrainingAnalysisArtifactRecord | None,
    ) -> AnalysisRequestView:
        status = row.status
        progress: float | None = None
        error = row.error
        retryable = False
        if artifact is not None and status in REQUEST_ACTIVE_STATUSES:
            if artifact.status == "pending":
                status = "queued"
            elif artifact.status == "running":
                status = "running"
                checkpoint = artifact.checkpoint or {}
                total = checkpoint.get("chunks_total")
                done = checkpoint.get("chunks_done", 0)
                progress = (done / total) if total else None
            else:
                status = artifact.status
                error = artifact.last_error
            if status == "failed":
                retryable = True
        elif status == "failed":
            retryable = True
        if status in ("succeeded", "partially_succeeded"):
            progress = 1.0
        return AnalysisRequestView(
            row.request_id,
            row.kind,
            status,
            progress,
            error,
            retryable,
            row.created_at,
            row.completed_at,
        )

    async def _provider_call(self, operation: Any, request: Any) -> Any:
        """Run one provider operation with the configured deadline."""
        metadata = {}
        token = PROVIDER_CALL_METADATA.set(metadata)
        try:
            return await asyncio.wait_for(
                operation(request), timeout=self.settings.timeout.total_seconds()
            )
        except TimeoutError as error:
            raise ProviderTimeout(
                f"Provider exceeded the {self.settings.timeout} deadline"
            ) from error
        finally:
            calls = self._provider_metadata.get()
            if calls is not None and metadata:
                calls.append(metadata)
            PROVIDER_CALL_METADATA.reset(token)

    def _provider_request_ids(self) -> list[str]:
        calls = self._provider_metadata.get()
        if calls:
            return [call["request_id"] for call in calls if call.get("request_id")]
        return list(getattr(self.provider, "provider_request_ids", ()) or [])

    def _usage_payload(self, *, cache_hit: bool = False) -> dict[str, Any]:
        return {
            "provider": self.settings.provider,
            "model": self.settings.model,
            "cache_hit": cache_hit,
            "calls": [] if cache_hit else list(self._provider_metadata.get() or []),
        }

    async def _request_row(
        self, session: AsyncSession, request_id: str
    ) -> TrainingAnalysisRequestRecord | None:
        return (
            await session.execute(
                select(TrainingAnalysisRequestRecord).where(
                    TrainingAnalysisRequestRecord.request_id == request_id
                )
            )
        ).scalar_one_or_none()

    async def _artifact_for_key(
        self, session: AsyncSession, key: str, **values: Any
    ) -> TrainingAnalysisArtifactRecord:
        record = await session.scalar(
            select(TrainingAnalysisArtifactRecord).where(
                TrainingAnalysisArtifactRecord.artifact_key == key
            ).with_for_update(key_share=True).execution_options(populate_existing=True)
        )
        if record is not None:
            return record
        await session.execute(
            pg_insert(TrainingAnalysisArtifactRecord)
            .values(id=uuid4(), artifact_key=key, **values)
            .on_conflict_do_nothing(index_elements=["artifact_key"])
        )
        record = await session.scalar(
            select(TrainingAnalysisArtifactRecord).where(
                TrainingAnalysisArtifactRecord.artifact_key == key
            ).with_for_update(key_share=True).execution_options(populate_existing=True)
        )
        assert record is not None
        return record

    async def cleanup(self, *, before: datetime, limit: int = 500) -> int:
        """Delete terminal artifacts and their requests older than a cutoff."""
        if limit < 1:
            raise ValueError("Cleanup batch limit must be positive")
        deleted = 0
        while True:
            removed = 0
            async with self.database.transaction() as session:
                request_ids = (
                    (
                        await session.execute(
                            select(TrainingAnalysisRequestRecord.id)
                            .where(
                                TrainingAnalysisRequestRecord.status.in_(
                                    ("succeeded", "partially_succeeded", "failed", "cancelled")
                                ),
                                TrainingAnalysisRequestRecord.completed_at < before,
                            )
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                if request_ids:
                    removed += (
                        await session.execute(
                            delete(TrainingAnalysisRequestRecord).where(
                                TrainingAnalysisRequestRecord.id.in_(request_ids)
                            )
                        )
                    ).rowcount
                artifacts = (
                    (
                        await session.execute(
                            select(TrainingAnalysisArtifactRecord.id)
                            .where(
                                TrainingAnalysisArtifactRecord.status.in_(
                                    ("succeeded", "partially_succeeded", "failed", "cancelled")
                                ),
                                func.coalesce(
                                    TrainingAnalysisArtifactRecord.completed_at,
                                    TrainingAnalysisArtifactRecord.failed_at,
                                )
                                < before,
                                ~select(TrainingAnalysisRequestRecord.id)
                                .where(
                                    TrainingAnalysisRequestRecord.artifact_id
                                    == TrainingAnalysisArtifactRecord.id
                                )
                                .exists(),
                            )
                            .order_by(TrainingAnalysisArtifactRecord.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                if artifacts:
                    removed += (
                        await session.execute(
                            delete(TrainingAnalysisArtifactRecord).where(
                                TrainingAnalysisArtifactRecord.id.in_(artifacts)
                            )
                        )
                    ).rowcount
            deleted += removed
            if removed < limit:
                break
        return deleted


class AnalysisJobHandler:
    """Durable-job handler adapter registering training analysis execution."""

    def __init__(self, service: AnalysisService) -> None:
        self.service = service

    async def __call__(self, payload: dict[str, Any]) -> None:
        await self.service.execute_job(payload)
