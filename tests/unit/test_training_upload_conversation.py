"""Real root router/FSM contracts; no Telegram, RPC, or database side effects."""

import base64
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Document, Message, User
from test_training_ui_boundary import training_navigation

from sitg_bot.application.contracts import ActionCode, TrainingUploadOperation
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.router import root_router

LIMIT = 4 * 1024 * 1024


def draft(identifier=1, errors=None):
    return {
        "draft_id": str(UUID(int=identifier)),
        "version": 1,
        "packet": {"name": f"Packet {identifier}", "themes": []},
        "errors": errors or [],
        "warnings": ["Review author"],
    }


class Conversation:
    def __init__(self, locale):
        self.locale = locale
        self.localization = LocalizationService()
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=123, chat_id=42, user_id=42))
        self.backend = SimpleNamespace(
            _execute=AsyncMock(return_value=SimpleNamespace(data={"drafts": [draft()]}))
        )
        self.bot = SimpleNamespace(download=AsyncMock(return_value=BytesIO(b"packet bytes")))
        self.claim = object()

    async def send(self, text=None, filename=None, size=12):
        msg = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=42, type="private"),
            text=text,
            document=Document(file_id="f", file_unique_id="u", file_name=filename, file_size=size)
            if filename is not None
            else None,
        )
        msg = msg.as_(self.bot)
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await root_router.propagate_event(
                update_type="message",
                event=msg,
                backend=self.backend,
                telegram_update_claim=self.claim,
                localization=self.localization,
                locale=self.locale,
                navigation=training_navigation(),
                state=self.state,
                raw_state=await self.state.get_state(),
                launch_links="https://mini.example.test",
                bot=self.bot,
            )
        return answer

    async def begin(self):
        answer = await self.send(self.localization.text("button.training.upload", self.locale))
        assert await self.state.get_state() == "TrainingUploadState:waiting_document"
        assert await self.state.get_data() == {}  # no tournament context
        assert answer.await_args.args[0] == self.localization.text(
            "training.document_prompt", self.locale
        )
        assert not answer.await_args.kwargs.get("reply_markup")  # no Mini App
        self.backend._execute.assert_not_awaited()


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("extension", [".json", ".DOCX", ".pdf"])
async def test_upload_prompt_multiple_files_and_private_operation(locale, extension):
    conv = Conversation(locale)
    await conv.begin()
    for _ in range(2):
        answer = await conv.send(filename="packet" + extension)
        claim, operation = conv.backend._execute.await_args.args
        assert claim is conv.claim
        assert isinstance(operation, TrainingUploadOperation)
        assert operation.action == ActionCode.TRAINING_UPLOAD
        assert operation.source_filename == "packet" + extension
        assert base64.b64decode(operation.source_base64) == b"packet bytes"
        assert "tournament_id" not in TrainingUploadOperation.model_fields
        assert "Review author" in "\n".join(call.args[0] for call in answer.await_args_list)
        buttons = answer.await_args.kwargs["reply_markup"].inline_keyboard
        assert [row[0].callback_data for row in buttons] == [
            f"td:{UUID(int=1).hex}:1:1",
            f"td:{UUID(int=1).hex}:1:0",
        ]
        assert all(button.web_app is None for row in buttons for button in row)
        assert await conv.state.get_state() == "TrainingUploadState:waiting_document"


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_upload_validation_results_are_separate_and_invalid_draft_cannot_publish(locale):
    conv = Conversation(locale)
    await conv.begin()
    conv.backend._execute.return_value = SimpleNamespace(
        data={"drafts": [draft(1), draft(2, errors=["Invalid question values"])]}
    )
    answer = await conv.send(filename="packets.json")
    assert answer.await_count == 2
    invalid = answer.await_args_list[-1]
    assert "Invalid question values" in invalid.args[0]
    assert [
        b.callback_data for row in invalid.kwargs["reply_markup"].inline_keyboard for b in row
    ] == [f"td:{UUID(int=2).hex}:1:0"]


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize(
    "kind", ["extension", "metadata_size", "download_size", "missing", "backend"]
)
async def test_upload_errors_keep_conversation_retryable(locale, kind):
    conv = Conversation(locale)
    await conv.begin()
    filename, size, key = "packet.json", 12, "training.unavailable"
    if kind == "extension":
        filename, key = "packet.exe", "training.file_invalid"
    elif kind == "metadata_size":
        size, key = LIMIT + 1, "packet_upload.file_too_large"
    elif kind == "download_size":
        conv.bot.download.return_value = BytesIO(b"x" * (LIMIT + 1))
        key = "packet_upload.file_too_large"
    elif kind == "missing":
        conv.bot.download.return_value = None
    else:
        conv.backend._execute.side_effect = ValueError("rejected")
    answer = await conv.send(filename=filename, size=size)
    assert answer.await_args.args[0] == conv.localization.text(key, locale, maximum_mb=4)
    assert await conv.state.get_state() == "TrainingUploadState:waiting_document"
    if kind != "backend":
        conv.backend._execute.assert_not_awaited()
    if kind in {"extension", "metadata_size"}:
        conv.bot.download.assert_not_awaited()


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_non_document_reprompts_cancel_exits_and_unsolicited_files_do_not_upload(locale):
    conv = Conversation(locale)
    await conv.begin()
    answer = await conv.send("not a document")
    assert answer.await_args.args[0] == conv.localization.text("training.document_prompt", locale)
    answer = await conv.send("/cancel")  # actual navigation router owns cancellation
    assert await conv.state.get_state() is None
    assert answer.await_args.args[0] == conv.localization.text("prompt.cancelled", locale)
    await conv.send(filename="packet.json")
    conv.bot.download.assert_not_awaited()
    conv.backend._execute.assert_not_awaited()


