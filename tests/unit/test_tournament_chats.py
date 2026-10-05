from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message, User
from test_telegram_frontend import navigation

from sitg_bot.application.contracts import (
    ActionCode,
    ChatSendOperation,
    TournamentChatGameTimeOperation,
)
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.bot.callbacks import CallbackReferenceStore
from sitg_bot.bot.chat_delivery import (
    tournament_chat_round_label,
    tournament_chat_system_text,
)
from sitg_bot.bot.handlers.chat import chat_scope
from sitg_bot.bot.handlers.player import notification_text
from sitg_bot.bot.handlers.tournament_chat import send_tournament_chat_list
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import PLAYER_TOURNAMENT_ACTIONS, PLAYER_TOURNAMENT_OTHER_ACTIONS
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import RemoveKeyboardModel
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.tournament_chats import seat_player_ids

TOURNAMENT = {
    "id": str(UUID(int=3)),
    "name": "Cup",
    "slug": "cup",
    "status": "active",
    "capability_actions": [],
}


def chat_navigation(**changes):
    values = {
        "context": "chat",
        "selected_player_tournament": TOURNAMENT,
        "active_chat": {
            "id": str(UUID(int=5)),
            "tournament_id": str(UUID(int=3)),
            "tournament_name": "Cup",
            "round_number": 2,
            "match_number": 1,
            "multiple_matches": False,
        },
    }
    values.update(changes)
    return navigation(**values)


def message():
    return Message(
        message_id=321,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Player"),
    )


def flattened(actions):
    return {action for row in actions for action in row}


def test_player_tournament_keyboard_replaces_link_and_leaders_with_packets():
    actions = flattened(PLAYER_TOURNAMENT_ACTIONS)
    assert "tournament.registration_link" not in actions
    assert "player.tournament.leaders" not in actions
    assert "player.tournament.chats" not in actions
    assert "player.tournament.packets" in actions
    other = flattened(PLAYER_TOURNAMENT_OTHER_ACTIONS)
    assert "player.tournament.chats" in other
    assert "player.tournament.packet_upload" in other


def test_player_tournament_allowed_actions_drop_registration_link():
    actions = TelegramNavigationService._allowed_actions(
        active_mode="player",
        context="tournament",
        available_modes=("player",),
        active_lobby=None,
        active_game=None,
        selected_player=None,
    )
    assert "player.tournament.chats" in actions
    assert "tournament.registration_link" not in actions


def test_manager_tournament_allowed_actions_keep_registration_link():
    actions = TelegramNavigationService._allowed_actions(
        active_mode="manager",
        context="tournament",
        available_modes=("player", "manager"),
        active_lobby=None,
        active_game=None,
        selected_player=None,
    )
    assert "tournament.registration_link" in actions


def test_chat_context_exposes_no_keyboard_actions():
    actions = TelegramNavigationService._allowed_actions(
        active_mode="player",
        context="chat",
        available_modes=("player",),
        active_lobby=None,
        active_game=None,
        selected_player=None,
    )
    assert set(actions) == {"start", "menu", "help", "language"}


def test_chat_scope_prefers_the_active_tournament_chat():
    scope = chat_scope(chat_navigation())
    assert scope == {"scope": "tournament_chat", "scope_id": UUID(int=5)}


def test_menu_message_in_chat_context_removes_the_keyboard():
    model = menu_message(chat_navigation(), LocalizationService(), "en")
    assert isinstance(model.keyboard, RemoveKeyboardModel)
    assert "Cup" in model.text



def test_tournament_chat_operations_are_typed_idempotent_mutations():
    operation = TournamentChatGameTimeOperation(
        action=ActionCode.TOURNAMENT_CHAT_GAME_TIME,
        chat_id=UUID(int=1),
        planned_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    assert operation.planned_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_CHAT_GAME_TIME].idempotency_required
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_CHAT_OPEN].idempotency_required
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_CHAT_QUIT].idempotency_required
    send = ChatSendOperation(
        action=ActionCode.CHAT_SEND,
        scope="tournament_chat",
        scope_id=UUID(int=2),
        message_id=5,
        text="Hello",
    )
    assert send.scope == "tournament_chat"


