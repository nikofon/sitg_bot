import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User
from test_telegram_frontend import navigation, token_request
from test_telegram_gameplay import handler_fixture, view

from sitg_bot.bot.chat_delivery import chat_delivery_handler
from sitg_bot.bot.handlers.chat import WhisperState, copyable, handle_chat
from sitg_bot.bot.handlers.game import handle_game_input
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.router import root_router
from sitg_bot.bot.state.settings import TournamentTokenRequestState


def message(**values):
    return Message(
        message_id=123,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Sender"),
        **values,
    )


def lobby_navigation(**changes):
    return navigation(
        active_lobby={
            "id": str(UUID(int=8)),
            "tournament_id": str(UUID(int=9)),
            "status": "assembling",
            "version": 1,
        },
        **changes,
    )


async def route(msg, nav, backend, monkeypatch, *, prompt=None, data=None):
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyz123456789")
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=bot.id, chat_id=42, user_id=42))
    await state.set_state(prompt)
    await state.set_data(data or {})
    monkeypatch.setattr(Message, "answer", AsyncMock())
    await root_router.propagate_event(
        "message",
        msg.as_(bot),
        bot=bot,
        backend=backend,
        navigation=nav,
        state=state,
        raw_state=await state.get_state(),
        telegram_update_claim=object(),
        localization=LocalizationService(),
        locale="en",
        callback_references=None,
    )
    return state


@pytest.mark.parametrize("text", ["My private justification", "+", "||", "!"])
async def test_token_commentary_in_lobby_is_never_broadcast(monkeypatch, text):
    backend = SimpleNamespace(
        chat_send=AsyncMock(), request_tournament_token=AsyncMock(return_value=token_request())
    )
    await route(
        message(text=text),
        lobby_navigation(active_mode="manager"),
        backend,
        monkeypatch,
        prompt=TournamentTokenRequestState.entering_commentary,
        data={"tournament_name": "Tournament"},
    )
    backend.chat_send.assert_not_awaited()
    assert backend.request_tournament_token.await_args.kwargs["commentary"] == text


async def test_lobby_chat_works_in_manager_menu_and_commands_are_not_broadcast(monkeypatch):
    backend = SimpleNamespace(chat_send=AsyncMock(return_value={"recipients": 2}))
    await route(
        message(text="Hello <everyone>"),
        lobby_navigation(active_mode="manager"),
        backend,
        monkeypatch,
    )
    assert backend.chat_send.await_args.kwargs["text"] == "Hello <everyone>"
    backend.chat_send.reset_mock()
    await route(message(text="/unknown"), lobby_navigation(), backend, monkeypatch)
    backend.chat_send.assert_not_awaited()


async def test_media_during_text_prompt_does_not_fall_through_to_chat(monkeypatch):
    backend = SimpleNamespace(chat_send=AsyncMock())
    await route(
        message(sticker=sticker()),
        lobby_navigation(active_mode="manager"),
        backend,
        monkeypatch,
        prompt=TournamentTokenRequestState.entering_commentary,
    )
    backend.chat_send.assert_not_awaited()


async def test_whisper_text_and_next_sticker_never_broadcast(monkeypatch):
    backend = SimpleNamespace(
        chat_send=AsyncMock(return_value={"recipients": 1}),
        chat_members=AsyncMock(return_value={"members": [{"id": str(UUID(int=2)), "name": "A B"}]}),
    )
    await route(
        message(text='/whisper "A B" secret <text>'), lobby_navigation(), backend, monkeypatch
    )
    assert backend.chat_send.await_args.kwargs["target"] == "A B"
    assert backend.chat_send.await_args.kwargs["text"] == "secret <text>"
    state = await route(message(text='/whisper "A B"'), lobby_navigation(), backend, monkeypatch)
    assert await state.get_state() == WhisperState.entering.state
    await route(
        message(sticker=sticker()),
        lobby_navigation(),
        backend,
        monkeypatch,
        prompt=WhisperState.entering,
        data=await state.get_data(),
    )
    assert backend.chat_send.await_args.kwargs["target"] == str(UUID(int=2))
    assert backend.chat_send.await_args.kwargs["text"] is None


