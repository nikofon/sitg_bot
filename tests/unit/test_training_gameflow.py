"""Behavioral gameplay seams; storage acceptance lives in integration/test_training.py."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message

from sitg_bot.application.contracts import ActionCode
from sitg_bot.bot.handlers import training
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.training_delivery import TrainingDelivery, training_keyboard
from sitg_bot.domain.training import TrainingSettings, text_digest
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.training import TrainingService

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER, RUN, QUESTION = (UUID(int=i) for i in (1, 2, 3))


def harness(settings=None, *, phase="reading", status="active"):
    settings = settings or TrainingSettings(token_delay=1, token_target_chars=3)
    record = SimpleNamespace(
        id=RUN, owner_id=OWNER, packet_version_id=UUID(int=4), version=1,
        event_sequence=0, progression_stage=None,
        phase=phase, status=status, settings=settings.to_dict(), question_index=0,
        token_cursor=0, best_buzz_consumed=False, hidden_deadline=None, text_hidden=False,
        deadline=NOW + timedelta(seconds=1) if phase == "reading" else None,
        paused_at=None, paused_phase=None, paused_remaining=None, paused_hidden_remaining=None,
    )
    question = SimpleNamespace(
        value=10, theme_revision_id=UUID(int=5), opened_at=NOW, paused_seconds=0, buzzed_at=None,
        buzz_delay_seconds=None, buzz_token_cursor=None, best_buzz=None,
    )
    revision = SimpleNamespace(
        id=QUESTION, text="One two three four", answer="Paris", accepted_answers=[],
        rejected_answers=[], commentary="Commentary", form="City", author_ids=(),
    )
    attempt = SimpleNamespace(
        answer="London", correct=False, original_correct=False, outcome="answered",
        appeal_status=None, appealed_at=None, appeal_resolved_at=None, appeal_was_paused=False,
    )
    events = []
    session = SimpleNamespace(add=events.append, scalar=AsyncMock(),
                              get=AsyncMock(return_value=SimpleNamespace(presentation={})),
                              events=events)

    @asynccontextmanager
    async def transaction():
        yield session

    service = TrainingService(SimpleNamespace(transaction=transaction))
    service._owned = AsyncMock(return_value=record)
    service._question = AsyncMock(return_value=(question, revision, "Theme"))
    service._attempt = AsyncMock(return_value=attempt)
    service._notice = AsyncMock()
    service._authors = AsyncMock(return_value="—")
    service._view = AsyncMock(return_value={"session": {"id": str(RUN)}})
    service.packets.authorize = AsyncMock()
    return service, record, question, revision, attempt, session


async def action(service, record, command, *, now=NOW, **values):
    return await service.act(
        OWNER, RUN, command, expected_version=record.version,
        question_revision_id=QUESTION, now=now, **values,
    )


async def test_accepted_buzz_catches_up_reading_and_records_actual_event_not_answer_time():
    service, record, question, _, _, _ = harness()
    await action(service, record, "buzz", now=NOW + timedelta(seconds=2.5))
    assert record.phase == "answering"
    assert question.buzzed_at == NOW + timedelta(seconds=2.5)
    assert question.buzz_delay_seconds == 2.5
    assert question.buzz_token_cursor == 1
    assert record.deadline == NOW + timedelta(seconds=17.5)
    await action(service, record, "stop", now=NOW + timedelta(seconds=3))
    assert question.buzzed_at == NOW + timedelta(seconds=2.5)
    assert record.status == "stopped"
    # Source authorization is not needed for stop (including retired packets).
    assert service.packets.authorize.await_count == 1


async def test_pause_freezes_reading_and_excludes_pause_from_accepted_buzz_delay():
    service, record, question, _, _, _ = harness()
    await action(service, record, "pause", now=NOW + timedelta(seconds=.25))
    assert record.deadline is None and record.status == "paused"
    await service._advance(None, record, NOW + timedelta(seconds=100))
    assert record.token_cursor == 0
    await action(service, record, "resume", now=NOW + timedelta(seconds=100.25))
    assert record.deadline == NOW + timedelta(seconds=101)
    assert question.paused_seconds == 100
    await action(service, record, "buzz", now=NOW + timedelta(seconds=101.5))
    assert question.buzz_delay_seconds == 1.5
    assert question.buzz_token_cursor == 1


async def test_pause_freezes_hidden_deadline_and_expired_hidden_stays_hidden():
    service, record, question, _, _, session = harness(TrainingSettings(hidden_after=2))
    await service._begin_question(session, record, NOW)
    await action(service, record, "pause", now=NOW + timedelta(seconds=1))
    assert record.hidden_deadline is None
    await service._advance(session, record, NOW + timedelta(seconds=30))
    assert not record.text_hidden
    await action(service, record, "resume", now=NOW + timedelta(seconds=31))
    assert record.hidden_deadline == NOW + timedelta(seconds=32)
    assert record.deadline == NOW + timedelta(seconds=40)
    await service._advance(session, record, NOW + timedelta(seconds=32))
    assert record.text_hidden
    await action(service, record, "pause", now=NOW + timedelta(seconds=33))
    assert record.text_hidden
    assert question.paused_seconds == 30


async def test_best_buzz_continue_resumes_configured_reading_not_instant_dump():
    settings = TrainingSettings(best_buzz=True, token_delay=2, token_target_chars=3)
    service, record, question, revision, _, session = harness(settings)
    question.best_buzz = {
        "schema_version": 1, "tokenizer_version": "announcement-v1",
        "question_revision_id": str(QUESTION), "text_digest": text_digest(revision.text),
        "source_end": 3,
    }
    await service._advance(session, record, NOW + timedelta(seconds=2))
    assert record.phase == "best_buzz" and record.token_cursor == 1
    await action(service, record, "continue", now=NOW + timedelta(seconds=30))
    assert record.phase == "reading" and record.token_cursor == 1
    assert record.deadline == NOW + timedelta(seconds=32)
    await service._advance(session, record, NOW + timedelta(seconds=31.9))
    assert record.token_cursor == 1
    await service._advance(session, record, NOW + timedelta(seconds=32))
    assert record.token_cursor == 2 and record.phase == "reading"
    with pytest.raises(ValueError):
        await action(service, record, "continue", now=NOW + timedelta(seconds=32))


@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("paused", [True, False])
async def test_solo_ballot_preserves_original_verdict_and_prior_pause(approve, paused):
    service, record, _, _, attempt, _ = harness(
        phase="revealed", status="paused" if paused else "active",
    )
    if paused:
        record.paused_at = NOW - timedelta(seconds=1)
    await action(service, record, "appeal")
    assert record.status == "paused" and attempt.appeal_status == "voting"
    assert record.deadline == NOW + timedelta(seconds=60)
    with pytest.raises(ValueError, match="Resolve the appeal"):
        await action(service, record, "resume")
    await action(service, record, "vote", approve=approve, now=NOW + timedelta(seconds=1))
    assert attempt.appeal_status == ("accepted" if approve else "rejected")
    assert attempt.correct is approve and attempt.original_correct is False
    assert record.status == ("paused" if paused else "active")
    assert record.deadline is None
    with pytest.raises(ValueError, match="no answer to appeal"):
        await action(service, record, "appeal", now=NOW + timedelta(seconds=2))


async def test_ballot_timeout_rejects_without_changing_score_verdict():
    service, record, _, _, attempt, session = harness(phase="revealed")
    await action(service, record, "appeal")
    await service._advance(session, record, NOW + timedelta(seconds=60))
    assert attempt.appeal_status == "rejected" and attempt.correct is False
    assert attempt.appeal_resolved_at == NOW + timedelta(seconds=60)
    assert record.status == "active" and record.deadline is None


async def test_stale_or_foreign_question_cannot_double_buzz():
    service, record, question, _, _, _ = harness()
    await action(service, record, "buzz")
    with pytest.raises(StaleWriteError):
        await service.act(OWNER, RUN, "buzz", expected_version=1,
                          question_revision_id=QUESTION, now=NOW)
    with pytest.raises(StaleWriteError):
        await service.act(OWNER, RUN, "buzz", expected_version=record.version,
                          question_revision_id=UUID(int=99), now=NOW)
    assert question.buzzed_at == NOW and record.version == 2


@pytest.mark.parametrize("phase", ["reading", "waiting", "answering", "revealed"])
@pytest.mark.parametrize("locale", ["en", "ru"])
def test_only_best_buzz_pause_has_inline_question_control(phase, locale):
    state = {"id": str(RUN), "version": 1, "status": "active", "phase": phase}
    assert training_keyboard(state, LocalizationService(), locale) is None
    state["phase"] = "best_buzz"
    buttons = training_keyboard(state, LocalizationService(), locale).inline_keyboard
    assert len(buttons) == 1 and len(buttons[0]) == 1
    assert buttons[0][0].callback_data.endswith(":continue")
    state["status"] = "paused"
    assert training_keyboard(state, LocalizationService(), locale) is None


def message(text):
    return Message(message_id=10, date=NOW, chat=Chat(id=42, type="private"), text=text)


@pytest.mark.parametrize("text,command", [("+", "buzz"), ("||", "pause"), ("!", "appeal"),
                                           ("/pause", "pause"), ("/resume", "resume"),
                                           ("Next question", "next"), ("Пропустить", "skip")])
async def test_registered_keyboard_controls_are_actions_not_answer_text(text, command):
    state = {"id": str(RUN), "version": 7, "question_revision_id": str(QUESTION),
             "presented_question_revision_id": str(QUESTION), "status": "active",
             "phase": "answering"}
    backend = SimpleNamespace(_execute=AsyncMock(
        return_value=SimpleNamespace(data={"session": state}),
    ))
    await training.router.propagate_event(
        "message", message(text), backend=backend, telegram_update_claim=object(),
        localization=LocalizationService(), locale="en",
        navigation=SimpleNamespace(active_mode="training", allowed_actions=()),
    )
    operation = backend._execute.await_args.args[1]
    assert operation.action == ActionCode.TRAINING_ACT and operation.command == command
    assert operation.answer is None


async def test_registered_quit_dismisses_completed_training_and_restores_menu(monkeypatch):
    menu = AsyncMock()
    monkeypatch.setattr(training, "show_menu", menu)
    state = {
        "id": str(RUN), "version": 7, "question_revision_id": str(QUESTION),
        "status": "completed", "phase": "revealed",
    }
    backend = SimpleNamespace(_execute=AsyncMock(
        return_value=SimpleNamespace(data={"session": state}),
    ))
    await training.router.propagate_event(
        "message", message("/quit"), backend=backend, telegram_update_claim=object(),
        localization=LocalizationService(), locale="en",
        navigation=SimpleNamespace(active_mode="training", allowed_actions=()),
    )
    assert backend._execute.await_args.args[1].command == "stop"
    menu.assert_awaited_once()


async def test_registered_score_command_reads_finished_score_without_mutation(monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    backend = SimpleNamespace(_execute=AsyncMock(return_value=SimpleNamespace(
        data={"session": {"score": -20}},
    )))
    await training.router.propagate_event(
        "message", message("/score"), backend=backend, telegram_update_claim=object(),
        localization=LocalizationService(), locale="en",
        navigation=SimpleNamespace(active_mode="training", allowed_actions=()),
    )
    assert backend._execute.await_count == 1
    assert backend._execute.await_args.args[1].score_only is True
    answer.assert_awaited_once_with("Score: -20")


async def test_score_only_current_returns_metadata_without_loading_retired_question(monkeypatch):
    monkeypatch.setattr("sitg_bot.services.training.require_trainee", AsyncMock())
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[RUN, -20]),
        get=AsyncMock(side_effect=[SimpleNamespace(training_lobby=None),
                                  SimpleNamespace(id=RUN, status="stopped")]),
    )

    @asynccontextmanager
    async def sessions():
        yield session

    service = TrainingService(SimpleNamespace(sessions=sessions))
    service.tick = AsyncMock()
    service.view = AsyncMock(side_effect=PermissionError("Source retired"))
    result = await service.current(OWNER, score_only=True)
    assert result == {"session": {"id": str(RUN), "status": "stopped", "score": -20}}
    service.view.assert_not_awaited()


async def test_bare_buzz_is_bound_to_delivered_question_not_latest_server_question():
    old = UUID(int=99)
    state = {"id": str(RUN), "version": 7, "question_revision_id": str(QUESTION),
             "presented_question_revision_id": str(old), "status": "active", "phase": "reading"}
    backend = SimpleNamespace(_execute=AsyncMock(
        return_value=SimpleNamespace(data={"session": state}),
    ))
    await training.handle_training_control(message("+"), backend, object(),
                                            LocalizationService(), "en", "buzz")
    assert backend._execute.await_args.args[1].question_revision_id == old


@pytest.mark.parametrize("text", ["Paris", "Yes"])
async def test_old_answer_prompt_cannot_submit_text_to_a_later_question(monkeypatch, text):
    monkeypatch.setattr(Message, "answer", AsyncMock())
    state = {
        "id": str(RUN), "version": 7, "question_revision_id": str(QUESTION),
        "status": "active", "phase": "answering", "answer_prompts": {"99": str(UUID(int=99))},
    }
    backend = SimpleNamespace(_execute=AsyncMock(
        return_value=SimpleNamespace(data={"session": state}),
    ))
    incoming = message(text).model_copy(update={
        "reply_to_message": message("Old form").model_copy(update={"message_id": 99}),
    })
    await training.router.propagate_event(
        "message", incoming, backend=backend, telegram_update_claim=object(),
        localization=LocalizationService(), locale="en",
        navigation=SimpleNamespace(active_mode="training", allowed_actions=()),
    )
    assert backend._execute.await_count == 1


async def test_yes_is_still_answer_text_when_not_voting():
    state = {"id": str(RUN), "version": 7, "question_revision_id": str(QUESTION),
             "status": "active", "phase": "answering"}
    backend = SimpleNamespace(_execute=AsyncMock(
        return_value=SimpleNamespace(data={"session": state}),
    ))
    await training.router.propagate_event(
        "message", message("Yes"), backend=backend, telegram_update_claim=object(),
        localization=LocalizationService(), locale="en",
        navigation=SimpleNamespace(active_mode="training", allowed_actions=()),
    )
    operation = backend._execute.await_args.args[1]
    assert operation.command == "answer" and operation.answer == "Yes"


@pytest.mark.parametrize("command", ["buzz", "stop", "next"])
async def test_obsolete_inline_actions_are_rejected_before_any_service_call(command):
    backend = SimpleNamespace(_execute=AsyncMock())
    callback = SimpleNamespace(data=f"tr:{RUN.hex}:1:{command}", message=message("Question"),
                               answer=AsyncMock())
    await training.handle_training_action(callback, backend, object(), LocalizationService(), "en")
    backend._execute.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_inline_continue_from_untracked_question_is_rejected():
    backend = SimpleNamespace(_execute=AsyncMock(return_value=SimpleNamespace(data={
        "session": {"question_revision_id": str(QUESTION), "message_ids": [99]},
    })))
    callback = SimpleNamespace(data=f"tr:{RUN.hex}:1:continue", message=message("Question"),
                               answer=AsyncMock())
    await training.handle_training_action(callback, backend, object(), LocalizationService(), "en")
    assert backend._execute.await_count == 1  # authorized read, never a mutation
    assert callback.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.parametrize("phase,correct", [("waiting", None), ("answering", False)])
async def test_only_accepted_buzz_timeout_is_an_incorrect_attempt(phase, correct):
    service, record, _, _, _, session = harness(phase=phase)
    record.deadline = NOW
    service._finish_question = AsyncMock()
    await service._advance(session, record, NOW)
    assert service._finish_question.await_args.kwargs["correct"] is correct


async def test_unchanged_reading_tick_does_not_fetch_question_again():
    service, record, _, _, _, session = harness()
    await service._advance(session, record, NOW)
    service._question.assert_not_awaited()
    service._notice.assert_not_awaited()


async def test_ordinary_question_delivery_installs_reply_controls_and_replays_without_spam():
    from training_delivery_support import deliver, delivery_case, question_ids

    case = delivery_case(locale="ru", text="Вопрос")
    await deliver(case)
    assert case.sends[0][2].keyboard[0][0].text == "+"
    assert case.messages[question_ids(case)[0]] == ("Вопрос", None)
    count_before = len(case.sends)
    await deliver(case)
    assert len(case.sends) == count_before
    assert not case.edits


async def test_lobby_delivery_edits_one_durable_status_on_save_and_close_without_spam():
    state = {"telegram_user_id": 42, "locale": "en", "version": 1, "presentation": {},
             "lobby": {"packet_name": "Packet <one>", "settings": {"best_buzz": True}}}

    async def request(action, **params):
        if action.endswith(".record"):
            state["presentation"] = params["presentation"]
            return {"recorded": True}
        return state

    protocol = SimpleNamespace(request=AsyncMock(side_effect=request))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
                          edit_message_text=AsyncMock())
    delivery = TrainingDelivery(bot, LocalizationService(), protocol)
    payload = {"owner_id": str(OWNER)}
    await delivery.lobby(payload)
    assert "Packet &lt;one&gt;" in bot.send_message.await_args.args[1]
    assert "Best buzz" in bot.send_message.await_args.args[1]
    await delivery.lobby(payload)
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.await_count == 0
    state["version"] = 2
    state["lobby"]["settings"] = {"reversed": True, "hidden_after": 2}
    await delivery.lobby(payload)
    assert "Reverse word order" in bot.edit_message_text.await_args.args[0]
    assert "Hide question text" in bot.edit_message_text.await_args.args[0]
    state["lobby"] = None
    await delivery.lobby(payload)
    assert bot.edit_message_text.await_args.args[0] == "Training lobby closed."
    assert bot.send_message.await_count == 1
    assert state["presentation"]["id"] == 10
