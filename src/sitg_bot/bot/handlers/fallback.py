from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import NavigationState

router = Router(name=__name__)


@router.message()
async def handle_unsupported(
    message: Message,
    backend,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    if navigation is None:
        await send_message_model(
            message, MessageModel(localization.text("registration.start_required", locale))
        )
        return
    if await state.get_state() is not None:
        await send_message_model(
            message, MessageModel(localization.text("chat.text_expected", locale))
        )
        return
    notice = localization.text("unsupported.menu", locale)
    current = menu_message(navigation, localization, locale)
    if navigation.context == "game":
        await backend.game_delivery.track(
            message.chat.id, navigation.active_game.id, message.message_id
        )
        await backend.game_delivery.send(
            message.chat.id,
            navigation.active_game.id,
            f"command:{message.message_id}:unsupported",
            MessageModel(f"{notice}\n\n{current.text}", current.keyboard),
        )
        return
    await send_message_model(message, MessageModel(f"{notice}\n\n{current.text}", current.keyboard))
