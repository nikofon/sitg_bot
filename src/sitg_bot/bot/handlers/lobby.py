import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import navigation_keyboard
from sitg_bot.bot.miniapps import mini_app_launch_url
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel, MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, GatewayCallError, NavigationState

router = Router(name=__name__)


class LobbyInviteState(StatesGroup):
    username = State()


async def show_lobby(
    message: Message,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    navigation: NavigationState | None,
    localization: LocalizationService,
    locale: str,
    launch_links: str | None,
    *,
    created: bool = False,
) -> None:
    if navigation is None or navigation.active_lobby is None:
        return
    lobby = await backend.lobby_info(claim, lobby_id=navigation.active_lobby.id)
    bot = await message.bot.get_me()
    invitation = f"https://t.me/{bot.username}?start=join_{lobby['invitation_code']}"
    text = localization.text(
        "lobby.summary",
        locale,
        tournament=lobby["tournament_name"],
        count=sum(m["role"] == "player" for m in lobby["members"]),
        capacity=lobby["max_players"],
        expires=localization.format_datetime(datetime.fromisoformat(lobby["expires_at"]), locale),
        invitation=invitation,
    )
    if created:
        text = localization.text("lobby.created", locale) + "\n\n" + text
    await send_message_model(
        message, MessageModel(text, navigation_keyboard(navigation, localization, locale))
    )
    if launch_links and not created:
        reference = await backend.lobby_link(claim, lobby_id=navigation.active_lobby.id)
        await send_message_model(
            message,
            MessageModel(
                localization.text("lobby.open_prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.lobby.open", locale),
                                web_app_url=mini_app_launch_url(launch_links, "lobbies", reference),
                            ),
                        ),
                    )
                ),
            ),
        )


class LobbyAction(Filter):
    async def __call__(
        self,
        message: Message,
        navigation: NavigationState | None,
        localization: LocalizationService,
    ):
        if (
            navigation is None
            or navigation.active_mode != "player"
            or navigation.context not in {"lobby", "lobby_other"}
        ):
            return False
        for action in navigation.allowed_actions:
            if any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"lobby_action": action}
        return False


@router.message(LobbyAction())
async def handle_lobby_action(
    message: Message,
    lobby_action: str,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
    launch_links: str | None,
):
    await state.clear()
    if lobby_action in {"back", "lobby.other"}:
        context = (
            "lobby_other"
            if lobby_action == "lobby.other"
            else ("lobby" if navigation.context == "lobby_other" else "tournament")
        )
        updated = await backend.lobby_context(
            telegram_update_claim, context=context, expected_version=navigation.navigation_version
        )
        await send_message_model(message, menu_message(updated, localization, locale))
        return
    lobby = await backend.lobby_info(telegram_update_claim, lobby_id=navigation.active_lobby.id)
    if lobby_action == "lobby.players":
        lines = [localization.text("lobby.players", locale)]
        for member in lobby["members"]:
            lines.append(
                localization.text(
                    "lobby.member",
                    locale,
                    name=member["display_name"],
                    role=localization.text(f"lobby.role.{member['role']}", locale),
                    ready=localization.text(
                        "button.lobby.ready" if member["ready"] else "button.lobby.unready", locale
                    ),
                )
            )
        await send_message_model(message, MessageModel("\n".join(lines)))
    elif lobby_action == "lobby.invite":
        await state.set_state(LobbyInviteState.username)
        await state.update_data(lobby_id=str(navigation.active_lobby.id))
        await send_message_model(
            message, MessageModel(localization.text("lobby.invite_prompt", locale))
        )
    elif lobby_action in {"lobby.packets", "lobby.options"}:
        if not launch_links:
            await send_message_model(
                message, MessageModel(localization.text("error.capability_unavailable", locale))
            )
            return
        reference = await backend.lobby_link(
            telegram_update_claim, lobby_id=navigation.active_lobby.id
        )
        section = "packets" if lobby_action == "lobby.packets" else "settings"
        url = mini_app_launch_url(launch_links, "lobbies", reference) + f"&section={section}"
        await send_message_model(
            message,
            MessageModel(
                localization.text("link.open_prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text(f"button.{lobby_action}", locale), web_app_url=url
                            ),
                        ),
                    )
                ),
            ),
        )
    elif lobby_action == "lobby.leave" and "cancel" in lobby["available_actions"]:
        await send_message_model(
            message,
            MessageModel(
                localization.text("lobby.close_confirm", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.yes", locale),
                                callback_data=f"lobbycancel:{navigation.active_lobby.id.hex}:{lobby['version']}",
                            ),
                            InlineButtonModel(
                                localization.text("button.no", locale),
                                callback_data="lobbycancel:no",
                            ),
                        ),
                    )
                ),
            ),
        )
    else:
        await backend.lobby_action(
            telegram_update_claim,
            lobby_id=navigation.active_lobby.id,
            action=lobby_action.removeprefix("lobby."),
            expected_version=lobby["version"],
        )
        updated = await backend.navigation(telegram_update_claim)
        if updated.context == "game":
            from sitg_bot.bot.handlers.common import send_menu_with_notification_alert

            await send_menu_with_notification_alert(
                message,
                backend=backend,
                claim=telegram_update_claim,
                navigation=updated,
                localization=localization,
                locale=locale,
            )
            return
        await send_message_model(message, menu_message(updated, localization, locale))


