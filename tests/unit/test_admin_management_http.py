import json
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import (
    ActionCode,
    AdminPacketAccessOperation,
    AdminTournamentModerateOperation,
    GatewayResponse,
)
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.miniapp_http import MiniAppHttpServer


@pytest.mark.parametrize("section", ["tournaments", "authors", "players", "packets"])
async def test_management_route_resolves_requested_section(section):
    class CatalogueGateway(FakeGateway):
        async def execute(self, principal, request):
            self.requests.append(request)
            assert request.operation.action == ActionCode.ADMIN_MANAGEMENT_LIST
            assert request.operation.section == section
            return GatewayResponse(action=request.operation.action,
                correlation_id=request.metadata.correlation_id, ok=True,
                data={"kind": "admin_management", "state": "ready", "section": section,
                      "items": []})

    gateway = CatalogueGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    request = make_mocked_request("GET",
        f"/api/miniapp/routes/resolve?path=%2Fadmin%2Fmanagement%3Fsection%3D{section}", headers={
            "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
        })
    response = await http._resolve_route(request)
    assert response.status == 200
    assert json.loads(response.text)["resource"]["section"] == section


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
        ("authors", "link", {"target": "@nickname"}, ActionCode.ADMIN_AUTHOR_LINK),
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