@pytest.mark.parametrize("multiple", [True, False])
def test_tournament_chat_system_text_formats_game_time(multiple):
    localization = LocalizationService()
    payload = {
        "system": {
            "event": "game_time_set",
            "planned_at": "2026-10-01T12:00:00+00:00",
            "actor_name": "Ann",
        },
        "round_number": 2,
        "match_number": 3,
        "multiple_matches": multiple,
        "tournament_name": "Cup",
    }
    text = tournament_chat_system_text(payload, localization, "en")
    expected_round = tournament_chat_round_label(payload, localization, "en")
    assert text.startswith(
        f"Game for {expected_round} in tournament Cup was planned to 2026-10-01 at 12:00."
    )


def test_tournament_chat_new_messages_notification_text():
    localization = LocalizationService()
    payload = {
        "round_number": 2,
        "match_number": 1,
        "multiple_matches": False,
        "tournament_name": "Cup",
    }
    text = localization.text(
        "tournament_chat.new_messages",
        "en",
        round=tournament_chat_round_label(payload, localization, "en"),
        tournament="Cup",
    )
    assert text == "You have new messages in the chat for Round 2 in tournament Cup."


def test_game_reminder_notification_text():
    text = notification_text(
        "tournament_chat.game_reminder",
        {
            "planned_at": "2026-10-01T12:00:00+00:00",
            "round_number": 2,
            "match_number": 1,
            "multiple_matches": False,
            "tournament_name": "Cup",
        },
        LocalizationService(),
        "en",
    )
    assert "Round 2" in text
    assert "2026-10-01" in text
    assert "12:00" in text


def test_seat_player_ids_skip_chair_and_invalid_seats():
    match = SimpleNamespace(
        seats=[str(UUID(int=1)), "chair:alpha", "not-a-uuid", 5, None]
    )
    assert seat_player_ids(match) == [UUID(int=1)]


async def test_chat_list_sends_one_room_message_per_room(monkeypatch):
    backend = SimpleNamespace(
        tournament_chat_list=AsyncMock(
            return_value={
                "rooms": [
                    {
                        "chat_id": str(UUID(int=7)),
                        "tournament_id": str(UUID(int=3)),
                        "tournament_name": "Cup",
                        "round_number": 2,
                        "match_number": 1,
                        "multiple_matches": False,
                        "planned_at": None,
                        "planned_by_id": None,
                        "participants": [
                            {"player_id": str(UUID(int=1)), "nickname": "Player"},
                            {"player_id": str(UUID(int=2)), "nickname": "Other"},
                        ],
                        "unread": True,
                    }
                ]
            }
        )
    )
    msg = message()
    monkeypatch.setattr(Message, "answer", AsyncMock())
    await send_tournament_chat_list(
        msg,
        backend=backend,
        claim=object(),
        localization=LocalizationService(),
        locale="en",
        navigation=chat_navigation(context="tournament", active_chat=None),
        callback_references=CallbackReferenceStore(),
    )
    assert Message.answer.await_count == 2
    rendered = " ".join(
        str(call.args) + str(call.kwargs) for call in Message.answer.await_args_list
    )
    assert "Round 2" in rendered
    assert "Open chat" in rendered
    assert "Player, Other" in rendered
    assert "new messages" in rendered


async def test_chat_list_without_rooms_answers_once(monkeypatch):
    backend = SimpleNamespace(
        tournament_chat_list=AsyncMock(return_value={"rooms": []})
    )
    msg = message()
    monkeypatch.setattr(Message, "answer", AsyncMock())
    await send_tournament_chat_list(
        msg,
        backend=backend,
        claim=object(),
        localization=LocalizationService(),
        locale="en",
        navigation=chat_navigation(context="tournament", active_chat=None),
        callback_references=CallbackReferenceStore(),
    )
    assert Message.answer.await_count == 1
    assert "no chats" in str(Message.answer.await_args.args).lower()
