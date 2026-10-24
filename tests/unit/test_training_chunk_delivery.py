from copy import deepcopy
from itertools import count
from types import SimpleNamespace
from uuid import UUID

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import ReplyKeyboardMarkup

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.training_delivery import TrainingDelivery


def delivery_case(text, *, phase="reading"):
    state = {
        "telegram_user_id": 42, "locale": "en", "presentation": {},
        "session": {
            "id": str(UUID(int=1)), "version": 1, "status": "active", "phase": phase,
            "question_revision_id": str(UUID(int=2)), "question_index": 0,
            "theme": "Theme", "value": 10, "hidden": False, "text": text,
        },
    }
    messages, edits, sends = {}, [], []
    identifiers = count(10)

    async def request(action, **params):
        if action.endswith(".record"):
            state["presentation"] = deepcopy(params["presentation"])
            return {"recorded": True}
        return deepcopy(state)

    async def send(chat, text, *, reply_markup=None):
        identifier = next(identifiers)
        sends.append((identifier, text, reply_markup))
        if not isinstance(reply_markup, ReplyKeyboardMarkup):
            messages[identifier] = (text, reply_markup)
        return SimpleNamespace(message_id=identifier)

    async def edit(text, *, chat_id, message_id, reply_markup):
        edits.append((message_id, text, reply_markup))
        method = EditMessageText(chat_id=chat_id, message_id=message_id, text=text)
        if message_id not in messages:
            raise TelegramBadRequest(method=method, message="message to edit not found")
        if messages[message_id] == (text, reply_markup):
            raise TelegramBadRequest(method=method, message="message is not modified")
        messages[message_id] = (text, reply_markup)

    delivery = TrainingDelivery(
        SimpleNamespace(send_message=send, edit_message_text=edit), LocalizationService(),
        SimpleNamespace(request=request),
    )
    return SimpleNamespace(
        state=state, messages=messages, edits=edits, sends=sends, delivery=delivery,
        payload={"owner_id": str(UUID(int=3)), "session_id": str(UUID(int=1))},
    )


async def test_incremental_reading_skips_prefixes_and_hidden_force_clears_every_chunk():
    case = delivery_case("word " * 260)
    await case.delivery(case.payload)
    ids = list(case.state["presentation"]["ids"])
    assert len(ids) == 3
    before = deepcopy(case.messages)
    case.state["session"]["text"] += "tail"
    await case.delivery(case.payload)
    assert [item[0] for item in case.edits] == [ids[-1]]
    assert all(case.messages[identifier] == before[identifier] for identifier in ids[:-1])
    assert case.messages[ids[-1]][0].endswith("tail")
    case.edits.clear()
    await case.delivery(case.payload)
    assert case.edits == []
    case.state["session"]["text"] += " more" * 140
    await case.delivery(case.payload)
    ids = list(case.state["presentation"]["ids"])
    assert len(ids) == 4
    assert [item[0] for item in case.edits] == [ids[2]]
    case.edits.clear()
    case.state["session"]["hidden"] = True
    case.state["session"]["text"] = ""
    await case.delivery(case.payload)
    assert [item[0] for item in case.edits] == ids
    assert "Question text is hidden." in case.messages[ids[0]][0]
    assert all(case.messages[identifier] == ("…", None) for identifier in ids[1:])
    case.edits.clear()
    await case.delivery(case.payload)
    assert case.edits == []
    await case.delivery({**case.payload, "force": True})
    assert [item[0] for item in case.edits] == ids


@pytest.mark.parametrize("phase,next_phase", [("best_buzz", "reading"), ("reading", "best_buzz")])
async def test_growing_frame_updates_markup_without_editing_unchanged_prefixes(phase, next_phase):
    header = "1. Theme · 10\n"
    case = delivery_case("x" * (1800 - len(header)), phase=phase)
    await case.delivery(case.payload)
    ids = list(case.state["presentation"]["ids"])
    assert len(ids) == 3
    last_text = case.messages[ids[-1]][0]
    case.state["session"]["phase"] = next_phase
    case.state["session"]["text"] += "tail"
    await case.delivery(case.payload)
    new_ids = list(case.state["presentation"]["ids"])
    assert len(new_ids) == 4
    assert case.edits == ([(ids[-1], last_text, None)] if phase == "best_buzz" else [])
    assert case.messages[new_ids[-1]][0] == "tail"
    if next_phase == "best_buzz":
        markup = case.messages[new_ids[-1]][1]
        assert markup.inline_keyboard[0][0].callback_data.endswith(":continue")
        case.edits.clear()
        case.state["session"]["phase"] = "reading"
        await case.delivery(case.payload)
        assert case.edits == [(new_ids[-1], "tail", None)]
    else:
        assert case.messages[new_ids[-1]][1] is None


async def test_legacy_cursor_and_forced_recovery_replace_missing_messages():
    case = delivery_case("word " * 260)
    await case.delivery(case.payload)
    old_ids = list(case.state["presentation"]["ids"])
    case.state["presentation"].pop("chunk_digests")
    case.state["session"]["text"] += "tail"
    await case.delivery(case.payload)
    assert [item[0] for item in case.edits] == old_ids
    case.edits.clear()
    del case.messages[old_ids[0]]
    await case.delivery({**case.payload, "force": True})
    ids = case.state["presentation"]["ids"]
    assert ids[0] != old_ids[0] and ids[1:] == old_ids[1:]
    assert [item[0] for item in case.edits] == old_ids
    case.edits.clear()
    case.state["session"]["text"] += " next"
    await case.delivery(case.payload)
    assert [item[0] for item in case.edits] == [ids[-1]]
