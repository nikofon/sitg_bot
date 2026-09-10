import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request

from sitg_bot.application.contracts import (
    ActionCode,
    ApplicationPrincipal,
    GatewayResponse,
)
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import MiniAppSecurityPolicy, MiniAppSessionContext


class FakeAuth:
    security_policy = MiniAppSecurityPolicy(frozenset({"https://mini.example.test"}))

    async def authorize_request(
        self, session_token: str, **values: object
    ) -> MiniAppSessionContext:
        assert session_token == "test-session"
        return MiniAppSessionContext(
            session_id=UUID(int=1),
            principal=ApplicationPrincipal(player_id=UUID(int=2), telegram_user_id=42),
            bot_id=123,
            environment="test",
            origin="https://mini.example.test",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            action=values["action"],  # type: ignore[arg-type]
            idempotency_key=values.get("idempotency_key"),  # type: ignore[arg-type]
            registration_status="active",
            telegram_public=False,
            preferred_locale="en",
        )


class FakeGateway:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, principal: ApplicationPrincipal, request: object) -> GatewayResponse:
        self.requests.append(request)
        operation = request.operation  # type: ignore[attr-defined]
        if operation.action == ActionCode.LIBRARY_LIST:
            data = {"items": []}
        elif operation.action == ActionCode.TOURNAMENT_LIST:
            data = {
                "items": [
                    {
                        "id": str(UUID(int=10)),
                        "name": "Managed Cup",
                        "slug": "managed-cup",
                        "available_actions": ["info", "select_manager"],
                    }
                ],
                "next_cursor": "next-page",
                "total": 2,
                "navigation_version": 7,
            }
        elif operation.action == ActionCode.LOBBY_INFO:
            data = {
                "id": str(UUID(int=20)),
                "version": 3,
                "status": "assembling",
                "available_actions": ["ready", "leave"],
                "members": [],
            }
        elif operation.action == ActionCode.AUTHORS_SEARCH:
            data = {
                "items": [
                    {
                        "author_id": str(UUID(int=30)),
                        "display_name": "Ada Lovelace",
                        "authorship": {
                            "packet_count": 0,
                            "theme_count": 0,
                            "question_count": 0,
                            "packet_names": [],
                            "theme_names": [],
                            "tournament_names": [],
                            "years": [],
                        },
                    }
                ],
                "next_cursor": None,
            }
        else:
            data = {"selected": True}
        return GatewayResponse(
            action=operation.action,
            correlation_id=request.metadata.correlation_id,  # type: ignore[attr-defined]
            ok=True,
            data=data,
        )


class FakeLaunchReferences:
    async def resolve(self, raw_reference: str, *, player_id: UUID) -> object:
        assert raw_reference == "opaque-reference"
        assert player_id == UUID(int=2)
        return SimpleNamespace(route="manager_settings", target_id=UUID(int=10))


class FakeLobbyLaunchReferences:
    async def resolve(self, raw_reference: str, *, player_id: UUID) -> object:
        assert raw_reference == "opaque-lobby"
        assert player_id == UUID(int=2)
        return SimpleNamespace(route="lobby", target_id=UUID(int=20))


class FakeManagementLaunchReferences:
    async def resolve(self, raw_reference: str, *, player_id: UUID) -> object:
        assert raw_reference == "opaque-reference"
        assert player_id == UUID(int=2)
        return SimpleNamespace(route="manager_management", target_id=UUID(int=10))


@pytest.mark.parametrize(
    "command, action",
    [
        ("delete", ActionCode.PACKET_MANAGEMENT_DELETE),
        ("release", ActionCode.PACKET_MANAGEMENT_RELEASE),
        ("save", ActionCode.PACKET_MANAGEMENT_UPDATE),
    ],
)
async def test_packet_management_mutations_use_launch_scope_and_write_guards(command, action):
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(), gateway, launch_references=FakeManagementLaunchReferences()
    )
    request = make_mocked_request(
        "POST",
        f"/api/miniapp/manager/tournaments/opaque-reference/packets/{UUID(int=30)}/{command}",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "packet-mutation-test",
        },
        match_info={
            "launch_ref": "opaque-reference",
            "assignment_id": str(UUID(int=30)),
            "command": command,
        },
    )
    body = {
        "expected_version": 3,
        "tournament_id": str(UUID(int=99)),
        "assignment_id": str(UUID(int=99)),
    }
    if command == "save":
        body.update(content={}, changes={}, field_author_ids={})
    request._read_bytes = json.dumps(body).encode()
    result = await http._management_packet(request)
    assert result.status == 200
    operation = gateway.requests[0].operation
    assert operation.action == action
    assert operation.tournament_id == UUID(int=10)
    assert operation.assignment_id == UUID(int=30)
    assert operation.expected_version == 3
    assert gateway.requests[0].metadata.idempotency_key == "packet-mutation-test"


async def test_packet_management_rejects_settings_launch_reference():
    http = MiniAppHttpServer(FakeAuth(), FakeGateway(), launch_references=FakeLaunchReferences())
    request = make_mocked_request(
        "GET",
        f"/api/miniapp/manager/tournaments/opaque-reference/packets/{UUID(int=30)}",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
        },
        match_info={"launch_ref": "opaque-reference", "assignment_id": str(UUID(int=30))},
    )
    with pytest.raises(LookupError):
        await http._management_packet(request)


