"""Exercise registered HTTP routes and typed gateway dispatch without live services."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import ActionCode
from sitg_bot.application.gateway import ACTION_POLICIES, ApplicationGateway
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import MiniAppAuthenticationError, MiniAppCsrfError


class BoundaryAuth(FakeAuth):
    def __init__(self):
        self.calls = []

    async def authorize_request(self, session_token, **values):
        self.calls.append(values)
        if session_token != "test-session":
            raise MiniAppAuthenticationError("No session")
        if values["method"] == "POST" and values.get("csrf_token") != "csrf":
            raise MiniAppCsrfError("Invalid CSRF")
        return await super().authorize_request(session_token, **values)


def dispatch_gateway():
    app = ApplicationGateway.__new__(ApplicationGateway)
    app.player_accounts = SimpleNamespace(
        lookup_by_telegram_user_id=AsyncMock(return_value=SimpleNamespace(player_id=UUID(int=2)))
    )
    app._require_active_principal = AsyncMock(return_value=UUID(int=2))
    app._reject_banned_player = AsyncMock()
    return app


HEADERS = {
    "Origin": "https://mini.example.test",
    "Cookie": "__Host-sitg_session=test-session",
    "X-CSRF-Token": "csrf",
    "X-Idempotency-Key": "catalogue-request",
}


@pytest.mark.parametrize(
    "command,body,action",
    [
        ("link_create", {}, ActionCode.KNOWLEDGE_LINK_CREATE),
        ("link_deactivate", {"link_id": str(UUID(int=8))}, ActionCode.KNOWLEDGE_LINK_DEACTIVATE),
        ("import", {"token": "a" * 43}, ActionCode.KNOWLEDGE_IMPORT),
        ("tag_create", {"name": "history", "colour": "#aAbB22"}, ActionCode.KNOWLEDGE_TAG_CREATE),
    ],
)
async def test_authenticated_mutation_routes_and_typed_gateway_forwarding(command, body, action):
    auth, gateway = BoundaryAuth(), FakeGateway()
    http = MiniAppHttpServer(auth, gateway)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.post(
            f"/api/miniapp/knowledge/{command}", json=body, headers=HEADERS
        )
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        request = gateway.requests[-1]
        assert request.operation.action == action
        assert request.metadata.idempotency_key == "catalogue-request"
        assert auth.calls[-1]["csrf_token"] == "csrf"
        policy = ACTION_POLICIES[action]
        assert policy.mutation and policy.idempotency_required and policy.sensitive_response
        # Continue through the real dispatcher into the application service seam.
        app = dispatch_gateway()
        app.knowledge = SimpleNamespace(mutate=AsyncMock(return_value={"updated": True}))
        await app._dispatch(
            (await auth.authorize_request("test-session", method="GET", action=action)).principal,
            request,
        )
        call = app.knowledge.mutate.await_args
        assert call.args[0] == UUID(int=2) and call.args[1] == command
        for key, value in body.items():
            assert str(call.args[2][key]) == value


@pytest.mark.parametrize("path", ["knowledge/tags", "knowledge/sharing", "training/packets"])
async def test_registered_reads_require_session_and_do_not_mutate(path):
    auth, gateway = BoundaryAuth(), FakeGateway()
    http = MiniAppHttpServer(auth, gateway)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get(f"/api/miniapp/{path}", headers=HEADERS)
        assert response.status == 200
        assert len(gateway.requests) == 1
        assert not ACTION_POLICIES[gateway.requests[0].operation.action].mutation
        missing = await client.get(f"/api/miniapp/{path}", headers={"Origin": HEADERS["Origin"]})
        assert missing.status == 401
        assert len(gateway.requests) == 1


async def test_knowledge_route_defers_packet_catalogue_until_explicit_read():
    class KnowledgeGateway(FakeGateway):
        async def execute(self, principal, request):
            result = await super().execute(principal, request)
            payloads = {
                ActionCode.KNOWLEDGE_FACTS: {"items": [], "total": 0},
                ActionCode.KNOWLEDGE_SHARING: {"grants": [], "links": []},
                ActionCode.KNOWLEDGE_DISSECTIONS: {"items": []},
                ActionCode.TRAINING_PACKETS: {"items": [], "next_cursor": None},
            }
            return result.model_copy(update={"data": payloads[request.operation.action]})

    auth, gateway = BoundaryAuth(), KnowledgeGateway()
    http = MiniAppHttpServer(auth, gateway)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get(
            "/api/miniapp/routes/resolve", params={"path": "/knowledge"}, headers=HEADERS,
        )
        assert response.status == 200
        assert (await response.json())["resource"]["dissection"]["packets"] == []
        assert [request.operation.action for request in gateway.requests] == [
            ActionCode.KNOWLEDGE_FACTS, ActionCode.KNOWLEDGE_SHARING,
            ActionCode.KNOWLEDGE_DISSECTIONS,
        ]
        response = await client.get("/api/miniapp/training/packets", headers=HEADERS)
        assert response.status == 200
        assert gateway.requests[-1].operation.action == ActionCode.TRAINING_PACKETS
        assert all(call["method"] == "GET" for call in auth.calls)


async def test_import_never_mutates_on_get_or_without_csrf_or_with_actor_override():
    auth, gateway = BoundaryAuth(), FakeGateway()
    http = MiniAppHttpServer(auth, gateway)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get(
            "/api/miniapp/knowledge/import?token=" + "a" * 43, headers=HEADERS
        )
        assert response.status in (404, 405)
        response = await client.post(
            "/api/miniapp/knowledge/import",
            json={"token": "a" * 43},
            headers={key: value for key, value in HEADERS.items() if key != "X-CSRF-Token"},
        )
        assert response.status == 403
        for body in (
            {"token": "a" * 43, "owner_id": str(UUID(int=1))},
            {"token": "invalid"},
            {"token": "a" * 43, "recipient_id": str(UUID(int=1))},
        ):
            response = await client.post(
                "/api/miniapp/knowledge/import", json=body, headers=HEADERS
            )
            assert response.status == 400
        assert gateway.requests == []


async def test_packet_search_sort_cursor_filters_forward_through_http_and_dispatch():
    auth, gateway = BoundaryAuth(), FakeGateway()
    http = MiniAppHttpServer(auth, gateway)
    params = {
        "name": "Name%",
        "author": "Author",
        "year_from": "2019",
        "year_to": "2025",
        "publication_from": "2020",
        "publication_to": "2026",
        "limit": "7",
        "order": "name_desc",
        "cursor": "opaque-cursor",
    }
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get("/api/miniapp/training/packets", params=params, headers=HEADERS)
        assert response.status == 200
    request = gateway.requests[-1]
    values = request.operation.model_dump(exclude={"action"})
    assert values == {
        key: int(value) if key.endswith(("from", "to")) or key == "limit" else value
        for key, value in params.items()
    }
    app = dispatch_gateway()
    app.training = SimpleNamespace(packets=SimpleNamespace(list_packets=AsyncMock()))
    principal = (
        await auth.authorize_request(
            "test-session", method="GET", action=ActionCode.TRAINING_PACKETS
        )
    ).principal
    await app._dispatch(principal, request)
    app.training.packets.list_packets.assert_awaited_once_with(UUID(int=2), **values)
