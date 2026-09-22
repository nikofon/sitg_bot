"""Chat is the final destination after commands, menus, and conversational inputs."""

import hashlib
import re

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.bot.presenters.models import MessageModel
from sitg_bot.bot.state import GatewayCallError

router = Router(name=__name__)
commands = Router(name=__name__ + ".commands")


class WhisperState(StatesGroup):
    entering = State()


async def remember_album(message, state, scope, target):
    if message.media_group_id:
        data = await state.get_data()
        albums = dict(data.get("chat_albums", {}))
        albums[message.media_group_id] = {
            "scope": scope["scope"],
            "scope_id": str(scope["scope_id"]),
            "target": target,
        }
        await state.update_data(chat_albums=dict(list(albums.items())[-20:]))


async def chat_reply(message, backend, scope, text):
    if scope and scope["scope"] == "game":
        await backend.game_delivery.track(message.chat.id, scope["scope_id"], message.message_id)
        suffix = hashlib.sha256(text.encode()).hexdigest()[:16]
        return await backend.game_delivery.send(
            message.chat.id,
            scope["scope_id"],
            f"command:{message.message_id}:chat:{suffix}",
            MessageModel(text),
        )
    return await message.answer(text)


def chat_scope(navigation):
    if navigation is None or navigation.context == "registration":
        return None
    if navigation.active_chat is not None and navigation.context == "chat":
        return {"scope": "tournament_chat", "scope_id": navigation.active_chat.id}
    if navigation.active_game is not None:
        return {"scope": "game", "scope_id": navigation.active_game.id}
    if navigation.active_lobby is not None:
        return {"scope": "lobby", "scope_id": navigation.active_lobby.id}
    return None


def copyable(message):
    if message.has_protected_content:
        return False
    if message.poll:
        return message.poll.type != "quiz" or message.poll.correct_option_id is not None
    return any(
        getattr(message, kind, None)
        for kind in (
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "video_note",
            "document",
            "sticker",
            "contact",
            "location",
            "venue",
            "dice",
        )
    )


async def send_chat(
    message, backend, claim, localization, locale, scope, *, target=None, text=None, caption=None
):
    try:
        result = await backend.chat_send(
            claim,
            **scope,
            message_id=message.message_id,
            text=text,
            target=target,
            caption=caption,
            media_group_id=message.media_group_id,
        )
    except GatewayCallError as error:
        if error.error.code == ErrorCode.INTERNAL_ERROR:
            raise
        await chat_reply(message, backend, scope, localization.text("chat.unavailable", locale))
        return
    if not result["recipients"]:
        await chat_reply(message, backend, scope, localization.text("chat.empty", locale))
    elif target is not None:
        await chat_reply(message, backend, scope, localization.text("chat.whisper_sent", locale))


@commands.message(Command("whisper"))
async def handle_whisper(
    message: Message, backend, telegram_update_claim, localization, locale, navigation, state
):
    scope = chat_scope(navigation)
    if scope is None:
        await chat_reply(message, backend, scope, localization.text("chat.no_context", locale))
        return
    raw = message.text or message.caption or ""
    parts = raw.split(maxsplit=1)
    if len(parts) == 1:
        try:
            result = await backend.chat_members(telegram_update_claim, **scope)
        except GatewayCallError as error:
            if error.error.code == ErrorCode.INTERNAL_ERROR:
                raise
            await chat_reply(message, backend, scope, localization.text("chat.unavailable", locale))
            return
        await chat_reply(message, backend, scope, localization.text("chat.whisper_usage", locale))
        for member in result["members"]:
            await chat_reply(
                message,
                backend,
                scope,
                localization.text("chat.member", locale, name=member["name"], id=member["id"]),
            )
        return
    match = re.fullmatch(r"""(?:"([^"]+)"|'([^']+)'|(\S+))(?:\s+([\s\S]*))?""", parts[1])
    if match is None:
        await chat_reply(message, backend, scope, localization.text("chat.whisper_usage", locale))
        return
    target = next(value for value in match.groups()[:3] if value is not None)
    text = match.group(4)
    if message.text and not (text and text.strip()):
        try:
            result = await backend.chat_members(telegram_update_claim, **scope)
            matches = [
                p
                for p in result["members"]
                if p["id"] == target or p["name"].casefold() == target.casefold()
            ]
            if len(matches) != 1:
                await chat_reply(
                    message, backend, scope, localization.text("chat.unavailable", locale)
                )
                return
        except GatewayCallError as error:
            if error.error.code == ErrorCode.INTERNAL_ERROR:
                raise
            await chat_reply(message, backend, scope, localization.text("chat.unavailable", locale))
            return
        await state.set_state(WhisperState.entering)
        await state.update_data(
            whisper_scope={**scope, "scope_id": str(scope["scope_id"])},
            whisper_target=matches[0]["id"],
        )
        await chat_reply(
            message,
            backend,
            scope,
            localization.text("chat.whisper_prompt", locale, name=matches[0]["name"]),
        )
        return
    await remember_album(message, state, scope, target)
    if not message.text and not copyable(message):
        await chat_reply(
            message, backend, scope, localization.text("chat.unsupported_media", locale)
        )
        return
    await send_chat(
        message,
        backend,
        telegram_update_claim,
        localization,
        locale,
        scope,
        target=target,
        text=text if message.text else None,
        caption=(text or "") if message.caption else None,
    )


@commands.message(StateFilter(WhisperState.entering))
async def handle_whisper_input(
    message: Message, backend, telegram_update_claim, localization, locale, state
):
    data = await state.get_data()
    scope = data["whisper_scope"]
    if (message.text or message.caption or "").startswith("/"):
        raise SkipHandler
    if message.text is None and not copyable(message):
        await chat_reply(
            message, backend, scope, localization.text("chat.unsupported_media", locale)
        )
        return
    await remember_album(message, state, scope, data["whisper_target"])
    await send_chat(
        message,
        backend,
        telegram_update_claim,
        localization,
        locale,
        data["whisper_scope"],
        target=data["whisper_target"],
        text=message.text,
    )
    albums = (await state.get_data()).get("chat_albums", {})
    await state.set_state(None)
    await state.set_data({"chat_albums": albums})


@router.message(StateFilter(None))
async def handle_chat(
    message: Message, backend, telegram_update_claim, localization, locale, navigation, state=None
):
    scope = chat_scope(navigation)
    target = None
    if message.media_group_id and state is not None:
        album = (await state.get_data()).get("chat_albums", {}).get(message.media_group_id)
        if album:
            scope = {"scope": album["scope"], "scope_id": album["scope_id"]}
            target = album["target"]
    if scope is None:
        raise SkipHandler
    content = message.text or message.caption or ""
    if content.startswith("/"):
        raise SkipHandler
    if scope["scope"] == "game" and message.text is None and target is None:
        view = await backend.game_view(telegram_update_claim, scope["scope_id"])
        if {"answer", "commentary"}.intersection(view["actions"]):
            await chat_reply(
                message, backend, scope, localization.text("chat.text_expected", locale)
            )
            return
    if message.text is None and not copyable(message):
        await chat_reply(
            message, backend, scope, localization.text("chat.unsupported_media", locale)
        )
        return
    await send_chat(
        message,
        backend,
        telegram_update_claim,
        localization,
        locale,
        scope,
        text=message.text,
        target=target,
    )
