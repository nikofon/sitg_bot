from html import escape, unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sitg_bot.bot.handlers.knowledge import send_message_model
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
