import json
import re
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sitg_bot.domain.game_settings import announcement_tokens
from sitg_bot.services.training_ai import (
    AnalysisValidationError,
    BuzzRequest,
    ExtractionRequest,
    ProviderSettings,
    QuestionText,
    packet_chunks,
    validate_fact_candidates,
)
from sitg_bot.services.training_provider import ResponsesAnalysisProvider


def provider(monkeypatch, output, *, status="completed", chunk_size=None):
    calls = []
    response = {
        "status": status,
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}
        ],
    }

    encoded = json.dumps(response).encode()
    chunks = (
        [encoded]
        if chunk_size is None
        else [encoded[index : index + chunk_size] for index in range(0, len(encoded), chunk_size)]
    )

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return self

        headers = {"x-request-id": "upstream-test"}
        status = 200
        content = type("Content", (), {"read": AsyncMock(side_effect=[*chunks, b""])})()

    monkeypatch.setattr(
        "sitg_bot.services.training_provider.aiohttp.ClientSession", lambda **kwargs: Context()
    )
    return ResponsesAnalysisProvider(
        ProviderSettings("openai", "configured-model", api_key="test-secret")
    ), calls


async def test_provider_accumulates_short_network_reads_until_eof(monkeypatch):
    instance, _ = provider(monkeypatch, {"candidates": []}, chunk_size=7)
    result = await instance.extract_facts(ExtractionRequest((), "", "en"))
    assert result == ()


async def test_provider_rejects_oversized_chunked_response(monkeypatch):
    instance, _ = provider(monkeypatch, {"padding": "x" * (1024 * 1024)}, chunk_size=65536)
    with pytest.raises(AnalysisValidationError, match="size limit"):
        await instance.extract_facts(ExtractionRequest((), "", "en"))


async def test_provider_uses_strict_private_structured_requests(monkeypatch):
    revision = uuid4()
    instance, calls = provider(
        monkeypatch,
        {
            "candidates": [
                {
                    "title": "A useful fact",
                    "description": "A self-contained description",
                    "links": [],
                    "question_revision_id": str(revision),
                }
            ]
        },
    )
    chunks = packet_chunks(
        (QuestionText(revision, "A paragraph\n\nAnother paragraph"),), max_chunk_characters=1000
    )
    result = await instance.extract_facts(ExtractionRequest(chunks, "  custom\n", "ru"))
    validate_fact_candidates(result, allowed_question_revision_ids={revision}, max_output_facts=200)
    body = calls[0][1]["json"]
    assert body["model"] == "configured-model" and body["store"] is False
    assert body["text"]["format"]["strict"] is True
    assert json.loads(body["input"])["custom_instructions"] == "  custom\n"
    assert "test-secret" not in json.dumps(body)
    assert instance.provider_request_ids == ["upstream-test"]


async def test_provider_maps_source_word_offsets_to_the_real_announcement_stream(monkeypatch):
    revision = uuid4()
    text = "One  two\n\nthree four five"
    source_end = list(re.finditer(r"\S+", text))[2].end()
    instance, _ = provider(
        monkeypatch,
        {
            "boundaries": [
                {
                    "question_revision_id": str(revision),
                    "source_end": source_end,
                }
            ]
        },
    )
    chunks = packet_chunks((QuestionText(revision, text),), max_chunk_characters=1000)
    result = await instance.best_buzz_boundaries(BuzzRequest(chunks, "en", target_chars=7))
    assert len(result) == 1
    consumed = sum(
        len(token.split()) for token in announcement_tokens(text, 7)[: result[0].token_index]
    )
    assert consumed >= 3
    instance, _ = provider(
        monkeypatch,
        {
            "boundaries": [
                {
                    "question_revision_id": str(revision),
                    "source_end": 1,
                }
            ]
        },
    )
    with pytest.raises(AnalysisValidationError, match="source word"):
        await instance.best_buzz_boundaries(BuzzRequest(chunks, "en"))


async def test_incomplete_provider_output_is_an_error_not_an_empty_result(monkeypatch):
    instance, _ = provider(monkeypatch, {"candidates": []}, status="incomplete")
    with pytest.raises(AnalysisValidationError, match="incomplete"):
        await instance.extract_facts(ExtractionRequest((), "", "en"))
