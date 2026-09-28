import html
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import navigation_keyboard
from sitg_bot.bot.miniapps import mini_app_launch_url
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel, MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, NavigationState

router = Router(name=__name__)


def _format_rating(value: object) -> str:
    if value is None:
        return "—"
    return f"{float(value):g}"


def lobby_info_text(lobby: dict, localization: LocalizationService, locale: str) -> str:
    """Project a lobby snapshot into a readable participant overview."""

    creator_id = lobby.get("creator_telegram_user_id")
    members = lobby.get("members") or []

    def ratings(member: dict) -> str:
        return localization.text(
            "lobby.info.ratings",
            locale,
            global_rating=_format_rating(member.get("global_rating")),
            tournament_rating=_format_rating(member.get("tournament_rating")),
        )

    def owner(member: dict) -> str:
        if creator_id is not None and member.get("telegram_user_id") == creator_id:
            return localization.text("lobby.info.owner", locale)
        return ""

    lines = [
        localization.text(
            "lobby.info.title", locale, tournament=lobby.get("tournament_name") or ""
        )
    ]
    players = [member for member in members if member.get("role") == "player"]
    observers = [member for member in members if member.get("role") == "observer"]
    if players:
        lines.append(localization.text("lobby.info.players", locale))
        for member in players:
            lines.append(
                localization.text(
                    "lobby.info.player_line",
                    locale,
                    icon="✅" if member.get("ready") else "⏳",
                    name=member.get("display_name", ""),
                    owner=owner(member),
                    ratings=ratings(member),
                    status=localization.text(
                        "lobby.info.status_ready"
                        if member.get("ready")
                        else "lobby.info.status_unready",
                        locale,
                    ),
                )
            )
    if observers:
        lines.append(localization.text("lobby.info.observers", locale))
        for member in observers:
            lines.append(
                localization.text(
                    "lobby.info.observer_line",
                    locale,
                    name=member.get("display_name", ""),
                    owner=owner(member),
                    ratings=ratings(member),
                )
            )
    packets = lobby.get("selected_packets") or []
    if packets:
        lines.append(localization.text("lobby.info.packets", locale))
        lines.extend(f"• {html.escape(str(packet.get('name', '')))}" for packet in packets)
    return "\n".join(lines)


def automatic_packet_selection(lobby: dict) -> list[dict]:
    """Choose the smallest valid packet set for a lobby without selected packets.

    Candidates must be playable for every member and have fresh content; packets
    with the least (but non-zero) fresh themes are preferred, and more packets are
    added until the lobby theme count requirement is satisfied.
    """

    theme_count = (lobby.get("settings") or {}).get("theme_count")
    try:
        required = int(theme_count) if theme_count is not None else 8
    except (TypeError, ValueError):
        required = 8
    candidates = [
        packet
        for packet in lobby.get("packet_suggestions") or ()
        if packet.get("playable_for_all") and int(packet.get("fresh_play_unit_count") or 0) > 0
    ]
    candidates.sort(key=lambda packet: int(packet["fresh_play_unit_count"]))
    chosen: list[dict] = []
    available = 0
    for packet in candidates:
        if chosen and available >= required:
            break
        chosen.append(packet)
        available += int(packet["fresh_play_unit_count"])
    if not chosen or available < required:
        return []
    return chosen


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
    settings = lobby.get("settings") or {}
    minimum = settings.get("minimum_players")
    maximum = settings.get("maximum_players")
    capacity = (
        f"{minimum}–{maximum}"
        if minimum is not None and maximum is not None
        else str(lobby["max_players"])
    )
    text = localization.text(
        "lobby.summary",
        locale,
        tournament=lobby["tournament_name"],
        count=sum(m["role"] == "player" for m in lobby["members"]),
        capacity=capacity,
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
    if lobby_action == "back":
        context = "lobby" if navigation.context == "lobby_other" else "tournament"
        updated = await backend.lobby_context(
            telegram_update_claim, context=context, expected_version=navigation.navigation_version
        )
        await send_message_model(message, menu_message(updated, localization, locale))
        return
    lobby = await backend.lobby_info(telegram_update_claim, lobby_id=navigation.active_lobby.id)
    if lobby_action == "lobby.info":
        info = MessageModel(lobby_info_text(lobby, localization, locale))
        await send_message_model(message, info)
        if launch_links:
            reference = await backend.lobby_link(
                telegram_update_claim, lobby_id=navigation.active_lobby.id
            )
            url = mini_app_launch_url(launch_links, "lobbies", reference) + "&section=settings"
            await send_message_model(
                message,
                MessageModel(
                    localization.text("lobby.info.settings_prompt", locale),
                    InlineKeyboardModel(
                        rows=(
                            (
                                InlineButtonModel(
                                    localization.text("button.lobby.settings", locale),
                                    web_app_url=url,
                                ),
                            ),
                        ),
                    ),
                ),
            )
    elif lobby_action == "lobby.start" and not lobby["selected_packets"]:
        packets = automatic_packet_selection(lobby)
        if not packets:
            await send_message_model(
                message, MessageModel(localization.text("lobby.start.no_valid_packet", locale))
            )
            return
        names = ", ".join(str(packet["name"]) for packet in packets)
        await send_message_model(
            message,
            MessageModel(
                localization.text("lobby.start.auto_packet_confirm", locale, packets=names),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.yes", locale),
                                callback_data=(
                                    f"lobbystart:yes:{navigation.active_lobby.id.hex}"
                                ),
                            ),
                            InlineButtonModel(
                                localization.text("button.no", locale),
                                callback_data=f"lobbystart:no:{navigation.active_lobby.id.hex}",
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
    from sitg_bot.bot.handlers.tournament_registration import show_invitation

    if role == "p" and await show_invitation(
        callback.message, backend, telegram_update_claim, f"join_{code}", localization, locale,
    ):
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


@router.callback_query(F.data.regexp(r"^lobbystart:(yes|no):[a-f0-9]{32}$"))
async def handle_start_confirmation_callback(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    from uuid import UUID

    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    decision, raw_lobby_id = (callback.data or "").split(":")[1:]
    if decision != "yes":
        return
    if navigation is None or navigation.active_lobby is None:
        return
    try:
        lobby_id = UUID(hex=raw_lobby_id)
    except ValueError:
        return
    if navigation.active_lobby.id != lobby_id:
        return
    lobby = await backend.lobby_info(telegram_update_claim, lobby_id=lobby_id)
    packets = automatic_packet_selection(lobby)
    if not packets:
        await send_message_model(
            callback.message,
            MessageModel(localization.text("lobby.start.no_valid_packet", locale)),
        )
        return
    version = int(lobby["version"])
    if not lobby.get("selected_packets"):
        selection = await backend.select_lobby_packets(
            telegram_update_claim,
            lobby_id=lobby_id,
            packet_ids=tuple(packet["packet_id"] for packet in packets),
            expected_version=version,
        )
        version = int(selection["version"])
    await backend.lobby_action(
        telegram_update_claim, lobby_id=lobby_id, action="start", expected_version=version
    )
    updated = await backend.navigation(telegram_update_claim)
    if updated.context == "game":
        from sitg_bot.bot.handlers.common import send_menu_with_notification_alert

        await send_menu_with_notification_alert(
            callback.message,
            backend=backend,
            claim=telegram_update_claim,
            navigation=updated,
            localization=localization,
            locale=locale,
        )
        return
    await send_message_model(callback.message, menu_message(updated, localization, locale))
