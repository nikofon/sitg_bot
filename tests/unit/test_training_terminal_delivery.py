"""Exit, delayed delivery and retry contracts for the shared game presentation ledger."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import DeleteMessage
from training_delivery_support import RUN, deliver, delivery_case

from sitg_bot.application.protocol import RetryableDeliveryError
from sitg_bot.services.training import TrainingService
from sitg_bot.storage.models import TrainingMemberRecord


async def test_completion_keeps_question_and_separate_answer_then_results_without_play_controls():
    case = delivery_case()
    await deliver(case)
    case.event("question_completed", text="Complete question", answer="Answer",
               commentary="Commentary", author="Author")
    case.event("game_completed")
    case.state["session"].update(status="completed", phase="revealed")
    await deliver(case)
    assert any("Answer" in text and "Commentary" in text for _, text, _ in case.sends)
    assert "Score: 10" in case.sends[-1][1]
    assert case.sends[-1][2].remove_keyboard is True
    before = list(case.sends)
    await deliver(case, force=True)
    assert case.sends == before


@pytest.mark.parametrize("destination", ["normal_game", "normal_lobby", "manager", "new_training"])
async def test_delayed_completed_events_do_not_replace_new_navigation_keyboard(destination):
    case = delivery_case(status="completed", phase="revealed")
    case.state["show_menu"] = False  # authoritative navigation, regardless of destination
    case.event("game_completed")
    await deliver(case, force=True)
    assert not case.sends


@pytest.mark.parametrize("eligible", [True, False])
async def test_stopped_delivery_restores_home_once_only_when_navigation_is_still_eligible(eligible):
    case = delivery_case()
    case.state.update(status="stopped", session=None, show_menu=eligible)
    case.state["presentation"]["dismissed"] = True
    await deliver(case)
    if eligible:
        assert len(case.sends) == 1
        assert case.sends[0][2].keyboard[0][0].text == "Start training"
        assert case.state["presentation"]["menu_restored"] is True
    else:
        assert not case.sends
    await deliver(case, force=True)
    assert len(case.sends) == int(eligible)


async def test_shared_appeal_delivery_delay_survives_retry_and_presenter_restart(monkeypatch):
    from sitg_bot.bot.i18n import LocalizationService
    from sitg_bot.bot.training_delivery import TrainingDelivery

    case = delivery_case()
    await deliver(case)
    monkeypatch.setattr("sitg_bot.bot.game_delivery.time.time", lambda: 100)
    case.event("appeal_resolved", approved=True)
    case.event("question_cost_announced", theme="Theme", value=20)
    with pytest.raises(RetryableDeliveryError, match="Appeal message delay"):
        await deliver(case)
    assert case.state["presentation"]["sequence"] == 4
    sent = list(case.sends)
    case.delivery = TrainingDelivery(case.bot, LocalizationService(),
                                     SimpleNamespace(request=case.request))
    with pytest.raises(RetryableDeliveryError, match="Appeal message delay"):
        await deliver(case)
    assert case.sends == sent
    monkeypatch.setattr("sitg_bot.bot.game_delivery.time.time", lambda: 104)
    await deliver(case)
    assert case.state["presentation"]["sequence"] == 5
    assert len(case.sends) == len(sent) + 1


async def test_cleanup_uses_captured_ids_and_persists_each_success_across_flood_retry():
    case = delivery_case()
    await deliver(case)
    captured = sorted(case.messages)
    case.state["presentation"]["dismissed"] = True
    # A newer session's message must not join this old session's deletion set.
    case.messages[999] = ("new run", None)
    delete = case.bot.delete_message.side_effect
    failed = False

    async def flood(chat, mid):
        nonlocal failed
        if mid == captured[1] and not failed:
            failed = True
            raise TelegramRetryAfter(method=DeleteMessage(chat_id=chat, message_id=mid),
                                     message="flood", retry_after=2)
        await delete(chat, mid)

    case.bot.delete_message.side_effect = flood
    payload = {**case.payload, "message_ids": captured}
    with pytest.raises(RetryableDeliveryError):
        await case.delivery.cleanup(payload)
    await case.delivery.cleanup(payload)
    assert case.deleted == captured  # first success was not retried
    assert case.messages == {999: ("new run", None)}
    assert case.state["presentation"]["messages"]["_deleted"]["ids"] == captured
    await deliver(case)
    assert case.messages == {999: ("new run", None)}


async def test_cleanup_treats_telegram_deletion_limit_as_terminal_for_only_that_id():
    case = delivery_case()
    await deliver(case)
    captured = sorted(case.messages)
    delete = case.bot.delete_message.side_effect

    async def limited(chat, mid):
        if mid == captured[0]:
            raise TelegramBadRequest(method=DeleteMessage(chat_id=chat, message_id=mid),
                                     message="message can't be deleted")
        await delete(chat, mid)

    case.bot.delete_message.side_effect = limited
    await case.delivery.cleanup({**case.payload, "message_ids": captured})
    assert case.deleted == captured[1:]
    assert case.state["presentation"]["messages"]["_deleted"]["ids"] == captured


async def test_stop_captures_controls_questions_inputs_and_legacy_ids(monkeypatch):
    from test_training_gameflow import OWNER as OWNER_ID
    from test_training_gameflow import RUN as RUN_ID
    from test_training_gameflow import harness

    service, record, _, _, _, session = harness()
    member = SimpleNamespace(presentation={"ids": [5], "messages": {
        "start": {"ids": [10]}, "question:q:0": {"ids": [11]}, "input:12": {"ids": [12]},
    }})
    session.get.return_value = member
    enqueue = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.training.TransactionalOutbox.enqueue", enqueue)
    await service._dismiss(session, record)
    assert member.presentation["dismissed"] is True
    payload = enqueue.await_args.kwargs["payload"]
    assert payload == {"session_id": str(RUN_ID), "owner_id": str(OWNER_ID),
                       "message_ids": [5, 10, 11, 12]}
    await service._dismiss(session, record)
    assert enqueue.await_count == 1


async def test_record_racing_dismissal_preserves_dismissal_and_enqueues_late_id(monkeypatch):
    from test_training_gameflow import OWNER as OWNER_ID
    from test_training_gameflow import RUN as RUN_ID
    from test_training_gameflow import harness

    service, record, _, _, _, session = harness()
    member = SimpleNamespace(presentation={"dismissed": True, "sequence": 8, "messages": {}})
    player = SimpleNamespace()
    session.get.side_effect = lambda model, key: member if model is TrainingMemberRecord else player
    enqueue = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.training.TransactionalOutbox.enqueue", enqueue)
    await service.delivery(OWNER_ID, RUN_ID, key="question:old", value={"ids": [77]})
    assert member.presentation["dismissed"] is True
    assert enqueue.await_args.kwargs["payload"]["message_ids"] == [77]
    await service.delivery(OWNER_ID, RUN_ID, sequence=2)
    assert member.presentation["sequence"] == 8


async def test_finished_closure_selects_training_terminal_cutoff_and_dismisses(monkeypatch):
    from test_training_gameflow import NOW

    class Rows:
        def scalars(self):
            return [SimpleNamespace(id=RUN)]

    session = SimpleNamespace(execute=AsyncMock(return_value=Rows()))

    @asynccontextmanager
    async def transaction():
        yield session

    service = TrainingService(SimpleNamespace(transaction=transaction))
    service._dismiss = AsyncMock()
    await service.close_finished_sessions(now=NOW)
    sql = session.execute.await_args.args[0]
    params = sql.compile().params
    assert NOW - timedelta(hours=12) in params.values()
    service._dismiss.assert_awaited_once()
