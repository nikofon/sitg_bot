from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message, ReplyKeyboardRemove
from test_telegram_frontend import navigation
from training_delivery_support import RUN, deliver, delivery_case

from sitg_bot.bot.handlers.navigation import router as navigation_router
from sitg_bot.bot.handlers.training import router as training_router
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import training_game_keyboard


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("voting", [True, False])
@pytest.mark.parametrize("entry", ["menu", "text", "mode_denied", "cancel"])
async def test_recovery_preserves_phase_owned_training_controls(monkeypatch, locale, voting, entry):
    case = delivery_case(locale=locale, phase="revealed",
                         status="paused" if voting else "active", voting=voting)
    loc = LocalizationService()
    current_keyboard = None
    send = case.bot.send_message.side_effect

    async def sent(*args, **kwargs):
        nonlocal current_keyboard
        markup = kwargs.get("reply_markup")
        if markup is not None:
            current_keyboard = None if isinstance(markup, ReplyKeyboardRemove) else markup
        return await send(*args, **kwargs)

    monkeypatch.setattr(Message, "answer", sent)
    case.bot.send_message.side_effect = sent
    await deliver(case)
    original_keyboard = current_keyboard
    assert original_keyboard is not None
    nav = navigation(active_mode="training", context="training", active_training_id=UUID(RUN),
                     allowed_actions=[])
    backend = SimpleNamespace(
        training_delivery=case.delivery, navigation=AsyncMock(return_value=nav),
        _execute=AsyncMock(return_value=SimpleNamespace(data={"session": case.state["session"]})),
    )
    text = {"menu": "/menu", "text": "ordinary text", "mode_denied": "Change mode",
            "cancel": "/cancel"}[entry]
    message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"),
                      text=text)
    await (training_router if entry == "text" else navigation_router).propagate_event(
        "message", message, bot=case.bot, backend=backend, telegram_update_claim=object(),
        localization=loc, locale=locale, navigation=nav,
        state=SimpleNamespace(clear=AsyncMock(), get_state=AsyncMock(return_value=None)),
    )
    assert current_keyboard == original_keyboard
    labels = [button.text for row in current_keyboard.keyboard for button in row]
    assert labels == ["+", "||", "!"]
    count_before = len(case.sends)
    await deliver(case)
    assert len(case.sends) == count_before


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("voting", [True, False])
async def test_play_keyboard_is_exactly_three_buttons_including_during_ballot(locale, voting):
    case = delivery_case(locale=locale, phase="revealed", status="paused" if voting
                         else "active", voting=voting)
    loc = LocalizationService()
    await deliver(case)
    controls = [entry for entry in case.sends if entry[2] is not None][-1]
    markup = controls[2]
    labels = [button.text for row in markup.keyboard for button in row]
    if voting:
        assert controls[1] == loc.text("training.appeal_voting", locale)
    else:
        assert controls[1] == loc.text("training.controls", locale)
    assert labels == ["+", "||", "!"]
    assert training_game_keyboard(loc, locale).rows == (("+",), ("||", "!"))


@pytest.mark.parametrize("voting", [True, False])
async def test_forced_delivery_reinstalls_controls_after_external_keyboard_replacement(voting):
    case = delivery_case(voting=voting, status="paused" if voting else "active")
    await deliver(case)
    before = len(case.sends)
    await deliver(case, force=True)
    assert len(case.sends) == before + 1
    labels = [button.text for row in case.sends[-1][2].keyboard for button in row]
    assert labels == ["+", "||", "!"]
    await deliver(case)
    assert len(case.sends) == before + 1
