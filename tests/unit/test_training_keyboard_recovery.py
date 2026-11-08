from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from test_telegram_frontend import navigation
from training_delivery_support import RUN, deliver, delivery_case

from sitg_bot.application.protocol import RetryableDeliveryError
from sitg_bot.bot.handlers.navigation import router as navigation_router
from sitg_bot.bot.handlers.training import router as training_router
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import training_game_keyboard
from sitg_bot.bot.training_delivery import TrainingDelivery


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
        if isinstance(markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)):
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
    controls = [entry for entry in case.sends if isinstance(entry[2], ReplyKeyboardMarkup)][-1]
    markup = controls[2]
    labels = [button.text for row in markup.keyboard for button in row]
    assert controls[1] == loc.text("training.controls", locale)
    if voting:
        ballot = case.sends[-1]
        assert ballot[1] == loc.text("training.appeal_voting", locale)
        assert [button.text for button in ballot[2].inline_keyboard[0]] == [
            loc.text("button.yes", locale), loc.text("button.no", locale),
        ]
    assert labels == ["+", "||", "!"]
    assert training_game_keyboard(loc, locale).rows == (("+",), ("||", "!"))


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_forced_first_delivery_does_not_duplicate_opening_controls(locale):
    case = delivery_case(locale=locale, phase="intermission")
    case.state["events"] = case.state["events"][:1]
    await deliver(case, force=True)
    assert len(case.sends) == 1
    assert case.sends[0][1] == LocalizationService().text("training.controls", locale)
    await deliver(case)
    assert len(case.sends) == 1


@pytest.mark.parametrize("pending", [100, 101, 200, 201])
@pytest.mark.parametrize("opening_delivered", [True, False])
async def test_forced_recovery_sends_controls_once_across_all_event_pages(
    pending, opening_delivered,
):
    case = delivery_case(phase="intermission")
    case.state["events"] = case.state["events"][:1]
    if opening_delivered:
        await deliver(case)
    before = len(case.sends)
    for _ in range(pending - int(not opening_delivered)):
        case.event("question_cost_announced", theme="Theme", value=10)
    pages = []

    async def request(action, **params):
        result = await case.request(action, **params)
        if action == "telegram.training.delivery":
            pages.append(len(result["events"]))
        return result

    case.delivery.protocol.request = request
    await deliver(case, force=True)
    controls = [entry for entry in case.sends[before:]
                if isinstance(entry[2], ReplyKeyboardMarkup)]
    assert len(controls) == 1
    assert [button.text for row in controls[0][2].keyboard for button in row] == ["+", "||", "!"]
    assert pages == [min(pending, 100), *([100] * (pending // 100)), pending % 100]
    assert case.state["presentation"]["sequence"] == pending + int(opening_delivered)
    before = len(case.sends)
    await deliver(case)
    assert len(case.sends) == before


@pytest.mark.parametrize("failure", ["send", "controls_record", "cursor_record"])
async def test_opening_retry_reuses_receipt_and_keeps_event_cursor(failure):
    case = delivery_case(phase="intermission")
    case.state["events"] = case.state["events"][:1]
    original_send = case.bot.send_message.side_effect
    failed = False

    async def send(*args, **kwargs):
        nonlocal failed
        if failure == "send" and not failed:
            failed = True
            raise TelegramRetryAfter(method=SendMessage(chat_id=42, text="opening"),
                                     message="flood", retry_after=1)
        return await original_send(*args, **kwargs)

    async def request(action, **params):
        nonlocal failed
        if not failed and (
            failure == "controls_record" and "presentation" in params
            or failure == "cursor_record" and "sequence" in params
        ):
            failed = True
            raise ConnectionError("record unavailable")
        return await case.request(action, **params)

    case.bot.send_message.side_effect = send
    case.delivery.protocol.request = request
    with pytest.raises(RetryableDeliveryError if failure == "send" else ConnectionError):
        await deliver(case)
    case.delivery = TrainingDelivery(case.bot, LocalizationService(),
                                     SimpleNamespace(request=case.request))
    await deliver(case)
    assert len(case.sends) == 1
    assert case.state["presentation"]["sequence"] == 1
    assert case.state["presentation"]["controls"] == "play"


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("resolution", ["accepted", "rejected", "expired"])
async def test_ballot_buttons_are_durable_and_retired_after_resolution(locale, resolution):
    case = delivery_case(locale=locale)
    await deliver(case)
    case.state["session"].update(version=2, status="paused", phase="revealed",
                                  appeal_status="voting")
    await deliver(case)
    key = "ballot:2"
    receipt = case.state["presentation"]["messages"][key]
    mid = receipt["ids"][0]
    assert receipt["round_id"] == case.state["session"]["question_revision_id"]
    markup = case.messages[mid][1]
    assert [button.callback_data for button in markup.inline_keyboard[0]] == [
        f"tr:{UUID(RUN).hex}:2:yes", f"tr:{UUID(RUN).hex}:2:no",
    ]
    assert all(len(button.callback_data.encode()) <= 64 for button in markup.inline_keyboard[0])
    before = list(case.sends)
    case.delivery = TrainingDelivery(case.bot, LocalizationService(),
                                     SimpleNamespace(request=case.request))
    await deliver(case)
    assert case.sends == before
    case.state["session"].update(version=3, status="active",
                                  appeal_status="rejected" if resolution == "expired"
                                  else resolution)
    case.event("appeal_resolved", approved=resolution == "accepted")
    await deliver(case)
    assert case.messages[mid] == (
        LocalizationService().text("training.appeal_closed", locale), None,
    )
    # The result message keeps the persistent keyboard, never Yes/No reply rows.
    reply = case.sends[-1][2]
    assert [button.text for row in reply.keyboard for button in row] == ["+", "||", "!"]
    before_edits = list(case.edits)
    await deliver(case)
    assert case.edits == before_edits


async def test_new_ballot_retires_old_question_buttons_without_reusing_the_message():
    case = delivery_case(voting=True, phase="revealed", status="paused")
    await deliver(case)
    old_mid = case.state["presentation"]["messages"]["ballot:1"]["ids"][0]
    new_question = str(UUID(int=99))
    case.state["session"].update(version=9, question_revision_id=new_question)
    await deliver(case)
    new_receipt = case.state["presentation"]["messages"]["ballot:9"]
    assert new_receipt["round_id"] == new_question
    assert new_receipt["ids"] != [old_mid]
    assert case.messages[old_mid][1] is None
    assert case.messages[new_receipt["ids"][0]][1].inline_keyboard


async def test_ballot_retirement_retries_after_flood_without_resending_result():
    case = delivery_case(voting=True, phase="revealed", status="paused")
    await deliver(case)
    mid = case.state["presentation"]["messages"]["ballot:1"]["ids"][0]
    case.state["session"].update(version=2, appeal_status="accepted", status="active")
    case.event("appeal_resolved", approved=True)
    edit = case.bot.edit_message_text.side_effect
    case.bot.edit_message_text.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=42, text="closing"), message="flood", retry_after=1,
    )
    before = list(case.sends)
    with pytest.raises(RetryableDeliveryError):
        await deliver(case)
    assert case.sends == before
    case.bot.edit_message_text.side_effect = edit
    case.delivery = TrainingDelivery(case.bot, LocalizationService(),
                                     SimpleNamespace(request=case.request))
    await deliver(case)
    assert case.messages[mid][1] is None
    assert len(case.sends) == len(before) + 1
    await deliver(case)
    assert len(case.sends) == len(before) + 1


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
