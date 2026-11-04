import json
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import (
    ActionCode,
    AdminAIPlayersOperation,
    AdminAIResetOperation,
    AdminAIUpdateOperation,
    AdminPacketAccessOperation,
    AdminPacketListOperation,
    AdminTournamentModerateOperation,
    AdminTournamentRatingWeightOperation,
    GatewayResponse,
)
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.miniapp_http import MiniAppHttpServer

AI_SETTINGS = {
    "expected_version": 1, "dissection_enabled": True, "best_buzz_enabled": False,
    "free": {"dissection": 0, "best_buzz": 24},
    "premium": {"dissection": None, "best_buzz": 100}, "reset_period": "week",
}


@pytest.mark.parametrize("path,action", [
    ("settings", ActionCode.ADMIN_AI_SETTINGS),
    ("provider", ActionCode.ADMIN_AI_PROVIDER),
    ("players?search=Alice&limit=25", ActionCode.ADMIN_AI_PLAYERS),
])
async def test_ai_admin_queries_use_existing_session_boundary(path, action):
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    request = make_mocked_request("GET", "/api/miniapp/admin/ai/" + path, headers={
        "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
    })
    match = await http.application.router.resolve(request)
    assert match.handler == http._admin_ai_query
    assert (await match.handler(request)).status == 200
    assert gateway.requests[0].operation.action == action
    assert ACTION_POLICIES[action].authentication_required
    assert not ACTION_POLICIES[action].mutation


@pytest.mark.parametrize("path,body,action", [
    ("settings", AI_SETTINGS, ActionCode.ADMIN_AI_UPDATE),
    ("tier", {"player_id": str(UUID(int=30)), "tier": "no_ai"}, ActionCode.ADMIN_AI_TIER),
    ("reset", {"expected_version": 1, "confirm": True}, ActionCode.ADMIN_AI_RESET),
])
async def test_ai_admin_mutations_require_csrf_and_idempotency(path, body, action):
    class RecordingAuth(FakeAuth):
        async def authorize_request(self, session_token, **values):
            assert values["csrf_token"] == "csrf"
            assert values["idempotency_key"] == "ai-admin-key"
            assert values["method"] == "POST"
            return await super().authorize_request(session_token, **values)

    gateway = FakeGateway()
    http = MiniAppHttpServer(RecordingAuth(), gateway)
    request = make_mocked_request("POST", "/api/miniapp/admin/ai/" + path, headers={
        "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
        "X-CSRF-Token": "csrf", "X-Idempotency-Key": "ai-admin-key",
        "Content-Type": "application/json",
    })
    request._read_bytes = json.dumps(body).encode()
    match = await http.application.router.resolve(request)
    assert match.handler == http._admin_ai_mutation
    assert (await match.handler(request)).status == 200
    assert gateway.requests[0].operation.action == action
    assert ACTION_POLICIES[action].mutation and ACTION_POLICIES[action].idempotency_required


async def test_removed_provider_check_route_is_not_available():
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    request = make_mocked_request("POST", "/api/miniapp/admin/ai/provider/check")
    match = await http.application.router.resolve(request)
    assert match.http_exception.status == 404
    assert not gateway.requests


@pytest.mark.parametrize("patch", [
    {"free": {"dissection": -1, "best_buzz": 24}},
    {"free": {"dissection": True, "best_buzz": 24}},
    {"free": {"dissection": "24", "best_buzz": 24}},
    {"free": {"dissection": 1.5, "best_buzz": 24}},
    {"free": {"dissection": 2147483648, "best_buzz": 24}},
    {"reset_period": "month"}, {"dissection_enabled": "true"}, {"api_key": "forbidden"},
])
def test_ai_admin_settings_validation(patch):
    with pytest.raises(ValidationError):
        AdminAIUpdateOperation.model_validate({
            "action": ActionCode.ADMIN_AI_UPDATE, **AI_SETTINGS, **patch,
        })


@pytest.mark.parametrize("values", [{"search": " "}, {"search": "a"},
    {"search": "ab", "limit": 26}, {"search": "a" * 101}])
def test_ai_admin_player_search_is_bounded(values):
    with pytest.raises(ValidationError):
        AdminAIPlayersOperation(action=ActionCode.ADMIN_AI_PLAYERS, **values)


@pytest.mark.parametrize("contract,action,values", [
    (AdminAIResetOperation, ActionCode.ADMIN_AI_RESET, {"expected_version": 1, "confirm": False}),
    (AdminAIResetOperation, ActionCode.ADMIN_AI_RESET, {"expected_version": 1}),
])
def test_ai_admin_reset_needs_confirmation(contract, action, values):
    with pytest.raises(ValidationError):
        contract(action=action, **values)


@pytest.mark.parametrize(
    "section", ["tournaments", "authors", "players", "packets", "link_requests", "ai"]
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