class FakeSelectionNotifier:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, object]] = []
        self.closed = False

    async def notify_selected(self, telegram_user_id: int, navigation_payload: object) -> None:
        self.notifications.append((telegram_user_id, navigation_payload))

    async def close(self) -> None:
        self.closed = True


async def test_tournament_route_resolves_role_filters_and_pagination() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "GET",
        (
            "/api/miniapp/routes/resolve?path="
            "/tournaments?role=manager%26relationship=managed%26search=cup%26order=name_asc"
        ),
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
        },
    )
    response = await http._resolve_route(request)

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["locale"] == "en"
    assert payload["resource"]["role"] == "manager"
    assert payload["resource"]["navigation_version"] == 7
    assert payload["resource"]["items"][0]["available_actions"] == [
        "info",
        "select_manager",
    ]
    assert payload["pagination"] == {"next": "next-page"}
    operation = gateway.requests[0].operation
    assert operation.relationship == "managed"
    assert operation.search == "cup"
    assert operation.order == "name_asc"


async def test_mini_app_http_rejects_missing_origin_before_exposing_data() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "GET",
        "/api/miniapp/routes/resolve?path=/tournaments",
        headers={"Cookie": "__Host-sitg_session=test-session"},
    )
    response = await http._headers(request, http._resolve_route)

    assert response.status == 401
    assert gateway.requests == []


async def test_tournament_selection_pushes_updated_context_to_telegram() -> None:
    gateway = FakeGateway()
    notifier = FakeSelectionNotifier()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        selection_notifier=notifier,  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "POST",
        f"/api/miniapp/tournaments/{UUID(int=10)}/select",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "selection",
        },
        match_info={"tournament_id": str(UUID(int=10))},
    )
    request._read_bytes = json.dumps(  # type: ignore[attr-defined]
        {"mode": "manager", "expected_version": 7}
    ).encode()

    response = await http._select(request)

    assert response.status == 200
    assert notifier.notifications == [(42, {"selected": True})]
    await http.close()
    assert notifier.closed


async def test_manager_settings_route_resolves_an_actor_bound_launch_reference() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        launch_references=FakeLaunchReferences(),  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "GET",
        ("/api/miniapp/routes/resolve?path=/manager/tournaments/opaque-reference/settings"),
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
        },
    )

    response = await http._resolve_route(request)

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["resource"]["kind"] == "manager_settings"
    operation = gateway.requests[0].operation
    assert operation.action == ActionCode.TOURNAMENT_MANAGER_SETTINGS
    assert operation.tournament_id == UUID(int=10)


async def test_manager_author_search_is_name_filtered_and_reference_bound() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        launch_references=FakeLaunchReferences(),  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "GET",
        "/api/miniapp/manager/tournaments/opaque-reference/authors?query=Ada",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
        },
        match_info={"launch_ref": "opaque-reference"},
    )

    response = await http._search_manager_authors(request)

    assert response.status == 200
    assert json.loads(response.text)["items"][0]["display_name"] == "Ada Lovelace"
    operation = gateway.requests[0].operation
    assert operation.action == ActionCode.AUTHORS_SEARCH
    assert operation.query == "Ada"


async def test_manager_management_route_is_actor_bound() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        launch_references=FakeManagementLaunchReferences(),  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "GET",
        "/api/miniapp/routes/resolve?path=/manager/tournaments/opaque-reference/management",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
        },
    )

    response = await http._resolve_route(request)

    assert response.status == 200
    assert json.loads(response.text)["resource"]["kind"] == "manager_management"
    operation = gateway.requests[0].operation
    assert operation.action == ActionCode.TOURNAMENT_MANAGER_MANAGEMENT
    assert operation.tournament_id == UUID(int=10)


async def test_lobby_route_rechecks_reference_and_returns_backend_capabilities() -> None:
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        launch_references=FakeLobbyLaunchReferences(),  # type: ignore[arg-type]
    )
    request = make_mocked_request(
        "GET",
        "/api/miniapp/routes/resolve?path=/lobbies/opaque-lobby",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
        },
    )

    response = await http._resolve_route(request)

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["resource"]["kind"] == "lobby"
    assert payload["resource"]["available_actions"] == ["ready", "leave"]
    assert gateway.requests[0].operation.action == ActionCode.LOBBY_INFO


@pytest.mark.parametrize(
    "command, action",
    [
        ("packet-select", ActionCode.LOBBY_PACKET_SELECT),
        ("packet-remove", ActionCode.LOBBY_PACKET_REMOVE),
        ("settings", ActionCode.LOBBY_SETTINGS_UPDATE),
    ],
)
async def test_lobby_mutation_does_not_reuse_write_authorization_for_read(command, action):
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway, launch_references=FakeLobbyLaunchReferences())
    request = make_mocked_request(
        "POST",
        f"/api/miniapp/lobbies/opaque-lobby/{command}",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "mutation-test",
        },
        match_info={"launch_ref": "opaque-lobby", "command": command},
    )
    body = {"expected_version": 3}
    if command == "settings":
        body["changes"] = {"theme_count": 2}
    else:
        body["packet_id"] = str(UUID(int=30))
    request._read_bytes = json.dumps(body).encode()
    result = await http._mutate_lobby(request)
    assert result.status == 200
    assert json.loads(result.text)["selected"] is True
    assert [request.operation.action for request in gateway.requests] == [action]
