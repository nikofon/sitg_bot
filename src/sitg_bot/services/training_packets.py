"""Owner-only uploads and the shared, live permission boundary for training/analysis."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sitg_bot.domain.game_rulesets import SIGameRuleset
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.packet_import import packets_from_document_bytes
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.library import PacketLibraryService
from sitg_bot.services.packet_catalogue import background_packet_batch
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.training_access import require_trainee
from sitg_bot.services.training_catalogue import (
    encode_packet_cursor,
    enrich_packet_cards,
    packet_cursor,
    packet_order,
)
from sitg_bot.storage.authorship import packet_author_rows
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameParticipantRecord,
    GameRecord,
    LogicalPacketRecord,
    PacketDraftRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TournamentRecord,
    TrainingSessionRecord,
)


@dataclass(frozen=True, slots=True)
class TrainingSourceQuestion:
    revision_id: UUID
    theme_revision_id: UUID
    theme: str
    value: int
    text: str
    answer: str
    accepted_answers: tuple[str, ...]
    rejected_answers: tuple[str, ...]
    commentary: str
    source: str
    form: str


@dataclass(frozen=True, slots=True)
class TrainingPacketSource:
    version_id: UUID
    packet_id: UUID
    scope: str
    owner_id: UUID | None
    fingerprint: str
    questions: tuple[TrainingSourceQuestion, ...]
    receipt: dict


class DisclosureRequired(ValueError):
    def __init__(self, count: int):
        super().__init__("Confirm packet exposure before training")
        self.fresh_unit_count = count


class TrainingPacketService:
    def __init__(self, database: Database):
        self.database = database
        self.packets = PacketAdminService(database)

    async def lock_access(self, session: AsyncSession, player_id: UUID, version_id: UUID) -> None:
        """Lock public grants before account locks, matching normal-game lock order."""
        _, logical = await self.authorize(session, player_id, version_id)
        if logical.scope == "tournament":
            await PacketLibraryService(self.database)._assignments(
                session,
                player_id,
                lock=True,
                version_id=version_id,
            )

    @staticmethod
    def _library_assignments(player_id: UUID):
        """SQL equivalent of can_read_assignment, evaluated before pagination."""
        assignment = TournamentPacketAssignmentRecord
        latest = aliased(PacketVersionRecord)
        latest_number = (
            select(func.max(latest.version_number))
            .where(
                latest.packet_id == LogicalPacketRecord.id,
                latest.state == "published",
            )
            .correlate(LogicalPacketRecord)
            .scalar_subquery()
        )
        manager = (
            select(TournamentManagerRecord.player_id)
            .where(
                TournamentManagerRecord.tournament_id == assignment.tournament_id,
                TournamentManagerRecord.player_id == player_id,
                TournamentManagerRecord.revoked_at.is_(None),
            )
            .correlate(assignment)
            .exists()
        )
        member = (
            select(TournamentMembershipRecord.player_id)
            .where(
                TournamentMembershipRecord.tournament_id == assignment.tournament_id,
                TournamentMembershipRecord.player_id == player_id,
                TournamentMembershipRecord.status == "active",
            )
            .correlate(assignment)
            .exists()
        )
        entitlement = (
            select(
                case(
                    (
                        TournamentPacketEntitlementRecord.readable_override.is_not(None),
                        TournamentPacketEntitlementRecord.readable_override,
                    ),
                    else_=or_(
                        assignment.content_visible_by_members,
                        TournamentPacketEntitlementRecord.content_visible,
                    ),
                )
            )
            .where(
                TournamentPacketEntitlementRecord.assignment_id == assignment.id,
                TournamentPacketEntitlementRecord.player_id == player_id,
                TournamentPacketEntitlementRecord.revoked_at.is_(None),
            )
            .correlate(assignment)
            .scalar_subquery()
        )
        # Correlate the logical packet, not the outer version, inside historical play.
        played = (
            select(PlayerExposureClaimRecord.id)
            .join(
                GameParticipantRecord,
                and_(
                    GameParticipantRecord.game_id == PlayerExposureClaimRecord.game_id,
                    GameParticipantRecord.player_id == player_id,
                ),
            )
            .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
            .where(
                PlayerExposureClaimRecord.player_id == player_id,
                PlayerExposureClaimRecord.state == "burnt",
                PlayerExposureClaimRecord.packet_version_id.in_(
                    select(PacketVersionRecord.id)
                    .where(
                        PacketVersionRecord.packet_id == LogicalPacketRecord.id,
                    )
                    .correlate(LogicalPacketRecord)
                ),
                GameRecord.tournament_id == assignment.tournament_id,
            )
            .correlate(assignment, LogicalPacketRecord)
            .exists()
        )
        return (
            select(assignment.id)
            .join(
                TournamentRecord,
                TournamentRecord.id == assignment.tournament_id,
            )
            .where(
                assignment.packet_id == LogicalPacketRecord.id,
                or_(
                    assignment.adopted_version_id == PacketVersionRecord.id,
                    and_(
                        assignment.adopted_version_id.is_(None),
                        PacketVersionRecord.version_number == latest_number,
                    ),
                ),
                assignment.status == "active",
                TournamentRecord.moderation_status != "abolished",
                or_(
                    manager,
                    and_(
                        member,
                        PacketVersionRecord.library_released_at.is_not(None),
                        func.coalesce(entitlement, assignment.content_visible_by_members),
                        or_(
                            assignment.library_viewing_rule == "anytime",
                            and_(
                                assignment.library_viewing_rule == "after-play",
                                played,
                            ),
                        ),
                    ),
                ),
            )
            .correlate(LogicalPacketRecord, PacketVersionRecord)
        )

    @classmethod
    def _library_right(cls, player_id: UUID):
        return cls._library_assignments(player_id).exists()

    @classmethod
    def _readable(cls, player_id: UUID):
        return and_(
            LogicalPacketRecord.retired_at.is_(None),
            PacketVersionRecord.deleted_at.is_(None),
            PacketVersionRecord.state == "published",
            or_(
                and_(
                    LogicalPacketRecord.scope == "training",
                    LogicalPacketRecord.uploader_id == player_id,
                ),
                and_(LogicalPacketRecord.scope == "tournament", cls._library_right(player_id)),
            ),
        )

    async def authorize(self, session: AsyncSession, player_id: UUID, version_id: UUID):
        await require_trainee(session, player_id)
        row = (
            await session.execute(
                select(
                    PacketVersionRecord,
                    LogicalPacketRecord,
                )
                .join(LogicalPacketRecord)
                .where(
                    PacketVersionRecord.id == version_id,
                    self._readable(player_id),
                )
            )
        ).first()
        if row is None:
            raise LookupError("Training packet not found")
        return row

    async def list_packets(
        self,
        player_id: UUID,
        *,
        cursor: str | UUID | None = None,
        limit: int = 25,
        name: str = "",
        author: str = "",
        year_from: int | None = None,
        year_to: int | None = None,
        publication_from: int | None = None,
        publication_to: int | None = None,
        order: str = "id_asc",
    ) -> dict:
        if not 1 <= limit <= 25:
            raise ValueError("Training page size must be between 1 and 25")
        filters = dict(name=name.strip().lower(), author=author.strip().lower(),
                       year_from=year_from, year_to=year_to,
                       publication_from=publication_from, publication_to=publication_to)
        signature = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:16]
        sort_key, descending = packet_order(order)
        async with background_packet_batch(), self.database.sessions() as session:
            await require_trainee(session, player_id)
            query = (
                select(
                    PacketVersionRecord.id, PacketVersionRecord.packet_id,
                    PacketVersionRecord.name, PacketVersionRecord.year,
                    LogicalPacketRecord.scope, sort_key.label("sort_value"),
                )
                .join(LogicalPacketRecord)
                .where(self._readable(player_id))
            )
            if cursor:
                value, identity = packet_cursor(str(cursor), order, signature)
                key = tuple_(sort_key, PacketVersionRecord.id)
                query = query.where(
                    key < (value, identity) if descending else key > (value, identity)
                )
            if filters["name"]:
                query = query.where(func.strpos(func.lower(PacketVersionRecord.name),
                                                filters["name"]) > 0)
            if filters["author"]:
                pairs = packet_author_rows(select(PacketVersionRecord.id)).subquery()
                matching = select(pairs.c.packet_version_id).join(
                    AuthorRecord, AuthorRecord.id == pairs.c.author_id
                ).where(func.strpos(func.lower(AuthorRecord.display_name), filters["author"]) > 0)
                query = query.where(PacketVersionRecord.id.in_(matching))
            for field, start, end in (
                (PacketVersionRecord.year, year_from, year_to),
                (func.extract("year", PacketVersionRecord.published_at),
                 publication_from, publication_to),
            ):
                if start is not None:
                    query = query.where(field >= start)
                if end is not None:
                    query = query.where(field <= end)
            ordering = (sort_key.desc(), PacketVersionRecord.id.desc()) if descending else (
                sort_key.asc(), PacketVersionRecord.id.asc()
            )
            rows = await session.execute(query.order_by(*ordering).limit(limit + 1))
            items = [dict(row) for row in rows.mappings()]
            next_cursor = None
            if len(items) > limit:
                last = items[limit - 1]
                next_cursor = encode_packet_cursor(last, last["sort_value"], order, signature)
            items = items[:limit]
            for item in items:
                item.pop("sort_value")
            await enrich_packet_cards(
                session, player_id, items, self._library_assignments(player_id)
            )
            return {"items": items, "next_cursor": next_cursor}

    async def disclose(
        self,
        session: AsyncSession,
        player_id: UUID,
        version_id: UUID,
        *,
        confirm: bool,
    ) -> TrainingPacketSource:
        await self.lock_access(session, player_id, version_id)
        await require_trainee(session, player_id, lock=True)
        version, logical = await self.authorize(session, player_id, version_id)
        if logical.scope == "training":
            await session.refresh(logical, with_for_update=True)
            if logical.retired_at is not None:
                raise LookupError("Training packet not found")
        rows = (
            await session.execute(
                select(
                    QuestionRevisionRecord.id,
                    ThemeRevisionRecord.id.label("theme_id"),
                    ThemeRevisionRecord.name,
                    PacketQuestionRecord.value,
                    QuestionRevisionRecord.text,
                    QuestionRevisionRecord.answer,
                    QuestionRevisionRecord.accepted_answers,
                    QuestionRevisionRecord.rejected_answers,
                    QuestionRevisionRecord.commentary,
                    QuestionRevisionRecord.source,
                    QuestionRevisionRecord.form,
                    ThemeRevisionRecord.theme_id.label("logical_theme_id"),
                    QuestionRevisionRecord.question_id.label("logical_question_id"),
                )
                .select_from(PacketQuestionRecord)
                .join(
                    QuestionRevisionRecord,
                    QuestionRevisionRecord.id == PacketQuestionRecord.question_revision_id,
                )
                .join(ThemeRevisionRecord)
                .where(
                    PacketQuestionRecord.packet_version_id == version_id,
                )
                .order_by(ThemeRevisionRecord.position, PacketQuestionRecord.position)
            )
        ).all()
        if not rows:
            raise ValueError("Training packet has no questions")
        claims = {("theme", row.logical_theme_id) for row in rows} | {
            ("question", row.logical_question_id) for row in rows
        }
        existing_query = (
            select(PlayerExposureClaimRecord)
            .where(
                PlayerExposureClaimRecord.player_id == player_id,
                tuple_(
                    PlayerExposureClaimRecord.claim_namespace, PlayerExposureClaimRecord.claim_id
                ).in_(sorted(claims)),
                PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
            )
            .order_by(PlayerExposureClaimRecord.claim_namespace, PlayerExposureClaimRecord.claim_id)
        )
        existing = list(await session.scalars(existing_query.with_for_update()))
        if any(claim.state == "reserved" for claim in existing):
            raise PermissionError("Packet is reserved for an undisclosed game")
        burnt = {(claim.claim_namespace, claim.claim_id) for claim in existing}
        if claims - burnt and not confirm:
            raise DisclosureRequired(
                len(
                    {
                        row.logical_theme_id
                        for row in rows
                        if ("theme", row.logical_theme_id) not in burnt
                        or ("question", row.logical_question_id) not in burnt
                    }
                )
            )
        for namespace, identity in sorted(claims - burnt):
            await session.execute(
                insert(PlayerExposureClaimRecord)
                .values(
                    player_id=player_id,
                    packet_version_id=version_id,
                    claim_namespace=namespace,
                    claim_id=identity,
                    state="burnt",
                    burnt_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(
                    index_elements=["player_id", "claim_namespace", "claim_id"],
                    index_where=text("state IN ('reserved', 'burnt')"),
                )
            )
        current = list(
            await session.scalars(
                existing_query.with_for_update().execution_options(
                    populate_existing=True,
                )
            )
        )
        if {(c.claim_namespace, c.claim_id) for c in current if c.state == "burnt"} != claims:
            raise PermissionError("Packet exposure changed during disclosure")
        questions = tuple(
            TrainingSourceQuestion(
                row.id,
                row.theme_id,
                row.name,
                row.value,
                row.text,
                row.answer,
                tuple(row.accepted_answers),
                tuple(row.rejected_answers),
                row.commentary,
                row.source,
                row.form,
            )
            for row in rows
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                [asdict(q) for q in questions],
                sort_keys=True,
                default=str,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        receipt = {
            "player_id": str(player_id),
            "version_id": str(version_id),
            "fingerprint": fingerprint,
            "confirmed_at": datetime.now(UTC).isoformat(),
        }
        return TrainingPacketSource(
            version_id,
            logical.id,
            logical.scope,
            logical.uploader_id,
            fingerprint,
            questions,
            receipt,
        )

    async def import_upload(
        self,
        player_id: UUID,
        source: bytes,
        filename: str,
        *,
        question_values: tuple[int, ...] = (10, 20, 30, 40, 50),
    ) -> dict:
        settings = GameSettings(
            question_values=question_values, minimum_players=1, maximum_players=1
        )
        filename = Path(filename).name
        if Path(filename).suffix.lower() not in {".json", ".docx", ".pdf"}:
            raise ValueError("Only DOCX, PDF and JSON packet files are supported")
        if not source or len(source) > self.packets.MAX_UPLOAD_BYTES:
            raise ValueError("Training upload exceeds the packet file size limit")
        async with self.database.sessions() as session:
            await require_trainee(session, player_id)
        try:
            packets = await asyncio.to_thread(packets_from_document_bytes, source, filename)
            self.packets._require_reasonable_content_size({"packets": [asdict(p) for p in packets]})
            contents = [asdict(p) for p in packets]
            validations = []
            for packet in packets:
                errors, warnings = self.packets.validate(packet)
                errors.extend(SIGameRuleset().validate_content(packet, settings))
                validations.append((errors, warnings))
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            contents, validations = [{}], [([str(error)], [])]
        drafts = []
        async with self.database.transaction() as session:
            await require_trainee(session, player_id, lock=True)
            for content, (errors, warnings) in zip(contents, validations, strict=True):
                draft = PacketDraftRecord(
                    scope="training",
                    uploader_id=player_id,
                    creation_tournament_id=None,
                    source_filename=filename,
                    source_checksum=hashlib.sha256(source).hexdigest(),
                    content=content,
                    validation_errors=errors,
                    validation_warnings=warnings,
                    status="validation_failed" if errors else "awaiting_confirmation",
                )
                session.add(draft)
                await session.flush()
                drafts.append(self._draft_view(draft))
        return {"drafts": drafts}

    @staticmethod
    def _draft_view(draft):
        return {
            "draft_id": str(draft.id),
            "version": draft.version,
            "status": draft.status,
            "packet": draft.content,
            "errors": draft.validation_errors,
            "warnings": draft.validation_warnings,
        }

    async def draft(self, player_id: UUID, draft_id: UUID) -> dict:
        async with self.database.sessions() as session:
            await require_trainee(session, player_id)
            draft = await session.get(PacketDraftRecord, draft_id)
            if draft is None or draft.scope != "training" or draft.uploader_id != player_id:
                raise LookupError("Training draft not found")
            if draft.published_version_id is not None:
                await self.authorize(session, player_id, draft.published_version_id)
            return self._draft_view(draft)

    async def access(
        self,
        player_id: UUID,
        version_id: UUID,
        *,
        confirm: bool,
        download: bool,
        request_key: str,
    ) -> dict:
        try:
            async with self.database.transaction() as session:
                source = await self.disclose(session, player_id, version_id, confirm=confirm)
                player = await require_trainee(session, player_id)
                pages = {}
                for question in source.questions:
                    page = pages.setdefault(
                        question.theme_revision_id,
                        {
                            "title": question.theme,
                            "author": "",
                            "questions": [],
                        },
                    )
                    page["questions"].append(
                        {
                            "value": question.value,
                            "text": question.text,
                            "answer": question.answer,
                            "accepted_answers": list(question.accepted_answers),
                            "rejected_answers": list(question.rejected_answers),
                            "commentary": question.commentary,
                            "source": question.source,
                            "form": question.form,
                        }
                    )
                version = await session.get(PacketVersionRecord, version_id)
                if source.scope == "training":
                    draft = await session.get(PacketDraftRecord, version.source_draft_id)
                    original = self.packets._packet_from_content(draft.content)
                    for page, theme in zip(pages.values(), original.themes, strict=True):
                        page.update(
                            authors=list(theme.authors),
                            author="; ".join(theme.authors),
                            commentary=theme.commentary,
                        )
                        for item, question in zip(page["questions"], theme.questions, strict=True):
                            item.update(
                                authors=list(question.authors or theme.authors),
                                inherits_theme_authors=not question.authors,
                            )
                if download:
                    if player.telegram_user_id is None:
                        raise PermissionError("Telegram account is required for downloads")
                    await TransactionalOutbox.enqueue(
                        session,
                        topic="telegram.training.document",
                        deduplication_key=f"training:download:{player_id}:{request_key}",
                        partition_key=f"telegram:chat:{player.telegram_user_id}",
                        payload={"owner_id": str(player_id), "version_id": str(version_id)},
                    )
                    return {"confirmation_required": False, "queued": True}
                return {
                    "confirmation_required": False,
                    "name": version.name,
                    "pages": list(pages.values()),
                }
        except DisclosureRequired as error:
            return {"confirmation_required": True, "fresh_unit_count": error.fresh_unit_count}

    async def decide_draft(
        self,
        player_id: UUID,
        draft_id: UUID,
        *,
        expected_version: int,
        publish: bool,
    ) -> dict:
        await self.draft(player_id, draft_id)
        if publish:
            stored = await self.packets.publish(
                draft_id,
                administrator_id=player_id,
                expected_version=expected_version,
            )
            return {"version_id": str(stored.version_id)}
        async with self.database.transaction() as session:
            await require_trainee(session, player_id)
            draft = await session.get(PacketDraftRecord, draft_id, with_for_update=True)
            if draft.version != expected_version:
                raise StaleWriteError("Training draft has changed")
            if draft.status not in {"awaiting_confirmation", "validation_failed"}:
                raise ValueError("Training draft is no longer editable")
            draft.status = "rejected"
            draft.version += 1
            return self._draft_view(draft)

    async def retire(self, player_id: UUID, version_id: UUID) -> dict:
        async with self.database.transaction() as session:
            await require_trainee(session, player_id, lock=True)
            _, logical = await self.authorize(session, player_id, version_id)
            if logical.scope != "training" or logical.uploader_id != player_id:
                raise LookupError("Private packet not found")
            await session.refresh(logical, with_for_update=True)
            logical.retired_at = datetime.now(UTC)
            records = await session.scalars(
                select(TrainingSessionRecord)
                .where(
                    TrainingSessionRecord.owner_id == player_id,
                    TrainingSessionRecord.packet_version_id.in_(
                        select(PacketVersionRecord.id).where(
                            PacketVersionRecord.packet_id == logical.id,
                        )
                    ),
                    TrainingSessionRecord.status.in_(("active", "paused")),
                )
                .with_for_update()
            )
            for record in records:
                record.status, record.ended_at = "stopped", logical.retired_at
                record.deadline = record.hidden_deadline = None
                record.version += 1
                record.presentation_version += 1
                await TransactionalOutbox.enqueue(
                    session,
                    topic="training.changed",
                    deduplication_key=f"training:{record.id}:presentation:{record.presentation_version}",
                    partition_key=f"training:{record.id}",
                    aggregate_type="training_session",
                    aggregate_id=record.id,
                    payload={"session_id": str(record.id), "owner_id": str(player_id)},
                )
            await TransactionalOutbox.enqueue(
                session,
                topic="training.packet.retired",
                deduplication_key=f"training:retired:{logical.id}",
                partition_key=f"training:owner:{player_id}",
                payload={"packet_id": str(logical.id), "owner_id": str(player_id)},
            )
            return {"retired": True}
