import json
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import (
    ActionCode,
    AdminGameListOperation,
    AdminPacketAccessOperation,
    AdminPacketListOperation,
    AdminTournamentModerateOperation,
    AdminTournamentRatingWeightOperation,
    GatewayResponse,
)
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.miniapp_http import MiniAppHttpServer


@pytest.mark.parametrize(
    "section",
    ["tournaments", "authors", "players", "packets", "link_requests",
     "ongoing_games", "active_lobbies", "past_games"],
)
@pytest.mark.parametrize("include_items", ["true", "false"])
async def test_management_route_resolves_requested_section(section, include_items):
    class CatalogueGateway(FakeGateway):
        async def execute(self, principal, request):
            self.requests.append(request)
            assert request.operation.action == ActionCode.ADMIN_MANAGEMENT_LIST
            assert request.operation.section == section
            assert request.operation.include_items is (include_items == "true")
            return GatewayResponse(action=request.operation.action,
                correlation_id=request.metadata.correlation_id, ok=True,
                data={"kind": "admin_management", "state": "ready", "section": section,
                      "items": []})

    gateway = CatalogueGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    request = make_mocked_request("GET",
        f"/api/miniapp/routes/resolve?path=%2Fadmin%2Fmanagement%3Fsection%3D{section}"
        f"&include_items={include_items}", headers={
            "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
        })
    response = await http._resolve_route(request)
    assert response.status == 200
    assert json.loads(response.text)["resource"]["section"] == section


async def test_admin_packet_page_is_authenticated_and_uses_version_cursor():
    class RecordingAuth(FakeAuth):
        async def authorize_request(self, session_token, **values):
            assert values["method"] == "GET"
            assert values["action"] == ActionCode.ADMIN_PACKET_LIST
            assert "csrf_token" not in values
            return await super().authorize_request(session_token, **values)

    gateway = FakeGateway()
    http = MiniAppHttpServer(RecordingAuth(), gateway)
    request = make_mocked_request("GET",
        f"/api/miniapp/admin/management/packets?cursor={UUID(int=7)}&limit=25", headers={
            "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
        })
    match = await http.application.router.resolve(request)
    assert match.handler == http._admin_packet_page
    response = await http._admin_packet_page(request)
    assert response.status == 200
    operation = gateway.requests[0].operation
    assert isinstance(operation, AdminPacketListOperation)
    assert operation.cursor == UUID(int=7) and operation.limit == 25
    policy = ACTION_POLICIES[ActionCode.ADMIN_PACKET_LIST]
    assert policy.authentication_required and policy.cursor_paginated and not policy.mutation


async def test_admin_game_page_is_authenticated_and_uses_game_cursor():
    class RecordingAuth(FakeAuth):
        async def authorize_request(self, session_token, **values):
            assert values["method"] == "GET"
            assert values["action"] == ActionCode.ADMIN_GAME_LIST
            assert "csrf_token" not in values
            return await super().authorize_request(session_token, **values)

    gateway = FakeGateway()
    http = MiniAppHttpServer(RecordingAuth(), gateway)
    request = make_mocked_request("GET",
        f"/api/miniapp/admin/management/past_games?cursor={UUID(int=7)}&limit=25", headers={
            "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
        })
    match = await http.application.router.resolve(request)
    assert match.handler == http._admin_game_page
    response = await http._admin_game_page(request)
    assert response.status == 200
    operation = gateway.requests[0].operation
    assert isinstance(operation, AdminGameListOperation)
    assert operation.cursor == UUID(int=7) and operation.limit == 25
    policy = ACTION_POLICIES[ActionCode.ADMIN_GAME_LIST]
    assert policy.authentication_required and policy.cursor_paginated and not policy.mutation


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": 26}, {"cursor": "invalid"}])
def test_admin_game_page_validation(values):
    with pytest.raises(ValidationError):
        AdminGameListOperation(action=ActionCode.ADMIN_GAME_LIST, **values)


@pytest.mark.parametrize("values", [{}, {"cursor": None, "limit": 1}])
def test_admin_game_page_defaults(values):
    operation = AdminGameListOperation(action=ActionCode.ADMIN_GAME_LIST, **values)
    assert operation.cursor is None and 1 <= operation.limit <= 25


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": 26}, {"cursor": "invalid"}])
def test_admin_packet_page_validation(values):
    with pytest.raises(ValidationError):
        AdminPacketListOperation(action=ActionCode.ADMIN_PACKET_LIST, **values)


