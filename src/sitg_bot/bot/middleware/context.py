from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import DEFAULT_LOCALE, LocalizationService
from sitg_bot.bot.state import BotBackend, GatewayCallError


class PlayerContextMiddleware(BaseMiddleware):
    def __init__(self, backend: BotBackend) -> None:
        self.backend = backend

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        claim = data.get("telegram_update_claim")
        if isinstance(claim, TelegramUpdateClaim):
            try:
                data["navigation"] = await self.backend.navigation(claim)
            except GatewayCallError as error:
                data["context_error"] = error
                data["navigation"] = None
        return await handler(event, data)


class LocaleMiddleware(BaseMiddleware):
    def __init__(self, localization: LocalizationService) -> None:
        self.localization = localization

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        navigation = data.get("navigation")
        preferred = navigation.account.preferred_locale if navigation is not None else None
        data["locale"] = self.localization.locale(preferred or DEFAULT_LOCALE)
        return await handler(event, data)
