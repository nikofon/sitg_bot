import json
import re
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from sitg_bot.domain.game_settings import announcement_tokens
from sitg_bot.services.training_ai import (
    PROVIDER_CALL_METADATA,
    AnalysisValidationError,
    BuzzRequest,
    CachedFactsRequest,
    CommonKnowledgeRequest,
    ExtractionRequest,
    ProviderOutcomeUncertain,
    ProviderSettings,
    QuestionText,
    packet_chunks,
    validate_fact_candidates,
)
from sitg_bot.services.training_provider import (
    RESPONSES_ENDPOINTS,
    analysis_provider_from_settings,
)


@pytest.fixture(params=["openai", "charm"])
def provider_name(request):
    return request.param


@pytest.fixture
def provider(monkeypatch, provider_name):
    def make(output, **kwargs):
        return mock_provider(monkeypatch, output, provider_name=provider_name, **kwargs)
    return make


def mock_provider(monkeypatch, output, *, provider_name, status="completed", chunk_size=17,
                  response_size=None, http_status=200, usage=None, failure=None):
    calls = []
    response = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}
        ],
        "usage": usage or {},
    }
    if status is not None:
        response["status"] = status
    body = json.dumps(response).encode()
    if response_size is not None:
        body += b" " * (response_size - len(body))
    offset = 0

    class Content:
        async def read(self, size):
            nonlocal offset
            if failure:
                raise failure
            end = min(offset + size, offset + chunk_size, len(body))
            chunk = body[offset:end]
            offset = end
            return chunk

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return self

        headers = {"x-request-id": "upstream-test"}
        status = http_status
        content = Content()

    monkeypatch.setattr(
        "sitg_bot.services.training_provider.aiohttp.ClientSession", lambda **kwargs: Context()
    )
    return analysis_provider_from_settings(
        ProviderSettings(provider_name, "configured-model", api_key="test-secret")
    ), calls


async def test_provider_uses_strict_private_structured_requests(provider, provider_name):
    revision = uuid4()
    instance, calls = provider({
        "candidates": [{
            "title": "A useful fact", "description": "A self-contained description",
            "links": [], "tags": [], "question_revision_id": str(revision),
        }],
    })
    chunks = packet_chunks(
        (QuestionText(revision, "A paragraph\n\nAnother paragraph"),), max_chunk_characters=1000
    )
    result = await instance.extract_facts(ExtractionRequest(chunks, "  custom\n", "ru"))
    validate_fact_candidates(result, allowed_question_revision_ids={revision}, max_output_facts=200)
    body = calls[0][1]["json"]
    assert body["model"] == "configured-model" and body["store"] is False
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    expected = ("https://api.openai.com/v1/responses" if provider_name == "openai"
                else "https://hyper.charm.land/v1/responses")
    assert calls[0][0] == expected
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-secret"
    assert calls[0][1]["allow_redirects"] is False
    assert json.loads(body["input"])["custom_instructions"] == "  custom\n"
    assert "test-secret" not in json.dumps(body)
    assert instance.provider_request_ids == ["upstream-test"]


async def test_provider_cached_decision_is_strict_and_supplies_no_source_text(provider):
    request = CachedFactsRequest((), "  Keep useful facts\n", "en", ("art",), ("art",))
    instance, calls = provider({"requires_source": False})
    result = await instance.decide_cached_facts(request)
    assert result.requires_source is False
    supplied = json.loads(calls[0][1]["json"]["input"])
    assert supplied == {"cached_facts": [], "custom_instructions": request.instructions,
                        "language": "en", "available_tags": ["art"], "new_tags": ["art"]}
    instance, _ = provider({"requires_source": "false"})
    with pytest.raises(ValidationError):
        await instance.decide_cached_facts(request)


async def test_provider_cached_transformation_preserves_provenance(provider):
    revision = uuid4()
    instance, calls = provider({"candidates": [{
        "title": "Fact", "description": "Description", "links": [], "tags": ["art"],
        "question_revision_id": str(revision),
    }]})
    result = await instance.transform_cached_facts(CachedFactsRequest((), "retag", "en", ("art",)))
    validate_fact_candidates(result, allowed_question_revision_ids={revision}, max_output_facts=200,
                             allowed_tag_names={"art"})
    supplied = json.loads(calls[0][1]["json"]["input"])
    assert supplied["cached_facts"] == [] and "chunks" not in supplied
    assert result[0].tags == ("art",)


