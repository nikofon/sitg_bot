from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import (
    ActionCode,
    KnowledgeSaveOperation,
    PacketAnalysisOperation,
)
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.analysis_jobs import AnalysisQuotaExceededError


@pytest.mark.parametrize(
    "command,body,expected",
    [
        (
            "create",
            {"title": "Title", "description": "Description", "tag_names": ["History"]},
            ActionCode.KNOWLEDGE_CREATE,
        ),
        (
            "update",
            {"fact_id": str(UUID(int=9)), "title": "Changed", "expected_version": 2},
            ActionCode.KNOWLEDGE_UPDATE,
        ),
        (
            "dissect",
            {
                "version_id": str(UUID(int=8)),
                "instructions": "  before 1500 A.C.\n",
                "comment": "more detail",
            },
            ActionCode.KNOWLEDGE_DISSECT,
        ),
        (
            "best_buzz",
            {"version_id": str(UUID(int=8)), "confirm_exposure": True},
            ActionCode.BEST_BUZZ_PREPARE,
        ),
    ],
)
async def test_http_knowledge_mutations_use_authenticated_typed_contracts(command, body, expected):
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    http._json_body = AsyncMock(return_value=body)
    request = make_mocked_request(
        "POST",
        f"/api/miniapp/knowledge/{command}",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "knowledge-test-key",
        },
        match_info={"command": command},
    )
    response = await http._knowledge_mutation(request)
    assert response.status == 200
    operation = gateway.requests[0].operation
    assert operation.action == expected
    assert gateway.requests[0].metadata.idempotency_key == "knowledge-test-key"
    if "instructions" in body:
        assert operation.instructions == body["instructions"]


def test_knowledge_contracts_reject_caller_identity_and_map_quota():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KnowledgeSaveOperation(action=ActionCode.KNOWLEDGE_CREATE, owner_id=UUID(int=1))
    with pytest.raises(ValidationError):
        PacketAnalysisOperation(
            action=ActionCode.KNOWLEDGE_DISSECT, version_id=UUID(int=1), private=False
        )
    error = ApplicationGateway._error(
        AnalysisQuotaExceededError("Exhausted"), action=ActionCode.KNOWLEDGE_DISSECT
    )
    assert error.code.value == "quota_exceeded"
    with pytest.raises(ValidationError):
        PacketAnalysisOperation(
            action=ActionCode.KNOWLEDGE_DISSECT, version_id=UUID(int=1), units=1
        )


def test_telegram_statistics_keep_partial_counts_and_estimates_explicit():
    from sitg_bot.bot.handlers.knowledge import fact_detail_message
    from sitg_bot.bot.i18n import LocalizationService

    model = fact_detail_message(
        {
            "title": "<Fact>",
            "description": "<description>",
            "origin": "manual",
            "editable": False,
            "links": [],
            "statistics": {
                "status": "fresh",
                "mentions": {"count": 4, "complete": False},
                "common_knowledge": 25,
            },
        },
        LocalizationService(),
        "en",
    )
    assert "&lt;description&gt;" in model.text
    assert "at least 4" in model.text
    assert "LLM estimate" in model.text
    assert "pending calibration" in model.text
