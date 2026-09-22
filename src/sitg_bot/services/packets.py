import asyncio
import hashlib
import itertools
import json
from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.packet import Packet
from sitg_bot.packet_import import packet_from_data, packets_from_document_bytes
from sitg_bot.services.author_exposure import burn_author_content, tournament_manager_ids
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.tournaments import PACKET_ACCESS_DEFAULT_POLICIES, TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    LogicalPacketRecord,
    LogicalQuestionRecord,
    PacketDraftRecord,
    PacketDraftTournamentRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerRecord,
    QuestionRevisionRecord,
    ThemeRecord,
    ThemeRevisionRecord,
    TournamentAuthorRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentRecord,
)
from sitg_bot.storage.packets import PostgresPacketRepository, StoredPacket


class PacketAdminService:
    MAX_UPLOAD_BYTES = 4 * 1024 * 1024
    MAX_INTERPRETED_BYTES = 4 * 1024 * 1024

    def __init__(self, database: Database) -> None:
        self.database = database
        self.tournaments = TournamentService(database)

    async def _management_records(
        self,
        session: AsyncSession,
        tournament_id: UUID,
        assignment_id: UUID,
        actor_id: UUID,
    ) -> tuple[TournamentPacketAssignmentRecord, PacketVersionRecord]:
        await self.tournaments._require_manager(session, tournament_id, actor_id)
        await self.tournaments.require_modifiable(session, tournament_id)
        assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
        if assignment is None or assignment.tournament_id != tournament_id:
            raise LookupError("Tournament packet not found")
        # Serialize all edits of this logical packet, including edits in other tournaments.
        await session.get(LogicalPacketRecord, assignment.packet_id, with_for_update=True)
        await session.refresh(assignment)
        if assignment.status != "active":
            raise LookupError("Tournament packet was deleted")
        version = await session.scalar(
            select(PacketVersionRecord)
            .where(
                PacketVersionRecord.packet_id == assignment.packet_id,
                PacketVersionRecord.state == "published",
                *(
                    [PacketVersionRecord.id == assignment.adopted_version_id]
                    if assignment.adopted_version_id
                    else []
                ),
            )
            .order_by(PacketVersionRecord.version_number.desc())
            .limit(1)
        )
        if version is None:
            raise LookupError("Published packet version not found")
        return assignment, version

    async def existing_packet(
        self, tournament_id: UUID, packet_id: UUID, actor_id: UUID,
        *, expected_version_id: UUID | None = None,
    ) -> dict:
        """Preview an accessible version, or attach that exact version after confirmation."""
        async with self.database.transaction() as session:
            await self.tournaments._require_manager(session, tournament_id, actor_id)
            await self.tournaments.require_modifiable(session, tournament_id)
            await session.get(LogicalPacketRecord, packet_id, with_for_update=True)
            sources = (await session.scalars(
                select(TournamentPacketAssignmentRecord)
                .join(TournamentManagerRecord, TournamentManagerRecord.tournament_id
                      == TournamentPacketAssignmentRecord.tournament_id)
                .where(
                    TournamentPacketAssignmentRecord.packet_id == packet_id,
                    TournamentPacketAssignmentRecord.status == "active",
                    TournamentPacketAssignmentRecord.tournament_id != tournament_id,
                    TournamentManagerRecord.player_id == actor_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
            )).all()
            versions = []
            for source in sources:
                version = await session.scalar(
                    select(PacketVersionRecord).where(
                        PacketVersionRecord.packet_id == packet_id,
                        PacketVersionRecord.state == "published",
                        *([PacketVersionRecord.id == source.adopted_version_id]
                          if source.adopted_version_id else []),
                    ).order_by(PacketVersionRecord.version_number.desc()).limit(1)
                )
                if version is not None:
                    versions.append(version)
            if not versions:
                raise LookupError("Packet not found in another managed tournament")
            version = max(versions, key=lambda item: item.version_number)
            if expected_version_id is not None and version.id != expected_version_id:
                raise StaleWriteError("Packet changed; look it up again")
            existing = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                TournamentPacketAssignmentRecord.packet_id == packet_id,
            ))
            if existing is not None and existing.status == "active":
                raise ValueError("Packet is already assigned to this tournament")
            stored = await PostgresPacketRepository().get(session, version.id)
            assert stored is not None
            context = await self.tournaments.context(session, tournament_id)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            errors = ruleset.validate_content(stored.packet, context.settings)
            if errors:
                raise ValueError("; ".join(errors))
            if expected_version_id is not None:
                assignment = existing or TournamentPacketAssignmentRecord(
                    tournament_id=tournament_id, packet_id=packet_id,
                )
                assignment.status = "active"
                assignment.adopted_version_id = version.id
                assignment.assigned_by_id = actor_id
                # A retired assignment retains its explicit per-player rights.
                if existing is None:
                    defaults = {name: context.policies.get(name, default)
                                for name, default in PACKET_ACCESS_DEFAULT_POLICIES.items()}
                    assignment.discoverable_by_members = defaults["packets_discoverable_by_default"]
                    assignment.playable_by_members = defaults["packets_playable_by_default"]
                    assignment.content_visible_by_members = defaults["packets_readable_by_default"]
                    assignment.library_viewing_rule = context.policies.get(
                        "library_viewing_rule_default", "after-play"
                    )
                    if defaults["packets_released_by_default"]:
                        version.library_released_at = (
                            version.library_released_at or datetime.now(UTC)
                        )
                session.add(assignment)
                await burn_author_content(
                    session, version_id=version.id,
                    player_ids=await tournament_manager_ids(session, [tournament_id]),
                )
                await self.tournaments._invalidate_assembling_lobbies(session, tournament_id)
            return {
                "packet_id": str(packet_id), "packet_version_id": str(version.id),
                "name": version.name, "year": version.year,
                "lead_author": stored.packet.lead_author,
                "authors": self._detected_authors(stored.packet),
                "theme_count": len(stored.packet.themes),
                "question_count": sum(len(theme.questions) for theme in stored.packet.themes),
            }

    @staticmethod
    async def _version_fields(session, version):
        themes = (
            await session.scalars(
                select(ThemeRevisionRecord)
                .where(ThemeRevisionRecord.packet_version_id == version.id)
                .order_by(ThemeRevisionRecord.position)
            )
        ).all()
        authors = {"lead_author": version.lead_author_id}
        rows = []
        for i, theme in enumerate(themes):
            authors[f"themes.{i}.author"] = theme.author_id
            questions = (
                await session.execute(
                    select(PacketQuestionRecord, QuestionRevisionRecord)
                    .join(
                        QuestionRevisionRecord,
                        QuestionRevisionRecord.id == PacketQuestionRecord.question_revision_id,
                    )
                    .where(PacketQuestionRecord.theme_revision_id == theme.id)
                    .order_by(PacketQuestionRecord.position)
                )
            ).all()
            for j, (_, question) in enumerate(questions):
                authors[f"themes.{i}.questions.{j}.author"] = question.author_id
            rows.append((theme, questions))
        return authors, rows

    async def management_editor(
        self,
        tournament_id: UUID,
        assignment_id: UUID,
        actor_id: UUID,
    ) -> dict[str, object]:
        async with self.database.transaction() as session:
            _, version = await self._management_records(
                session, tournament_id, assignment_id, actor_id
            )
            stored = await PostgresPacketRepository().get(session, version.id)
            assert stored is not None
            authors, _ = await self._version_fields(session, version)
            associated = (
                await session.scalars(
                    select(AuthorRecord).where(
                        AuthorRecord.id.in_({value for value in authors.values() if value})
                    )
                )
            ).all()
            context = await self.tournaments.context(session, tournament_id)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            _, warnings = self.validate(stored.packet)
            return {
                "assignment_id": str(assignment_id),
                "version": version.version_number,
                "packet": asdict(stored.packet),
                "editor": dict(ruleset.packet_editor(context.settings)),
                "errors": [],
                "warnings": warnings,
                "can_publish": False,
                "can_reject": False,
                "field_author_ids": {
                    key: str(value) if value else None for key, value in authors.items()
                },
                "associated_authors": [
                    {"author_id": str(a.id), "display_name": a.display_name} for a in associated
                ],
            }

    async def management_action(
        self,
        tournament_id: UUID,
        assignment_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        delete: bool,
    ) -> None:
        async with self.database.transaction() as session:
            assignment, version = await self._management_records(
                session, tournament_id, assignment_id, actor_id
            )
            if version.version_number != expected_version:
                raise StaleWriteError("Packet version has changed")
            if delete:
                assignment.status = "retired"
                await self.tournaments._invalidate_assembling_lobbies(session, tournament_id)
            elif version.library_released_at is None:
                version.library_released_at = datetime.now(UTC)

    @staticmethod
    def _flat_fields(content: dict[str, object]) -> dict[str, object]:
        fields = {key: value for key, value in content.items() if key != "themes"}
        for i, theme in enumerate(content["themes"]):
            fields.update(
                {f"themes.{i}.{key}": value for key, value in theme.items() if key != "questions"}
            )
            for j, question in enumerate(theme["questions"]):
                fields.update(
                    {f"themes.{i}.questions.{j}.{key}": value for key, value in question.items()}
                )
        return fields

    @classmethod
    def _validate_changes(cls, old, new, old_authors, authors, changes):
        before, after = cls._flat_fields(old), cls._flat_fields(new)
        if before.keys() != after.keys() or old_authors.keys() != authors.keys():
            raise ValueError("Packet structure cannot be changed in this editor")
        actual = {key for key in before if before[key] != after[key]}
        actual.update(key for key in authors if authors[key] != old_authors[key])
        if not actual or actual != changes.keys():
            raise ValueError("Every enabled field must change; every change must be classified")
        for path, kind in changes.items():
            allowed = (
                path.startswith("themes.")
                and path.endswith(".name")
                or ".questions." in path
                and path.rsplit(".", 1)[-1] in {"text", "answer", "accepted_answers"}
            )
            if kind not in {"correction", "substitution"} or kind == "substitution" and not allowed:
                raise ValueError(
                    "Substitution is only supported for theme names and question content"
                )

    async def modify_packet(
        self,
        tournament_id: UUID,
        assignment_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        content: dict[str, object],
        changes: dict[str, str],
        field_author_ids: dict[str, UUID | None],
    ) -> None:
        self._require_reasonable_content_size(content)
        packet = packet_from_data(content)
        async with self.database.transaction() as session:
            assignment, old = await self._management_records(
                session, tournament_id, assignment_id, actor_id
            )
            if old.version_number != expected_version:
                raise StaleWriteError("Packet version has changed")
            stored = await PostgresPacketRepository().get(session, old.id)
            assert stored is not None
            old_authors, rows = await self._version_fields(session, old)
            self._validate_changes(
                asdict(stored.packet), asdict(packet), old_authors, field_author_ids, changes
            )
            substitution = "substitution" in changes.values()
            assignments = (
                await session.scalars(
                    select(TournamentPacketAssignmentRecord)
                    .where(
                        TournamentPacketAssignmentRecord.packet_id == old.packet_id,
                        TournamentPacketAssignmentRecord.status == "active",
                    )
                    .order_by(TournamentPacketAssignmentRecord.tournament_id)
                )
            ).all()
            # A substitution branches the shared revision for this tournament only.
            latest_id = await session.scalar(
                select(PacketVersionRecord.id)
                .where(
                    PacketVersionRecord.packet_id == old.packet_id,
                    PacketVersionRecord.state == "published",
                )
                .order_by(PacketVersionRecord.version_number.desc())
                .limit(1)
            )
            adopted_versions = {
                item.id: item.adopted_version_id or latest_id for item in assignments
            }
            affected = (
                [assignment]
                if substitution
                else [item for item in assignments if adopted_versions[item.id] == old.id]
            )
            errors, warnings = self.validate(packet)
            for item in affected:
                context = await self.tournaments.context(session, item.tournament_id)
                ruleset = self.tournaments.rulesets.get(
                    context.ruleset_key, context.ruleset_version
                )
                errors.extend(ruleset.validate_content(packet, context.settings))
            if errors:
                raise ValueError("; ".join(errors))
            # Capture the former author's exposure before changing statistical attribution.
            await burn_author_content(session, version_id=old.id)
            fields = self._flat_fields(asdict(packet))
            resolved: dict[str, AuthorRecord] = {}
            author_ids = dict(field_author_ids)
            for path, author_id in author_ids.items():
                name = str(fields[path]).strip()
                if author_id:
                    author = await session.get(AuthorRecord, author_id)
                    if author is None or name != author.display_name:
                        raise ValueError("Author field must match its selected registered author")
                    resolved[str(author.id)] = author
                elif name:
                    author = await self._author(session, name, resolved)
                    author_ids[path] = author.id
            now = datetime.now(UTC)
            draft = PacketDraftRecord(
                status="published",
                source_filename="packet-management",
                source_checksum=hashlib.sha256(
                    json.dumps(content, sort_keys=True).encode()
                ).hexdigest(),
                content=asdict(packet),
                validation_errors=[],
                validation_warnings=warnings,
                uploader_id=actor_id,
                creation_tournament_id=tournament_id,
                confirmed_by_id=actor_id,
                confirmed_at=now,
            )
            session.add(draft)
            await session.flush()
            number = await session.scalar(
                select(func.max(PacketVersionRecord.version_number)).where(
                    PacketVersionRecord.packet_id == old.packet_id
                )
            )
            version = PacketVersionRecord(
                packet_id=old.packet_id,
                version_number=number + 1,
                name=packet.name,
                year=packet.year,
                language=packet.language,
                lead_author_id=author_ids["lead_author"],
                source_draft_id=draft.id,
                previous_version_id=old.id,
                change_type="substitution" if substitution else "correction",
                change_reason=json.dumps(changes, sort_keys=True),
                published_by_id=actor_id,
                library_released_at=old.library_released_at,
            )
            session.add(version)
            await session.flush()
            draft.published_version_id = version.id
            logical_packet = await session.get(LogicalPacketRecord, old.packet_id)
            if "lead_author" in changes:
                logical_packet.statistical_author_id = version.lead_author_id
            for i, (old_theme, questions) in enumerate(rows):
                theme = packet.themes[i]
                prefix = f"themes.{i}"
                replace_theme = changes.get(f"{prefix}.name") == "substitution"
                logical_theme = (
                    ThemeRecord(packet_id=old.packet_id)
                    if replace_theme
                    else await session.get(ThemeRecord, old_theme.theme_id)
                )
                theme_author_id = author_ids[f"{prefix}.author"]
                if replace_theme or f"{prefix}.author" in changes:
                    logical_theme.statistical_author_id = theme_author_id
                session.add(logical_theme)
                await session.flush()
                revision_number = await session.scalar(
                    select(func.max(ThemeRevisionRecord.revision_number)).where(
                        ThemeRevisionRecord.theme_id == logical_theme.id
                    )
                )
                theme_revision = ThemeRevisionRecord(
                    theme_id=logical_theme.id,
                    packet_version_id=version.id,
                    revision_number=(revision_number or 0) + 1,
                    position=i + 1,
                    name=theme.name,
                    author_id=theme_author_id,
                    commentary=theme.commentary,
                )
                session.add(theme_revision)
                await session.flush()
                for j, (_, old_question) in enumerate(questions):
                    question = theme.questions[j]
                    path = f"{prefix}.questions.{j}"
                    replace_question = replace_theme or any(
                        changes.get(f"{path}.{field}") == "substitution"
                        for field in ("text", "answer", "accepted_answers")
                    )
                    question_author_id = author_ids[f"{path}.author"]
                    if (
                        f"{prefix}.author" in changes
                        and f"{path}.author" not in changes
                        and old_question.author_id == old_theme.author_id
                    ):
                        question_author_id = theme_author_id
                    logical_question = (
                        LogicalQuestionRecord(packet_id=old.packet_id)
                        if replace_question
                        else await session.get(LogicalQuestionRecord, old_question.question_id)
                    )
                    if replace_question or question_author_id != old_question.author_id:
                        logical_question.statistical_author_id = question_author_id
                    session.add(logical_question)
                    await session.flush()
                    revision_number = await session.scalar(
                        select(func.max(QuestionRevisionRecord.revision_number)).where(
                            QuestionRevisionRecord.question_id == logical_question.id
                        )
                    )
                    revision = QuestionRevisionRecord(
                        question_id=logical_question.id,
                        revision_number=(revision_number or 0) + 1,
                        text=question.text,
                        answer=question.answer,
                        accepted_answers=list(question.accepted_answers),
                        commentary=question.commentary,
                        form=question.form,
                        source=question.source,
                        author_id=question_author_id,
                    )
                    session.add(revision)
                    await session.flush()
                    session.add(
                        PacketQuestionRecord(
                            packet_version_id=version.id,
                            theme_revision_id=theme_revision.id,
                            question_revision_id=revision.id,
                            position=j + 1,
                            value=question.value,
                        )
                    )
            # Pin followers before publishing a branch so another tournament cannot
            # silently start following this tournament's substitution.
            for item in assignments:
                item.adopted_version_id = adopted_versions[item.id]
            for item in affected:
                item.adopted_version_id = version.id
                await self._add_tournament_authors(
                    session, item.tournament_id, resolved.values(), actor_id, lead=None
                )
                await self.tournaments._invalidate_assembling_lobbies(session, item.tournament_id)
            if not substitution:
                old.deleted_at = now
            if not any(item.adopted_version_id == old.id for item in assignments):
                old.state = "archived"
            if substitution:
                actor = await session.get(PlayerRecord, actor_id)
                for item in assignments:
                    if item.id == assignment.id:
                        continue
                    managers = await session.scalars(
                        select(TournamentManagerRecord.player_id).where(
                            TournamentManagerRecord.tournament_id == item.tournament_id,
                            TournamentManagerRecord.revoked_at.is_(None),
                        )
                    )
                    for manager_id in managers:
                        await NotificationWriter.create_for_player(
                            session,
                            recipient_player_id=manager_id,
                            audience="manager",
                            kind="packet.substituted",
                            deduplication_key=f"packet-substitution:{version.id}:{item.id}:{manager_id}",
                            payload={
                                "packet_name": old.name,
                                "manager_name": actor.public_nickname or actor.real_name,
                                "tournament_id": str(item.tournament_id),
                            },
                        )
            # The editing actor and every manager of this tournament have seen the
            # new version, including freshly substituted theme identities.
            seen_players = await tournament_manager_ids(session, (tournament_id,))
            seen_players.add(actor_id)
            await burn_author_content(
                session, version_id=version.id, player_ids=seen_players
            )

    async def import_json(
        self,
        path: str | Path,
        *,
        uploader_id: UUID | None = None,
        tournament_id: UUID,
        intended_tournament_ids: tuple[UUID, ...] = (),
    ) -> UUID:
        path = Path(path)
        source = path.read_bytes()
        checksum = hashlib.sha256(source).hexdigest()
        content: object = None
        try:
            content = json.loads(source)
            if not isinstance(content, dict):
                raise TypeError("the document root must be an object")
            packet = packet_from_data(content)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            async with self.database.transaction() as session:
                context = await self.tournaments.context(session, tournament_id)
                await self.tournaments.require_modifiable(session, tournament_id)
                if uploader_id is not None:
                    await self._require_upload_access(session, context, uploader_id)
                draft = PacketDraftRecord(
                    status="validation_failed",
                    source_filename=path.name,
                    source_checksum=checksum,
                    content=(
                        content
                        if isinstance(content, dict)
                        else {"unparsed": source.decode("utf-8", errors="replace")}
                    ),
                    validation_errors=[str(error)],
                    validation_warnings=[],
                    uploader_id=uploader_id,
                    creation_tournament_id=context.tournament_id,
                )
                session.add(draft)
                await session.flush()
                for intended_id in intended_tournament_ids or (context.tournament_id,):
                    session.add(
                        PacketDraftTournamentRecord(draft_id=draft.id, tournament_id=intended_id)
                    )
            return draft.id
        return await self.create_draft(
            packet,
            source_filename=path.name,
            source_checksum=checksum,
            uploader_id=uploader_id,
            tournament_id=tournament_id,
            intended_tournament_ids=intended_tournament_ids,
        )

    async def upload_eligibility(self, tournament_id: UUID, actor_id: UUID) -> dict[str, object]:
        async with self.database.sessions() as session:
            context = await self.tournaments.context(session, tournament_id)
            await self._require_upload_access(session, context, actor_id)
            return {
                "tournament_id": str(context.tournament_id),
                "ruleset_key": context.ruleset_key,
                "ruleset_version": context.ruleset_version,
                "accepted_extensions": [".docx", ".pdf", ".json"],
                "maximum_bytes": self.MAX_UPLOAD_BYTES,
            }

    async def import_upload(
        self,
        source: bytes,
        *,
        source_filename: str,
        uploader_id: UUID,
        tournament_id: UUID,
    ) -> dict[str, object]:
        """Persist all interpreted drafts; single uploads retain their v1 response shape."""
        filename = Path(source_filename).name
        extension = Path(filename).suffix.casefold()
        if extension not in {".docx", ".pdf", ".json"}:
            raise ValueError("Only DOCX, PDF and JSON packet files are supported")
        if not source or len(source) > self.MAX_UPLOAD_BYTES:
            raise ValueError(f"Packet file must be between 1 and {self.MAX_UPLOAD_BYTES} bytes")
        await self.upload_eligibility(tournament_id, uploader_id)
        checksum = hashlib.sha256(source).hexdigest()
        parsed_content: dict[str, object] = {}
        try:
            if extension == ".json":
                decoded = json.loads(source.decode("utf-8-sig"))
                if isinstance(decoded, dict):
                    parsed_content = decoded
            packets = await asyncio.to_thread(packets_from_document_bytes, source, filename)
            self._require_reasonable_content_size({"packets": [asdict(p) for p in packets]})
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            stored_content = parsed_content
            if (
                len(
                    json.dumps(
                        stored_content, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                )
                > self.MAX_INTERPRETED_BYTES
            ):
                stored_content = {}
            async with self.database.transaction() as session:
                context = await self.tournaments.context(session, tournament_id)
                await self._require_upload_access(session, context, uploader_id)
                draft = PacketDraftRecord(
                    status="validation_failed",
                    source_filename=filename,
                    source_checksum=checksum,
                    content=stored_content,
                    validation_errors=[str(error)],
                    validation_warnings=[],
                    uploader_id=uploader_id,
                    creation_tournament_id=context.tournament_id,
                )
                session.add(draft)
                await session.flush()
                session.add(
                    PacketDraftTournamentRecord(
                        draft_id=draft.id, tournament_id=context.tournament_id
                    )
                )
                draft_id = draft.id
            return await self.draft_summary(draft_id, uploader_id)
        draft_ids = await self._create_drafts(
            packets,
            source_filename=filename,
            source_checksum=checksum,
            uploader_id=uploader_id,
            tournament_id=tournament_id,
        )
        summaries = [await self.draft_summary(draft_id, uploader_id) for draft_id in draft_ids]
        return summaries[0] if len(summaries) == 1 else {"drafts": summaries}

    async def create_draft(
        self,
        packet: Packet,
        *,
        source_filename: str,
        source_checksum: str | None = None,
        uploader_id: UUID | None = None,
        tournament_id: UUID,
        intended_tournament_ids: tuple[UUID, ...] = (),
    ) -> UUID:
        return (await self._create_drafts(
            (packet,), source_filename=source_filename, source_checksum=source_checksum,
            uploader_id=uploader_id, tournament_id=tournament_id,
            intended_tournament_ids=intended_tournament_ids,
        ))[0]

    async def _create_drafts(
        self,
        packets: tuple[Packet, ...],
        *,
        source_filename: str,
        source_checksum: str | None = None,
        uploader_id: UUID | None = None,
        tournament_id: UUID,
        intended_tournament_ids: tuple[UUID, ...] = (),
    ) -> tuple[UUID, ...]:
        draft_ids: list[UUID] = []
        async with self.database.transaction() as session:
            await self.tournaments.require_modifiable(session, tournament_id)
            context = await self.tournaments.context(session, tournament_id)
            if uploader_id is not None:
                await self._require_upload_access(session, context, uploader_id)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            for packet in packets:
                content = asdict(packet)
                encoded = json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
                errors, warnings = self.validate(packet)
                errors.extend(ruleset.validate_content(packet, context.settings))
                draft = PacketDraftRecord(
                    status="validation_failed" if errors else "awaiting_confirmation",
                    source_filename=source_filename,
                    source_checksum=source_checksum or hashlib.sha256(encoded).hexdigest(),
                    content=content,
                    validation_errors=errors,
                    validation_warnings=warnings,
                    uploader_id=uploader_id,
                    creation_tournament_id=context.tournament_id,
                )
                session.add(draft)
                await session.flush()
                draft_ids.append(draft.id)
                for intended_id in intended_tournament_ids or (context.tournament_id,):
                    session.add(
                        PacketDraftTournamentRecord(draft_id=draft.id, tournament_id=intended_id)
                    )
        return tuple(draft_ids)

    async def preview(self, draft_id: UUID) -> dict[str, object]:
        async with self.database.sessions() as session:
            draft = await session.get(PacketDraftRecord, draft_id)
            if draft is None:
                raise LookupError("Packet draft not found")
            return {
                "id": str(draft.id),
                "status": draft.status,
                "source_filename": draft.source_filename,
                "errors": draft.validation_errors,
                "warnings": draft.validation_warnings,
                "packet": draft.content,
            }

    async def draft_summary(self, draft_id: UUID, actor_id: UUID) -> dict[str, object]:
        draft, context = await self._authorized_draft(draft_id, actor_id)
        packet = self._optional_packet(draft.content)
        authors = self._detected_authors(packet) if packet is not None else ()
        can_publish = await self._can_publish(context.tournament_id, actor_id)
        return {
            "draft_id": str(draft.id),
            "version": draft.version,
            "status": draft.status,
            "source_filename": draft.source_filename,
            "ruleset_key": context.ruleset_key,
            "ruleset_version": context.ruleset_version,
            "packet_name": packet.name if packet is not None else "",
            "theme_count": len(packet.themes) if packet is not None else None,
            "question_count": (
                sum(len(theme.questions) for theme in packet.themes)
                if packet is not None
                else None
            ),
            "detected_authors": list(authors),
            "errors": list(draft.validation_errors),
            "warnings": list(draft.validation_warnings),
            "can_publish": can_publish
            and draft.status == "awaiting_confirmation"
            and not draft.validation_errors,
            "can_reject": draft.status in {"awaiting_confirmation", "validation_failed"},
        }

    async def editable_draft(self, draft_id: UUID, actor_id: UUID) -> dict[str, object]:
        draft, context = await self._authorized_draft(draft_id, actor_id)
        ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
        summary = await self.draft_summary(draft_id, actor_id)
        content = (
            draft.content
            if self._optional_packet(draft.content) is not None
            else {
                "name": "",
                "language": "und",
                "lead_author": "",
                "year": None,
                "themes": [],
            }
        )
        return {
            **summary,
            "packet": content,
            "editor": dict(ruleset.packet_editor(context.settings)),
            "author_bindings": dict(draft.author_bindings),
            "lead_author_id": str(draft.lead_author_id) if draft.lead_author_id else None,
            "associated_authors": await self._associated_authors(draft),
        }

    async def _associated_authors(self, draft: PacketDraftRecord) -> list[dict[str, str]]:
        ids = {UUID(value) for value in draft.author_bindings.values()}
        if draft.lead_author_id:
            ids.add(draft.lead_author_id)
        async with self.database.sessions() as session:
            authors = (await session.scalars(
                select(AuthorRecord).where(AuthorRecord.id.in_(ids))
                .order_by(AuthorRecord.display_name, AuthorRecord.id)
            )).all()
            return [{"author_id": str(author.id), "display_name": author.display_name}
                    for author in authors]

    async def create_author(
        self, draft_id: UUID, actor_id: UUID, *, first_name: str,
        second_name: str | None, surname: str, telegram_link: str | None,
    ) -> dict[str, str]:
        draft, context = await self._authorized_draft(draft_id, actor_id)
        if draft.status not in {"awaiting_confirmation", "validation_failed"}:
            raise ValueError("Only an unpublished packet draft can be edited")
        author = await self.tournaments.create_tournament_author(
            context.tournament_id, actor_id, first_name=first_name,
            second_name=second_name, surname=surname, telegram_link=telegram_link,
        )
        return {"author_id": str(author.id), "display_name": author.display_name}

    async def update_draft(
        self,
        draft_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        content: dict[str, object],
        author_bindings: dict[str, UUID] | None = None,
        lead_author_id: UUID | None = None,
    ) -> dict[str, object]:
        self._require_reasonable_content_size(content)
        packet = packet_from_data(content)
        async with self.database.transaction() as session:
            draft = await session.scalar(
                select(PacketDraftRecord).where(PacketDraftRecord.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("Packet draft not found")
            context = await self.tournaments.context(session, draft.creation_tournament_id)
            await self._require_upload_access(session, context, actor_id)
            if draft.status not in {"awaiting_confirmation", "validation_failed"}:
                raise ValueError("Only an unpublished packet draft can be edited")
            if draft.version != expected_version:
                raise StaleWriteError("Packet draft has changed")
            if lead_author_id is not None:
                lead = await session.get(AuthorRecord, lead_author_id)
                if lead is None:
                    raise LookupError("Lead author not found")
                if not packet.lead_author.strip():
                    packet = replace(packet, lead_author=lead.display_name)
            names = set(self._detected_authors(packet))
            bindings = (
                {name: UUID(value) for name, value in draft.author_bindings.items()
                 if name in names}
                if author_bindings is None else author_bindings
            )
            if set(bindings) - names:
                raise ValueError("Author association must refer to an author in the packet")
            if lead_author_id is None and packet.lead_author == draft.content.get("lead_author"):
                lead_author_id = draft.lead_author_id
            resolved, lead = await self._resolve_authors(
                session, packet, bindings, lead_author_id,
            )
            draft.author_bindings = {name: str(author.id) for name, author in resolved.items()}
            draft.lead_author_id = lead.id if lead else None
            await self._add_tournament_authors(
                session, context.tournament_id, resolved.values(), actor_id,
                lead=lead,
            )
            errors, warnings = self.validate(packet)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            errors.extend(ruleset.validate_content(packet, context.settings))
            draft.content = asdict(packet)
            draft.validation_errors = errors
            draft.validation_warnings = warnings
            draft.status = "validation_failed" if errors else "awaiting_confirmation"
            draft.version += 1
        return await self.editable_draft(draft_id, actor_id)

    async def bind_telegram_message(
        self,
        draft_id: UUID,
        actor_id: UUID,
        *,
        chat_id: int,
        message_id: int,
        locale: str,
    ) -> None:
        if not locale.strip() or len(locale) > 10:
            raise ValueError("Telegram message locale is invalid")
        async with self.database.transaction() as session:
            draft = await session.scalar(
                select(PacketDraftRecord).where(PacketDraftRecord.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("Packet draft not found")
            context = await self.tournaments.context(session, draft.creation_tournament_id)
            await self._require_upload_access(session, context, actor_id)
            if draft.uploader_id != actor_id:
                raise PermissionError("Only the packet uploader can bind its Telegram message")
            existing = (draft.telegram_chat_id, draft.telegram_message_id)
            requested = (chat_id, message_id)
            if existing != (None, None) and existing != requested:
                raise ValueError("Packet draft is already bound to another Telegram message")
            draft.telegram_chat_id = chat_id
            draft.telegram_message_id = message_id
            draft.telegram_locale = locale

    async def reject(
        self,
        draft_id: UUID,
        *,
        actor_id: UUID | None = None,
        notify_bound_telegram: bool = False,
    ) -> None:
        async with self.database.transaction() as session:
            draft = await session.scalar(
                select(PacketDraftRecord).where(PacketDraftRecord.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("Packet draft not found")
            if actor_id is not None:
                context = await self.tournaments.context(session, draft.creation_tournament_id)
                await self._require_upload_access(session, context, actor_id)
            if draft.status not in {"awaiting_confirmation", "validation_failed"}:
                raise ValueError("Only an unpublished draft can be rejected")
            draft.status = "rejected"
            draft.version += 1
            if notify_bound_telegram:
                await self._enqueue_bound_telegram_status(session, draft, "rejected")

    async def publish(
        self,
        draft_id: UUID,
        *,
        administrator_id: UUID | None = None,
        notify_bound_telegram: bool = False,
    ) -> StoredPacket:
        async with self.database.transaction() as session:
            draft = await session.scalar(
                select(PacketDraftRecord).where(PacketDraftRecord.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("Packet draft not found")
            if draft.status != "awaiting_confirmation":
                raise ValueError("Only a validated draft awaiting confirmation can publish")
            if administrator_id is not None and draft.creation_tournament_id is not None:
                await self.tournaments.context(session, draft.creation_tournament_id)
                administrator = await session.get(PlatformAdministratorRecord, administrator_id)
                manager = await session.get(
                    TournamentManagerRecord,
                    (draft.creation_tournament_id, administrator_id),
                )
                if not (
                    administrator is not None
                    and administrator.revoked_at is None
                    or manager is not None
                    and manager.revoked_at is None
                ):
                    raise PermissionError(
                        "Tournament manager or platform administrator role is required"
                    )

            packet = self._packet_from_content(draft.content)
            logical_packet = LogicalPacketRecord(uploader_id=draft.uploader_id)
            session.add(logical_packet)
            await session.flush()
            operation_authors, lead_author = await self._resolve_authors(
                session, packet,
                {name: UUID(value) for name, value in draft.author_bindings.items()},
                draft.lead_author_id,
            )
            draft.author_bindings = {
                name: str(author.id) for name, author in operation_authors.items()
            }
            draft.lead_author_id = lead_author.id if lead_author else None
            logical_packet.statistical_author_id = draft.lead_author_id
            version = PacketVersionRecord(
                packet_id=logical_packet.id,
                version_number=1,
                name=packet.name,
                year=packet.year,
                lead_author_id=lead_author.id if lead_author else None,
                language=packet.language,
                source_draft_id=draft.id,
                published_by_id=administrator_id,
            )
            session.add(version)
            await session.flush()

            for theme_position, theme in enumerate(packet.themes, 1):
                theme_author = await self._author(session, theme.author, operation_authors)
                logical_theme = ThemeRecord(
                    packet_id=logical_packet.id,
                    statistical_author_id=theme_author.id if theme_author else None,
                )
                session.add(logical_theme)
                await session.flush()
                theme_revision = ThemeRevisionRecord(
                    theme_id=logical_theme.id,
                    packet_version_id=version.id,
                    revision_number=1,
                    position=theme_position,
                    name=theme.name,
                    author_id=theme_author.id if theme_author else None,
                    commentary=theme.commentary,
                )
                session.add(theme_revision)
                await session.flush()
                for question_position, question in enumerate(theme.questions, 1):
                    question_author = await self._author(
                        session, question.author or theme.author, operation_authors
                    )
                    logical_question = LogicalQuestionRecord(
                        packet_id=logical_packet.id,
                        statistical_author_id=question_author.id if question_author else None,
                    )
                    session.add(logical_question)
                    await session.flush()
                    revision = QuestionRevisionRecord(
                        question_id=logical_question.id,
                        revision_number=1,
                        text=question.text,
                        answer=question.answer,
                        accepted_answers=list(question.accepted_answers),
                        commentary=question.commentary,
                        form=question.form,
                        source=question.source,
                        author_id=question_author.id if question_author else None,
                    )
                    session.add(revision)
                    await session.flush()
                    session.add(
                        PacketQuestionRecord(
                            packet_version_id=version.id,
                            theme_revision_id=theme_revision.id,
                            question_revision_id=revision.id,
                            position=question_position,
                            value=question.value,
                        )
                    )

            draft.status = "published"
            draft.confirmed_by_id = administrator_id
            draft.confirmed_at = datetime.now(UTC)
            draft.published_version_id = version.id
            intended_tournaments = tuple(
                (
                    await session.execute(
                        select(PacketDraftTournamentRecord.tournament_id).where(
                            PacketDraftTournamentRecord.draft_id == draft.id
                        )
                    )
                ).scalars()
            ) or (draft.creation_tournament_id,)
            for tournament_id in intended_tournaments:
                await self.tournaments.require_modifiable(session, tournament_id)
                context = await self.tournaments.context(session, tournament_id)
                access_defaults = {
                    name: context.policies.get(name, default)
                    for name, default in PACKET_ACCESS_DEFAULT_POLICIES.items()
                }
                if access_defaults["packets_released_by_default"]:
                    version.library_released_at = datetime.now(UTC)
                await self._add_tournament_authors(
                    session, tournament_id, operation_authors.values(), administrator_id,
                    lead=lead_author,
                )
                session.add(
                    TournamentPacketAssignmentRecord(
                        tournament_id=tournament_id,
                        packet_id=logical_packet.id,
                        adopted_version_id=version.id,
                        assigned_by_id=administrator_id,
                        discoverable_by_members=access_defaults["packets_discoverable_by_default"],
                        playable_by_members=access_defaults["packets_playable_by_default"],
                        content_visible_by_members=access_defaults["packets_readable_by_default"],
                        library_viewing_rule=(
                            context.policies.get("library_viewing_rule_default", "after-play")
                        ),
                    )
                )
            # The uploader and every manager of each destination tournament have
            # seen this content and must never be able to play it.
            seen_players = await tournament_manager_ids(session, intended_tournaments)
            if draft.uploader_id is not None:
                seen_players.add(draft.uploader_id)
            await burn_author_content(
                session, version_id=version.id, player_ids=seen_players
            )
            if notify_bound_telegram:
                await self._enqueue_bound_telegram_status(session, draft, "published")
            await session.flush()
            stored = await PostgresPacketRepository().get(session, version.id)
            assert stored is not None
            return stored

    async def _resolve_authors(
        self, session: AsyncSession, packet: Packet, bindings: dict[str, UUID],
        lead_author_id: UUID | None,
    ) -> tuple[dict[str, AuthorRecord], AuthorRecord | None]:
        resolved: dict[str, AuthorRecord] = {}
        for name, author_id in bindings.items():
            author = await session.get(AuthorRecord, author_id)
            if author is None:
                raise LookupError("Associated author not found")
            resolved[name] = author
        lead = await session.get(AuthorRecord, lead_author_id) if lead_author_id else None
        if lead_author_id and lead is None:
            raise LookupError("Lead author not found")
        for name in self._detected_authors(packet):
            if name == " ".join(packet.lead_author.split()) and lead and name not in resolved:
                resolved[name] = lead
            if len(name) <= 300:
                await self._author(session, name, resolved)
        if lead is None:
            lead = resolved.get(" ".join(packet.lead_author.split()))
        return resolved, lead

    @staticmethod
    async def _add_tournament_authors(
        session: AsyncSession, tournament_id: UUID, authors: Iterable[AuthorRecord],
        actor_id: UUID | None,
        *, lead: AuthorRecord | None,
    ) -> None:
        tournament = await session.get(TournamentRecord, tournament_id, with_for_update=True)
        if tournament is None:
            raise LookupError("Tournament not found")
        ids = {author.id for author in authors}
        if lead:
            ids.add(lead.id)
        changed = False
        for author_id in ids:
            if await session.get(TournamentAuthorRecord, (tournament_id, author_id)) is None:
                session.add(TournamentAuthorRecord(
                    tournament_id=tournament_id, author_id=author_id, added_by_id=actor_id,
                ))
                changed = True
        if changed:
            tournament.settings_version += 1

    async def _authorized_draft(self, draft_id: UUID, actor_id: UUID):
        async with self.database.sessions() as session:
            draft = await session.get(PacketDraftRecord, draft_id)
            if draft is None:
                raise LookupError("Packet draft not found")
            context = await self.tournaments.context(session, draft.creation_tournament_id)
            await self._require_upload_access(session, context, actor_id)
            session.expunge(draft)
            return draft, context

    async def _can_publish(self, tournament_id: UUID, actor_id: UUID) -> bool:
        async with self.database.sessions() as session:
            administrator = await session.get(PlatformAdministratorRecord, actor_id)
            if administrator is not None and administrator.revoked_at is None:
                return True
            manager = await session.get(TournamentManagerRecord, (tournament_id, actor_id))
            return manager is not None and manager.revoked_at is None

    @staticmethod
    async def _enqueue_bound_telegram_status(
        session: AsyncSession,
        draft: PacketDraftRecord,
        status: str,
    ) -> None:
        if draft.telegram_chat_id is None or draft.telegram_message_id is None:
            return
        await TransactionalOutbox.enqueue(
            session,
            topic="telegram.packet.draft_status",
            deduplication_key=(
                f"packet-draft:{draft.id}:status:{status}:message:{draft.telegram_message_id}"
            ),
            partition_key=f"telegram:chat:{draft.telegram_chat_id}",
            aggregate_type="packet_draft",
            aggregate_id=draft.id,
            payload={
                "chat_id": draft.telegram_chat_id,
                "message_id": draft.telegram_message_id,
                "locale": draft.telegram_locale or "ru",
                "status": status,
            },
        )

    @classmethod
    def _require_reasonable_content_size(cls, content: dict[str, object]) -> None:
        encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > cls.MAX_INTERPRETED_BYTES:
            raise ValueError("Interpreted packet content is too large")

    @staticmethod
    def _optional_packet(content: dict[str, object]) -> Packet | None:
        try:
            return packet_from_data(content)
        except ValueError:
            return None

    @staticmethod
    def _detected_authors(packet: Packet) -> tuple[str, ...]:
        names = [packet.lead_author]
        for theme in packet.themes:
            names.append(theme.author)
            names.extend(question.author for question in theme.questions)
        return tuple(
            dict.fromkeys(" ".join(name.split()) for name in names if name.strip())
        )

    async def set_library_release(
        self,
        packet_id: UUID,
        actor_id: UUID,
        *,
        released: bool,
        version_id: UUID | None = None,
    ) -> None:
        """Set the owner-controlled packet library release gate."""
        async with self.database.transaction() as session:
            packet = await session.get(LogicalPacketRecord, packet_id)
            if packet is None:
                raise LookupError("Packet not found")
            administrator = await session.get(PlatformAdministratorRecord, actor_id)
            is_administrator = administrator is not None and administrator.revoked_at is None
            if packet.uploader_id != actor_id and not is_administrator:
                raise PermissionError("Packet owner or platform administrator role is required")
            version = (
                await session.get(PacketVersionRecord, version_id)
                if version_id is not None
                else await session.scalar(
                    select(PacketVersionRecord)
                    .where(
                        PacketVersionRecord.packet_id == packet_id,
                        PacketVersionRecord.state == "published",
                    )
                    .order_by(PacketVersionRecord.version_number.desc())
                    .limit(1)
                )
            )
            if version is None or version.packet_id != packet_id or version.state != "published":
                raise ValueError("Library release requires a published version of the packet")
            version.library_released_at = datetime.now(UTC) if released else None

    @staticmethod
    def validate(packet: Packet) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not packet.name.strip():
            errors.append("Packet name is empty")
        elif len(packet.name) > 500:
            errors.append("Packet name must be no longer than 500 characters")
        if packet.year is not None and not 1000 <= packet.year <= 9999:
            errors.append("Packet year must contain four digits")
        if not packet.lead_author.strip():
            warnings.append("Packet has no lead author")
        elif len(packet.lead_author) > 300:
            errors.append("Packet lead author must be no longer than 300 characters")
        if not 8 <= len(packet.themes) <= 12:
            warnings.append("A packet normally contains 8–12 themes")
        seen_questions: set[str] = set()
        for theme_index, theme in enumerate(packet.themes, 1):
            if not theme.name.strip():
                errors.append(f"Theme {theme_index} has an empty name")
            elif len(theme.name) > 500:
                errors.append(f"Theme {theme_index} name is longer than 500 characters")
            if not theme.author.strip():
                warnings.append(f"Theme {theme_index} has no author")
            elif len(theme.author) > 300:
                errors.append(f"Theme {theme_index} author is longer than 300 characters")
            for question in theme.questions:
                if not question.text.strip() or not question.answer.strip():
                    errors.append(
                        f"Theme {theme_index}, question {question.value} has empty content"
                    )
                if not (question.author or theme.author).strip():
                    warnings.append(f"Theme {theme_index}, question {question.value} has no author")
                elif len(question.author or theme.author) > 300:
                    errors.append(
                        f"Theme {theme_index}, question {question.value} author is longer "
                        "than 300 characters"
                    )
                normalized = " ".join(question.text.casefold().split())
                if normalized in seen_questions:
                    warnings.append(
                        f"Duplicate question text in theme {theme_index} at {question.value}"
                    )
                seen_questions.add(normalized)
                if not question.form.strip():
                    warnings.append(f"Theme {theme_index}, question {question.value} has no form")
                if not question.source.strip():
                    warnings.append(f"Theme {theme_index}, question {question.value} has no source")
        return errors, warnings

    @staticmethod
    def _name_match_forms(name: str) -> tuple[str, ...]:
        """Casefolded single-space word orders for precise namesake matching."""
        words = name.casefold().split()
        if not words:
            return ()
        if len(words) > 5:
            return (" ".join(words),)
        return tuple(sorted(" ".join(form) for form in set(itertools.permutations(words))))

    @staticmethod
    async def _matching_author(session: AsyncSession, name: str) -> AuthorRecord | None:
        forms = PacketAdminService._name_match_forms(name)
        if not forms:
            return None
        collapsed = func.regexp_replace(
            func.lower(AuthorRecord.display_name), r"\s+", " ", "g"
        )
        return await session.scalar(
            select(AuthorRecord)
            .where(collapsed.in_(forms))
            .order_by(func.lower(AuthorRecord.display_name), AuthorRecord.id)
            .limit(1)
        )

    @staticmethod
    async def _author(
        session: AsyncSession, name: str, operation_authors: dict[str, AuthorRecord]
    ) -> AuthorRecord | None:
        normalized = " ".join(name.strip().split())
        if not normalized:
            return None
        author = operation_authors.get(normalized)
        if author is not None:
            return author
        # Namesakes are the same person by default: a name matches an existing
        # author exactly, ignoring only capitalisation and word order. An
        # explicit association is still required to keep two same-named people
        # apart.
        author = await PacketAdminService._matching_author(session, normalized)
        if author is None:
            author = AuthorRecord(display_name=normalized)
            session.add(author)
            await session.flush()
        operation_authors[normalized] = author
        return author

    @staticmethod
    def _packet_from_content(content: dict[str, object]) -> Packet:
        return packet_from_data(content)

    @staticmethod
    async def _require_upload_access(session, context, player_id: UUID) -> None:
        await TournamentService.require_modifiable(session, context.tournament_id)
        administrator = await session.get(PlatformAdministratorRecord, player_id)
        if administrator is not None and administrator.revoked_at is None:
            return
        manager = await session.get(TournamentManagerRecord, (context.tournament_id, player_id))
        if manager is not None and manager.revoked_at is None:
            return
        membership = await session.get(
            TournamentMembershipRecord, (context.tournament_id, player_id)
        )
        if not (
            context.policies.get("member_uploads")
            and membership is not None
            and membership.status == "active"
        ):
            raise PermissionError("Tournament packet upload permission is required")
