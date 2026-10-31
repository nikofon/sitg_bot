"""The training host uses the same question chunks and message ledger as regular SI."""

from copy import deepcopy
from uuid import UUID

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from training_delivery_support import deliver, delivery_case, question_ids


async def test_incremental_reading_skips_unchanged_prefixes_and_buzz_hides_every_chunk():
    case = delivery_case(text="word " * 1500)
    await deliver(case)
    ids = question_ids(case)
    assert len(ids) > 1
    before = deepcopy(case.messages)
    case.event("question_token_revealed", text="word " * 1500 + "tail")
    await deliver(case)
    assert [item[0] for item in case.edits] == [ids[-1]]
    assert all(case.messages[mid] == before[mid] for mid in ids[:-1])
    case.edits.clear()
    case.state["session"]["phase"] = "answering"
    case.event("player_buzzed", mine=True, form="City")
    await deliver(case)
    assert {item[0] for item in case.edits} == set(ids)
    assert all("word" not in case.messages[mid][0] for mid in ids)
    assert case.sends[-1][2].force_reply is True
    case.edits.clear()
    await deliver(case)
    assert not case.edits


@pytest.mark.parametrize("phase,next_phase", [("best_buzz", "reading"), ("reading", "best_buzz")])
async def test_best_buzz_control_is_per_question_and_removed_when_continuing(phase, next_phase):
    case = delivery_case(text="word " * 260, phase=phase)
    await deliver(case)
    ids = question_ids(case)
    markup = case.messages[ids[-1]][1]
    assert (markup is not None) is (phase == "best_buzz")
    if markup:
        assert markup.inline_keyboard[0][0].callback_data.endswith(":continue")
    case.state["session"]["phase"] = next_phase
    case.event("question_token_revealed", text="word " * 260 + "tail")
    await deliver(case)
    markup = case.messages[ids[-1]][1]
    assert (markup is not None) is (next_phase == "best_buzz")
    old_ids = set(ids)
    case.state["session"]["question_revision_id"] = str(UUID(int=99))
    case.event("question_cost_announced", theme="Theme", value=20)
    case.event("question_token_revealed", text="New question")
    await deliver(case)
    assert set(question_ids(case)) > old_ids
    assert all("New question" not in case.messages[mid][0] for mid in old_ids)


async def test_best_buzz_pause_removes_control_and_resume_rebinds_its_version():
    case = delivery_case(phase="best_buzz", text="Prepared boundary")
    await deliver(case)
    mid = question_ids(case)[0]
    assert case.messages[mid][1].inline_keyboard[0][0].callback_data.endswith(":1:continue")
    case.state["session"].update(status="paused", version=2)
    case.event("game_paused")
    await deliver(case)
    assert case.messages[mid][1] is None
    case.state["session"].update(status="active", version=3)
    case.event("game_resumed")
    await deliver(case)
    assert case.messages[mid][1].inline_keyboard[0][0].callback_data.endswith(":3:continue")


async def test_force_recovery_replaces_a_missing_question_message_without_replaying_announcements():
    case = delivery_case(text="Question")
    await deliver(case)
    old_id = question_ids(case)[0]
    original_edit = case.bot.edit_message_text.side_effect

    async def edit(text, *, message_id, **kwargs):
        if message_id == old_id:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=42, message_id=old_id, text=text),
                message="message to edit not found",
            )
        return await original_edit(text, message_id=message_id, **kwargs)

    case.bot.edit_message_text.side_effect = edit
    case.state["session"]["text"] = "Changed question"
    before = len(case.sends)
    await deliver(case, force=True)
    assert question_ids(case)[0] != old_id
    assert len(case.sends) == before + 2  # replacement text + controls, not cost/theme


async def test_expired_hidden_recovery_never_sends_queued_visible_text():
    case = delivery_case(text="Secret question")
    case.state["session"]["hidden"] = True
    case.event("training_text_hidden")
    await deliver(case)
    assert all("Secret" not in text for _, text, _ in case.sends)
    assert all("Secret" not in text for _, text, _ in case.edits)
    await deliver(case, force=True)
    assert all("Secret" not in text for _, text, _ in case.sends)