async def test_upload_summary_escapes_untrusted_text_and_bounds_long_diagnostics():
    conv = Conversation("en")
    await conv.begin()
    item = draft(errors=["<&" * 1000] * 10)
    item["packet"]["name"] = "<script>" * 100
    item["warnings"] = ["<&" * 1000] * 10
    conv.backend._execute.return_value = SimpleNamespace(data={"drafts": [item]})
    answer = await conv.send(filename="packet.json")
    answer.assert_awaited_once()
    text = answer.await_args.args[0]
    assert len(text) < 4096
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "&amp;lt;" not in text  # no double escaping
    assert "<b>" in text and "8 more" in text


async def test_another_training_action_ends_upload_conversation():
    conv = Conversation("en")
    await conv.begin()
    conv.backend.navigation = AsyncMock(return_value=training_navigation(lobby=True))
    await conv.send(conv.localization.text("button.training.train", "en"))
    assert await conv.state.get_state() is None


@pytest.mark.parametrize("publish", [False, True])
async def test_training_draft_decision_uses_existing_private_dispatch(publish):
    conv = Conversation("en")
    callback = CallbackQuery(
        id="d",
        from_user=User(id=42, is_bot=False, first_name="Player"),
        chat_instance="chat",
        data=f"td:{UUID(int=1).hex}:1:{int(publish)}",
        message=Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private")),
    )
    with (
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
        patch.object(Message, "edit_reply_markup", new_callable=AsyncMock) as edit,
    ):
        await root_router.propagate_event(
            update_type="callback_query",
            event=callback,
            backend=conv.backend,
            telegram_update_claim=conv.claim,
            localization=conv.localization,
            locale="en",
        )
    operation = conv.backend._execute.await_args.args[1]
    assert operation.action == ActionCode.TRAINING_DRAFT_DECIDE
    assert operation.draft_id == UUID(int=1)
    assert operation.expected_version == 1 and operation.publish is publish
    edit.assert_awaited_once_with(reply_markup=None)
