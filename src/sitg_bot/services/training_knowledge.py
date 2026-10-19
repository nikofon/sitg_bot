"""Production wiring for knowledge storage, packet authorization and analysis."""

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import String, delete, func, select

from sitg_bot.domain.game_settings import announcement_tokens
from sitg_bot.domain.knowledge import BingoScore, CommonKnowledgeEstimate, SourceLink
from sitg_bot.domain.training import text_digest
from sitg_bot.services.analysis_jobs import (
    AnalysisFeatureDisabledError,
    AnalysisJobHandler,
    AnalysisService,
)
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.entitlements import TRAINING_FEATURES, EntitlementService
from sitg_bot.services.knowledge import DEFAULT_TAG_COLOUR, KnowledgeService
from sitg_bot.services.knowledge_store import PostgresKnowledgeStore
from sitg_bot.services.reliable_delivery import DurableJobQueue, TransactionalOutbox
from sitg_bot.services.training_access import require_trainee
from sitg_bot.services.training_ai import (
    BestBuzzSource,
    CommonKnowledgeSource,
    DissectionSource,
    ProviderSettings,
    QuestionText,
    packet_chunks,
    text_fingerprint,
)
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.services.training_provider import ResponsesAnalysisProvider
from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    KnowledgeFactRecord,
    KnowledgeShareLinkRecord,
    KnowledgeSnapshotRecord,
    LogicalPacketRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerRecord,
    QuestionRevisionRecord,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
)

POPULATION = (
    "adults in the language community of the fact; general population, not quiz specialists"
)


def dissection_text(question):
    return json.dumps(
        {
            "question": question.text,
            "answer": question.answer,
            "commentary": question.commentary,
            "sources": question.source,
        },
        ensure_ascii=False,
    )


def provider_settings_from_environment():
    return ProviderSettings(
        provider="openai",
        model=os.getenv("SITG_TRAINING_AI_MODEL") or "unconfigured",
        api_key=os.getenv("SITG_TRAINING_AI_API_KEY") or None,
    )


class TrainingAnalysisAccess:
    def __init__(self, database, packets):
        self.database, self.packets = database, packets

    async def authorize_in_session(self, session, owner, source):
        await require_trainee(session, owner)
        if source["type"] == "common_knowledge":
            if "fact_id" in source:
                fact = await session.get(KnowledgeFactRecord, UUID(source["fact_id"]))
                if fact is None or fact.owner_id != owner or fact.deleted_at is not None:
                    raise LookupError("Fact not found")
                if fact.content_version != source["content_version"]:
                    raise PermissionError("Fact content changed")
            return
        await self.packets.authorize(session, owner, UUID(source["packet_revision_id"]))

    async def load_question_texts(self, *, owner_player_id, source):
        async with self.database.transaction() as session:
            await self.authorize_in_session(session, owner_player_id, source)
            ids = [UUID(value) for value in source["question_revision_ids"]]
            # Reconstruct the manifest from the authorized version, never trust caller IDs.
            from sitg_bot.storage.models import PacketQuestionRecord

            rows = list(
                (
                    await session.execute(
                        select(
                            QuestionRevisionRecord.id,
                            QuestionRevisionRecord.text,
                            QuestionRevisionRecord.answer,
                            QuestionRevisionRecord.commentary,
                            QuestionRevisionRecord.source,
                        )
                        .join(
                            PacketQuestionRecord,
                            PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                        )
                        .where(
                            PacketQuestionRecord.packet_version_id
                            == UUID(source["packet_revision_id"])
                        )
                        .order_by(PacketQuestionRecord.position)
                    )
                ).all()
            )
            if {row.id for row in rows} != set(ids) or len(rows) != len(ids):
                raise PermissionError("Analysis source revisions changed")
            by_id = {row.id: row for row in rows}
            return tuple(
                QuestionText(
                    item,
                    dissection_text(by_id[item])
                    if source["type"] == "dissection"
                    else by_id[item].text,
                )
                for item in ids
            )


