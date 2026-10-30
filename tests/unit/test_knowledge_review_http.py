from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import ActionCode, GatewayResponse
from sitg_bot.miniapp_http import MiniAppHttpServer


class ReviewGateway(FakeGateway):
    async def execute(self, principal, request):
        self.requests.append(request)
        return GatewayResponse(
            action=request.operation.action,
            correlation_id=request.metadata.correlation_id,
            ok=True,
            data={"marker": "direct"},
        )


@pytest.mark.parametrize(
    "method,path,body,action",
    [
        ("GET", f"themes?version_id={UUID(int=8)}", None, ActionCode.KNOWLEDGE_THEMES),
        ("GET", "imports", None, ActionCode.KNOWLEDGE_IMPORTS),
        ("GET", f"imports/{UUID(int=9)}", None, ActionCode.KNOWLEDGE_IMPORT_DETAIL),
        (
            "POST",
            "theme_preview",
            {
                "version_id": str(UUID(int=8)),
                "theme_revision_id": str(UUID(int=10)),
                "confirm_exposure": True,
            },
            ActionCode.KNOWLEDGE_THEME_PREVIEW,
        ),
        (
            "POST",
            "import_decide",
            {
                "import_id": str(UUID(int=9)),
                "decision": "approve",
                "selected_candidate_ids": ["candidate"],
            },
            ActionCode.KNOWLEDGE_IMPORT_DECIDE,
        ),
        (
            "POST",
            "dissect",
            {
                "version_id": str(UUID(int=8)),
                "theme_revision_ids": [str(UUID(int=10))],
                "tag_ids": [str(UUID(int=11))],
                "comment": "  Exact\n",
            },
            ActionCode.KNOWLEDGE_DISSECT,
        ),
    ],
)
async def test_review_transport_is_authenticated_typed_and_direct(method, path, body, action):
    gateway = ReviewGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    headers = {
        "Origin": "https://mini.example.test",
        "Cookie": "__Host-sitg_session=test-session",
        "X-CSRF-Token": "csrf",
        "X-Idempotency-Key": "review-transport",
    }
    async with TestClient(TestServer(http.application)) as client:
        response = await client.request(
            method, "/api/miniapp/knowledge/" + path, headers=headers, json=body
        )
        assert response.status == 200 and await response.json() == {"marker": "direct"}
    operation = gateway.requests[-1].operation
    assert operation.action == action
    if action == ActionCode.KNOWLEDGE_DISSECT:
        assert operation.theme_revision_ids == [UUID(int=10)]
        assert operation.tag_ids == [UUID(int=11)] and operation.comment == "  Exact\n"


async def test_explicit_empty_theme_selection_is_rejected_before_dispatch():
    gateway = ReviewGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.post(
            "/api/miniapp/knowledge/dissect",
            headers={
                "Origin": "https://mini.example.test",
                "Cookie": "__Host-sitg_session=test-session",
                "X-CSRF-Token": "csrf",
                "X-Idempotency-Key": "empty-themes",
            },
            json={"version_id": str(UUID(int=8)), "theme_revision_ids": []},
        )
        assert response.status == 400
    assert not gateway.requests
