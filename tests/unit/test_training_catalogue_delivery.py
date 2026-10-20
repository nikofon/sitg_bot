"""Combined lobby/catalogue HTTP contract; service/SQL and auth remain isolated seams."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from test_knowledge_catalogue_http import HEADERS, BoundaryAuth, dispatch_gateway

from sitg_bot.application.contracts import GatewayResponse
from sitg_bot.miniapp_http import MiniAppHttpServer


async def test_catalogue_http_dispatch_returns_direct_card_metadata_and_filters():
    card = {
        "id": "version-one", "packet_id": "logical-one", "name": "Training packet",
        "year": 2024, "scope": "training",
        "authors": [{"id": "author-one", "name": "Author"}],
        "tournaments": [{"id": "tournament-one", "name": "Cup"}],
        "total_theme_count": 12, "fresh_theme_count": 0,
        "dissected_theme_count": None, "dissected": True, "best_buzz_calculated": False,
    }
    page = {"items": [card], "next_cursor": "opaque-next-cursor"}
    app = dispatch_gateway()
    catalogue = AsyncMock(return_value=page)
    app.training = SimpleNamespace(packets=SimpleNamespace(list_packets=catalogue))

    class DispatchGateway:
        async def execute(self, principal, request):
            data = await app._dispatch(principal, request)
            return GatewayResponse(
                action=request.operation.action, correlation_id=request.metadata.correlation_id,
                ok=True, data=data,
            )

    http = MiniAppHttpServer(BoundaryAuth(), DispatchGateway())
    filters = {
        "name": "Training", "author": "Author", "year_from": "2020", "year_to": "2025",
        "publication_from": "2021", "publication_to": "2026", "order": "name_desc",
        "limit": "25", "cursor": "opaque-existing-cursor",
    }
    async with TestClient(TestServer(http.application)) as client:
        response = await client.get(
            "/api/miniapp/training/packets", params=filters, headers=HEADERS
        )
        assert response.status == 200
        assert await response.json() == page
        assert response.headers["Cache-Control"] == "no-store"
    forwarded = catalogue.await_args.kwargs
    assert forwarded == {
        **filters, "year_from": 2020, "year_to": 2025, "publication_from": 2021,
        "publication_to": 2026, "limit": 25,
    }
    catalogue.assert_awaited_once()
