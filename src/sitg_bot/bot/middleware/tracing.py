import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from sitg_bot.bot.log_context import (
    correlation_id_context,
    telegram_update_id_context,
)

LOGGER = logging.getLogger(__name__)


class CorrelationLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        correlation_id = uuid4()
        data["correlation_id"] = correlation_id
        update = event if isinstance(event, Update) else data.get("event_update")
        update_id = update.update_id if isinstance(update, Update) else None
        correlation_token = correlation_id_context.set(correlation_id)
        update_token = telegram_update_id_context.set(update_id)
        LOGGER.info("Telegram update started")
        try:
            return await handler(event, data)
        except Exception:
            LOGGER.exception("Unhandled Telegram update failure")
            raise
        finally:
            LOGGER.info("Telegram update finished")
            telegram_update_id_context.reset(update_token)
            correlation_id_context.reset(correlation_token)
