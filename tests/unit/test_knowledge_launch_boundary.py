"""Runtime lobby-baseline dispatch tests, not compatible with the legacy origin handler."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, Message
from test_telegram_frontend import navigation

from sitg_bot.bot.handlers.training import router
from sitg_bot.bot.i18n import LocalizationService


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("configured", [True, False])
async def test_registered_training_knowledge_entry(locale, configured):
    loc = LocalizationService()
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        text=loc.text("button.training.knowledge", locale),
    )
    adapter = SimpleNamespace(
        facts_page=AsyncMock(return_value={"items": [], "total": 0, "next_cursor": None})
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.propagate_event(
            update_type="message",
            event=message,
            backend=None,
            bot=SimpleNamespace(),
            telegram_update_claim=object(),
            localization=loc,
            locale=locale,
            knowledge_backend=adapter,
            navigation=navigation(active_mode="training", allowed_actions=("training.knowledge",)),
            launch_links="https://mini.example.test/" if configured else None,
        )
    answer.assert_awaited_once()
    if configured:
        button = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == loc.text("training.knowledge", locale)
        assert button.web_app.url == "https://mini.example.test/knowledge?_launch=1"
        adapter.facts_page.assert_not_awaited()
    else:
        adapter.facts_page.assert_awaited_once()
        assert answer.await_args.args[0] == loc.text(
            "knowledge.facts.title", locale, total=0
        ) + "\n\n" + loc.text("knowledge.facts.empty", locale)
