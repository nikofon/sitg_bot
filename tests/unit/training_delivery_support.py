"""In-memory protocol ledger; Telegram transport only is faked."""

from copy import deepcopy
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.training_delivery import TrainingDelivery

OWNER, RUN, QUESTION = (str(UUID(int=i)) for i in (1, 2, 3))


def delivery_case(*, locale="en", phase="reading", status="active", text="Question", voting=False):
    state = {
        "telegram_user_id": 42, "locale": locale, "presentation": {}, "show_menu": True,
        "events": [], "session": {
            "id": RUN, "owner_id": OWNER, "player_name": "Player", "version": 1,
            "status": status, "phase": phase, "question_revision_id": QUESTION,
            "question_index": 0, "theme": "Theme", "value": 10, "hidden": False,
            "text": text, "score": 10, "appeal_status": "voting" if voting else None,
        },
    }
    messages, edits, sends, deleted = {}, [], [], []
    identifiers = count(10)

    async def request(action, **params):
        presentation = state["presentation"]
        if action.endswith(".record"):
            presentation.update(params.get("presentation", {}))
            if "key" in params:
                presentation.setdefault("messages", {})[params["key"]] = deepcopy(params["value"])
            if "sequence" in params:
                presentation["sequence"] = max(presentation.get("sequence", 0), params["sequence"])
            return {"recorded": True}
        result = deepcopy(state)
        result["events"] = [event for event in result["events"]
                            if event["sequence"] > presentation.get("sequence", 0)][:100]
        return result

    async def send(chat, text, **kwargs):
        identifier = next(identifiers)
        sends.append((identifier, text, kwargs.get("reply_markup")))
        messages[identifier] = (text, kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=identifier)

    async def edit(text, *, chat_id, message_id, **kwargs):
        edits.append((message_id, text, kwargs.get("reply_markup")))
        messages[message_id] = (text, kwargs.get("reply_markup"))

    async def delete(chat, mid):
        deleted.append(mid)
        messages.pop(mid, None)

    def event(kind, **parameters):
        state["events"].append({
            "sequence": len(state["events"]) + 1, "kind": kind,
            "parameters": {"round_id": state["session"]["question_revision_id"], **parameters},
        })

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                          edit_message_text=AsyncMock(side_effect=edit),
                          delete_message=AsyncMock(side_effect=delete))
    delivery = TrainingDelivery(bot, LocalizationService(), SimpleNamespace(request=request))
    event("game_started")
    event("question_cost_announced", theme="Theme", value=10)
    event("question_token_revealed", text=text)
    return SimpleNamespace(
        state=state, messages=messages, edits=edits, sends=sends, deleted=deleted,
        bot=bot, delivery=delivery, event=event, request=request,
        payload={"owner_id": OWNER, "session_id": RUN},
    )


def question_ids(case):
    return [mid for entry in case.state["presentation"].get("messages", {}).values()
            if entry.get("kind") == "question" for mid in entry["ids"]]


async def deliver(case, **payload):
    # The production presenter rate-limits question edits. Unit transport has no clock delay.
    case.delivery.last_edit.clear()
    await case.delivery({**case.payload, **payload})
