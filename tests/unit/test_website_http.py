import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import MiniAppSecurityPolicy, MiniAppSessionCredentials


def request(path: str, *, cookie: str = "", method: str = "GET"):
    headers = {"Host": "mini.example.test", "Origin": "https://mini.example.test"}
    if cookie:
        headers["Cookie"] = cookie
    return make_mocked_request(method, path, headers=headers)


async def test_public_profile_uses_anonymous_principal() -> None:
    gateway = FakeGateway()
    execute = AsyncMock(wraps=gateway.execute)
    gateway.execute = execute
    http = MiniAppHttpServer(FakeAuth(), gateway, website_origins={"https://mini.example.test"})
    response = await http._headers(
        request(f"/api/miniapp/routes/resolve?path=/players/{UUID(int=5)}"),
        http._resolve_route,
    )
    assert response.status == 200
    assert execute.call_args.args[0] == ApplicationPrincipal()
    assert response.headers["Cache-Control"] == "no-store"


async def test_directory_forwards_ruleset_selection() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway, website_origins={"https://mini.example.test"})
    response = await http._headers(
        request("/api/miniapp/routes/resolve?path=/players%3Fruleset%3Dsi%26offset%3D20"),
        http._resolve_route,
    )
    assert response.status == 200
    operation = gateway.requests[0].operation
    assert operation.action == ActionCode.PLAYER_LIST
    assert operation.ruleset_key == "si"
    assert operation.offset == 20


@pytest.mark.parametrize("path,action", [
    ("%2Fplayers", ActionCode.PLAYER_LIST),
    ("%2Ftournaments%3Finclude_managed_public%3Dtrue", ActionCode.TOURNAMENT_LIST),
])
@pytest.mark.parametrize("headers", [
    {"Origin": "http://127.0.0.1:5174", "Host": "127.0.0.1:8080"},
    {"Referer": "http://127.0.0.1:5174/players", "Host": "127.0.0.1:8080"},
    {"Host": "127.0.0.1:5174"},
])
async def test_guest_route_resolution_from_separate_website(path, action, headers) -> None:
    auth = FakeAuth()
    origin = "http://127.0.0.1:5174"
    auth.security_policy = MiniAppSecurityPolicy(frozenset({"https://mini.example.test", origin}))
    gateway = FakeGateway()
    gateway.execute = AsyncMock(wraps=gateway.execute)
    http = MiniAppHttpServer(auth, gateway, website_origins={origin})
    response = await http._headers(
        make_mocked_request("GET", f"/api/miniapp/routes/resolve?path={path}", headers=headers),
        http._resolve_route,
    )
    assert response.status == 200
    assert json.loads(response.text)["authorization"] == {"allowed": True}
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert response.headers["Cache-Control"] == "no-store"
    assert gateway.execute.call_args.args[0] == ApplicationPrincipal()
    assert gateway.requests[0].operation.action == action


@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:8080"},
    {"Host": "mini.example.test", "Origin": "https://attacker.example"},
    {"Host": "mini.example.test", "Origin": "null"},
    {"Host": "mini.example.test", "Referer": "https://attacker.example/players"},
    {"Host": "127.0.0.1:8080", "X-Forwarded-Host": "mini.example.test"},
])
@pytest.mark.parametrize("path", ["/players", "/tournaments", "/library"])
async def test_origin_rejection_is_not_a_login_error(headers, path) -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    handler = AsyncMock(wraps=http._resolve_route)
    response = await http._headers(
        make_mocked_request("GET", f"/api/miniapp/routes/resolve?path={path}", headers=headers),
        handler,
    )
    assert response.status == 403
    assert json.loads(response.text)["error"]["code"] == "origin_not_allowed"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response.headers
    handler.assert_not_awaited()
    assert gateway.requests == []


async def test_guest_session_bootstrap_and_mutation_still_require_login() -> None:
    auth = FakeAuth()
    auth.logout_website_session = AsyncMock()
    http = MiniAppHttpServer(
        auth, FakeGateway(), website_origins={"https://mini.example.test"}
    )
    bootstrap = make_mocked_request("POST", "/api/website/session", headers={
        "Origin": "https://mini.example.test", "Content-Type": "application/json",
    })
    http._json_body = AsyncMock(return_value={})
    response = await http._headers(bootstrap, http._resume_website_session)
    assert response.status == 401
    assert json.loads(response.text)["error"]["code"] == "authentication_required"
    response = await http._headers(
        request("/api/website/logout", method="POST"), http._website_logout
    )
    assert response.status == 401
    assert json.loads(response.text)["error"]["code"] == "authentication_required"
    auth.logout_website_session.assert_not_awaited()


