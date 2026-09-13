import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import CancelHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, TelegramObject, Update

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.miniapps import mini_app_route_url
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel, MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state.models import NavigationState

LOGGER = logging.getLogger(__name__)


class BannedPlayerMiddleware(BaseMiddleware):
    """Answers banned players on every interaction except opening the library."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        navigation = data.get("navigation")
        if not isinstance(navigation, NavigationState) or navigation.ban_reason is None:
            return await handler(event, data)
        localization = data.get("localization")
        locale = data.get("locale", "ru")
        if not isinstance(localization, LocalizationService):
            localization = LocalizationService()
        update = event if isinstance(event, Update) else data.get("event_update")
        message = update.message if isinstance(update, Update) else None
        if (
            isinstance(message, Message)
            and message.text is not None
            and self._is_library_intent(message.text, localization)
            and "player.library" in navigation.allowed_actions
        ):
            return await handler(event, data)
        callback = update.callback_query if isinstance(update, Update) else None
        if callback is not None:
            with suppress(TelegramAPIError):
                await callback.answer(
                    localization.text("player.banned", locale, reason=navigation.ban_reason),
                    show_alert=True,
                )
            raise CancelHandler
        if isinstance(message, Message):
            await self._send_banned_notice(message, navigation, localization, locale, data)
        raise CancelHandler

    @staticmethod
    def _is_library_intent(text: str, localization: LocalizationService) -> bool:
        return any(
            text == localization.text("button.player.library", candidate)
            for candidate in localization.catalogs
        )

    @staticmethod
    async def _send_banned_notice(
        message: Message,
        navigation: NavigationState,
        localization: LocalizationService,
        locale: str,
        data: dict[str, Any],
    ) -> None:
        keyboard: InlineKeyboardModel | None = None
        launch_links = data.get("launch_links")
        if launch_links is not None:
            try:
                url = mini_app_route_url(launch_links, "library")
            except ValueError:
                url = None
            if url is not None:
                keyboard = InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.player.library", locale),
                                web_app_url=url,
                            ),
                        ),
                    )
                )
        with suppress(TelegramAPIError):
            await send_message_model(
                message,
                MessageModel(
                    localization.text("player.banned", locale, reason=navigation.ban_reason),
                    keyboard,
                ),
            )
