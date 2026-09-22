"""Classic tournament match chats: listing, history replay, relay, shared game times."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.classic import ClassicService, is_chair
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ClassicChatMemberRecord,
    ClassicChatMessageRecord,
    ClassicChatRecord,
    ClassicMatchRecord,
    ClassicRoundRecord,
    ClassicStageRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
    TournamentMembershipRecord,
    TournamentRecord,
    TournamentTypeVersionRecord,
)

CHAT_HISTORY_LIMIT = 50
REMINDER_OFFSET_HOURS = (24, 1)


def seat_player_ids(match: ClassicMatchRecord) -> list[UUID]:
    """Prescribed seats store platform player IDs; Chair seats are not chat members."""
    ids: list[UUID] = []
    for seat in match.seats or ():
        if isinstance(seat, str) and not is_chair(seat):
            try:
                ids.append(UUID(seat))
            except ValueError:
                continue
    return ids


class TournamentChatService:
    """Transactional chat lifecycle for unplayed prescribed Classic matches."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    async def _context(
        session: AsyncSession, chat_id: UUID
    ) -> tuple[
        ClassicChatRecord,
        ClassicMatchRecord,
        ClassicRoundRecord,
        ClassicStageRecord,
        TournamentRecord,
    ]:
        chat = await session.get(ClassicChatRecord, chat_id)
        if chat is None:
            raise LookupError("Chat not found")
        match = await session.get(ClassicMatchRecord, chat.match_id)
        if match is None:
            raise LookupError("Chat not found")
        round_record = await session.get(ClassicRoundRecord, match.round_id)
        stage = await session.get(ClassicStageRecord, round_record.stage_id)
        tournament = await session.get(TournamentRecord, stage.tournament_id)
        return chat, match, round_record, stage, tournament

    @staticmethod
    def _chat_available(match, round_record, stage) -> bool:
        return (
            match.game_id is None
            and match.results is None
            and bool(match.seats)
            and round_record.assignment_id is not None
            and stage.started_at is not None
            and stage.completed_at is None
        )


    @staticmethod
    async def _player(session: AsyncSession, telegram_user_id: int) -> PlayerRecord:
        player = await session.scalar(
            select(PlayerRecord)
            .where(PlayerRecord.telegram_user_id == telegram_user_id)
            .with_for_update()
        )
        if player is None or player.status != "active":
            raise PermissionError("Completed player registration is required")
        return player

    @staticmethod
    async def _player_read(session: AsyncSession, telegram_user_id: int) -> PlayerRecord:
        player = await session.scalar(
            select(PlayerRecord).where(PlayerRecord.telegram_user_id == telegram_user_id)
        )
        if player is None or player.status != "active":
            raise PermissionError("Completed player registration is required")
        return player

    @staticmethod
    async def _require_membership(
        session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> None:
        membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
        if membership is None or membership.status != "active":
            raise PermissionError("Active tournament membership is required")

    @staticmethod
    async def _match_counts(session: AsyncSession, tournament_id: UUID) -> dict[UUID, int]:
        rows = (
            await session.execute(
                select(ClassicMatchRecord.round_id, func.count(ClassicMatchRecord.id))
                .join(ClassicRoundRecord, ClassicRoundRecord.id == ClassicMatchRecord.round_id)
                .join(ClassicStageRecord, ClassicStageRecord.id == ClassicRoundRecord.stage_id)
                .where(ClassicStageRecord.tournament_id == tournament_id)
                .group_by(ClassicMatchRecord.round_id)
            )
        ).all()
        return {round_id: count for round_id, count in rows}

    @staticmethod
    async def _max_sequence(session: AsyncSession, chat_id: UUID) -> int:
        return (
            await session.scalar(
                select(func.max(ClassicChatMessageRecord.sequence)).where(
                    ClassicChatMessageRecord.chat_id == chat_id
                )
            )
            or 0
        )

    @staticmethod
    def _label(
        round_record: ClassicRoundRecord,
        match: ClassicMatchRecord,
        tournament: TournamentRecord,
        counts: dict[UUID, int],
    ) -> dict[str, object]:
        return {
            "round_number": round_record.number,
            "match_number": match.number,
            "multiple_matches": counts.get(round_record.id, 1) > 1,
            "tournament_name": tournament.name,
        }

    async def _ensure_chat(
        self, session: AsyncSession, match: ClassicMatchRecord, stage: ClassicStageRecord
    ) -> ClassicChatRecord:
        chat = await session.scalar(
            select(ClassicChatRecord).where(ClassicChatRecord.match_id == match.id)
        )
        if chat is None:
            chat = ClassicChatRecord(match_id=match.id, tournament_id=stage.tournament_id)
            session.add(chat)
            await session.flush()
            for player_id in seat_player_ids(match):
                if await session.get(ClassicChatMemberRecord, (chat.id, player_id)) is None:
                    session.add(ClassicChatMemberRecord(chat_id=chat.id, player_id=player_id))
            await session.flush()
        return chat


    async def _projection(
        self,
        session: AsyncSession,
        chat: ClassicChatRecord,
        match: ClassicMatchRecord,
        round_record: ClassicRoundRecord,
        tournament: TournamentRecord,
        *,
        counts: dict[UUID, int],
        member: ClassicChatMemberRecord | None,
        max_sequence: int,
    ) -> dict[str, object]:
        participants = []
        for player_id in seat_player_ids(match):
            player = await session.get(PlayerRecord, player_id)
            if player is not None:
                participants.append(
                    {"player_id": str(player_id), "nickname": player.public_nickname}
                )
        return {
            "chat_id": str(chat.id),
            "tournament_id": str(tournament.id),
            "tournament_name": tournament.name,
            "round_number": round_record.number,
            "match_number": match.number,
            "multiple_matches": counts.get(round_record.id, 1) > 1,
            "planned_at": chat.planned_at.isoformat() if chat.planned_at else None,
            "planned_by_id": str(chat.planned_by_id) if chat.planned_by_id else None,
            "participants": participants,
            "unread": (member.last_read_sequence if member is not None else 0) < max_sequence,
        }

    async def _enqueue_history(
        self,
        session: AsyncSession,
        chat: ClassicChatRecord,
        message: ClassicChatMessageRecord,
        player: PlayerRecord,
        round_record: ClassicRoundRecord,
        match: ClassicMatchRecord,
        tournament: TournamentRecord,
        counts: dict[UUID, int],
    ) -> None:
        sender = (
            await session.get(PlayerRecord, message.sender_id)
            if message.sender_id is not None
            else None
        )
        await TransactionalOutbox.enqueue(
            session,
            topic="telegram.chat.message",
            deduplication_key=f"chat-history:{chat.id}:{message.id}:{player.id}",
            partition_key=f"telegram:chat:{player.telegram_user_id}",
            aggregate_type="tournament_chat",
            aggregate_id=chat.id,
            payload={
                "scope": "tournament_chat",
                "scope_id": str(chat.id),
                "message_row": str(message.id),
                "sender_id": str(message.sender_id) if message.sender_id else None,
                "sender_name": sender.public_nickname if sender else None,
                "source_chat_id": message.source_chat_id,
                "source_message_id": message.source_message_id,
                "recipient_id": str(player.id),
                "recipient_telegram_user_id": player.telegram_user_id,
                "locale": player.preferred_locale,
                "whisper": False,
                "text": message.text,
                "caption": message.caption,
                "media_group_id": None,
                "system": message.system,
                **self._label(round_record, match, tournament, counts),
            },
        )

    async def list_rooms(self, telegram_user_id: int, operation) -> dict[str, object]:
        async with self.database.transaction() as session:
            player = await self._player(session, telegram_user_id)
            tournament = await session.get(TournamentRecord, operation.tournament_id)
            if tournament is None:
                raise LookupError("Tournament not found")
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            if type_version is None or type_version.key != "classic":
                raise ValueError("Chats are available only for Classic tournaments")
            await self._require_membership(session, tournament.id, player.id)
            rows = (
                await session.execute(
                    select(ClassicMatchRecord, ClassicRoundRecord, ClassicStageRecord)
                    .join(ClassicRoundRecord, ClassicRoundRecord.id == ClassicMatchRecord.round_id)
                    .join(ClassicStageRecord, ClassicStageRecord.id == ClassicRoundRecord.stage_id)
                    .where(ClassicStageRecord.tournament_id == tournament.id)
                    .order_by(
                        ClassicStageRecord.kind,
                        ClassicRoundRecord.number,
                        ClassicMatchRecord.group_number,
                        ClassicMatchRecord.number,
                    )
                )
            ).all()
            counts = await self._match_counts(session, tournament.id)
            rooms = []
            for match, round_record, stage in rows:
                if player.id not in seat_player_ids(match):
                    continue
                if not self._chat_available(match, round_record, stage):
                    continue
                if not await ClassicService.round_access(
                    session, stage, round_record, "playable"
                ):
                    continue
                chat = await self._ensure_chat(session, match, stage)
                member = await session.get(ClassicChatMemberRecord, (chat.id, player.id))
                rooms.append(
                    await self._projection(
                        session,
                        chat,
                        match,
                        round_record,
                        tournament,
                        counts=counts,
                        member=member,
                        max_sequence=await self._max_sequence(session, chat.id),
                    )
                )
            return {"rooms": rooms}

    async def open(self, telegram_user_id: int, operation) -> dict[str, object]:
        async with self.database.transaction() as session:
            player = await self._player(session, telegram_user_id)
            chat, match, round_record, stage, tournament = await self._context(
                session, operation.chat_id
            )
            if player.id not in seat_player_ids(match) or not self._chat_available(
                match, round_record, stage
            ):
                raise PermissionError("Chat is not available")
            await self._require_membership(session, tournament.id, player.id)
            chat = await self._ensure_chat(session, match, stage)
            navigation = await session.scalar(
                select(PlayerTelegramNavigationRecord)
                .where(PlayerTelegramNavigationRecord.player_id == player.id)
                .with_for_update()
            )
            if navigation is None:
                navigation = PlayerTelegramNavigationRecord(player_id=player.id, version=1)
                session.add(navigation)
                await session.flush()
            navigation.interaction_mode = "player"
            navigation.player_context = "chat"
            navigation.active_chat_id = chat.id
            navigation.version += 1
            await session.flush()

            member = await session.get(
                ClassicChatMemberRecord, (chat.id, player.id), with_for_update=True
            )
            max_sequence = await self._max_sequence(session, chat.id)
            if member is not None:
                member.last_read_sequence = max_sequence
                member.notified = False
            history = list(
                await session.scalars(
                    select(ClassicChatMessageRecord)
                    .where(ClassicChatMessageRecord.chat_id == chat.id)
                    .order_by(ClassicChatMessageRecord.sequence.desc())
                    .limit(CHAT_HISTORY_LIMIT)
                )
            )[::-1]
            counts = await self._match_counts(session, tournament.id)
            if player.telegram_user_id is not None:
                for message in history:
                    await self._enqueue_history(
                        session, chat, message, player, round_record, match, tournament, counts
                    )
            projection = await self._projection(
                session,
                chat,
                match,
                round_record,
                tournament,
                counts=counts,
                member=member,
                max_sequence=max_sequence,
            )
            return {"chat": projection, "navigation_version": navigation.version}

    async def members(self, telegram_user_id: int, operation) -> dict[str, object]:
        async with self.database.sessions() as session:
            player = await self._player_read(session, telegram_user_id)
            chat, match, _, _, _ = await self._context(session, operation.scope_id)
            if player.id not in seat_player_ids(match):
                raise PermissionError("Active chat membership required")
            others = []
            for player_id in seat_player_ids(match):
                if player_id == player.id:
                    continue
                other = await session.get(PlayerRecord, player_id)
                if other is not None and other.status == "active":
                    others.append({"id": str(other.id), "name": other.public_nickname})
            return {"members": others}

    async def send(self, telegram_user_id: int, operation) -> dict[str, object]:
        async with self.database.transaction() as session:
            player = await self._player(session, telegram_user_id)
            chat, match, round_record, stage, tournament = await self._context(
                session, operation.scope_id
            )
            if player.id not in seat_player_ids(match) or not self._chat_available(
                match, round_record, stage
            ):
                raise PermissionError("Chat is not available")
            await self._require_membership(session, tournament.id, player.id)
            chat = await self._ensure_chat(session, match, stage)
            recipients = []
            for player_id in seat_player_ids(match):
                if player_id == player.id:
                    continue
                recipient = await session.get(PlayerRecord, player_id)
                if (
                    recipient is not None
                    and recipient.status == "active"
                    and recipient.telegram_user_id is not None
                ):
                    recipients.append(recipient)
            target = operation.target
            if target is not None:
                wanted = target.casefold()
                recipients = [
                    recipient
                    for recipient in recipients
                    if str(recipient.id) == wanted
                    or (recipient.public_nickname or "").casefold() == wanted
                ]
                if len(recipients) != 1:
                    raise ValueError(
                        "Choose one current participant by unique nickname or ID"
                    )
            message = ClassicChatMessageRecord(
                chat_id=chat.id,
                sender_id=player.id,
                kind="text" if operation.text is not None else "media",
                text=operation.text,
                caption=operation.caption,
                source_chat_id=telegram_user_id,
                source_message_id=operation.message_id,
            )
            session.add(message)
            await session.flush()
            counts = await self._match_counts(session, tournament.id)
            label = self._label(round_record, match, tournament, counts)
            for recipient in recipients:
                await TransactionalOutbox.enqueue(
                    session,
                    topic="telegram.chat.message",
                    deduplication_key=(
                        f"chat:{chat.id}:{telegram_user_id}:"
                        f"{operation.message_id}:{recipient.id}"
                    ),
                    partition_key=f"telegram:chat:{recipient.telegram_user_id}",
                    aggregate_type="tournament_chat",
                    aggregate_id=chat.id,
                    payload={
                        "scope": "tournament_chat",
                        "scope_id": str(chat.id),
                        "message_row": str(message.id),
                        "sender_id": str(player.id),
                        "sender_name": player.public_nickname,
                        "source_chat_id": telegram_user_id,
                        "source_message_id": operation.message_id,
                        "recipient_id": str(recipient.id),
                        "recipient_telegram_user_id": recipient.telegram_user_id,
                        "locale": recipient.preferred_locale,
                        "whisper": target is not None,
                        "text": operation.text,
                        "caption": operation.caption,
                        "media_group_id": operation.media_group_id,
                        **label,
                    },
                )
            return {"recipients": len(recipients)}

    async def info(self, telegram_user_id: int, operation) -> dict[str, object]:
        async with self.database.sessions() as session:
            player = await self._player_read(session, telegram_user_id)
            chat, match, round_record, stage, tournament = await self._context(
                session, operation.chat_id
            )
            if player.id not in seat_player_ids(match) or not self._chat_available(
                match, round_record, stage
            ):
                raise PermissionError("Chat is not available")
            counts = await self._match_counts(session, tournament.id)
            member = await session.get(ClassicChatMemberRecord, (chat.id, player.id))
            projection = await self._projection(
                session,
                chat,
                match,
                round_record,
                tournament,
                counts=counts,
                member=member,
                max_sequence=await self._max_sequence(session, chat.id),
            )
            return {**projection, "kind": "tournament_chat", "state": "ready"}

    async def set_game_time(self, telegram_user_id: int, operation) -> dict[str, object]:
        planned = operation.planned_at
        if planned is not None:
            if planned.tzinfo is None:
                raise ValueError("A timezone-aware date and time is required")
            planned = planned.astimezone(UTC)
        async with self.database.transaction() as session:
            player = await self._player(session, telegram_user_id)
            chat, match, round_record, stage, tournament = await self._context(
                session, operation.chat_id
            )
            if player.id not in seat_player_ids(match) or not self._chat_available(
                match, round_record, stage
            ):
                raise PermissionError("Chat is not available")
            await self._require_membership(session, tournament.id, player.id)
            chat = await self._ensure_chat(session, match, stage)
            chat.planned_at = planned
            chat.planned_by_id = player.id if planned is not None else None
            message = ClassicChatMessageRecord(
                chat_id=chat.id,
                sender_id=player.id,
                kind="system",
                system={
                    "event": "game_time_set" if planned is not None else "game_time_cleared",
                    "planned_at": planned.isoformat() if planned is not None else None,
                    "actor_name": player.public_nickname,
                },
            )
            session.add(message)
            await session.flush()
            counts = await self._match_counts(session, tournament.id)
            label = self._label(round_record, match, tournament, counts)
            for player_id in seat_player_ids(match):
                recipient = await session.get(PlayerRecord, player_id)
                if (
                    recipient is None
                    or recipient.status != "active"
                    or recipient.telegram_user_id is None
                ):
                    continue
                await TransactionalOutbox.enqueue(
                    session,
                    topic="telegram.chat.message",
                    deduplication_key=f"chat:{chat.id}:system:{message.id}:{recipient.id}",
                    partition_key=f"telegram:chat:{recipient.telegram_user_id}",
                    aggregate_type="tournament_chat",
                    aggregate_id=chat.id,
                    payload={
                        "scope": "tournament_chat",
                        "scope_id": str(chat.id),
                        "message_row": str(message.id),
                        "sender_id": str(player.id),
                        "sender_name": player.public_nickname,
                        "source_chat_id": None,
                        "source_message_id": None,
                        "recipient_id": str(recipient.id),
                        "recipient_telegram_user_id": recipient.telegram_user_id,
                        "locale": recipient.preferred_locale,
                        "whisper": False,
                        "text": None,
                        "caption": None,
                        "media_group_id": None,
                        "system": message.system,
                        **label,
                    },
                )
            projection = await self._projection(
                session,
                chat,
                match,
                round_record,
                tournament,
                counts=counts,
                member=None,
                max_sequence=await self._max_sequence(session, chat.id),
            )
            return {**projection, "kind": "tournament_chat", "state": "ready"}

    async def quit(self, telegram_user_id: int) -> dict[str, object]:
        async with self.database.transaction() as session:
            player = await self._player(session, telegram_user_id)
            navigation = await session.scalar(
                select(PlayerTelegramNavigationRecord)
                .where(PlayerTelegramNavigationRecord.player_id == player.id)
                .with_for_update()
            )
            if (
                navigation is None
                or navigation.player_context != "chat"
                or navigation.active_chat_id is None
            ):
                raise PermissionError("No tournament chat is open")
            chat_id = navigation.active_chat_id
            member = await session.get(
                ClassicChatMemberRecord, (chat_id, player.id), with_for_update=True
            )
            if member is not None:
                message_ids = [
                    message_id
                    for value in (member.message_ids or {}).values()
                    for message_id in value
                ]
                if message_ids and player.telegram_user_id is not None:
                    await TransactionalOutbox.enqueue(
                        session,
                        topic="telegram.chat.cleanup",
                        deduplication_key=f"chat:{chat_id}:cleanup:{player.id}:{uuid4()}",
                        partition_key=f"telegram:chat:{player.telegram_user_id}",
                        aggregate_type="tournament_chat",
                        aggregate_id=chat_id,
                        payload={
                            "recipient_telegram_user_id": player.telegram_user_id,
                            "message_ids": message_ids,
                        },
                    )
                member.message_ids = {}
                member.notified = False
            navigation.player_context = "tournament"
            navigation.active_chat_id = None

    async def delivery(self, payload, *, key=None, message_id=None) -> dict[str, object]:
        """Revalidate membership and choose relay versus closed-chat notification."""
        chat_id = UUID(payload["scope_id"])
        recipient_id = UUID(payload["recipient_id"])
        async with self.database.transaction() as session:
            chat = None
            match = None
            round_record = None
            stage = None
            try:
                chat, match, round_record, stage, _ = await self._context(session, chat_id)
            except LookupError:
                chat, match, round_record, stage = None, None, None, None
            seat_ids = seat_player_ids(match) if match is not None else []
            sender_raw = payload.get("sender_id")
            sender_id = UUID(sender_raw) if sender_raw else None
            available = bool(
                chat is not None
                and match is not None
                and round_record is not None
                and stage is not None
                and self._chat_available(match, round_record, stage)
            )
            member = None
            if recipient_id in seat_ids:
                member = await session.get(
                    ClassicChatMemberRecord, (chat_id, recipient_id), with_for_update=True
                )
            valid = available and member is not None and (
                sender_id is None or sender_id in seat_ids
            )
            if not valid:
                if message_id is not None:
                    await TransactionalOutbox.enqueue(
                        session,
                        topic="telegram.chat.cleanup",
                        deduplication_key=f"chat:late-cleanup:{uuid4()}",
                        partition_key=f"telegram:chat:{payload['recipient_telegram_user_id']}",
                        aggregate_type="tournament_chat",
                        aggregate_id=chat_id,
                        payload={
                            "recipient_telegram_user_id": payload[
                                "recipient_telegram_user_id"
                            ],
                            "message_ids": [message_id],
                        },
                    )
                return {"skip": True, "messages": {}}
            ledger = dict(member.message_ids or {})
            system = bool(payload.get("system"))
            navigation = await session.get(PlayerTelegramNavigationRecord, recipient_id)
            chat_open = bool(
                navigation is not None
                and navigation.interaction_mode == "player"
                and navigation.player_context == "chat"
                and navigation.active_chat_id == chat_id
            )
            if message_id is not None:
                if key is not None:
                    ledger[key] = [message_id]
                member.message_ids = ledger
                if not (system or chat_open):
                    member.notified = True
                await session.flush()
                return {"skip": False, "messages": ledger}
            if system or chat_open:
                return {"skip": False, "messages": ledger}
            if member.notified:
                return {"skip": True, "messages": ledger}
            return {
                "skip": False,
                "notify": True,
                "messages": ledger,
                "round_number": payload.get("round_number"),
                "match_number": payload.get("match_number"),
                "multiple_matches": payload.get("multiple_matches"),
                "tournament_name": payload.get("tournament_name"),
            }

    async def send_reminders(self, *, now: datetime | None = None) -> int:
        """Create player notifications for chats whose planned game time is near."""
        now = now or datetime.now(UTC)
        created = 0
        async with self.database.transaction() as session:
            chats = list(
                await session.scalars(
                    select(ClassicChatRecord).where(
                        ClassicChatRecord.planned_at.is_not(None),
                        ClassicChatRecord.planned_at > now,
                    )
                )
            )
            for chat in chats:
                for offset_hours in REMINDER_OFFSET_HOURS:
                    if now < chat.planned_at - timedelta(hours=offset_hours):
                        continue
                    created += await self._remind_chat(
                        session, chat, offset_hours=offset_hours, now=now
                    )
        return created

    async def _remind_chat(
        self,
        session: AsyncSession,
        chat: ClassicChatRecord,
        *,
        offset_hours: int,
        now: datetime,
    ) -> int:
        from sitg_bot.services.notifications import NotificationWriter

        match = await session.get(ClassicMatchRecord, chat.match_id)
        if match is None:
            return 0
        round_record = await session.get(ClassicRoundRecord, match.round_id)
        stage = await session.get(ClassicStageRecord, round_record.stage_id)
        tournament = await session.get(TournamentRecord, stage.tournament_id)
        if not self._chat_available(match, round_record, stage):
            return 0
        counts = await self._match_counts(session, tournament.id)
        label = self._label(round_record, match, tournament, counts)
        created = 0
        for player_id in seat_player_ids(match):
            key = f"chat-reminder:{chat.id}:{chat.planned_at.isoformat()}:{offset_hours}"
            existing = await session.scalar(
                select(PlayerNotificationRecord.id).where(
                    PlayerNotificationRecord.deduplication_key == key
                )
            )
            if existing is not None:
                continue
            await NotificationWriter.create_for_player(
                session,
                recipient_player_id=player_id,
                audience="player",
                kind="tournament_chat.game_reminder",
                deduplication_key=key,
                payload={
                    **label,
                    "planned_at": chat.planned_at.isoformat(),
                    "offset_hours": offset_hours,
                },
            )
            created += 1
        return created
