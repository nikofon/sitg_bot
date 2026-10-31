from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_knowledge_catalogue_http import dispatch_gateway
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
        ("GET", "imports?cursor=opaque&limit=7", None, ActionCode.KNOWLEDGE_IMPORTS),
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
    auth = FakeAuth()
    http = MiniAppHttpServer(auth, gateway)
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
    if action == ActionCode.KNOWLEDGE_IMPORTS:
        assert operation.cursor == ("opaque" if "cursor=" in path else None)
        assert operation.limit == (7 if "limit=" in path else 100)
        app = dispatch_gateway()
        listing = AsyncMock(return_value={"items": [], "next_cursor": "next"})
        app.knowledge = SimpleNamespace(reviews=SimpleNamespace(list=listing))
        principal = (await auth.authorize_request(
            "test-session", method="GET", action=action,
        )).principal
        payload = await app._dispatch(principal, gateway.requests[-1])
        assert payload == {"items": [], "next_cursor": "next"}
        listing.assert_awaited_once_with(
            UUID(int=2), cursor=operation.cursor, limit=operation.limit,
        )
    if action == ActionCode.KNOWLEDGE_DISSECT:
        assert operation.theme_revision_ids == [UUID(int=10)]
        assert operation.tag_ids == [UUID(int=11)] and operation.comment == "  Exact\n"


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=bad", "cursor=" + "x" * 1001])
async def test_invalid_import_page_parameters_are_rejected_before_dispatch(query):
    gateway = ReviewGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get(
            "/api/miniapp/knowledge/imports?" + query,
            headers={"Origin": "https://mini.example.test",
                     "Cookie": "__Host-sitg_session=test-session"},
        )
        assert response.status == 400
    assert not gateway.requests


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
