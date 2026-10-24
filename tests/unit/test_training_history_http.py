"""Training history HTTP seams: authenticated list/detail routes and boundaries.

Only account/session storage is in-memory; the gateway is mocked at the typed
operation boundary so the HTTP contract (auth, CSRF-free GETs, cursor/limit
passthrough, error mapping) is exercised without touching live storage.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from test_training_launch_auth import MemoryAuthStorage, signed_init_data

from sitg_bot.application.contracts import (
    ActionCode,
    ErrorCode,
    GatewayError,
    GatewayResponse,
)
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import MiniAppAuthService

TOKEN = "123456:fixture-only"
ORIGIN = "https://mini.example.test"
SESSION_ID = "11111111-1111-1111-1111-111111111111"


def history_http(action: ActionCode, data: dict | None = None, *, ok: bool = True):
    database = MemoryAuthStorage("en")
    accounts = SimpleNamespace(
        create_or_resume_registration=AsyncMock(
            return_value=SimpleNamespace(
                account=SimpleNamespace(player_id=database.player.id, preferred_locale="en")
            )
        )
    )
    auth = MiniAppAuthService(
        database,
        bot_token=TOKEN,
        session_signing_key="s" * 32,
        allowed_origins={ORIGIN},
        environment="test",
        player_accounts=accounts,
    )
    response = (
        GatewayResponse(action=action, correlation_id=UUID(int=1), ok=True, data=data)
        if ok
        else GatewayResponse(
            action=action,
            correlation_id=UUID(int=1),
            ok=False,
            error=GatewayError(code=ErrorCode.NOT_FOUND, message_key="error.not_found"),
        )
    )
    gateway = SimpleNamespace(execute=AsyncMock(return_value=response))
    return MiniAppHttpServer(auth, gateway), database, gateway


async def session_cookie(http: MiniAppHttpServer) -> str:
    http._json_body = AsyncMock(return_value={"init_data": signed_init_data()})
    request = make_mocked_request(
        "POST",
        "/api/miniapp/session",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    response = await http._headers(request, http._create_session)
    assert response.status == 200
    return response.cookies["__Host-sitg_session"].value


@pytest.mark.parametrize(
    ("path", "action"),
    [
        ("/training/history", ActionCode.TRAINING_HISTORY),
        (f"/training/history/{SESSION_ID}", ActionCode.TRAINING_HISTORY_DETAIL),
    ],
)
async def test_resolve_routes_require_authentication(path, action):
    http, _, gateway = history_http(action, {"items": [], "next_cursor": None})
    route = make_mocked_request(
        "GET", f"/api/miniapp/routes/resolve?path={path}", headers={"Origin": ORIGIN},
    )
    response = await http._headers(route, http._resolve_route)
    assert response.status == 401
    gateway.execute.assert_not_awaited()


async def test_resolve_training_history_list():
    http, database, gateway = history_http(
        ActionCode.TRAINING_HISTORY, {"items": [], "next_cursor": None}
    )
    cookie = await session_cookie(http)
    route = make_mocked_request(
        "GET",
        "/api/miniapp/routes/resolve?path=%2Ftraining%2Fhistory%3Flimit%3D10",
        headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie}"},
    )
    response = await http._headers(route, http._resolve_route)
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["resource"]["kind"] == "training_history"
    assert payload["authorization"] == {"allowed": True}
    principal, request = gateway.execute.await_args.args
    assert principal.player_id == database.player.id
    operation = request.operation
    assert operation.action == ActionCode.TRAINING_HISTORY
    assert operation.cursor is None and operation.limit == 10


async def test_resolve_training_history_detail():
    http, _, gateway = history_http(
        ActionCode.TRAINING_HISTORY_DETAIL, {"rows": [], "next_cursor": None}
    )
    cookie = await session_cookie(http)
    route = make_mocked_request(
        "GET",
        f"/api/miniapp/routes/resolve?path=%2Ftraining%2Fhistory%2F{SESSION_ID}",
        headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie}"},
    )
    response = await http._headers(route, http._resolve_route)
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["resource"]["kind"] == "training_history_detail"
    operation = gateway.execute.await_args.args[1].operation
    assert operation.action == ActionCode.TRAINING_HISTORY_DETAIL
    assert operation.session_id == UUID(SESSION_ID)
    assert operation.limit == 25


@pytest.mark.parametrize(
    ("path", "query", "action"),
    [
        ("/api/miniapp/training/history", "cursor=abc&limit=5", ActionCode.TRAINING_HISTORY),
        (
            f"/api/miniapp/training/history/{SESSION_ID}",
            "cursor=abc&limit=5",
            ActionCode.TRAINING_HISTORY_DETAIL,
        ),
    ],
)
async def test_direct_history_gets_pass_cursor_and_limit(path, query, action):
    http, _, gateway = history_http(action, {"items": [], "next_cursor": None})
    cookie = await session_cookie(http)
    request = make_mocked_request(
        "GET", f"{path}?{query}",
        headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie}"},
        match_info={"session_id": SESSION_ID},
    )
    handler = (
        http._training_history_list
        if action == ActionCode.TRAINING_HISTORY
        else http._training_history_detail
    )
    response = await http._headers(request, handler)
    assert response.status == 200
    operation = gateway.execute.await_args.args[1].operation
    assert operation.action == action
    assert operation.cursor == "abc" and operation.limit == 5
    if action == ActionCode.TRAINING_HISTORY_DETAIL:
        assert operation.session_id == UUID(SESSION_ID)


async def test_direct_history_detail_maps_not_found_for_other_owners():
    http, _, gateway = history_http(ActionCode.TRAINING_HISTORY_DETAIL, None, ok=False)
    cookie = await session_cookie(http)
    request = make_mocked_request(
        "GET", f"/api/miniapp/training/history/{SESSION_ID}",
        headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie}"},
        match_info={"session_id": SESSION_ID},
    )
    response = await http._headers(request, http._training_history_detail)
    assert response.status == 404
    payload = json.loads(response.text)
    assert payload["error"]["code"] == "not_found"


async def test_invalid_path_shapes_do_not_resolve():
    http, _, gateway = history_http(ActionCode.TRAINING_HISTORY, {"items": []})
    cookie = await session_cookie(http)
    for malformed in (
        "%2Ftraining%2Fhistory%2Fnot-a-uuid",
        "%2Ftraining%2Fhistory%2F..%2Fplayers",
        "https%3A%2F%2Fevil.example%2Ftraining%2Fhistory",
    ):
        route = make_mocked_request(
            "GET",
            f"/api/miniapp/routes/resolve?path={malformed}",
            headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie}"},
        )
        response = await http._headers(route, http._resolve_route)
        payload = json.loads(response.text)
        assert payload["authorization"] == {"allowed": False, "reason_code": "not_found"}
    gateway.execute.assert_not_awaited()


async def test_history_path_is_not_a_training_mutation_command():
    # The history route is GET-only: a POST resolves to the generic training
    # mutation contract with command "history", which is not in its allowlist.
    http, _, _gateway = history_http(ActionCode.TRAINING_HISTORY, {"items": []})
    cookie = await session_cookie(http)
    request = make_mocked_request(
        "POST", "/api/miniapp/training/history",
        headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie}"},
        match_info={"command": "history"},
    )
    from aiohttp import web

    with pytest.raises(web.HTTPNotFound):
        await http._training_mutation(request)