@pytest.mark.parametrize(
    "section,command,body,action",
    [
        (
            "tournaments",
            "halt",
            {"expected_version": 2, "confirm": True},
            ActionCode.ADMIN_TOURNAMENT_MODERATE,
        ),
        (
            "tournaments",
            "resume",
            {"expected_version": 2, "confirm": True},
            ActionCode.ADMIN_TOURNAMENT_MODERATE,
        ),
        (
            "tournaments",
            "abolish",
            {"expected_version": 2, "confirm": True},
            ActionCode.ADMIN_TOURNAMENT_MODERATE,
        ),
        (
            "tournaments",
            "rating_weight",
            {"weight": 0.5, "expected_version": 2},
            ActionCode.ADMIN_TOURNAMENT_RATING_WEIGHT,
        ),
        ("authors", "link", {"target": "@nickname"}, ActionCode.ADMIN_AUTHOR_LINK),
        (
            "authors",
            "merge",
            {"merge_author_id": str(UUID(int=31)), "confirm": True},
            ActionCode.ADMIN_AUTHOR_MERGE,
        ),
        ("link_requests", "approve", {}, ActionCode.AUTHOR_LINK_ADMIN_DECIDE),
        ("link_requests", "reject", {}, ActionCode.AUTHOR_LINK_ADMIN_DECIDE),
        ("packets", "view", {"confirm": True}, ActionCode.ADMIN_PACKET_ACCESS),
        ("packets", "download", {"confirm": True}, ActionCode.ADMIN_PACKET_ACCESS),
        ("players", "ban", {"reason": "Review"}, ActionCode.PLAYER_BAN),
        ("players", "unban", {}, ActionCode.PLAYER_UNBAN),
    ],
)
async def test_management_actions_use_authenticated_csrf_and_idempotency(
    section, command, body, action
):
    class RecordingAuth(FakeAuth):
        async def authorize_request(self, session_token, **values):
            assert values["csrf_token"] == "csrf"
            assert values["idempotency_key"] == "admin-request"
            assert values["method"] == "POST"
            return await super().authorize_request(session_token, **values)

    gateway = FakeGateway()
    http = MiniAppHttpServer(RecordingAuth(), gateway)
    resource_id = str(UUID(int=30))
    path = f"/api/miniapp/admin/management/{section}/{resource_id}/{command}"
    request = make_mocked_request(
        "POST",
        path,
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "admin-request",
        },
        match_info={"section": section, "resource_id": resource_id, "command": command},
    )
    request._read_bytes = json.dumps(body).encode()
    assert (await http._admin_management_action(request)).status == 200
    assert gateway.requests[0].operation.action == action
    assert ACTION_POLICIES[action].mutation
    assert ACTION_POLICIES[action].idempotency_required


@pytest.mark.parametrize(
    "contract,values",
    [
        (
            AdminTournamentModerateOperation,
            {
                "action": ActionCode.ADMIN_TOURNAMENT_MODERATE,
                "tournament_id": UUID(int=1),
                "command": "abolish",
                "expected_version": 1,
            },
        ),
        (
            AdminPacketAccessOperation,
            {
                "action": ActionCode.ADMIN_PACKET_ACCESS,
                "version_id": UUID(int=1),
                "command": "view",
            },
        ),
    ],
)
def test_confirmation_is_explicit_and_caller_cannot_supply_authority(contract, values):
    assert contract.model_validate(values).confirm is False
    for extra in ({"confirm": "true"}, {"administrator_id": UUID(int=2)}, {"role": "admin"}):
        with pytest.raises(ValidationError):
            contract.model_validate({**values, **extra})


def test_rating_weight_operation_bounds_and_caller_cannot_supply_authority():
    values = {
        "action": ActionCode.ADMIN_TOURNAMENT_RATING_WEIGHT,
        "tournament_id": UUID(int=1),
        "weight": 0.5,
        "expected_version": 1,
    }
    assert AdminTournamentRatingWeightOperation.model_validate(values).weight == 0.5
    for weight in (0.05, 1.05, 0, -1):
        with pytest.raises(ValidationError):
            AdminTournamentRatingWeightOperation.model_validate({**values, "weight": weight})
    for extra in ({"administrator_id": UUID(int=2)}, {"role": "admin"}):
        with pytest.raises(ValidationError):
            AdminTournamentRatingWeightOperation.model_validate({**values, **extra})
