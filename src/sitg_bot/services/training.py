"""Durable solo execution, independent of competitive game accounting."""

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.game_rulesets import SIGameRuleset
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.training import TrainingSettings, best_buzz_cursor
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.telegram_game import FINISHED_GAME_CLOSURE_TIMEOUT
from sitg_bot.services.training_access import require_no_training, require_trainee
from sitg_bot.services.training_packets import (
    DisclosureRequired,
    TrainingPacketService,
    TrainingPacketSource,
)
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameParticipantRecord,
    GameRecord,
    PacketVersionRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
    PregameLobbyRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
    TrainingAttemptRecord,
    TrainingEventRecord,
    TrainingMemberRecord,
    TrainingQuestionRecord,
    TrainingSessionRecord,
)


class BestBuzzPreparation(Protocol):
    async def prepared(
        self,
        session: AsyncSession,
        player_id: UUID,
        source: TrainingPacketSource,
    ) -> dict[UUID, dict] | None: ...


class TrainingService:
    def __init__(self, database: Database, *, best_buzz: BestBuzzPreparation | None = None):
        self.database = database
        self.packets = TrainingPacketService(database)
        self.best_buzz = best_buzz

    async def lobby(
        self,
        player_id: UUID,
        command: str,
        *,
        expected_version: int | None = None,
        version_id: UUID | None = None,
        settings: dict | None = None,
    ) -> dict:
        async with self.database.transaction() as session:
            await require_trainee(session, player_id)
            player = await session.get(PlayerRecord, player_id, with_for_update=True)
            lobby = player.training_lobby
            previous_lobby = lobby
            if command == "create":
                await require_no_training(session, player_id)
                if lobby is None:
                    player.training_lobby_version += 1
                    lobby = {
                        "version": player.training_lobby_version,
                        "version_id": None,
                        "packet_name": None,
                        "settings": TrainingSettings().to_dict(),
                    }
            else:
                if lobby is None:
                    raise ValueError("Create a training lobby first")
                if expected_version != lobby["version"]:
                    raise StaleWriteError("Training lobby has changed")
                if command == "quit":
                    lobby = None
                elif command in {"save", "select_packet", "save_settings"}:
                    # Copy the JSON projection: packet autosave must not overwrite
                    # settings, and explicit settings saves must not change packets.
                    lobby = dict(lobby)
                    if command in {"save", "select_packet"}:
                        if version_id is None:
                            raise ValueError("Select a packet first")
                        packet, _ = await self.packets.authorize(session, player_id, version_id)
                        lobby.update(version_id=str(version_id), packet_name=packet.name)
                    if command in {"save", "save_settings"}:
                        lobby["settings"] = TrainingSettings.from_mapping(settings or {}).to_dict()
                    player.training_lobby_version += 1
                    lobby["version"] = player.training_lobby_version
                else:
                    raise ValueError("Unknown training lobby command")
            player.training_lobby = lobby
            if lobby is not previous_lobby:
                await self._lobby_notice(session, player)
            return {"lobby": lobby}

    @staticmethod
    async def _lobby_notice(session, player):
        await TransactionalOutbox.enqueue(
            session, topic="training.lobby.changed",
            deduplication_key=(f"training:lobby:{player.id}:{player.training_lobby_version}:"
                               f"{bool(player.training_lobby)}"),
            partition_key=f"training:lobby:{player.id}",
            aggregate_type="player", aggregate_id=player.id,
            payload={"owner_id": str(player.id)},
        )

    async def lobby_delivery(self, player_id: UUID, *, presentation=None) -> dict:
        async with self.database.transaction() as session:
            await require_trainee(session, player_id)
            player = await session.get(PlayerRecord, player_id, with_for_update=True)
            if presentation is not None:
                player.training_lobby_presentation = presentation
                return {"recorded": True}
            return {
                "lobby": player.training_lobby,
                "version": player.training_lobby_version,
                "presentation": player.training_lobby_presentation,
                "telegram_user_id": player.telegram_user_id,
                "locale": player.preferred_locale,
            }

    async def delivery(
        self, player_id: UUID, session_id: UUID, *, presentation=None,
        key=None, value=None, sequence=None,
    ) -> dict:
        """Trusted Telegram adapter projection with a persistent presentation cursor."""
        async with self.database.transaction() as session:
            record = await self._owned(session, player_id, session_id)
            player = await session.get(PlayerRecord, player_id)
            member = await session.get(TrainingMemberRecord, (session_id, player_id))
            if presentation is not None or key is not None or sequence is not None:
                saved = dict(member.presentation)
                if presentation is not None:
                    saved.update(presentation)
                if key is not None:
                    messages = dict(saved.get("messages", {}))
                    messages[key] = value
                    saved["messages"] = messages
                    if value.get("kind") == "question":
                        saved["question_revision_id"] = value["round_id"]
                if sequence is not None:
                    saved["sequence"] = max(saved.get("sequence", 0), sequence)
                member.presentation = saved
                # A send racing a stop/closure joins that session's cleanup only.
                if (saved.get("dismissed") and key is not None
                    and key != "_deleted" and value.get("ids")):
                    await TransactionalOutbox.enqueue(
                        session, topic="telegram.training.cleanup",
                        deduplication_key=f"training:{record.id}:late:{key}:{value['ids']}",
                        partition_key=f"training:{record.id}", aggregate_type="training_session",
                        aggregate_id=record.id, payload={
                            "session_id": str(record.id), "owner_id": str(player_id),
                            "message_ids": value.get("ids", []),
                        },
                    )
                return {"recorded": True}
            if record.status == "stopped":
                await self._dismiss(session, record)
            if record.status == "stopped" or member.presentation.get("dismissed"):
                result = {"session": None}
            else:
                await self.packets.authorize(session, player_id, record.packet_version_id)
                now = datetime.now(UTC)
                if record.event_sequence == 0:
                    await self._recover_presentation(session, record, member)
                question_data = await self._advance(session, record, now)
                result = await self._view(
                    session, record, now, question_data=question_data,
                    presentation=member.presentation,
                )
            show_menu = False
            if record.status in {"stopped", "completed"}:
                navigation = TelegramNavigationService(self.database)
                snapshot = await navigation._snapshot(
                    session, navigation.player_accounts._account_snapshot(player),
                )
                show_menu = snapshot.active_mode == "training" and snapshot.context == "menu"
            return {
                **result,
                "show_menu": show_menu,
                "status": record.status,
                "telegram_user_id": player.telegram_user_id,
                "locale": player.preferred_locale,
                "presentation": member.presentation,
                "events": await self._events(session, record, member.presentation),
            }

    async def start(
        self,
        player_id: UUID,
        version_id: UUID,
        *,
        settings: TrainingSettings | None = None,
        confirm: bool = False,
        now: datetime | None = None,
        lobby_version: int | None = None,
    ) -> dict:
        now = now or datetime.now(UTC)
        settings = settings or TrainingSettings()
        try:
            async with self.database.transaction() as session:
                await self.packets.lock_access(session, player_id, version_id)
                player = await session.get(PlayerRecord, player_id, with_for_update=True)
                if lobby_version is not None:
                    lobby = player.training_lobby
                    if lobby is None or lobby["version"] != lobby_version:
                        raise StaleWriteError("Training lobby has changed")
                    if lobby["version_id"] != str(version_id):
                        raise StaleWriteError("Training packet has changed")
                    settings = TrainingSettings.from_mapping(lobby["settings"])
                source = await self.packets.disclose(
                    session,
                    player_id,
                    version_id,
                    confirm=confirm,
                )
                await require_no_training(session, player_id)
                playing = await session.scalar(
                    select(GameRecord.id)
                    .join(
                        GameParticipantRecord,
                        GameParticipantRecord.game_id == GameRecord.id,
                    )
                    .where(
                        GameParticipantRecord.player_id == player_id,
                        GameParticipantRecord.active.is_(True),
                        GameRecord.status.in_(("lobby", "active")),
                    )
                    .limit(1)
                )
                if playing:
                    raise ValueError("Finish normal play before starting training")
                assembling = await session.scalar(
                    select(PregameLobbyMemberRecord.id)
                    .join(
                        PregameLobbyRecord,
                        PregameLobbyRecord.id == PregameLobbyMemberRecord.lobby_id,
                    )
                    .where(
                        PregameLobbyMemberRecord.player_id == player_id,
                        PregameLobbyMemberRecord.active.is_(True),
                        PregameLobbyRecord.status == "assembling",
                    )
                    .limit(1)
                )
                if assembling:
                    raise ValueError("Leave the normal-game lobby before starting training")
                analyses = None
                if settings.best_buzz:
                    analyses = (
                        await self.best_buzz.prepared(session, player_id, source)
                        if self.best_buzz
                        else None
                    )
                    if analyses is None or any(
                        q.revision_id not in analyses for q in source.questions
                    ):
                        raise BestBuzzUnavailable()
                    for question in source.questions:
                        best_buzz_cursor(
                            question.text,
                            revision_id=question.revision_id,
                            analysis=analyses[question.revision_id],
                            target_chars=settings.token_target_chars,
                        )
                record = TrainingSessionRecord(
                    owner_id=player_id,
                    packet_version_id=version_id,
                    disclosure_receipt=source.receipt,
                    settings=settings.to_dict(),
                )
                session.add(record)
                await session.flush()
                session.add(TrainingMemberRecord(session_id=record.id, player_id=player_id))
                for position, question in enumerate(source.questions):
                    session.add(
                        TrainingQuestionRecord(
                            session_id=record.id,
                            position=position,
                            question_revision_id=question.revision_id,
                            theme_revision_id=question.theme_revision_id,
                            value=question.value,
                            best_buzz=analyses[question.revision_id] if analyses else None,
                        )
                    )
                await session.flush()
                player.training_lobby = None
                await self._lobby_notice(session, player)
                record.phase = "intermission"
                record.progression_stage = "ready_countdown"
                record.deadline = now + GameSettings().ready_delta
                await self._event(session, record, "game_started", {})
                await self._notice(session, record)
                return await self._view(session, record, now)
        except DisclosureRequired as error:
            return {"confirmation_required": True, "fresh_unit_count": error.fresh_unit_count}
        except BestBuzzUnavailable:
            return {"preparation": "unavailable", "session": None}

    async def current(
        self, player_id: UUID, *, now: datetime | None = None, score_only: bool = False,
    ) -> dict:
        async with self.database.sessions() as session:
            await require_trainee(session, player_id)
            query = select(TrainingSessionRecord.id).where(
                TrainingSessionRecord.owner_id == player_id,
            )
            if not score_only:
                query = query.where(
                    TrainingSessionRecord.status.in_(("active", "paused", "completed")),
                )
            identifier = await session.scalar(query.order_by(
                TrainingSessionRecord.created_at.desc(), TrainingSessionRecord.id.desc(),
            ).limit(1))
            player = await session.get(PlayerRecord, player_id)
            lobby = player.training_lobby
        if identifier is None:
            return {"session": None, "lobby": lobby}
        await self.tick(identifier, now=now)
        async with self.database.sessions() as session:
            record = await session.get(TrainingSessionRecord, identifier)
            if not score_only and lobby is not None and record.status == "completed":
                return {"session": None, "lobby": lobby}
            if score_only:
                return {"session": {
                    "id": str(record.id), "status": record.status,
                    "score": await self._score(session, record.id),
                }}
            member = await session.get(TrainingMemberRecord, (record.id, player_id))
            if record.status == "stopped" or member.presentation.get("dismissed"):
                return {"session": None, "lobby": lobby}
        return await self.view(player_id, identifier, now=now)

    async def view(self, player_id: UUID, session_id: UUID, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            record = await self._owned(session, player_id, session_id)
            await self.packets.authorize(session, player_id, record.packet_version_id)
            question_data = await self._advance(session, record, now)
            return await self._view(session, record, now, question_data=question_data)

    async def act(
        self,
        player_id: UUID,
        session_id: UUID,
        command: str,
        *,
        expected_version: int,
        question_revision_id: UUID,
        answer: str | None = None,
        approve: bool | None = None,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            record = await self._owned(session, player_id, session_id)
            question_data = await self._question(session, record)
            # Catch reading up before binding the action. Presentation-only token
            # advances do not invalidate a buzz, but an expired answer does.
            if command != "stop":
                await self.packets.authorize(session, player_id, record.packet_version_id)
                question_data = (await self._advance(
                    session, record, now, question_data=question_data,
                )) or question_data
            question, revision, _ = question_data
            if record.version != expected_version or revision.id != question_revision_id:
                raise StaleWriteError("Training question has changed")
            # Stop remains available after source revocation or retirement.
            if command == "stop":
                if record.status == "completed":
                    await self._dismiss(session, record)
                    return {"session": None, "stopped": True}
                if record.status not in {"active", "paused"}:
                    raise ValueError("Training has already ended")
                attempt = (
                    await self._attempt(session, record) if record.phase == "revealed" else None
                )
                if attempt is not None and attempt.appeal_status == "voting":
                    attempt.appeal_status, attempt.appeal_resolved_at = "rejected", now
                record.status, record.ended_at = "stopped", now
                record.deadline = record.hidden_deadline = None
                record.version += 1
                await self._dismiss(session, record)
                await self._notice(session, record)
                return {"session": None, "stopped": True}
            else:
                if record.status not in {"active", "paused"}:
                    raise ValueError("Training has already ended")
                settings = TrainingSettings.from_mapping(record.settings)
                attempt = (
                    await self._attempt(session, record) if record.phase == "revealed" else None
                )
                voting = attempt is not None and attempt.appeal_status == "voting"
                if command == "appeal":
                    if (
                        attempt is None or not attempt.answer
                        or attempt.correct is None or attempt.appeal_status is not None
                    ):
                        raise ValueError("There is no answer to appeal")
                    attempt.appeal_status, attempt.appealed_at = "voting", now
                    attempt.appeal_was_paused = record.status == "paused"
                    if record.status == "active":
                        self._pause(record, now)
                    # The regular default ballot window; solo training has one voter.
                    record.deadline = now + timedelta(seconds=60)
                elif command == "vote" and voting:
                    if type(approve) is not bool:
                        raise ValueError("A yes or no vote is required")
                    attempt.appeal_status = "accepted" if approve else "rejected"
                    attempt.appeal_resolved_at = now
                    record.deadline = None
                    if approve:
                        attempt.correct = not attempt.correct
                    if not attempt.appeal_was_paused:
                        self._resume(record, question, now)
                    await self._event(session, record, "appeal_resolved", {
                        "approved": approve, "appeal_id": str(record.id),
                    })
                elif voting:
                    raise ValueError("Resolve the appeal before acting")
                elif command == "pause" and record.status == "active":
                    self._pause(record, now)
                    await self._event(session, record, "game_paused", {})
                elif command == "resume" and record.status == "paused":
                    self._resume(record, question, now)
                    await self._event(session, record, "game_resumed", {})
                elif record.status != "active":
                    raise ValueError("Resume training before acting")
                elif command == "buzz" and record.phase in {"reading", "waiting", "best_buzz"}:
                    question.buzzed_at = now
                    question.buzz_token_cursor = record.token_cursor
                    question.buzz_delay_seconds = (
                        max(0, (now - question.opened_at).total_seconds()
                            - float(question.paused_seconds or 0))
                        if question.opened_at is not None else None
                    )
                    record.phase = "answering"
                    record.deadline = now + timedelta(seconds=settings.answer_timeout)
                    await self._event(session, record, "player_buzzed", {
                        "mine": True, "form": revision.form,
                    })
                elif command == "answer" and record.phase == "answering":
                    if answer is None or not answer.strip() or len(answer) > 4000:
                        raise ValueError("An answer of 1–4000 characters is required")
                    correct = SIGameRuleset().judge_answer(
                        answer,
                        [revision.answer, *revision.accepted_answers],
                        rejected_answers=revision.rejected_answers,
                    )
                    await self._finish_question(
                        session, record, revision, "answered", answer, correct, now=now
                    )
                elif command in {"reveal", "skip"} and record.phase in {
                    "reading", "waiting", "best_buzz",
                }:
                    await self._finish_question(
                        session,
                        record,
                        revision,
                        "revealed" if command == "reveal" else "skipped", now=now,
                    )
                elif command == "continue" and record.phase == "best_buzz":
                    record.best_buzz_consumed = True
                    if record.token_cursor == len(settings.tokens(revision.text)):
                        record.phase = "waiting"
                        record.deadline = now + timedelta(seconds=settings.buzz_timeout)
                    else:
                        record.phase = "reading"
                        record.deadline = now + timedelta(seconds=settings.token_delay)
                    await self._reading_event(session, record, revision)
                elif command == "next" and record.phase == "revealed":
                    question_data = await self._progress(session, record, now)
                else:
                    raise ValueError("Training action is unavailable in this phase")
            record.version += 1
            await self._notice(session, record)
            return await self._view(session, record, now, question_data=question_data)

    @staticmethod
    def _pause(record, now):
        record.paused_phase, record.paused_at = record.phase, now
        record.paused_remaining = (
            max(0, (record.deadline - now).total_seconds()) if record.deadline else None
        )
        record.paused_hidden_remaining = (
            max(0, (record.hidden_deadline - now).total_seconds())
            if record.hidden_deadline else None
        )
        record.status, record.deadline, record.hidden_deadline = "paused", None, None

    @staticmethod
    def _resume(record, question, now):
        if record.paused_at is not None:
            question.paused_seconds = float(question.paused_seconds or 0) + max(
                0, (now - record.paused_at).total_seconds()
            )
        record.status = "active"
        if record.paused_remaining is not None:
            record.deadline = now + timedelta(seconds=float(record.paused_remaining))
        if record.paused_hidden_remaining is not None:
            record.hidden_deadline = now + timedelta(seconds=float(record.paused_hidden_remaining))
        record.paused_phase = record.paused_remaining = record.paused_at = None
        record.paused_hidden_remaining = None

    @staticmethod
    async def _attempt(session, record):
        return await session.scalar(select(TrainingAttemptRecord).where(
            TrainingAttemptRecord.session_id == record.id,
            TrainingAttemptRecord.question_index == record.question_index,
        ))

    async def tick(self, session_id: UUID, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            record = await session.get(TrainingSessionRecord, session_id, with_for_update=True)
            if record is None or record.status not in {"active", "paused"}:
                return
            try:
                await self.packets.authorize(session, record.owner_id, record.packet_version_id)
            except (LookupError, PermissionError):
                attempt = (
                    await self._attempt(session, record) if record.phase == "revealed" else None
                )
                if attempt is not None and attempt.appeal_status == "voting":
                    attempt.appeal_status, attempt.appeal_resolved_at = "rejected", now
                record.status, record.ended_at = "stopped", now
                record.deadline = record.hidden_deadline = None
                record.version += 1
                await self._dismiss(session, record)
                await self._notice(session, record)
                return
            await self._advance(session, record, now)

    @staticmethod
    async def _owned(session, player_id, session_id):
        await require_trainee(session, player_id)
        record = await session.get(TrainingSessionRecord, session_id, with_for_update=True)
        if record is None or record.owner_id != player_id:
            raise LookupError("Training session not found")
        return record

    @staticmethod
    async def _question(session, record):
        return (
            await session.execute(
                select(
                    TrainingQuestionRecord,
                    QuestionRevisionRecord,
                    ThemeRevisionRecord.name,
                )
                .join(
                    QuestionRevisionRecord,
                    QuestionRevisionRecord.id == TrainingQuestionRecord.question_revision_id,
                )
                .join(
                    ThemeRevisionRecord,
                    ThemeRevisionRecord.id == TrainingQuestionRecord.theme_revision_id,
                )
                .where(
                    TrainingQuestionRecord.session_id == record.id,
                    TrainingQuestionRecord.position == record.question_index,
                )
            )
        ).one()

    async def _begin_question(self, session, record, now):
        question_data = await self._question(session, record)
        question, revision, _ = question_data
        question.opened_at, question.paused_seconds = now, 0
        settings = TrainingSettings.from_mapping(record.settings)
        record.best_buzz_consumed = False
        record.text_hidden = False
        record.hidden_deadline = None
        if settings.hidden_after is not None:
            record.phase, record.token_cursor = "waiting", len(settings.tokens(revision.text))
            record.hidden_deadline = now + timedelta(seconds=settings.hidden_after)
            record.deadline = now + timedelta(seconds=settings.buzz_timeout)
        else:
            record.phase = "reading"
            record.token_cursor = min(1, len(settings.tokens(revision.text)))
            record.deadline = now + timedelta(seconds=settings.token_delay)
            if settings.best_buzz:
                boundary = best_buzz_cursor(
                    revision.text, revision_id=revision.id, analysis=question.best_buzz,
                    target_chars=settings.token_target_chars,
                )
                if boundary <= record.token_cursor:
                    record.token_cursor, record.phase, record.deadline = boundary, "best_buzz", None
            if (record.phase == "reading"
                and record.token_cursor == len(settings.tokens(revision.text))):
                record.phase = "waiting"
                record.deadline = now + timedelta(seconds=settings.buzz_timeout)
        await self._reading_event(session, record, revision)
        return question_data

    async def _advance(self, session, record, now, *, question_data=None):
        if record.status not in {"active", "paused"}:
            return
        changed = False
        if record.phase == "revealed" and record.deadline is not None and record.deadline <= now:
            attempt = await self._attempt(session, record)
            if attempt is not None and attempt.appeal_status == "voting":
                attempt.appeal_status, attempt.appeal_resolved_at = "rejected", now
                record.deadline = None
                if not attempt.appeal_was_paused:
                    question_data = question_data or await self._question(session, record)
                    self._resume(record, question_data[0], now)
                await self._event(session, record, "appeal_resolved", {
                    "approved": False, "appeal_id": str(record.id),
                })
            elif record.status == "active":
                question_data = await self._progress(session, record, now)
            record.version += 1
            await self._notice(session, record)
            return question_data
        if (
            record.status == "active"
            and not record.text_hidden
            and record.hidden_deadline is not None
            and record.hidden_deadline <= now
        ):
            record.text_hidden = True
            question_data = question_data or await self._question(session, record)
            await self._event(session, record, "training_text_hidden", {})
            changed = True
        if record.status == "paused":
            if changed:
                await self._notice(session, record)
            return question_data
        if record.deadline and record.deadline <= now:
            if record.phase == "intermission":
                question_data = await self._progress(session, record, now)
                record.version += 1
                await self._notice(session, record)
                return question_data
            question_data = question_data or await self._question(session, record)
            question, revision, _ = question_data
            settings = TrainingSettings.from_mapping(record.settings)
            if record.phase == "reading":
                tokens = settings.tokens(revision.text)
                # Regular SI advances one frame per persisted deadline. A delayed
                # worker must not consume the unseen question and buzz window.
                cursor = min(len(tokens), record.token_cursor + 1)
                boundary = (
                    best_buzz_cursor(
                        revision.text,
                        revision_id=revision.id,
                        analysis=question.best_buzz,
                        target_chars=settings.token_target_chars,
                    )
                    if settings.best_buzz and not record.best_buzz_consumed
                    else None
                )
                if boundary is not None and cursor >= boundary:
                    record.token_cursor, record.phase, record.deadline = boundary, "best_buzz", None
                else:
                    record.token_cursor = cursor
                    if cursor == len(tokens):
                        record.phase = "waiting"
                        record.deadline = now + timedelta(seconds=settings.buzz_timeout)
                    else:
                        record.deadline = now + timedelta(seconds=settings.token_delay)
                await self._reading_event(session, record, revision)
                changed = True
            if (
                record.deadline
                and record.deadline <= now
                and record.phase in {"waiting", "answering"}
            ):
                await self._finish_question(
                    session, record, revision, "timeout",
                    correct=False if record.phase == "answering" else None, now=now,
                )
                record.version += 1
                changed = True
        if changed:
            await self._notice(session, record)
        return question_data

    async def _finish_question(
        self, session, record, revision, outcome, answer=None, correct=None, *, now=None,
    ):
        now = now or datetime.now(UTC)
        if record.phase == "answering":
            await self._event(session, record, "answer_judged", {
                "mine": True, "answer": answer, "original_correct": correct,
                "timed_out": outcome == "timeout",
            })
        session.add(
            TrainingAttemptRecord(
                session_id=record.id,
                player_id=record.owner_id,
                question_revision_id=revision.id,
                question_index=record.question_index,
                answer=answer,
                correct=correct,
                original_correct=correct,
                outcome=outcome,
                token_cursor=record.token_cursor,
            )
        )
        record.phase = "revealed"
        record.progression_stage = "question_complete"
        next_theme = await session.scalar(select(TrainingQuestionRecord.theme_revision_id).where(
            TrainingQuestionRecord.session_id == record.id,
            TrainingQuestionRecord.position == record.question_index + 1,
        ))
        question, _, _ = await self._question(session, record)
        timing = GameSettings()
        record.deadline = now + (
            timing.between_questions_delta if next_theme == question.theme_revision_id
            else timing.last_question_to_theme_complete_delta
        )
        record.hidden_deadline, record.text_hidden = None, False
        await self._event(session, record, "question_completed", {
            "text": " ".join(TrainingSettings.from_mapping(record.settings).tokens(revision.text)),
            "answer": revision.answer, "commentary": revision.commentary,
            "author": await self._authors(session, revision),
        })

    @staticmethod
    async def _authors(session, revision):
        if not revision.author_ids:
            return "—"
        names = dict((await session.execute(select(AuthorRecord.id, AuthorRecord.display_name)
                     .where(AuthorRecord.id.in_(revision.author_ids)))).all())
        return ", ".join(names[value] for value in revision.author_ids)

    async def _event(self, session, record, kind, parameters):
        record.event_sequence = (record.event_sequence or 0) + 1
        question, revision, theme = await self._question(session, record)
        session.add(TrainingEventRecord(
            session_id=record.id, sequence=record.event_sequence, kind=kind,
            parameters={"round_id": str(revision.id), "theme": theme, **parameters},
        ))

    async def _reading_event(self, session, record, revision):
        settings = TrainingSettings.from_mapping(record.settings)
        tokens = settings.tokens(revision.text)
        await self._event(session, record, "question_token_revealed", {
            "text": " ".join(tokens[:record.token_cursor]),
            "revealed_token_count": record.token_cursor, "total_token_count": len(tokens),
        })

    async def _next_question(self, session, record, now):
        total = await session.scalar(select(func.count()).select_from(TrainingQuestionRecord)
                                     .where(TrainingQuestionRecord.session_id == record.id))
        if record.question_index + 1 >= total:
            record.status, record.ended_at, record.deadline = "completed", now, None
            record.phase, record.progression_stage = "revealed", None
            await self._event(session, record, "game_completed", {})
            return await self._question(session, record)
        record.question_index += 1
        record.phase, record.progression_stage = "intermission", "question_start"
        record.deadline = now
        return await self._progress(session, record, now)

    async def _progress(self, session, record, now):
        """SI presentation stages, using regular-play timing and event contracts.

        No competitive host is created: training retains its own pins/attempts.
        One lifecycle boundary per deadline keeps delayed workers from silently
        eating announcement/intermission time.
        """
        timing = GameSettings()
        question_data = await self._question(session, record)
        question, revision, theme = question_data
        stage = record.progression_stage
        if stage == "ready_countdown":
            rows = (await session.execute(select(ThemeRevisionRecord.id, ThemeRevisionRecord.name)
                    .join(TrainingQuestionRecord,
                          TrainingQuestionRecord.theme_revision_id == ThemeRevisionRecord.id)
                    .where(TrainingQuestionRecord.session_id == record.id)
                    .order_by(TrainingQuestionRecord.position))).all()
            themes = list({str(row.id): {"name": row.name} for row in rows}.values())
            await self._event(session, record, "themes_announced", {"themes": themes})
            record.progression_stage = "theme_start"
            record.deadline = now + timing.game_start_to_first_theme_delta
        elif stage == "theme_start":
            theme_record = await session.get(ThemeRevisionRecord, question.theme_revision_id)
            packet = await session.get(PacketVersionRecord, record.packet_version_id)
            position = await session.scalar(select(func.count(func.distinct(
                TrainingQuestionRecord.theme_revision_id))).where(
                    TrainingQuestionRecord.session_id == record.id,
                    TrainingQuestionRecord.position <= record.question_index,
                ))
            await self._event(session, record, "theme_started", {
                "position": position, "name": theme, "packet_name": packet.name,
                "author": await self._authors(session, theme_record),
            })
            if theme_record.commentary.strip():
                record.progression_stage = "theme_commentary"
                record.deadline = now + timing.theme_author_to_commentary_delta
            else:
                record.progression_stage = "question_start"
                record.deadline = now + timing.theme_to_first_question_delta
        elif stage == "theme_commentary":
            theme_record = await session.get(ThemeRevisionRecord, question.theme_revision_id)
            await self._event(session, record, "theme_commentary_announced", {
                "commentary": theme_record.commentary,
            })
            record.progression_stage = "question_start"
            record.deadline = now + timing.theme_commentary_to_question_delta
        elif stage == "question_start":
            await self._event(session, record, "question_cost_announced", {"value": question.value})
            record.progression_stage = "question_reveal_start"
            record.deadline = now + timing.question_cost_announcement_delta
        elif stage == "question_reveal_start":
            record.progression_stage = None
            question_data = await self._begin_question(session, record, now)
        elif stage in {"question_complete", None}:
            next_theme = await session.scalar(
                select(TrainingQuestionRecord.theme_revision_id).where(
                    TrainingQuestionRecord.session_id == record.id,
                    TrainingQuestionRecord.position == record.question_index + 1,
                ),
            )
            if next_theme == question.theme_revision_id:
                return await self._next_question(session, record, now)
            record.phase, record.progression_stage = "intermission", "theme_scoreboard"
            await self._event(session, record, "theme_completed", {"name": theme})
            record.deadline = now + timing.theme_complete_to_scoreboard_delta
        elif stage == "theme_scoreboard":
            player = await session.get(PlayerRecord, record.owner_id)
            await self._event(session, record, "scoreboard", {"players": [{
                "id": str(record.owner_id), "name": player.public_nickname,
                "score": await self._score(session, record.id),
            }]})
            record.progression_stage = "next_theme"
            record.deadline = now + timing.between_themes_delta
        elif stage == "next_theme":
            total = await session.scalar(select(func.count()).select_from(TrainingQuestionRecord)
                                         .where(TrainingQuestionRecord.session_id == record.id))
            if record.question_index + 1 >= total:
                return await self._next_question(session, record, now)
            record.question_index += 1
            record.progression_stage = "theme_start"
            return await self._progress(session, record, now)
        else:
            raise ValueError("Unknown training progression stage")
        return question_data

    async def _recover_presentation(self, session, record, member):
        """Upgrade an in-flight pre-event-ledger session without inventing play evidence."""
        question, revision, _ = await self._question(session, record)
        presentation = dict(member.presentation)
        messages = dict(presentation.get("messages", {}))
        for index, mid in enumerate(presentation.get("ids", [])):
            messages[f"question:{revision.id}:{index}"] = {
                "ids": [mid], "kind": "question", "round_id": str(revision.id),
            }
        presentation["messages"] = messages
        member.presentation = presentation
        await self._event(session, record, "question_cost_announced", {"value": question.value})
        if record.phase == "revealed":
            await self._event(session, record, "question_completed", {
                "text": " ".join(
                    TrainingSettings.from_mapping(record.settings).tokens(revision.text),
                ),
                "answer": revision.answer, "commentary": revision.commentary,
                "author": await self._authors(session, revision),
            })
        else:
            await self._reading_event(session, record, revision)
            if record.phase == "answering":
                await self._event(session, record, "player_buzzed", {
                    "mine": True, "form": revision.form,
                })

    @staticmethod
    async def _events(session, record, presentation):
        if record.status == "stopped" or presentation.get("dismissed"):
            return []
        rows = (await session.execute(select(TrainingEventRecord).where(
            TrainingEventRecord.session_id == record.id,
            TrainingEventRecord.sequence > presentation.get("sequence", 0),
        ).order_by(TrainingEventRecord.sequence).limit(100))).scalars()
        return [{"sequence": row.sequence, "kind": row.kind, "parameters": row.parameters}
                for row in rows]

    async def _dismiss(self, session, record):
        member = await session.get(TrainingMemberRecord, (record.id, record.owner_id))
        presentation = dict(member.presentation)
        if presentation.get("dismissed"):
            return
        presentation["dismissed"] = True
        member.presentation = presentation
        ids = {mid for entry in presentation.get("messages", {}).values()
               for mid in entry.get("ids", [])}
        ids.update(presentation.get("ids", []))  # pre-event-ledger sessions
        await TransactionalOutbox.enqueue(
            session, topic="telegram.training.cleanup",
            deduplication_key=f"training:{record.id}:cleanup",
            partition_key=f"training:{record.id}", aggregate_type="training_session",
            aggregate_id=record.id, payload={
                "session_id": str(record.id), "owner_id": str(record.owner_id),
                "message_ids": sorted(ids),
            },
        )

    async def close_finished_sessions(self, *, now=None):
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            rows = (await session.execute(select(TrainingSessionRecord)
                    .join(TrainingMemberRecord,
                          TrainingMemberRecord.session_id == TrainingSessionRecord.id)
                    .where(TrainingSessionRecord.status.in_(("completed", "stopped")),
                           TrainingSessionRecord.ended_at <= now - FINISHED_GAME_CLOSURE_TIMEOUT,
                           TrainingMemberRecord.presentation["dismissed"].as_boolean()
                           .is_distinct_from(True))
                    .order_by(TrainingSessionRecord.id).limit(100).with_for_update())).scalars()
            for record in rows:
                await self._dismiss(session, record)

    @staticmethod
    async def _score(session, session_id):
        return await session.scalar(
            select(func.coalesce(func.sum(case(
                (TrainingAttemptRecord.correct.is_(True), TrainingQuestionRecord.value),
                (TrainingAttemptRecord.correct.is_(False), -TrainingQuestionRecord.value),
                else_=0,
            )), 0))
            .select_from(TrainingAttemptRecord)
            .join(TrainingQuestionRecord, (
                (TrainingQuestionRecord.session_id == TrainingAttemptRecord.session_id)
                & (TrainingQuestionRecord.position == TrainingAttemptRecord.question_index)
            ))
            .where(TrainingAttemptRecord.session_id == session_id)
        )

    async def _view(self, session, record, now, *, question_data=None, presentation=None):
        question, revision, theme = question_data or await self._question(session, record)
        settings = TrainingSettings.from_mapping(record.settings)
        revealed = record.phase == "revealed"
        hidden = not revealed and (
            record.text_hidden
            or record.hidden_deadline is not None and record.hidden_deadline <= now
        )
        tokens = settings.tokens(revision.text)
        text = "" if hidden or record.phase == "intermission" else " ".join(
            tokens if revealed else tokens[: record.token_cursor]
        )
        if presentation is None:
            member = await session.get(TrainingMemberRecord, (record.id, record.owner_id))
            presentation = member.presentation
        score = await self._score(session, record.id)
        player = await session.get(PlayerRecord, record.owner_id)
        result = {
            "id": str(record.id),
            "owner_id": str(record.owner_id),
            "player_name": player.public_nickname,
            "score": score,
            "presented_question_revision_id": presentation.get("question_revision_id"),
            "answer_prompts": {
                str(mid): entry["round_id"]
                for entry in presentation.get("messages", {}).values()
                if entry.get("kind") == "answer"
                for mid in entry.get("ids", [])
            },
            "message_ids": [
                mid for entry in presentation.get("messages", {}).values()
                if entry.get("kind") == "question" and entry.get("round_id") == str(revision.id)
                for mid in entry.get("ids", [])
            ] or presentation.get("ids", []),
            "version": record.version,
            "status": record.status,
            "presentation_version": record.presentation_version,
            "phase": record.phase,
            "packet_version_id": str(record.packet_version_id),
            "question_revision_id": str(revision.id),
            "question_index": record.question_index,
            "theme": theme,
            "form": revision.form,
            "best_buzz_available": bool(settings.best_buzz and question.best_buzz),
            "value": question.value,
            "text": text,
            "hidden": hidden,
            "token_cursor": record.token_cursor,
            "fully_announced": revealed or record.token_cursor >= len(tokens),
            "deadline": record.deadline,
            "hidden_deadline": record.hidden_deadline,
            "settings": record.settings,
        }
        if revealed:
            attempt = await self._attempt(session, record)
            result.update(
                answer=revision.answer,
                commentary=revision.commentary,
                correct=attempt.correct if attempt else None,
                outcome=attempt.outcome if attempt else None,
                appeal_status=attempt.appeal_status if attempt else None,
                can_appeal=bool(attempt and attempt.answer and attempt.correct is not None
                                and attempt.appeal_status is None),
            )
        return {"confirmation_required": False, "session": result}

    @staticmethod
    async def _notice(session, record):
        # Content is always fetched through live authorization by the presentation consumer.
        record.presentation_version += 1
        await TransactionalOutbox.enqueue(
            session,
            topic="training.changed",
            deduplication_key=f"training:{record.id}:presentation:{record.presentation_version}",
            partition_key=f"training:{record.id}",
            aggregate_type="training_session",
            aggregate_id=record.id,
            payload={"session_id": str(record.id), "owner_id": str(record.owner_id)},
        )


class BestBuzzUnavailable(ValueError):
    pass
