"""Durable analysis execution and persistent artifact caching for training mode.

Agent C module (see `docs/training-mode-plan.md`). Analysis requests are
owner-scoped and idempotent; computation is coalesced per immutable artifact
and executed by durable ``training.analysis`` jobs claimed only by analysis
workers. Artifacts are never mutated after success, and failures are never
cached as successful empty data. Provider calls happen outside transactions
under compute leases; stale completions are fenced by lease tokens.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sitg_bot.domain.game_settings import announcement_tokens
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
    BuzzBoundary,
    BuzzRequest,
    CommonKnowledgeRequest,
    ExtractionRequest,
    FactCandidate,
    ProviderOutcomeUncertain,
    ProviderSettings,
    ProviderTimeout,
    QuestionText,
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
from sitg_bot.storage.models import DurableJobRecord

#: Durable job kind claimed only by analysis workers (kind-filtered claims).
TRAINING_ANALYSIS_JOB_KIND = "training.analysis"

FEATURE_BY_KIND = {
    ANALYSIS_DISSECTION: FEATURE_DISSECTION,
    ANALYSIS_BEST_BUZZ: FEATURE_BEST_BUZZ,
    ANALYSIS_COMMON_KNOWLEDGE: FEATURE_COMMON_KNOWLEDGE,
}

REQUEST_ACTIVE_STATUSES = ("queued", "running")


def _check_digest(constraint: Any, table: Any) -> str:
    return sha256(str(constraint.sqltext).encode()).hexdigest()[:12]


ANALYSIS_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "ck": "ck_%(table_name)s_%(check_digest)s",
        "check_digest": _check_digest,
    }
)


def _uuid_column() -> Mapped[UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)


class AnalysisBase(DeclarativeBase):
    metadata = ANALYSIS_METADATA


class TrainingAnalysisArtifactRecord(AnalysisBase):
    """An immutable validated analysis artifact, coalesced by unique key.

    ``owner_player_id`` is only null for shared base dissections (empty
    instructions); custom instructions and every private source stay
    owner-scoped even when their content hashes match another packet.
    """

    __tablename__ = "training_analysis_artifacts"

    id: Mapped[UUID] = _uuid_column()
    artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    owner_player_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    instruction_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(30), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False)
    tokenizer_version: Mapped[str] = mapped_column(String(30), nullable=False)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default=text("'pending'")
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_request_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    provider_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("artifact_key", name="uq_training_analysis_artifacts_key"),
        CheckConstraint("kind IN ('dissection', 'best_buzz', 'common_knowledge')"),
        CheckConstraint("generation >= 1"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partially_succeeded', "
            "'failed', 'cancelled')"
        ),
        CheckConstraint(
            "(shared AND owner_player_id IS NULL) OR "
            "(NOT shared AND owner_player_id IS NOT NULL)"
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)"
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'partially_succeeded') AND completed_at IS NOT NULL "
            "AND failed_at IS NULL) OR "
            "(status = 'failed' AND failed_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('pending', 'running', 'cancelled') "
            "AND completed_at IS NULL AND failed_at IS NULL)"
        ),
        Index("ix_training_analysis_artifacts_owner", "owner_player_id", "kind", "created_at"),
    )


class TrainingAnalysisRequestRecord(AnalysisBase):
    """An owner-scoped analysis request with idempotency and instructions."""

    __tablename__ = "training_analysis_requests"

    id: Mapped[UUID] = _uuid_column()
    request_id: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_player_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("training_analysis_artifacts.id"),
        nullable=False,
    )
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default=text("'queued'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("request_id", name="uq_training_analysis_requests_request"),
        CheckConstraint("kind IN ('dissection', 'best_buzz', 'common_knowledge')"),
        CheckConstraint("length(request_id) > 0"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partially_succeeded', "
            "'failed', 'cancelled')"
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'partially_succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL) OR "
            "(status IN ('queued', 'running') AND completed_at IS NULL)"
        ),
        Index(
            "ix_training_analysis_requests_owner",
            "owner_player_id",
            "kind",
            "status",
        ),
        Index("ix_training_analysis_requests_artifact", "artifact_id", "status"),
    )


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

    async def request(
        self,
        session: AsyncSession,
        *,
        owner_player_id: UUID,
        source: Any,
        instructions: str = "",
        idempotency_key: str,
        now: datetime | None = None,
    ) -> AnalysisRequestView:
        """Authorize, reserve quota, persist and enqueue within one transaction."""
        if not idempotency_key:
            raise ValueError("Analysis idempotency key is required")
        kind = source_kind(source)
        moment = now or datetime.now(UTC)
        normalized = instructions.strip()
        existing = await self._request_row(session, idempotency_key)
        if existing is not None:
            if existing.owner_player_id != owner_player_id:
                raise AnalysisIdempotencyConflictError(
                    "Analysis idempotency key already belongs to another trainee"
                )
            return self._view(existing, artifact=None)
        descriptor = source_descriptor(source)
        fingerprint = source_fingerprint_of(source)
        digest = instruction_digest(normalized)
        shared = not normalized
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
        artifact = await self._artifact_for_key(
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
        if policy.queued_request_cap is not None and (queued or 0) >= policy.queued_request_cap:
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
                used_units=1,
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
            artifact = None
            if row.status in REQUEST_ACTIVE_STATUSES:
                artifact = await session.get(TrainingAnalysisArtifactRecord, row.artifact_id)
                if artifact is not None:
                    await self._reconcile_artifact(session, artifact, moment)
            return self._view(row, artifact=artifact)

    async def result(
        self, *, owner_player_id: UUID, request_id: str
    ) -> dict[str, Any] | None:
        """Return the validated artifact payload for a terminal request."""
        async with self.database.transaction() as session:
            row = await self._request_row(session, request_id)
            if row is None or row.owner_player_id != owner_player_id:
                return None
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
            artifact = await session.get(
                TrainingAnalysisArtifactRecord, row.artifact_id, with_for_update=True
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
                TrainingAnalysisArtifactRecord, artifact_id, with_for_update=True
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
            for row in rows:
                row.status = "running"
            if not rows:
                await self._abandon(session, artifact_id, lease_token)
                return
            instructions = rows[0].instructions
            descriptor = dict(rows[0].source)
            initiating_owner = rows[0].owner_player_id
            kind = artifact.kind
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
            await self._record_uncertain(artifact_id, error)
            raise

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
        questions = await self.source_loader.load_question_texts(
            owner_player_id=initiating_owner, source=descriptor
        )
        if not questions:
            raise RuntimeError("The analysis source has no authorized question revisions")
        chunks = packet_chunks(questions, max_chunk_characters=self.settings.max_chunk_characters)
        total = len(chunks)
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
                    ExtractionRequest((chunk,), instructions, language),
                )
                partial.extend(
                    {
                        "candidate_id": candidate.candidate_id,
                        "title": candidate.title,
                        "description": candidate.description,
                        "links": list(candidate.links),
                        "question_revision_id": str(candidate.question_revision_id),
                    }
                    for candidate in result
                )
            else:
                result = await self._provider_call(
                    self.provider.best_buzz_boundaries, BuzzRequest((chunk,), language)
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
                )
                for item in partial
            )
            validated = validate_fact_candidates(
                candidates,
                allowed_question_revision_ids=allowed,
                max_output_facts=self.settings.max_output_facts,
            )
            payload: dict[str, Any] = {
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "title": candidate.title,
                        "description": candidate.description,
                        "links": list(candidate.links),
                        "question_revision_id": str(candidate.question_revision_id),
                    }
                    for candidate in validated
                ]
            }
        else:
            boundaries = tuple(
                BuzzBoundary(
                    UUID(item["question_revision_id"]),
                    item["text_digest"],
                    int(item["token_index"]),
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
            row.status = status
            row.error = error[:2000] if error else None
            row.completed_at = moment
            if self.entitlements is None:
                continue
            feature = FEATURE_BY_KIND[row.kind]
            if settle:
                await self.entitlements.settle(
                    session,
                    player_id=row.owner_player_id,
                    feature=feature,
                    request_id=row.request_id,
                    used_units=1,
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
        if artifact.status not in ("pending", "running"):
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
        self, artifact_id: UUID, error: ProviderOutcomeUncertain
    ) -> None:
        """Record possibly-billed upstream request IDs before retrying."""
        async with self.database.transaction() as session:
            artifact = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
            if artifact is None:
                return
            ids = list(artifact.provider_request_ids or [])
            if error.provider_request_id not in ids:
                ids.append(error.provider_request_id)
            artifact.provider_request_ids = ids
            artifact.last_error = (
                f"Uncertain provider outcome for request {error.provider_request_id}"
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
        try:
            return await asyncio.wait_for(
                operation(request), timeout=self.settings.timeout.total_seconds()
            )
        except TimeoutError as error:
            raise ProviderTimeout(
                f"Provider exceeded the {self.settings.timeout} deadline"
            ) from error

    def _provider_request_ids(self) -> list[str]:
        return list(getattr(self.provider, "provider_request_ids", ()) or [])

    def _usage_payload(self, *, cache_hit: bool = False) -> dict[str, Any]:
        return {
            "provider": self.settings.provider,
            "model": self.settings.model,
            "cache_hit": cache_hit,
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
            )
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
            )
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
                            .limit(limit)
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