async def test_game_plain_answer_uses_prompt_instead_of_chat():
    snapshot = view(
        actions=["answer"],
        messages={"prompt": {"kind": "answer", "round_id": str(UUID(int=5)), "ids": [321]}},
    )
    backend, msg, nav = handler_fixture(snapshot)
    await handle_game_input(msg, backend, object(), LocalizationService(), "en", nav)
    assert backend.game_action.await_args.kwargs["command"] == "answer"
    assert backend.game_action.await_args.kwargs["text"] == "answer"


async def test_every_album_item_keeps_whisper_recipient(monkeypatch):
    backend = SimpleNamespace(chat_send=AsyncMock(return_value={"recipients": 1}))
    media = {"photo": [{"file_id": "photo", "file_unique_id": "p", "width": 100, "height": 100}]}
    state = await route(
        message(**media, caption='/whisper "Recipient" caption', media_group_id="album"),
        lobby_navigation(),
        backend,
        monkeypatch,
    )
    assert backend.chat_send.await_args.kwargs["caption"] == "caption"
    await route(
        message(**media, media_group_id="album"),
        lobby_navigation(),
        backend,
        monkeypatch,
        data=await state.get_data(),
    )
    assert backend.chat_send.await_args.kwargs["target"] == "Recipient"
    assert backend.chat_send.await_args.kwargs["media_group_id"] == "album"
    await route(
        message(**media, media_group_id="different-album"),
        lobby_navigation(),
        backend,
        monkeypatch,
        data=await state.get_data(),
    )
    assert backend.chat_send.await_args.kwargs["target"] is None


async def test_no_membership_does_not_send():
    backend = SimpleNamespace(chat_send=AsyncMock())
    with pytest.raises(SkipHandler):
        await handle_chat(
            message(text="Hello"), backend, object(), LocalizationService(), "en", navigation()
        )
    backend.chat_send.assert_not_awaited()


async def test_answer_waiting_for_prompt_delivery_is_not_broadcast():
    backend, msg, nav = handler_fixture(view(actions=["answer"], messages={}))
    await handle_game_input(msg, backend, object(), LocalizationService(), "en", nav)
    backend.game_action.assert_not_awaited()
    backend.game_delivery.send.assert_awaited_once()


def sticker():
    return {
        "file_id": "sticker-id",
        "file_unique_id": "unique",
        "type": "regular",
        "width": 512,
        "height": 512,
        "is_animated": False,
        "is_video": False,
    }


@pytest.mark.parametrize(
    "media",
    [
        {"sticker": sticker()},
        {
            "video": {
                "file_id": "video-id",
                "file_unique_id": "v",
                "width": 640,
                "height": 480,
                "duration": 12,
            }
        },
        {"document": {"file_id": "doc-id", "file_unique_id": "d"}},
        {"contact": {"phone_number": "123", "first_name": "Person"}},
        {"location": {"latitude": 1, "longitude": 2}},
    ],
)
def test_media_is_copyable_without_downloads(media):
    assert copyable(message(**media))
    assert not copyable(message(**media, has_protected_content=True))


async def test_delivery_copies_media_tracks_ids_and_skips_after_exit():
    saved = {}
    valid = True

    async def request(action, **values):
        assert action == "telegram.chat.delivery"
        if "message_id" in values:
            saved[values["key"]] = {"ids": [values["message_id"]]}
        return {"skip": not valid, "messages": dict(saved)}

    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=102)),
    )
    protocol = SimpleNamespace(request=AsyncMock(side_effect=request))
    gameplay = SimpleNamespace(locks=defaultdict(asyncio.Lock))
    deliver = chat_delivery_handler(bot, LocalizationService(), protocol, gameplay)
    payload = {
        "recipient_telegram_user_id": 42,
        "source_chat_id": 43,
        "source_message_id": 7,
        "scope": "game",
        "scope_id": str(UUID(int=1)),
        "sender_name": "<Sender>",
        "locale": "en",
        "whisper": True,
        "text": None,
        "caption": "<caption>",
    }
    await deliver(payload)
    assert bot.send_message.await_args.args[1] == "<b>Whisper from &lt;Sender&gt;</b>:"
    bot.copy_message.assert_awaited_once_with(
        chat_id=42, from_chat_id=43, message_id=7, caption="<caption>", parse_mode=None
    )
    assert {mid for v in saved.values() for mid in v["ids"]} == {101, 102}
    await deliver(payload)
    assert bot.copy_message.await_count == 1
    valid = False
    payload["source_message_id"] = 8
    await deliver(payload)
    assert bot.copy_message.await_count == 1
