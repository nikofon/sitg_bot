from datetime import UTC, datetime
from html import escape, unescape
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message

from sitg_bot.bot.handlers.knowledge import send_message_model
from sitg_bot.bot.handlers.training import router as training_router
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel, MessageModel


async def test_long_escaped_knowledge_projection_preserves_text_and_final_navigation():
    text = ("A < B & C 😀\n" * 700).strip()
    keyboard = InlineKeyboardModel(rows=((InlineButtonModel("More", callback_data="klib:next"),),))
    message = SimpleNamespace(answer=AsyncMock())
    await send_message_model(message, MessageModel(text=escape(text), keyboard=keyboard))
    calls = message.answer.await_args_list
    assert len(calls) > 1
    assert all(len(unescape(call.args[0])) <= 4096 for call in calls)
    assert "".join(unescape(call.args[0]) for call in calls).replace("\n", "") == text.replace(
        "\n", ""
    )
    assert all(call.kwargs.get("reply_markup") is None for call in calls[:-1])
    assert calls[-1].kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "klib:next"


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_unconfigured_training_knowledge_menu_chunks_maximum_titles(monkeypatch, locale):
    items = [{
        "fact_id": str(UUID(int=index + 1)),
        "title": f"{index:02d}" + ("<b>&😀" * 40)[:198],
        "tag_names": ["tag <i>&😀</i>"], "statistic_status": "fresh",
    } for index in range(25)]
    assert all(len(item["title"]) == 200 for item in items)
    adapter = SimpleNamespace(facts_page=AsyncMock(return_value={
        "items": items, "total": 50, "page": 1, "next_page": 2,
    }))
    decoded = []

    class PlainKnowledgeHTML(HTMLParser):
        def handle_starttag(self, tag, attrs):
            raise AssertionError(f"Unescaped user markup: {tag}")

        def handle_data(self, data):
            self.text += data

    async def accept(text, **kwargs):
        parser = PlainKnowledgeHTML(convert_charrefs=True)
        parser.text = ""
        parser.feed(text)
        parser.close()
        assert len(parser.text.encode("utf-16-le")) // 2 <= 4096
        decoded.append(parser.text)
        return SimpleNamespace(message_id=len(decoded))

    answer = AsyncMock(side_effect=accept)
    monkeypatch.setattr(Message, "answer", answer)
    localization = LocalizationService()
    claim = object()
    backend = SimpleNamespace(_execute=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await training_router.propagate_event(
        "message", Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"),
            text=localization.text("button.training.knowledge", locale),
        ),
        bot=SimpleNamespace(), backend=backend, knowledge_backend=adapter,
        telegram_update_claim=claim, localization=localization, locale=locale,
        navigation=SimpleNamespace(
            active_mode="training", context="menu", allowed_actions=("training.knowledge",),
        ),
        launch_links=None, state=state,
    )
    adapter.facts_page.assert_awaited_once_with(claim)
    backend._execute.assert_not_awaited()
    state.clear.assert_awaited_once()
    calls = answer.await_args_list
    assert len(calls) > 1
    complete = "".join(decoded)
    assert all(complete.count(item["title"]) == 1 for item in items)
    assert complete.count("tag <i>&😀</i>") == 25
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    buttons = [button for row in calls[-1].kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons[:-1]] == [
        f"klib:fact:{item['fact_id']}" for item in items
    ]
    assert buttons[-1].callback_data == "klib:page:2"
