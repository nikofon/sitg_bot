from collections.abc import Mapping

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.render import telegram_keyboard
from sitg_bot.bot.state import NavigationState


class TelegramTournamentSelectionNotifier:
    """Push the selected tournament's menu into the originating private chat."""

    def __init__(self, bot_token: str, *, localization: LocalizationService | None = None) -> None:
        self.bot = Bot(
            bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.localization = localization or LocalizationService()

    async def notify_selected(
        self, telegram_user_id: int, navigation_payload: Mapping[str, object]
    ) -> None:
        navigation = NavigationState.model_validate(navigation_payload)
        locale = navigation.account.preferred_locale
        model = menu_message(navigation, self.localization, locale)
        await self.bot.send_message(
            telegram_user_id,
            model.text,
            reply_markup=telegram_keyboard(model.keyboard),
            protect_content=model.protect_content,
        )

    async def lobby_invitation_url(self, invitation_code: str) -> str:
        identity = await self.bot.me()
        return f"https://t.me/{identity.username}?start=join_{invitation_code}"

    async def close(self) -> None:
        await self.bot.session.close()
