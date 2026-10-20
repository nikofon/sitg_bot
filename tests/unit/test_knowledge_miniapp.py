"""Knowledge launch and HTTP transport regressions; all external effects mocked."""

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import ActionCode, GatewayResponse
from sitg_bot.bot.miniapps import mini_app_route_url
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import MiniAppAuthenticationError


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://mini.example.test/", "https://mini.example.test/knowledge?_launch=1"),
        (
            "https://mini.example.test/app/?old=1#fragment",
            "https://mini.example.test/app/knowledge?_launch=1",
        ),
    ],
)
def test_knowledge_launch_route_is_supported(base, expected):
    assert mini_app_route_url(base, "knowledge") == expected


@pytest.mark.parametrize(
    "base,route",
    [
        ("http://mini.example.test", "knowledge"),
        ("https://mini.example.test", "knowledge/other"),
        ("https://mini.example.test", "../knowledge"),
    ],
)
def test_knowledge_launch_does_not_relax_route_validation(base, route):
    with pytest.raises(ValueError):
        mini_app_route_url(base, route)


class KnowledgeGateway(FakeGateway):
    async def execute(self, principal, request):
        self.requests.append(request)
        assert principal.player_id == UUID(int=2)
        data = {
            ActionCode.KNOWLEDGE_FACTS: {
                "state": "empty",
                "items": [],
                "total": 0,
                "next_cursor": None,
                "order": "title_asc",
            },
            ActionCode.KNOWLEDGE_SHARING: {"grants": [], "shared": []},
            ActionCode.KNOWLEDGE_DISSECTIONS: {"items": []},
            ActionCode.TRAINING_PACKETS: {
                "items": [{"id": str(UUID(int=8)), "name": "Packet"}],
                "next_cursor": "next-packet",
            },
            ActionCode.KNOWLEDGE_CREATE: {"id": str(UUID(int=9)), "title": "Created"},
        }[request.operation.action]
        return GatewayResponse(
            action=request.operation.action,
            correlation_id=request.metadata.correlation_id,
            ok=True,
            data=data,
        )


async def test_knowledge_http_resolves_resource_and_forwards_filters_and_mutations():
    auth = FakeAuth()
    auth.authorize_request = AsyncMock(wraps=auth.authorize_request)
    gateway = KnowledgeGateway()
    http = MiniAppHttpServer(auth, gateway)
    headers = {"Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session"}
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get(
            "/api/miniapp/routes/resolve?path=/knowledge%3F_launch%3D1", headers=headers
        )
        assert response.status == 200
        resource = (await response.json())["resource"]
        assert resource["kind"] == "knowledge"
        assert resource["facts"]["state"] == "empty"
        assert resource["sharing"] == {"grants": [], "shared": []}
        assert resource["dissection"]["next_cursor"] is None
        assert resource["dissection"]["packets"] == []
        assert [r.operation.action for r in gateway.requests] == [
            ActionCode.KNOWLEDGE_FACTS,
            ActionCode.KNOWLEDGE_SHARING,
            ActionCode.KNOWLEDGE_DISSECTIONS,
        ]
        # Packet catalogue is fetched only when the redesigned Packets tab opens.
        response = await client.get("/api/miniapp/training/packets", headers=headers)
        assert response.status == 200
        assert (await response.json())["next_cursor"] == "next-packet"
        assert gateway.requests[-1].operation.action == ActionCode.TRAINING_PACKETS
        response = await client.get(
            "/api/miniapp/knowledge/facts?search=History&tag=Past&order=updated_desc",
            headers=headers,
        )
        assert response.status == 200
        operation = gateway.requests[-1].operation
        assert (operation.search, operation.tag, operation.order) == (
            "History",
            "Past",
            "updated_desc",
        )
        response = await client.post(
            "/api/miniapp/knowledge/create",
            headers={**headers, "X-CSRF-Token": "csrf", "X-Idempotency-Key": "create-fact"},
            json={
                "title": "Created",
                "description": "Description",
                "tag_names": ["Past"],
                "links": [],
            },
        )
        assert response.status == 200
        assert (await response.json())["title"] == "Created"
        assert gateway.requests[-1].metadata.idempotency_key == "create-fact"
        assert auth.authorize_request.await_args.kwargs["csrf_token"] == "csrf"
        assert auth.authorize_request.await_args.kwargs["method"] == "POST"


async def test_knowledge_http_never_uses_public_browser_authorization():
    auth = FakeAuth()
    auth.authorize_request = AsyncMock(side_effect=MiniAppAuthenticationError("Missing session"))
    gateway = KnowledgeGateway()
    http = MiniAppHttpServer(auth, gateway)
    async with TestClient(TestServer(http.application)) as client:
        for path in ("/api/miniapp/routes/resolve?path=/knowledge", "/api/miniapp/knowledge/facts"):
            response = await client.get(path, headers={"Origin": "https://mini.example.test"})
            assert response.status == 401
            assert (await response.json())["error"]["code"] == "authentication_required"
        assert auth.authorize_request.await_count == 0  # Missing cookie rejected before auth.
        response = await client.get(
            "/api/miniapp/routes/resolve?path=/knowledge",
            headers={
                "Origin": "https://mini.example.test",
                "Cookie": "__Host-sitg_session=expired",
            },
        )
        assert response.status == 401
    auth.authorize_request.assert_awaited_once()
    assert not gateway.requests
