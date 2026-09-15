from uuid import UUID

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel, MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, GatewayCallError, NavigationState

router = Router(name=__name__)


def invitation_message(
    invitation: dict[str, object], reference: str,
    localization: LocalizationService, locale: str,
) -> MessageModel:
    status = invitation["membership_status"]
    keyboard = None
    if not invitation["registration_open"]:
        key = "tournament.registration.closed"
    elif invitation["is_manager"]:
        key = "tournament.registration.manager"
    elif status in {"registered", "approved", "active"}:
        key = f"tournament.registration.{status}"
    else:
        key = (
            "tournament.registration.nonparticipant"
            if reference.startswith("join_") else "tournament.registration.confirm"
        )
        keyboard = InlineKeyboardModel(rows=((
            InlineButtonModel(
                localization.text("button.yes", locale),
                callback_data=f"treg:yes:{reference}",
            ),
            InlineButtonModel(
                localization.text("button.no", locale), callback_data="treg:no",
            ),
        ),))
    return MessageModel(
        localization.text(key, locale, tournament=invitation["name"]), keyboard,
    )


async def show_invitation(
    message: Message, backend: BotBackend, claim: TelegramUpdateClaim,
    reference: str, localization: LocalizationService, locale: str,
) -> bool:
    """Return False only when an active participant can continue joining the lobby."""
    try:
        invitation = await backend.registration_invitation(claim, reference=reference)
    except GatewayCallError as error:
        if error.error.code != ErrorCode.NOT_FOUND:
            raise
        await send_message_model(message, MessageModel(
            localization.text("tournament.registration.invalid_link", locale),
        ))
        return True
    if reference.startswith("join_") and invitation["membership_status"] == "active":
        return False
    await send_message_model(
        message, invitation_message(invitation, reference, localization, locale),
    )
    return True


@router.message(StateFilter(None), F.text.in_({"Registration link", "Ссылка на регистрацию"}))
async def handle_registration_link(
    message: Message, backend: BotBackend, telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService, locale: str, navigation: NavigationState | None,
) -> None:
    if navigation is None or "tournament.registration_link" not in navigation.allowed_actions:
        return
    selected = (
        navigation.selected_manager_tournament if navigation.active_mode == "manager"
        else navigation.selected_player_tournament
    )
    if selected is None:
        return
    link = await backend.registration_link(telegram_update_claim, tournament_id=selected.id)
    bot = await message.bot.get_me()
    text = localization.text(
        "tournament.registration.link", locale, tournament=link["name"],
        link=f"https://t.me/{bot.username}?start={link['reference']}",
    )
    if link["visibility"] == "private":
        text += "\n\n" + localization.text("tournament.registration.private_warning", locale)
    await send_message_model(message, MessageModel(text))


@router.callback_query(F.data.regexp(r"^treg:(no|yes:(reg|join)_[A-Za-z0-9_-]{32})$"))
async def handle_registration_confirmation(
    callback: CallbackQuery, backend: BotBackend, telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService, locale: str, navigation: NavigationState | None,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback.data == "treg:no":
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    if navigation is None or navigation.context == "registration":
        await send_message_model(callback.message, MessageModel(
            localization.text("lobby.register_first", locale),
        ))
        return
    reference = callback.data.removeprefix("treg:yes:")
    invitation = None
    try:
        invitation = await backend.registration_invitation(
            telegram_update_claim, reference=reference,
        )
        if (
            not invitation["registration_open"] or invitation["is_manager"]
            or invitation["membership_status"] in {"registered", "approved", "active"}
        ):
            await callback.message.edit_reply_markup(reply_markup=None)
            await send_message_model(callback.message, invitation_message(
                invitation, reference, localization, locale,
            ))
            return
        decision = await backend.register_tournament(
            telegram_update_claim, tournament_id=UUID(str(invitation["tournament_id"])),
            invitation_reference=reference,
        )
    except GatewayCallError as error:
        if error.error.code not in {ErrorCode.NOT_FOUND, ErrorCode.VALIDATION_FAILED}:
            raise
        # The window or membership may change between the prompt and confirmation.
        if error.error.code == ErrorCode.VALIDATION_FAILED:
            current = await backend.registration_invitation(
                telegram_update_claim, reference=reference,
            )
            if current == invitation:
                raise
            await callback.message.edit_reply_markup(reply_markup=None)
            await send_message_model(callback.message, invitation_message(
                current, reference, localization, locale,
            ))
            return
        await show_invitation(
            callback.message, backend, telegram_update_claim, reference, localization, locale,
        )
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    if decision["accepted"]:
        text = localization.text(
            f"tournament.registration.{decision['status']}", locale,
            tournament=invitation["name"],
        )
    else:
        text = localization.text(
            "tournament.registration.rejected", locale, tournament=invitation["name"],
            reasons="\n".join(str(reason) for reason in decision["reasons"]),
        )
    await send_message_model(callback.message, MessageModel(text))
    if decision["status"] == "active" and reference.startswith("join_"):
        await backend.join_lobby(
            telegram_update_claim, invitation_code=reference.removeprefix("join_"),
        )
        updated = await backend.navigation(telegram_update_claim)
        await send_message_model(callback.message, menu_message(updated, localization, locale))
