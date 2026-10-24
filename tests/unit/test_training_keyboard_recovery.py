from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message, ReplyKeyboardRemove
from test_telegram_frontend import navigation

from sitg_bot.bot.handlers.navigation import router as navigation_router
from sitg_bot.bot.handlers.training import router as training_router
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.training_delivery import TrainingDelivery


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("voting", [True, False])
@pytest.mark.parametrize("entry", ["menu", "text", "mode_denied", "cancel"])
async def test_recovery_preserves_phase_owned_training_controls(monkeypatch, locale, voting, entry):
    owner, run, question = (UUID(int=index) for index in range(1, 4))
    loc = LocalizationService()
    current_keyboard = None

    async def sent(*args, **kwargs):
        nonlocal current_keyboard
        markup = kwargs.get("reply_markup")
        if markup is not None:
            current_keyboard = None if isinstance(markup, ReplyKeyboardRemove) else markup
        return SimpleNamespace(message_id=10)

    monkeypatch.setattr(Message, "answer", sent)
    state = {
        "telegram_user_id": 42, "locale": locale, "presentation": {},
        "session": {
            "id": str(run), "question_revision_id": str(question), "version": 1,
            "status": "paused" if voting else "active", "phase": "revealed",
            "question_index": 0, "theme": "Theme", "value": 10, "hidden": False,
            "text": "Question", "answer": "Answer", "commentary": "Comment",
            "score": 10, "appeal_status": "voting" if voting else None,
        },
    }

    async def request(action, **params):
        if action.endswith(".record"):
            state["presentation"] = params["presentation"]
            return {"recorded": True}
        return state

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=sent), edit_message_text=AsyncMock())
    delivery = TrainingDelivery(bot, loc, SimpleNamespace(request=request))
    payload = {"owner_id": str(owner), "session_id": str(run)}
    await delivery(payload)
    original_keyboard = current_keyboard
    assert original_keyboard is not None
    nav = navigation(
        active_mode="training", context="training", active_training_id=run, allowed_actions=[],
    )
    backend = SimpleNamespace(
        training_delivery=delivery, navigation=AsyncMock(return_value=nav),
        _execute=AsyncMock(return_value=SimpleNamespace(data={"session": state["session"]})),
    )
    text = {
        "menu": "/menu", "text": "ordinary text", "mode_denied": "Change mode", "cancel": "/cancel",
    }[entry]
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text=text,
    )
    await (training_router if entry == "text" else navigation_router).propagate_event(
        "message", message, bot=bot, backend=backend, telegram_update_claim=object(),
        localization=loc, locale=locale, navigation=nav,
        state=SimpleNamespace(clear=AsyncMock(), get_state=AsyncMock(return_value=None)),
    )
    assert current_keyboard == original_keyboard
    labels = [button.text for row in current_keyboard.keyboard for button in row]
    assert (loc.text("button.yes", locale) in labels) is voting
    assert (loc.text("button.no", locale) in labels) is voting
    if entry in {"menu", "text"}:
        assert bot.send_message.await_count == 3
    await delivery(payload)
    count_before = bot.send_message.await_count
    await delivery(payload)
    assert bot.send_message.await_count == count_before


@pytest.mark.parametrize("voting", [True, False])
async def test_forced_delivery_reinstalls_controls_after_external_keyboard_replacement(voting):
    loc = LocalizationService()
    state = {
        "telegram_user_id": 42, "locale": "en", "presentation": {},
        "session": {
            "id": str(UUID(int=1)), "version": 1, "status": "paused" if voting else "active",
            "phase": "revealed", "question_index": 0, "theme": "Theme", "value": 10,
            "hidden": False, "text": "Question", "appeal_status": "voting" if voting else None,
        },
    }

    async def request(action, **params):
        if action.endswith(".record"):
            state["presentation"] = params["presentation"]
        return state

    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_message_text=AsyncMock(),
    )
    delivery = TrainingDelivery(bot, loc, SimpleNamespace(request=request))
    payload = {"owner_id": str(UUID(int=2)), "session_id": str(UUID(int=1))}
    await delivery(payload)
    await delivery({**payload, "force": True})
    assert bot.send_message.await_count == 3
    labels = [button.text for row in bot.send_message.await_args.kwargs["reply_markup"].keyboard
              for button in row]
    assert ("Yes" in labels) is voting
    await delivery(payload)
    assert bot.send_message.await_count == 3