async def test_provider_maps_source_word_offsets_to_the_real_announcement_stream(provider):
    revision = uuid4()
    text = "One  two\n\nthree four five"
    source_end = list(re.finditer(r"\S+", text))[2].end()
    instance, _ = provider({"boundaries": [{
        "question_revision_id": str(revision), "source_end": source_end,
    }]})
    chunks = packet_chunks((QuestionText(revision, text),), max_chunk_characters=1000)
    result = await instance.best_buzz_boundaries(BuzzRequest(chunks, "en", target_chars=7))
    assert len(result) == 1
    consumed = sum(
        len(token.split()) for token in announcement_tokens(text, 7)[: result[0].token_index]
    )
    assert consumed >= 3
    instance, _ = provider({"boundaries": [{
        "question_revision_id": str(revision), "source_end": 1,
    }]})
    with pytest.raises(AnalysisValidationError, match="source word"):
        await instance.best_buzz_boundaries(BuzzRequest(chunks, "en"))


async def test_common_knowledge_accepts_fragmented_provider_response(provider):
    instance, _ = provider({"value": 42}, chunk_size=1)
    result = await instance.estimate_common_knowledge(
        CommonKnowledgeRequest("A fact", "en", "adults")
    )
    assert result.value == 42


@pytest.mark.parametrize("response_size", [1024 * 1024, 1024 * 1024 + 1])
async def test_provider_response_limit_across_network_chunks(provider, response_size):
    instance, _ = provider({"candidates": []}, chunk_size=65536, response_size=response_size)
    request = ExtractionRequest((), "", "en")
    if response_size > 1024 * 1024:
        with pytest.raises(AnalysisValidationError, match="exceeds size limit"):
            await instance.extract_facts(request)
    else:
        assert await instance.extract_facts(request) == ()


async def test_incomplete_provider_output_is_an_error_not_an_empty_result(provider):
    instance, _ = provider({"candidates": []}, status="incomplete")
    with pytest.raises(AnalysisValidationError, match="incomplete"):
        await instance.extract_facts(ExtractionRequest((), "", "en"))


@pytest.mark.parametrize("http_status", [302, 400, 401, 402, 404, 429, 500])
async def test_provider_errors_never_retry_or_switch_endpoint(provider, http_status):
    instance, calls = provider({"error": "do not expose provider body"}, http_status=http_status)
    metadata = {}
    token = PROVIDER_CALL_METADATA.set(metadata)
    try:
        with pytest.raises(AnalysisValidationError, match=f"HTTP {http_status}"):
            await instance.extract_facts(ExtractionRequest((), "", "en"))
    finally:
        PROVIDER_CALL_METADATA.reset(token)
    assert len(calls) == 1 and calls[0][0] == instance.endpoint
    assert metadata == {"request_id": "upstream-test"}


async def test_provider_timeout_is_uncertain_without_retry(provider):
    instance, calls = provider({}, failure=TimeoutError())
    with pytest.raises(ProviderOutcomeUncertain) as error:
        await instance.extract_facts(ExtractionRequest((), "", "en"))
    assert error.value.provider_request_id == calls[0][1]["headers"]["X-Client-Request-Id"]
    assert len(calls) == 1


async def test_documented_hyper_response_without_status_and_usage_aliases(monkeypatch):
    instance, _ = mock_provider(monkeypatch, {"value": 42}, provider_name="charm", status=None,
                                usage={"prompt_tokens": 8, "completion_tokens": 42,
                                       "total_tokens": 50, "untrusted": "secret"})
    metadata = {}
    token = PROVIDER_CALL_METADATA.set(metadata)
    try:
        result = await instance.estimate_common_knowledge(
            CommonKnowledgeRequest("Fact", "en", "all")
        )
    finally:
        PROVIDER_CALL_METADATA.reset(token)
    assert result.value == 42
    assert metadata == {"request_id": "upstream-test", "usage": {
        "input_tokens": 8, "output_tokens": 42, "total_tokens": 50,
    }}


async def test_real_http_transport_keeps_provider_auth_and_model(monkeypatch, provider_name):
    received = []

    async def respond(request):
        received.append((request.headers["Authorization"], await request.json()))
        return web.json_response({
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"value": 17}'},
            ]}], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }, headers={"x-request-id": "local-http-contract"})

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    async with TestServer(app) as server:
        monkeypatch.setitem(RESPONSES_ENDPOINTS, provider_name,
                            str(server.make_url("/v1/responses")))
        instance = analysis_provider_from_settings(
            ProviderSettings(provider_name, "operator-model", api_key="local-fixture-key")
        )
        result = await instance.estimate_common_knowledge(
            CommonKnowledgeRequest("Fact", "ru", "adults")
        )
    assert result.value == 17
    assert instance.provider_request_ids == ["local-http-contract"]
    assert len(received) == 1 and received[0][0] == "Bearer local-fixture-key"
    assert received[0][1]["model"] == "operator-model"
    assert received[0][1]["text"]["format"]["type"] == "json_schema"


async def test_openai_still_requires_completed_status(monkeypatch):
    instance, _ = mock_provider(monkeypatch, {"value": 42}, provider_name="openai", status=None)
    with pytest.raises(AnalysisValidationError, match="incomplete"):
        await instance.estimate_common_knowledge(CommonKnowledgeRequest("Fact", "en", "all"))
