"""Tournament match chats: room listing, opening, scheduling, and leaving."""

from datetime import datetime

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.chat_delivery import tournament_chat_round_label
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.miniapps import mini_app_route_url
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
    RemoveKeyboardModel,
)
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, GatewayCallError, NavigationState

router = Router(name=__name__)
commands = Router(name=__name__ + ".commands")


def _chat_room_projection(payload, localization, locale):
    projection = dict(payload)
    projection["round"] = tournament_chat_round_label(payload, localization, locale)
    return projection


async def send_tournament_chat_list(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    callback_references,
) -> None:
    selected = navigation.selected_player_tournament
    if selected is None:
        await send_message_model(
            message, MessageModel(localization.text("tournament_chat.unavailable", locale))
        )
        return
    try:
        result = await backend.tournament_chat_list(claim, tournament_id=selected.id)
    except GatewayCallError as error:
        if error.error.code == ErrorCode.INTERNAL_ERROR:
            raise
        await send_message_model(
            message, MessageModel(localization.text("tournament_chat.unavailable", locale))
        )
        return
    rooms = result.get("rooms", []) if result else []
    if not rooms:
        await send_message_model(
            message, MessageModel(localization.text("tournament_chat.empty", locale))
        )
        return
    await send_message_model(
        message, MessageModel(localization.text("tournament_chat.list_prompt", locale))
    )
    for room in rooms:
        room = _chat_room_projection(room, localization, locale)
        players = ", ".join(participant["nickname"] for participant in room["participants"])
        planned = ""
        if room.get("planned_at"):
            planned = localization.format_datetime(
                datetime.fromisoformat(room["planned_at"]), locale
            )
        text = localization.text(
            "tournament_chat.room",
            locale,
            round=room["round"],
            tournament=room["tournament_name"],
            players=players,
            planned=planned
            or localization.text("tournament_chat.room_not_planned", locale),
            unread=localization.text("tournament_chat.unread", locale)
            if room.get("unread")
            else "",
        )
        reference = callback_references.issue(
            scope="tournament_chat",
            actor_id=navigation.account.player_id,
            resource_id=room["chat_id"],
            action="open",
        )
        await send_message_model(
            message,
            MessageModel(
                text,
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.tournament_chat.open", locale),
                                callback_data=f"tchat:{reference}",
                            ),
                        ),
                    )
                ),
            ),
        )



@router.callback_query(F.data.regexp(r"^tchat:[A-Za-z0-9_-]+$"))
async def handle_open_tournament_chat(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    callback_references,
    state: FSMContext,
) -> None:
    if navigation is None:
        await callback.answer(
            localization.text("tournament_chat.unavailable", locale), show_alert=True
        )
        return
    reference = (callback.data or "").removeprefix("tchat:")
    try:
        target = callback_references.resolve(
            reference,
            scope="tournament_chat",
            actor_id=navigation.account.player_id,
        )
    except LookupError:
        await callback.answer(
            localization.text("tournament_chat.expired", locale), show_alert=True
        )
        return
    try:
        result = await backend.tournament_chat_open(
            telegram_update_claim, chat_id=target.resource_id
        )
    except GatewayCallError as error:
        if error.error.code == ErrorCode.INTERNAL_ERROR:
            raise
        await callback.answer(
            localization.text("tournament_chat.unavailable", locale), show_alert=True
        )
        return
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message) or not result:
        return
    chat = _chat_room_projection(result["chat"], localization, locale)
    planned = localization.text("tournament_chat.room_not_planned", locale)
    if chat.get("planned_at"):
        planned = localization.format_datetime(
            datetime.fromisoformat(chat["planned_at"]), locale
        )
    await send_message_model(
        callback.message,
        MessageModel(
            localization.text(
                "tournament_chat.opened",
                locale,
                round=chat["round"],
                tournament=chat["tournament_name"],
                planned=planned,
            ),
            RemoveKeyboardModel(),
        ),
    )



@commands.message(Command("set_game_time"))
async def handle_set_game_time(
    message: Message,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    launch_links: str | None,
) -> None:
    if (
        navigation is None
        or navigation.context != "chat"
        or navigation.active_chat is None
    ):
        await send_message_model(
            message,
            MessageModel(localization.text("tournament_chat.schedule_chat_only", locale)),
        )
        return
    if launch_links is None:
        await send_message_model(
            message, MessageModel(localization.text("error.capability_unavailable", locale))
        )
        return
    chat = navigation.active_chat
    url = mini_app_route_url(launch_links, f"chats/{chat.id}/schedule")
    round_label = (
        localization.text(
            "tournament_chat.round_match",
            locale,
            number=chat.round_number,
            match=chat.match_number,
        )
        if chat.multiple_matches
        else localization.text("tournament_chat.round", locale, number=chat.round_number)
    )
    await send_message_model(
        message,
        MessageModel(
            localization.text(
                "tournament_chat.schedule_prompt",
                locale,
                round=round_label,
                tournament=chat.tournament_name,
            ),
            InlineKeyboardModel(
                rows=(
                    (
                        InlineButtonModel(
                            localization.text("button.tournament_chat.schedule", locale),
                            web_app_url=url,
                        ),
                    ),
                )
            ),
        ),
    )


@commands.message(Command("quit"))
async def handle_tournament_chat_quit(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    if (
        navigation is None
        or navigation.context != "chat"
        or navigation.active_chat is None
    ):
        raise SkipHandler
    await state.clear()
    try:
        await backend.tournament_chat_quit(telegram_update_claim)
    except GatewayCallError as error:
        if error.error.code == ErrorCode.INTERNAL_ERROR:
            raise
        await send_message_model(
            message, MessageModel(localization.text("tournament_chat.unavailable", locale))
        )
        return
    updated = await backend.navigation(telegram_update_claim)
    from sitg_bot.bot.handlers.common import send_menu_with_notification_alert

    await send_message_model(
        message, MessageModel(localization.text("tournament_chat.closed", locale))
    )
    if updated is not None:
        await send_menu_with_notification_alert(
            message,
            backend=backend,
            claim=telegram_update_claim,
            navigation=updated,
            localization=localization,
            locale=locale,
        )
