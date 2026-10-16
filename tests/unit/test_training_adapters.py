import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError
from test_miniapp_http import FakeAuth, FakeGateway
from test_telegram_frontend import navigation

from sitg_bot.application.contracts import ActionCode
from sitg_bot.bot.handlers.training import handle_training_action
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.training_delivery import TrainingDelivery
from sitg_bot.miniapp_http import MiniAppHttpServer


def test_training_menu_recovers_even_with_a_finished_normal_game_view():
    state = navigation(
        active_mode="training", context="training", active_training_id=UUID(int=7),
        allowed_actions=("training.train", "training.upload"),
        active_game={"id": UUID(int=8), "tournament_id": UUID(int=9),
                     "status": "finalized", "phase": "completed", "version": 1,
                     "joined": True, "active": True, "can_reconnect": False},
    )
    model = menu_message(state, LocalizationService(), "en")
    assert model.text == "Training"
    assert ("Train",) in model.keyboard.rows


async def test_training_http_requires_authenticated_csrf_mutation_and_rejects_actor_override():
    class RecordingAuth(FakeAuth):
        async def authorize_request(self, session_token, **values):
            assert values["csrf_token"] == "csrf"
            assert values["idempotency_key"] == "training-request"
            assert values["method"] == "POST"
            return await super().authorize_request(session_token, **values)

    gateway = FakeGateway()
    http = MiniAppHttpServer(RecordingAuth(), gateway)
    request = make_mocked_request(
        "POST",
        "/api/miniapp/training/start",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "training-request",
        },
        match_info={"command": "start"},
    )
    request._read_bytes = json.dumps({"version_id": str(UUID(int=1)), "confirm": False}).encode()
    assert (await http._training_mutation(request)).status == 200
    assert gateway.requests[0].operation.action == ActionCode.TRAINING_START
    request._read_bytes = json.dumps(
        {"version_id": str(UUID(int=1)), "player_id": str(UUID(int=9))}
    ).encode()
    with pytest.raises(ValidationError):
        await http._training_mutation(request)


async def test_training_callback_keeps_encoded_version_and_uses_authorized_question():
    state = {"question_revision_id": str(UUID(int=2))}
    backend = SimpleNamespace(
        _execute=AsyncMock(return_value=SimpleNamespace(data={"session": state}))
    )
    callback = SimpleNamespace(data=f"tr:{UUID(int=1).hex}:7:continue", answer=AsyncMock())
    claim = object()
    await handle_training_action(callback, backend, claim, LocalizationService(), "en")
    operation = backend._execute.await_args_list[-1].args[1]
    assert operation.expected_version == 7
    assert operation.question_revision_id == UUID(int=2)
    assert operation.command == "continue"
    assert backend._execute.await_args.args[0] is claim


async def test_telegram_best_buzz_escapes_text_and_attaches_inline_continue_to_question():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_message_text=AsyncMock(),
    )
    state = {
        "id": str(UUID(int=1)),
        "version": 4,
        "status": "active",
        "phase": "best_buzz",
        "question_index": 0,
        "theme": "<theme>",
        "value": 10,
        "hidden": False,
        "text": "<question> & text",
    }
    protocol = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "telegram_user_id": 42,
                "locale": "en",
                "presentation": {},
                "session": state,
            }
        )
    )
    delivery = TrainingDelivery(bot, LocalizationService(), protocol)
    await delivery({"owner_id": str(UUID(int=3)), "session_id": str(UUID(int=1))})
    args = bot.send_message.await_args
    assert "&lt;question&gt; &amp; text" in args.args[1]
    buttons = args.kwargs["reply_markup"].inline_keyboard
    continuation = next(
        button for row in buttons for button in row if button.text == "Continue reading"
    )
    assert continuation.callback_data == f"tr:{UUID(int=1).hex}:4:continue"
    assert len(continuation.callback_data.encode()) <= 64


async def test_hidden_telegram_recovery_clears_every_persisted_question_chunk():
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
    state = {
        "id": str(UUID(int=1)),
        "version": 5,
        "status": "active",
        "phase": "waiting",
        "question_index": 0,
        "theme": "Theme",
        "value": 10,
        "hidden": True,
        "text": "",
    }
    protocol = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "telegram_user_id": 42,
                "locale": "en",
                "presentation": {"ids": [10, 11, 12]},
                "session": state,
            }
        )
    )
    await TrainingDelivery(bot, LocalizationService(), protocol)(
        {
            "owner_id": str(UUID(int=3)),
            "session_id": str(UUID(int=1)),
        }
    )
    assert bot.edit_message_text.await_count == 3
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        10,
        11,
        12,
    ]
    assert all(call.args[0] == "…" for call in bot.edit_message_text.await_args_list[1:])
    bot.send_message.assert_not_awaited()