@router.message(LobbyInviteState.username)
async def handle_invite_username(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
):
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", message.text or ""):
        await send_message_model(
            message, MessageModel(localization.text("lobby.invite_prompt", locale))
        )
        return
    data = await state.get_data()
    if navigation.active_lobby is None or str(navigation.active_lobby.id) != data.get("lobby_id"):
        await state.clear()
        await send_message_model(message, menu_message(navigation, localization, locale))
        return
    try:
        await backend.lobby_action(
            telegram_update_claim,
            lobby_id=navigation.active_lobby.id,
            action="invite",
            expected_version=navigation.active_lobby.version,
            username=message.text,
        )
    except GatewayCallError as error:
        if error.error.code != ErrorCode.NOT_FOUND:
            raise
        await send_message_model(
            message, MessageModel(localization.text("lobby.invitee_not_found", locale))
        )
        return
    await state.clear()
    await send_message_model(message, MessageModel(localization.text("lobby.invited", locale)))


@router.callback_query(F.data.regexp(r"^lobbyjoin:[poc]:[A-Za-z0-9_-]{32}$"))
async def handle_join_callback(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
):
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    _, role, code = callback.data.split(":")
    if navigation is None or navigation.context == "registration":
        await send_message_model(
            callback.message, MessageModel(localization.text("lobby.register_first", locale))
        )
        return
    if role == "o":
        await send_message_model(
            callback.message,
            MessageModel(
                localization.text("lobby.observer_confirm", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.yes", locale),
                                callback_data=f"lobbyjoin:c:{code}",
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    await backend.join_lobby(
        telegram_update_claim,
        invitation_code=code,
        role="observer" if role == "c" else "player",
        confirm_fresh=role == "c",
    )
    updated = await backend.navigation(telegram_update_claim)
    await send_message_model(callback.message, menu_message(updated, localization, locale))


@router.callback_query(F.data.startswith("lobbycancel:"))
async def handle_cancel_callback(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
):
    from uuid import UUID

    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback.data != "lobbycancel:no":
        match = re.fullmatch(r"lobbycancel:([a-f0-9]{32}):([0-9]{1,10})", callback.data)
        if match is None:
            return
        await backend.lobby_action(
            telegram_update_claim,
            lobby_id=UUID(hex=match.group(1)),
            action="cancel",
            expected_version=int(match.group(2)),
        )
    await callback.message.edit_reply_markup(reply_markup=None)
    updated = await backend.navigation(telegram_update_claim)
    await send_message_model(callback.message, menu_message(updated, localization, locale))
