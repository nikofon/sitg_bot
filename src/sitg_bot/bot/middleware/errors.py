import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.state import GatewayCallError

LOGGER = logging.getLogger(__name__)


class ErrorMappingMiddleware(BaseMiddleware):
    def __init__(self, localization: LocalizationService) -> None:
        self.localization = localization

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            context_error = data.get("context_error")
            if isinstance(context_error, GatewayCallError):
                raise context_error
            return await handler(event, data)
        except GatewayCallError as error:
            locale = self.localization.locale(data.get("locale"))
            text = self.localization.text(error.error.message_key, locale, **error.error.details)
            update = event if isinstance(event, Update) else data.get("event_update")
            log = LOGGER.error if error.error.code.value == "internal_error" else LOGGER.warning
            log(
                "Mapped gateway error correlation=%s code=%s",
                data.get("correlation_id"),
                error.error.code,
            )
            if isinstance(update, Update) and update.message is not None:
                await update.message.answer(text)
            elif isinstance(update, Update) and update.callback_query is not None:
                callback: CallbackQuery = update.callback_query
                await callback.answer(text, show_alert=True)
                if isinstance(callback.message, Message):
                    await callback.message.answer(text)
            return None