@pytest.mark.parametrize("path", ["/library", "/ongoing", "/admin/management"])
async def test_private_routes_require_login(path: str) -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway, website_origins={"https://mini.example.test"})
    response = await http._headers(
        request(f"/api/miniapp/routes/resolve?path={path}"), http._resolve_route
    )
    assert response.status == 401
    assert gateway.requests == []


async def test_manager_direct_url_preserves_actor_and_gateway_action() -> None:
    http = MiniAppHttpServer(FakeAuth(), FakeGateway(), website_origins={"https://mini.example.test"})
    context, tournament_id = await http._resolve_manager_reference(
        request("/manager", cookie="__Host-sitg_session=test-session"), str(UUID(int=9)),
        action=ActionCode.TOURNAMENT_MANAGER_MANAGEMENT,
    )
    assert context.principal.player_id == UUID(int=2)
    assert context.action == ActionCode.TOURNAMENT_MANAGER_MANAGEMENT
    assert tournament_id == UUID(int=9)


@pytest.mark.parametrize("cookie,state", [("", "state"), ("state", ""), ("other", "state")])
async def test_login_callback_requires_browser_state(cookie: str, state: str) -> None:
    auth = FakeAuth()
    auth.create_website_session = AsyncMock()
    http = MiniAppHttpServer(auth, FakeGateway(), website_origins={"https://mini.example.test"})
    response = await http._headers(
        request(f"/auth/telegram/callback?state={state}", cookie=f"__Host-sitg_login={cookie}"),
        http._website_login_callback,
    )
    assert response.status == 401
    auth.create_website_session.assert_not_called()


async def test_login_callback_sets_private_cookie_and_removes_credentials_from_url() -> None:
    auth = FakeAuth()
    auth.create_website_session = AsyncMock(return_value=MiniAppSessionCredentials(
        "opaque-token", "csrf-token", datetime.now(UTC) + timedelta(minutes=15), "en"
    ))
    http = MiniAppHttpServer(auth, FakeGateway(), website_origins={"https://mini.example.test"})
    response = await http._headers(
        request("/auth/telegram/callback?state=state&id=42&hash=signed",
                cookie="__Host-sitg_login=state"),
        http._website_login_callback,
    )
    assert response.status == 303
    assert response.headers["Location"] == "/tournaments"
    assert response.headers["Cache-Control"] == "no-store"
    cookie = response.cookies["__Host-sitg_session"]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict"
    assert response.cookies["__Host-sitg_login"]["max-age"] == "0"
    auth.create_website_session.assert_awaited_once_with(
        {"id": "42", "hash": "signed"}, origin="https://mini.example.test"
    )


async def test_login_callback_rejects_duplicate_fields() -> None:
    http = MiniAppHttpServer(FakeAuth(), FakeGateway(), website_origins={"https://mini.example.test"})
    response = await http._headers(
        request("/auth/telegram/callback?state=state&id=42&id=43",
                cookie="__Host-sitg_login=state"),
        http._website_login_callback,
    )
    assert response.status == 401
    assert json.loads(response.text)["error"]["code"] == "authentication_required"


async def test_domains_cannot_use_each_others_login_endpoints() -> None:
    auth = FakeAuth()
    auth.security_policy = MiniAppSecurityPolicy(frozenset({
        "https://mini.example.test", "https://website.example.test",
    }))
    http = MiniAppHttpServer(auth, FakeGateway(), website_origins={"https://website.example.test"})
    for method, path, origin, handler in [
        ("POST", "/api/miniapp/session", "https://website.example.test", http._create_session),
        ("POST", "/api/website/session", "https://mini.example.test", http._resume_website_session),
        ("GET", "/auth/telegram", "https://mini.example.test", http._website_login),
    ]:
        req = make_mocked_request(method, path, headers={
            "Origin": origin, "Host": origin.removeprefix("https://"),
        })
        response = await http._headers(req, handler)
        assert response.status == 403
        assert json.loads(response.text)["error"]["code"] == "origin_not_allowed"


async def test_website_host_never_serves_miniapp_assets(tmp_path) -> None:
    (tmp_path / "index.html").write_text("Mini App")
    http = MiniAppHttpServer(
        FakeAuth(), FakeGateway(), web_dist=tmp_path,
        website_origins={"https://mini.example.test"},
    )
    with pytest.raises(web.HTTPNotFound):
        await http._static(request("/"))


async def test_server_rejects_shared_domains_even_with_different_ports() -> None:
    from sitg_bot.server import main

    args = SimpleNamespace(
        mini_app_port=8080, bot_token="123:token", application_security_key="s" * 32,
        mini_app_allowed_origins='["https://same.example.test"]',
        website_allowed_origins='["https://same.example.test:8443"]',
    )
    with pytest.raises(ValueError, match="domains must be different"):
        await main(args)
