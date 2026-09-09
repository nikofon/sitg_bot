"""Membership-scoped participant chat, using the existing transactional outbox."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.telegram_game import TelegramGameService
from sitg_bot.storage.models import (
    GameObserverRecord,
    GameParticipantRecord,
    GameRecord,
    OutboxEventRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
    PregameLobbyRecord,
    TelegramGameViewRecord,
)


class ParticipantChatService:
    def __init__(self, database):
        self.database = database

    async def _members(self, session, scope, scope_id):
        """Lock the same aggregate as join/leave and return current chat sessions."""
        model = PregameLobbyRecord if scope == "lobby" else GameRecord
        aggregate = await session.get(model, scope_id, with_for_update=True)
        if aggregate is None:
            raise LookupError("Chat not found")
        if scope == "lobby":
            if aggregate.status != "assembling" or aggregate.expires_at <= datetime.now(UTC):
                return []
            membership_models = [PregameLobbyMemberRecord]
        else:
            if aggregate.status not in {"lobby", "active", "completed", "finalized"}:
                return []
            membership_models = [GameParticipantRecord, GameObserverRecord]
        members = []
        for membership_model in membership_models:
            scope_column = (
                membership_model.lobby_id if scope == "lobby" else membership_model.game_id
            )
            rows = await session.execute(
                select(membership_model, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == membership_model.player_id)
                .where(scope_column == scope_id, PlayerRecord.status == "active")
            )
            for membership, player in rows:
                if player.telegram_user_id is None:
                    continue
                if isinstance(membership, GameParticipantRecord):
                    if not membership.active and (
                        (
                            membership.abandoned_at is not None
                            and (
                                membership.reconnected_at is None
                                or membership.abandoned_at > membership.reconnected_at
                            )
                        )
                        or aggregate.status not in {"completed", "finalized"}
                    ):
                        continue
                    if await PersistentGameService._has_later_game(session, membership):
                        continue
                    token = membership.reconnected_at or membership.created_at
                else:
                    if not membership.active:
                        continue
                    token = getattr(membership, "joined_at", membership.created_at)
                if scope == "game":
                    cursor = await session.get(TelegramGameViewRecord, (scope_id, player.id))
                    if cursor and cursor.dismissed_at:
                        continue
                members.append((player, str(token)))
        return members

    @staticmethod
    def _actor(members, telegram_user_id):
        actor = next((p for p in members if p[0].telegram_user_id == telegram_user_id), None)
        if actor is None:
            raise PermissionError("Active chat membership required")
        return actor

    async def members(self, telegram_user_id, operation):
        async with self.database.transaction() as session:
            members = await self._members(session, operation.scope, operation.scope_id)
            self._actor(members, telegram_user_id)
            return {
                "members": [
                    {"id": str(p.id), "name": p.public_nickname}
                    for p, _ in members
                    if p.telegram_user_id != telegram_user_id
                ]
            }

    async def send(self, telegram_user_id, operation):
        async with self.database.transaction() as session:
            members = await self._members(session, operation.scope, operation.scope_id)
            sender, sender_session = self._actor(members, telegram_user_id)
            target_name = operation.target
            if operation.media_group_id:
                previous = await session.scalar(
                    select(OutboxEventRecord.payload)
                    .where(
                        OutboxEventRecord.topic == "telegram.chat.message",
                        OutboxEventRecord.aggregate_id == operation.scope_id,
                        OutboxEventRecord.payload["source_chat_id"].as_string()
                        == str(telegram_user_id),
                        OutboxEventRecord.payload["media_group_id"].as_string()
                        == operation.media_group_id,
                        OutboxEventRecord.payload["whisper"].as_boolean().is_(True),
                    )
                    .limit(1)
                )
                if previous:
                    # Keep an accepted album private even if frontend state was lost.
                    tokens = {str(p.id): token for p, token in members}
                    if (
                        previous["sender_session"] != sender_session
                        or tokens.get(previous["recipient_id"]) != previous["recipient_session"]
                    ):
                        raise PermissionError("Album chat session has ended")
                    target_name = previous["recipient_id"]
            recipients = [(p, token) for p, token in members if p.id != sender.id]
            if target_name is not None:
                target = target_name.casefold()
                recipients = [
                    (p, token)
                    for p, token in recipients
                    if str(p.id) == target or (p.public_nickname or "").casefold() == target
                ]
                if len(recipients) != 1:
                    raise ValueError("Choose one current participant by unique nickname or ID")
            if operation.scope == "game":
                cursor = await TelegramGameService._cursor(session, operation.scope_id, sender.id)
                cursor.messages = {
                    **(cursor.messages or {}),
                    f"input:{operation.message_id}": {"ids": [operation.message_id]},
                }
            for recipient, recipient_session in recipients:
                await TransactionalOutbox.enqueue(
                    session,
                    topic="telegram.chat.message",
                    deduplication_key=(
                        f"chat:{operation.scope_id}:{telegram_user_id}:"
                        f"{operation.message_id}:{recipient.id}"
                    ),
                    partition_key=f"telegram:chat:{recipient.telegram_user_id}",
                    aggregate_type=operation.scope,
                    aggregate_id=operation.scope_id,
                    payload={
                        "scope": operation.scope,
                        "scope_id": str(operation.scope_id),
                        "sender_id": str(sender.id),
                        "sender_session": sender_session,
                        "sender_name": sender.public_nickname,
                        "source_chat_id": telegram_user_id,
                        "source_message_id": operation.message_id,
                        "recipient_id": str(recipient.id),
                        "recipient_session": recipient_session,
                        "recipient_telegram_user_id": recipient.telegram_user_id,
                        "locale": recipient.preferred_locale,
                        "whisper": target_name is not None,
                        "text": operation.text,
                        "caption": operation.caption,
                        "media_group_id": operation.media_group_id,
                    },
                )
            return {"recipients": len(recipients)}

    async def delivery(self, payload, *, key=None, message_id=None):
        """Revalidate queued traffic, and atomically account for sends racing with exit."""
        scope, scope_id = payload["scope"], UUID(payload["scope_id"])
        recipient_id = UUID(payload["recipient_id"])
        async with self.database.transaction() as session:
            try:
                members = await self._members(session, scope, scope_id)
            except LookupError:
                members = []
            tokens = {str(p.id): token for p, token in members}
            valid = (
                tokens.get(payload["sender_id"]) == payload["sender_session"]
                and tokens.get(payload["recipient_id"]) == payload["recipient_session"]
            )
            messages = {}
            if scope == "game":
                cursor = await TelegramGameService._cursor(session, scope_id, recipient_id)
                messages = cursor.messages or {}
                if message_id is not None:
                    if valid:
                        cursor.messages = {**messages, key: {"ids": [message_id]}}
                    else:
                        # Exit may have snapshotted cleanup before Telegram returned the ID.
                        await TransactionalOutbox.enqueue(
                            session,
                            topic="telegram.game.cleanup",
                            deduplication_key=f"chat:late-cleanup:{uuid4()}",
                            partition_key=f"telegram:chat:{payload['recipient_telegram_user_id']}",
                            aggregate_type="game",
                            aggregate_id=scope_id,
                            payload={
                                "game_id": str(scope_id),
                                "recipient_telegram_user_id": payload["recipient_telegram_user_id"],
                                "message_ids": [message_id],
                            },
                        )
            return {"skip": not valid, "messages": messages}
