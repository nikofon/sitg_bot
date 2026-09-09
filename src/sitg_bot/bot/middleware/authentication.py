import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from aiogram import BaseMiddleware, Bot
from aiogram.dispatcher.event.bases import CancelHandler
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject, Update

from sitg_bot.application.adapters import AuthenticatedTelegramIdentity
from sitg_bot.application.telegram import (
    DuplicateTelegramUpdate,
    TelegramUpdateClaim,
    TelegramUpdateRejected,
)

LOGGER = logging.getLogger(__name__)


def update_sender_candidate(update: Update) -> int:
    if update.message is not None and update.message.from_user is not None:
        return update.message.from_user.id
    if update.callback_query is not None:
        return update.callback_query.from_user.id
    raise TelegramUpdateRejected("Telegram update has no supported user sender")


def private_update_user_id(update: Update, *, verified_transport: bool) -> int:
    if not verified_transport:
        raise TelegramUpdateRejected("Telegram update transport is not verified")
    if update.channel_post is not None or update.edited_channel_post is not None:
        raise TelegramUpdateRejected("Channel posts are not accepted")
    if update.message is not None:
        return _private_message_user_id(update.message)
    if update.callback_query is not None:
        sender = update.callback_query.from_user
        message = update.callback_query.message
        if sender.is_bot or message is None:
            raise TelegramUpdateRejected("Callback sender is not an authenticated user")
        chat = message.chat
        if chat.type != ChatType.PRIVATE or chat.id != sender.id:
            raise TelegramUpdateRejected("Callback chat and user identities do not match")
        return sender.id
    raise TelegramUpdateRejected("Telegram update type is not supported")


def _private_message_user_id(message: Message) -> int:
    sender = message.from_user
    if message.chat.type != ChatType.PRIVATE:
        raise TelegramUpdateRejected("Only private Telegram chats are supported")
    if sender is None or sender.is_bot or message.sender_chat is not None:
        raise TelegramUpdateRejected("Anonymous or non-user Telegram sender is rejected")
    if message.chat.id != sender.id:
        raise TelegramUpdateRejected("Telegram chat and user identities do not match")
    return sender.id


class TelegramDeduplicationMiddleware(BaseMiddleware):
    def __init__(self, auth_service: Any) -> None:
        self.auth_service = auth_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = event if isinstance(event, Update) else data.get("event_update")
        bot = data.get("bot")
        if not isinstance(update, Update) or not isinstance(bot, Bot):
            raise CancelHandler
        try:
            claim = await self.auth_service.claim(
                bot_id=bot.id,
                update_id=update.update_id,
                telegram_user_id=update_sender_candidate(update),
                correlation_id=data.get("correlation_id")
                if isinstance(data.get("correlation_id"), UUID)
                else None,
            )
        except (DuplicateTelegramUpdate, TelegramUpdateRejected) as error:
            LOGGER.info(
                "Rejected Telegram update bot=%s update=%s reason=%s",
                bot.id,
                update.update_id,
                type(error).__name__,
            )
            raise CancelHandler from error
        data["telegram_update_claim"] = claim
        try:
            result = await handler(event, data)
        except Exception as error:
            await self._complete(claim, succeeded=False, error_code=type(error).__name__)
            raise
        await self._complete(claim, succeeded=True)
        return result

    async def _complete(
        self,
        claim: TelegramUpdateClaim,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        await self.auth_service.complete(
            claim.receipt_id, succeeded=succeeded, error_code=error_code
        )


class PrivateTelegramIdentityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = event if isinstance(event, Update) else data.get("event_update")
        claim = data.get("telegram_update_claim")
        if not isinstance(update, Update) or not isinstance(claim, TelegramUpdateClaim):
            raise CancelHandler
        try:
            telegram_user_id = private_update_user_id(update, verified_transport=True)
            if telegram_user_id != claim.telegram_user_id:
                raise TelegramUpdateRejected("Claimed and validated Telegram users differ")
        except TelegramUpdateRejected as error:
            LOGGER.info(
                "Rejected Telegram sender correlation=%s reason=%s",
                claim.correlation_id,
                type(error).__name__,
            )
            raise CancelHandler from error
        data["telegram_identity"] = AuthenticatedTelegramIdentity(telegram_user_id)
        return await handler(event, data)