class KnowledgeApplication:
    def __init__(self, database, *, analysis=None, settings=None):
        self.database = database
        self.packets = TrainingPacketService(database)
        self.entitlements = EntitlementService(database)
        self.settings = settings or provider_settings_from_environment()
        self.analysis = analysis or AnalysisService(
            database,
            DurableJobQueue(database, lease_duration=timedelta(hours=1)),
            ResponsesAnalysisProvider(self.settings),
            self.settings,
            entitlements=self.entitlements,
            source_loader=TrainingAnalysisAccess(database, self.packets),
        )

    async def initialize(self):
        features = None
        if self.settings.api_key and self.settings.model != "unconfigured":
            features = {
                feature: {
                    "enabled": True,
                    "limit": 50 if feature == "common_knowledge" else 24,
                    "reset_period": "daily",
                    "cache_hit_charging": False,
                    "queued_request_cap": 3,
                    "in_flight_request_cap": 2,
                }
                for feature in TRAINING_FEATURES
            }
        await self.entitlements.ensure_defaults(features=features)

    @staticmethod
    def service(session):
        return KnowledgeService(PostgresKnowledgeStore(session))

    async def facts(self, actor, **params):
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            page = await self.service(session).list_facts(actor, **params)
            snapshots = await self._latest_snapshots(session, [item.fact_id for item in page.items])
            items = [
                {
                    "id": item.fact_id,
                    "fact_id": item.fact_id,
                    "owner_id": item.owner_id,
                    "title": item.title,
                    "tag_names": item.tag_names,
                    "origin": item.origin,
                    "statistic_status": item.statistic_status,
                    "updated_at": item.updated_at,
                    "statistics": self._statistics(
                        item.statistic_status, snapshots.get(item.fact_id)
                    ),
                }
                for item in page.items
            ]
            return {
                "state": "ready" if items else "empty",
                "items": items,
                "total": page.total,
                "next_cursor": page.next_cursor,
                "order": page.order,
            }

    @staticmethod
    async def _latest_snapshots(session, ids):
        if not ids:
            return {}
        rows = await session.scalars(
            select(KnowledgeSnapshotRecord)
            .where(KnowledgeSnapshotRecord.fact_id.in_(ids))
            .distinct(KnowledgeSnapshotRecord.fact_id)
            .order_by(
                KnowledgeSnapshotRecord.fact_id,
                KnowledgeSnapshotRecord.created_at.desc(),
                KnowledgeSnapshotRecord.id.desc(),
            )
        )
        return {row.fact_id: row for row in rows}

    @staticmethod
    def _statistics(status, snapshot):
        current = snapshot if status == "fresh" else None
        common = current.common if current else None
        mentions = current.mentions if current else None
        return {
            "status": status
            if status != "fresh"
            else (
                "fresh"
                if common and common["status"] == "available"
                else common["status"]
                if common
                else "unavailable"
            ),
            "mentions": {"count": len(mentions["question_ids"]), "complete": not mentions["capped"]}
            if mentions
            else None,
            "common_knowledge": common.get("value") if common else None,
            "common_estimate": common,
            "bingo": current.bingo
            if current and current.bingo
            else {"status": "pending_calibration", "value": None},
        }

    async def detail(self, actor, fact_id):
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            detail = await self.service(session).fact_detail(actor, fact_id)
            snapshots = await self._latest_snapshots(session, [fact_id])
            return self._detail(detail, snapshots.get(fact_id))

    def _detail(self, detail, snapshot=None):
        # Packet metadata is never a consequence of sharing the collection.
        return {
            "id": detail.fact_id,
            "fact_id": detail.fact_id,
            "owner_id": detail.owner_id,
            "title": detail.content.title,
            "description": detail.content.description,
            "links": [asdict(link) for link in detail.content.links],
            "tags": [
                {"id": tag.tag_id, "name": tag.name, "colour": tag.colour} for tag in detail.tags
            ],
            "origin": detail.provenance.origin,
            "content_version": detail.content_version,
            "editable": detail.editable,
            "statistics": self._statistics(detail.statistic_status, snapshot),
        }

    async def sharing(self, actor):
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            service = self.service(session)
            grants = await service.list_grants(actor)
            shared = await service.shared_collections(actor)
            ids = {grant.recipient_id for grant in grants} | {grant.owner_id for grant in shared}
            names = (
                dict(
                    (
                        await session.execute(
                            select(PlayerRecord.id, PlayerRecord.public_nickname).where(
                                PlayerRecord.id.in_(ids)
                            )
                        )
                    ).all()
                )
                if ids
                else {}
            )
            links = await session.scalars(
                select(KnowledgeShareLinkRecord).where(KnowledgeShareLinkRecord.owner_id == actor)
                .order_by(KnowledgeShareLinkRecord.created_at.desc(), KnowledgeShareLinkRecord.id)
            )
            return {
                "links": [self._link_view(row) for row in links],
                "grants": [
                    {
                        "id": row.recipient_id,
                        "recipient_id": row.recipient_id,
                        "recipient_name": names.get(row.recipient_id) or str(row.recipient_id),
                        "permanent": True,
                        "copied_count": row.copied_count,
                    }
                    for row in grants
                ],
                "shared": [
                    {
                        "id": row.owner_id,
                        "owner_id": row.owner_id,
                        "owner_name": names.get(row.owner_id) or str(row.owner_id),
                        "copied_count": row.copied_count,
                    }
                    for row in shared
                ],
            }

    @staticmethod
    def _link_view(row):
        return {
            "id": row.id, "token": row.token, "url": f"/knowledge#import={row.token}",
            "created_at": row.created_at, "deactivated_at": row.deactivated_at,
            "active": row.deactivated_at is None,
        }

    async def _import_link(self, session, actor, token):
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise ValueError("Invalid sharing token")
        row = await session.scalar(select(KnowledgeShareLinkRecord).where(
            KnowledgeShareLinkRecord.token == token
        ))
        if row is None or row.deactivated_at is not None:
            raise LookupError("Sharing link is unavailable")
        if actor == row.owner_id:
            raise ValueError("A trainee cannot import their own collection")
        # Use the same account-lock order as direct copying. Then recheck link
        # under lock so deactivation and import have a single linearization point.
        for owner_id in sorted({actor, row.owner_id}):
            await require_trainee(session, owner_id, lock=True)
        await session.refresh(row, with_for_update=True)
        if row.deactivated_at is not None:
            raise LookupError("Sharing link is unavailable")
        total = await session.scalar(select(func.count()).select_from(KnowledgeFactRecord).where(
            KnowledgeFactRecord.owner_id == row.owner_id, KnowledgeFactRecord.deleted_at.is_(None)
        ))
        copied = await self.service(session).grant_collection_access(row.owner_id, actor)
        return {"owner_id": row.owner_id, "recipient_id": actor,
                "imported_count": copied.copied_count, "skipped_count": total - copied.copied_count}

    async def mutate(self, actor, command, values):
        async with self.database.transaction() as session:
            if command == "import":
                await require_trainee(session, actor)
                return await self._import_link(session, actor, values.get("token"))
            if command == "grant":
                await require_trainee(session, actor)
                recipient = values["recipient"]
                try:
                    target = await session.get(PlayerRecord, UUID(recipient))
                except ValueError:
                    target = await session.scalar(
                        select(PlayerRecord).where(PlayerRecord.public_nickname == recipient)
                    )
                if target is None:
                    raise LookupError("Recipient not found")
                for owner_id in sorted({actor, target.id}):
                    await require_trainee(session, owner_id, lock=True)
                return await self.service(session).grant_collection_access(actor, target.id)
            await require_trainee(session, actor, lock=True)
            service = self.service(session)
            if command == "link_create":
                key = values.get("creation_key")
                key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
                if key_hash:
                    existing = await session.scalar(select(KnowledgeShareLinkRecord).where(
                        KnowledgeShareLinkRecord.owner_id == actor,
                        KnowledgeShareLinkRecord.creation_key_hash == key_hash,
                    ))
                    if existing is not None:
                        return self._link_view(existing)
                row = KnowledgeShareLinkRecord(
                    id=uuid4(), owner_id=actor, token=secrets.token_urlsafe(32),
                    creation_key_hash=key_hash, created_at=datetime.now(UTC),
                )
                session.add(row)
                await session.flush()
                return self._link_view(row)
            if command == "link_deactivate":
                row = await session.scalar(select(KnowledgeShareLinkRecord).where(
                    KnowledgeShareLinkRecord.id == values.get("link_id"),
                    KnowledgeShareLinkRecord.owner_id == actor,
                ).with_for_update())
                if row is None:
                    raise LookupError("Sharing link not found")
                if row.deactivated_at is None:
                    row.deactivated_at = datetime.now(UTC)
                return self._link_view(row)
            if command in ("create", "update"):
                fields = {
                    key: values[key]
                    for key in ("title", "description", "tag_names", "expected_version")
                    if values.get(key) is not None
                }
                if values.get("links") is not None:
                    fields["links"] = tuple(SourceLink(**link) for link in values["links"])
                if command == "create":
                    fields.pop("expected_version", None)
                    detail = await service.create_fact(actor, **fields)
                else:
                    if values.get("fact_id") is None or "expected_version" not in fields:
                        raise ValueError("Fact ID and expected version are required")
                    try:
                        detail = await service.update_fact(actor, values["fact_id"], **fields)
                    except ValueError as error:
                        if str(error) == "Fact changed since it was read":
                            raise StaleWriteError(str(error)) from error
                        raise
                await self._schedule_statistics(
                    session, actor, detail.fact_id, detail.content_version
                )
                return self._detail(detail)
            if command == "delete":
                await service.delete_fact(
                    actor,
                    values["fact_id"],
                    confirm_dissection_origin=values["confirm_dissection_origin"],
                )
            elif command == "merge":
                detail = await service.merge_facts(
                    actor,
                    values["source_fact_id"],
                    values["target_fact_id"],
                    expected_target_version=values["expected_target_version"],
                    confirm_source_delete=values["confirm_source_delete"],
                )
                await self._schedule_statistics(
                    session, actor, detail.fact_id, detail.content_version
                )
                return self._detail(detail)
            elif command == "revoke":
                return await service.revoke_collection_access(actor, values["grant_id"])
            elif command == "tag_create":
                return await service.create_tag(
                    actor, values["name"], values.get("colour") or DEFAULT_TAG_COLOUR
                )
            elif command == "tag_rename":
                return await service.rename_tag(
                    actor, values["tag_id"], values["name"], values.get("colour")
                )
            elif command == "tag_delete":
                await service.delete_tag(actor, values["tag_id"])
            else:
                raise ValueError("Unknown knowledge command")
            return {"updated": True}

    async def _schedule_statistics(self, session, owner, fact_id, version):
        await DurableJobQueue.schedule_in_session(
            session,
            "knowledge.statistics",
            f"knowledge.statistics:{fact_id}:{version}",
            {"owner_id": str(owner), "fact_id": str(fact_id), "version": version},
            priority=250,
        )
        row = await session.get(KnowledgeFactRecord, fact_id)
        if row.statistic_status != "fresh":
            row.statistic_status = "pending"

    async def request_analysis(self, actor, **values):
        from sitg_bot.services.training_packets import DisclosureRequired

        try:
            return await self._request_analysis(actor, **values)
        except DisclosureRequired as error:
            return {"confirmation_required": True, "fresh_unit_count": error.fresh_unit_count}

    async def _request_analysis(
        self,
        actor,
        *,
        version_id,
        kind,
        instructions="",
        comment="",
        confirm_exposure=False,
        token_target_chars=18,
        idempotency_key,
    ):
        async with self.database.transaction() as session:
            source = await self.packets.disclose(
                session, actor, version_id, confirm=confirm_exposure
            )
            questions = tuple(
                QuestionText(
                    question.revision_id,
                    dissection_text(question) if kind == "dissection" else question.text,
                )
                for question in source.questions
            )
            chunks = packet_chunks(
                questions, max_chunk_characters=self.settings.max_chunk_characters
            )
            if len(chunks) > 16:
                raise ValueError("Analysis accepts at most 16 source chunks per packet")
            if not self.settings.api_key and isinstance(
                self.analysis.provider, ResponsesAnalysisProvider
            ):
                raise AnalysisFeatureDisabledError("Training AI provider is not configured")
            policy = await self.entitlements.policy_for(session, actor, kind)
            if not policy.enabled:
                raise AnalysisFeatureDisabledError(f"Feature {kind} is disabled")
            version = await session.get(PacketVersionRecord, version_id)
            player = await session.get(PlayerRecord, actor)
            language = (
                version.language if version.language in ("en", "ru") else player.preferred_locale
            )
            if kind == "dissection":
                view = await self.service(session).request_dissection(
                    actor,
                    packet_id=source.packet_id,
                    packet_revision=str(version_id),
                    instructions=instructions,
                    comment=comment,
                )
                row = await session.get(KnowledgeDissectionRecord, view.request_id)
                if row.analysis_request_id and row.status != "failed":
                    return {
                        "request_id": row.analysis_request_id,
                        "dissection_id": row.id,
                        "status": row.status,
                    }
                typed = DissectionSource(
                    version_id, tuple(q.revision_id for q in source.questions), language=language
                )
                instruction_text = (
                    instructions
                    if comment == ""
                    else json.dumps(
                        {"instructions": instructions, "comment": comment}, ensure_ascii=False
                    )
                )
            else:
                typed = BestBuzzSource(
                    version_id,
                    tuple(q.revision_id for q in source.questions),
                    token_target_chars,
                    language=language,
                )
                row = None
                instruction_text = ""
            request_id = f"{actor}:{kind}:{idempotency_key}"
            result = await self.analysis.request(
                session,
                owner_player_id=actor,
                source=typed,
                instructions=instruction_text,
                idempotency_key=request_id,
                private=source.scope == "training",
                content_fingerprint=source.fingerprint,
                units=len({question.theme_revision_id for question in source.questions}),
            )
            if row is not None:
                row.analysis_request_id = result.request_id
                row.status = "pending"
                await self._apply_ready(session, row)
            return {**asdict(result), "dissection_id": row.id if row else None}

    async def dissections(self, actor):
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            rows = list(
                await session.scalars(
                    select(KnowledgeDissectionRecord)
                    .where(KnowledgeDissectionRecord.owner_id == actor)
                    .order_by(KnowledgeDissectionRecord.created_at.desc())
                    .limit(100)
                )
            )
            items = []
            for row in rows:
                # A job list reveals no private packet name or question content.
                view = await self.service(session).dissection_request(actor, row.id)
                analysis = (
                    await self.analysis._request_row(session, row.analysis_request_id)
                    if row.analysis_request_id
                    else None
                )
                items.append(
                    {
                        "id": row.id,
                        "packet_id": row.packet_revision,
                        "base": row.base,
                        "status": analysis.status
                        if analysis and row.status == "pending"
                        else row.status,
                        "request_id": row.analysis_request_id,
                        "applied_count": len(view.applied_fact_ids),
                        "applied_fact_ids": view.applied_fact_ids,
                        "failed_candidate_ids": view.failed_candidate_ids,
                        "partial": row.status == "partially_succeeded",
                    }
                )
            return {"items": items}

    async def _apply_ready(self, session, row):
        if row.status != "pending" or not row.analysis_request_id:
            return
        request = await self.analysis._request_row(session, row.analysis_request_id)
        if request is None:
            return
        if request.status in ("queued", "running"):
            artifact = await session.get(TrainingAnalysisArtifactRecord, request.artifact_id)
            if artifact is not None:
                await self.analysis._reconcile_artifact(session, artifact, datetime.now(UTC))
        if request.status in ("failed", "cancelled"):
            row.status = "failed"
            return
        if request.status not in ("succeeded", "partially_succeeded"):
            return
        await self.analysis.source_loader.authorize_in_session(
            session, row.owner_id, request.source
        )
        artifact = await session.get(TrainingAnalysisArtifactRecord, request.artifact_id)
        if artifact is None or artifact.payload is None:
            return
        ids = [UUID(value) for value in request.source["question_revision_ids"]]
        revisions = {
            item.id: item
            for item in await session.scalars(
                select(QuestionRevisionRecord).where(QuestionRevisionRecord.id.in_(ids))
            )
        }
        candidates = []
        for value in artifact.payload["candidates"]:
            revision_id = UUID(value["question_revision_id"])
            if revision_id not in revisions:
                raise ValueError("Unpinned question candidate")
            references = set(
                re.findall(
                    r"https?://[^\s<>\[\]\"']+",
                    revisions[revision_id].text + " " + revisions[revision_id].source,
                )
            )
            candidates.append(
                {
                    "content": {
                        "title": value["title"],
                        "description": value["description"],
                        "links": [
                            {"url": url, "position": index, "origin": "dissection"}
                            for index, url in enumerate(value["links"])
                            if url in references
                        ],
                    },
                    "packet_id": str(row.packet_id),
                    "question_id": str(revisions[revision_id].question_id),
                    "question_revision": str(revision_id),
                }
            )
        view = await self.service(session).apply_dissection_result(row.owner_id, row.id, candidates)
        if request.status == "succeeded" and view.status == "succeeded":
            # Keep non-content coverage after the expiring request/cache is pruned.
            row.theme_revision_ids = sorted({str(value) for value in await session.scalars(
                select(PacketQuestionRecord.theme_revision_id).where(
                    PacketQuestionRecord.packet_version_id == UUID(row.packet_revision),
                    PacketQuestionRecord.question_revision_id.in_(ids),
                )
            )})
        for fact_id in view.applied_fact_ids:
            await self._schedule_statistics(session, row.owner_id, fact_id, 1)

    async def reconcile(self):
        async with self.database.transaction() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeDissectionRecord)
                    .where(
                        KnowledgeDissectionRecord.status == "pending",
                        KnowledgeDissectionRecord.analysis_request_id.is_not(None),
                    )
                    .order_by(KnowledgeDissectionRecord.created_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                try:
                    await self.packets.lock_access(session, row.owner_id, UUID(row.packet_revision))
                    await require_trainee(session, row.owner_id, lock=True)
                    await self._apply_ready(session, row)
                except (LookupError, PermissionError):
                    row.status = "failed"

    async def analysis_job(self, payload):
        from sitg_bot.services.training_ai import (
            AnalysisValidationError,
            ProviderOutcomeUncertain,
            ProviderTimeout,
        )

        try:
            await AnalysisJobHandler(self.analysis)(payload)
        except (
            AnalysisValidationError,
            ProviderOutcomeUncertain,
            ProviderTimeout,
            ValueError,
            PermissionError,
            LookupError,
        ) as error:
            async with self.database.transaction() as session:
                artifact = await session.get(
                    TrainingAnalysisArtifactRecord,
                    UUID(payload["artifact_id"]),
                    with_for_update=True,
                )
                if artifact is not None and artifact.status == "running":
                    await self.analysis._fail_artifact(
                        session, artifact, str(error), datetime.now(UTC)
                    )
        except Exception:
            # A retry resumes its checkpoint immediately after an ordinary worker failure.
            async with self.database.transaction() as session:
                artifact = await session.get(
                    TrainingAnalysisArtifactRecord,
                    UUID(payload["artifact_id"]),
                    with_for_update=True,
                )
                if artifact is not None and artifact.status == "running":
                    artifact.lease_expires_at = datetime.now(UTC)
            raise
        await self.reconcile()

    async def cleanup_retired(self):
        outbox = TransactionalOutbox(self.database)
        events = await outbox.claim(topics=("training.packet.retired",), limit=10)
        for event in events:
            try:
                async with self.database.transaction() as session:
                    packet_id, owner = (
                        UUID(event.payload["packet_id"]),
                        UUID(event.payload["owner_id"]),
                    )
                    packet = await session.get(LogicalPacketRecord, packet_id)
                    if packet is None or packet.retired_at is None or packet.scope != "training":
                        raise ValueError("Expected a retired private packet")
                    versions = select(PacketVersionRecord.id.cast(String)).where(
                        PacketVersionRecord.packet_id == packet_id
                    )
                    artifacts = list(
                        await session.scalars(
                            select(TrainingAnalysisArtifactRecord)
                            .join(
                                TrainingAnalysisRequestRecord,
                                TrainingAnalysisRequestRecord.artifact_id
                                == TrainingAnalysisArtifactRecord.id,
                            )
                            .where(
                                TrainingAnalysisRequestRecord.owner_player_id == owner,
                                TrainingAnalysisRequestRecord.source[
                                    "packet_revision_id"
                                ].astext.in_(versions),
                            )
                            .distinct()
                        )
                    )
                    for artifact in artifacts:
                        await self.analysis._complete_requests(
                            session, artifact.id, "cancelled", "Source retired", datetime.now(UTC)
                        )
                        await session.execute(
                            delete(TrainingAnalysisRequestRecord).where(
                                TrainingAnalysisRequestRecord.artifact_id == artifact.id
                            )
                        )
                        await session.delete(artifact)
                    rows = await session.scalars(
                        select(KnowledgeDissectionRecord).where(
                            KnowledgeDissectionRecord.owner_id == owner,
                            KnowledgeDissectionRecord.packet_id == packet_id,
                            KnowledgeDissectionRecord.status == "pending",
                        )
                    )
                    for row in rows:
                        row.status = "failed"
                await outbox.acknowledge(event.event_id, event.lease_token)
            except Exception:
                await outbox.reject(
                    event.event_id, event.lease_token, "Private analysis cleanup failed"
                )

    async def reconcile_requests(self):
        async with self.database.transaction() as session:
            rows = await session.scalars(
                select(TrainingAnalysisArtifactRecord)
                .where(TrainingAnalysisArtifactRecord.status.in_(("pending", "running")))
                .order_by(TrainingAnalysisArtifactRecord.created_at)
                .limit(100)
            )
            for artifact in rows:
                await self.analysis._reconcile_artifact(session, artifact, datetime.now(UTC))

    async def statistics_job(self, payload):
        unavailable_status = "unavailable"
        owner, fact_id, version = (
            UUID(payload["owner_id"]),
            UUID(payload["fact_id"]),
            payload["version"],
        )
        async with self.database.transaction() as session:
            await require_trainee(session, owner, lock=True)
            fact = await self.service(session).store.get_fact(fact_id)
            if (
                fact is None
                or fact.deleted_at
                or fact.owner_id != owner
                or fact.content_version != version
            ):
                return
            request_id = f"statistics:{fact_id}:{version}"
            if self.settings.api_key or not isinstance(
                self.analysis.provider, ResponsesAnalysisProvider
            ):
                try:
                    async with session.begin_nested():
                        source = CommonKnowledgeSource(
                            fact.content.title + "\n" + fact.content.description,
                            language=(await session.get(PlayerRecord, owner)).preferred_locale,
                            population=POPULATION,
                        )
                        await self.analysis.request(
                            session,
                            owner_player_id=owner,
                            source=source,
                            idempotency_key=request_id,
                            private=True,
                            content_fingerprint=fact.content.fingerprint(),
                            source_context={"fact_id": str(fact_id), "content_version": version},
                        )
                except Exception as error:
                    from sitg_bot.services.analysis_jobs import AnalysisRequestError

                    if not isinstance(error, AnalysisRequestError):
                        raise
                    unavailable_status = "denied"
                else:
                    return
            await self.service(session).record_statistics(
                owner,
                fact_id,
                common=CommonKnowledgeEstimate(status=unavailable_status, population=POPULATION),
                bingo=BingoScore(status="pending_calibration"),
            )

    async def reconcile_statistics(self):
        async with self.database.transaction() as session:
            rows = list(
                await session.execute(
                    select(TrainingAnalysisRequestRecord, KnowledgeFactRecord)
                    .join(
                        KnowledgeFactRecord,
                        TrainingAnalysisRequestRecord.source["fact_id"].astext
                        == KnowledgeFactRecord.id.cast(String),
                    )
                    .where(
                        TrainingAnalysisRequestRecord.kind == "common_knowledge",
                        TrainingAnalysisRequestRecord.status.in_(
                            ("succeeded", "failed", "cancelled")
                        ),
                        KnowledgeFactRecord.statistic_status == "pending",
                        KnowledgeFactRecord.deleted_at.is_(None),
                    )
                    .limit(100)
                )
            )
            for request, fact in rows:
                try:
                    await require_trainee(session, fact.owner_id, lock=True)
                except PermissionError:
                    continue
                await session.refresh(fact)
                if (
                    request.source.get("content_version") != fact.content_version
                    or fact.statistic_status != "pending"
                    or fact.deleted_at is not None
                ):
                    continue
                artifact = await session.get(TrainingAnalysisArtifactRecord, request.artifact_id)
                available = request.status == "succeeded" and artifact and artifact.payload
                common = CommonKnowledgeEstimate(
                    status="available" if available else "failed",
                    value=artifact.payload["value"] if available else None,
                    model=self.settings.model,
                    prompt_version=self.settings.prompt_version,
                    schema_version=self.settings.schema_version,
                    population=POPULATION,
                    estimated_at=datetime.now(UTC) if available else None,
                )
                await self.service(session).record_statistics(
                    fact.owner_id,
                    fact.id,
                    common=common,
                    bingo=BingoScore(status="pending_calibration"),
                )

    async def prepared(self, session, player_id, source):
        rows = await session.execute(
            select(TrainingAnalysisArtifactRecord, TrainingAnalysisRequestRecord)
            .join(
                TrainingAnalysisRequestRecord,
                TrainingAnalysisRequestRecord.artifact_id == TrainingAnalysisArtifactRecord.id,
            )
            .where(
                TrainingAnalysisRequestRecord.owner_player_id == player_id,
                TrainingAnalysisRequestRecord.kind == "best_buzz",
                TrainingAnalysisRequestRecord.status == "succeeded",
                TrainingAnalysisRequestRecord.source["packet_revision_id"].astext
                == str(source.version_id),
                TrainingAnalysisRequestRecord.source["content_fingerprint"].astext
                == source.fingerprint,
            )
            .order_by(TrainingAnalysisRequestRecord.created_at.desc())
            .limit(1)
        )
        pair = rows.first()
        if pair is None:
            return None
        artifact, request = pair
        if artifact.schema_version != "1" or artifact.tokenizer_version != "announcement-v1":
            return None
        by_id = {question.revision_id: question for question in source.questions}
        target = request.source["question_token_target_chars"]
        result = {}
        for boundary in artifact.payload["boundaries"]:
            revision = UUID(boundary["question_revision_id"])
            question = by_id[revision]
            if boundary["text_digest"] != text_fingerprint(question.text):
                raise ValueError("Best buzz source changed")
            tokens = announcement_tokens(question.text, target)
            index = boundary["token_index"]
            if type(index) is not int or not 0 <= index <= len(tokens):
                raise ValueError("Invalid Best buzz token boundary")
            word_count = sum(len(token.split()) for token in tokens[:index])
            words = list(re.finditer(r"\S+", question.text))
            result[revision] = {
                "schema_version": 1,
                "tokenizer_version": "announcement-v1",
                "question_revision_id": str(revision),
                "text_digest": text_digest(question.text),
                "source_end": words[word_count - 1].end() if word_count else 0,
            }
        return result
