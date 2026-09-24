import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendDocument
from pydantic import ValidationError

from sitg_bot.application.contracts import ActionCode, LibraryAccessOperation
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.application.protocol import RetryableDeliveryError, TerminalDeliveryError
from sitg_bot.bot.library_delivery import library_document_delivery_handler
from sitg_bot.bot.miniapps import mini_app_route_url
from sitg_bot.packet_export import WORD, library_docx


def pages():
    return [{
        "title": "Тема <one>", "author": "Theme Author",
        "questions": [{
            "value": 10, "text": "Question & <text>\nNext line", "answer": "Primary answer",
            "accepted_answers": ["Alternative 1", "Alternative 2"],
            "commentary": "Explanation", "author": "Question Author",
            "form": "Name", "source": "Book",
        }],
    }, {"title": "Second theme", "author": "", "questions": []}]


def test_docx_contains_ordered_headings_all_question_fields_and_safe_xml():
    data = library_docx("Packet & name", pages())
    with ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        for path in (
            "[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels", "word/styles.xml"
        ):
            ElementTree.fromstring(archive.read(path))
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = {"w": WORD}
    paragraphs = document.findall("w:body/w:p", ns)
    texts = ["".join(node.itertext()) for node in paragraphs]
    assert texts == [
        "Packet & name", "Тема <one>", "Author: Theme Author",
        "10) Question & <text>Next line", "Answer: Primary answer",
        "Additional answers: Alternative 1, Alternative 2", "Commentary: Explanation",
        "Authors: Question Author", "Form: Name", "Source: Book", "Second theme",
    ]
    assert len(document.findall(".//w:br", ns)) == 1
    assert [node.get(f"{{{WORD}}}val") for node in document.findall(".//w:pStyle", ns)] == [
        "Heading1", "Heading2", "Heading2",
    ]


async def test_telegram_download_sends_docx_to_authenticated_recipient():
    bot = SimpleNamespace(send_document=AsyncMock())
    await library_document_delivery_handler(bot)({
        "recipient_telegram_user_id": 42, "name": "A/B: packet", "pages": pages(),
    })
    sent = bot.send_document.await_args.kwargs
    assert sent["chat_id"] == 42
    assert sent["document"].filename == "A_B_ packet.docx"
    with ZipFile(io.BytesIO(sent["document"].data)) as archive:
        assert "word/document.xml" in archive.namelist()


@pytest.mark.parametrize("flood_wait", [True, False])
async def test_telegram_delivery_uses_existing_retry_policy(flood_wait):
    method = SendDocument(chat_id=42, document="file-id")
    error = (
        TelegramRetryAfter(method=method, message="Wait", retry_after=7) if flood_wait
        else TelegramForbiddenError(method=method, message="Blocked")
    )
    bot = SimpleNamespace(send_document=AsyncMock(side_effect=error))
    with pytest.raises(RetryableDeliveryError if flood_wait else TerminalDeliveryError):
        await library_document_delivery_handler(bot)({
            "recipient_telegram_user_id": 42, "name": "Packet", "pages": pages(),
        })


@pytest.mark.parametrize("action", [ActionCode.LIBRARY_VIEW, ActionCode.LIBRARY_DOWNLOAD])
def test_library_actions_require_csrf_and_idempotency_without_trusting_identity(action):
    assert ACTION_POLICIES[action].authentication_required
    assert ACTION_POLICIES[action].mutation
    assert ACTION_POLICIES[action].idempotency_required
    values = {"action": action, "version_id": UUID(int=1)}
    assert LibraryAccessOperation.model_validate(values).confirm is False
    for extra in ({"confirm": "false"}, {"player_id": UUID(int=2)}, {"role": "manager"}):
        with pytest.raises(ValidationError):
            LibraryAccessOperation.model_validate({**values, **extra})
    assert ACTION_POLICIES[ActionCode.LIBRARY_VIEW].sensitive_response


def test_library_launch_link_uses_shared_mini_app():
    assert mini_app_route_url("https://mini.example.test/app", "library") == (
        "https://mini.example.test/app/library?_launch=1"
    )
