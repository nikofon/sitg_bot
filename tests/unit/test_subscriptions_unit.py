import json
from uuid import uuid4

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError
from test_miniapp_http import FakeAuth, FakeGateway, FakeManagementLaunchReferences

from sitg_bot.application.contracts import ActionCode, TournamentSubscriptionsUpdateOperation
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.miniapp_http import MiniAppHttpServer


def operation(values, command="create"):
    return TournamentSubscriptionsUpdateOperation.model_validate(
        {
            "action": ActionCode.TOURNAMENT_SUBSCRIPTIONS_UPDATE,
            "tournament_id": uuid4(),
            "expected_version": 1,
            "command": command,
            "values": values,
        }
    )


@pytest.mark.parametrize(
    "values",
    [
        {"name": " "},
        {"name": "x", "packet_count": 0},
        {"name": "x", "packet_count": -1},
        {"name": "x", "packet_count": True},
        {"name": "x", "packet_count": 1.5},
        {"name": "x", "packet_count": "52"},
        {"name": "x", "readable": "yes"},
        {"name": "x", "playable": 1},
        {"name": "x", "unexpected": True},
    ],
)
def test_subscription_values_are_strict(values):
    with pytest.raises(ValidationError):
        operation(values)


def test_subscription_defaults_and_mutation_policy():
    parsed = operation({"name": " Yearly ", "packet_count": 52, "playable": True})
    assert parsed.values == {
        "name": "Yearly",
        "packet_count": 52,
        "discoverable": None,
        "readable": None,
        "playable": True,
    }
    assert operation({"name": "Unlimited"}).values["packet_count"] is None
    policy = ACTION_POLICIES[parsed.action]
    assert policy.mutation and policy.idempotency_required
    assert policy.stale_write_field == "expected_version"


@pytest.mark.parametrize(
    "command,values",
    [("assign", {"card_id": str(uuid4())}), ("revoke", {"subscription_id": "bad"})],
)
def test_subscription_commands_require_valid_identifiers(command, values):
    with pytest.raises(ValidationError):
        operation(values, command)


async def test_subscription_http_uses_authorized_tournament_scope():
    gateway = FakeGateway()
    http = MiniAppHttpServer(
        FakeAuth(), gateway, launch_references=FakeManagementLaunchReferences()
    )
    request = make_mocked_request(
        "POST",
        "/api/miniapp/manager/tournaments/opaque-reference/subscriptions",
        headers={
            "Origin": "https://mini.example.test",
            "Cookie": "__Host-sitg_session=test-session",
            "Content-Type": "application/json",
            "X-CSRF-Token": "csrf",
            "X-Idempotency-Key": "subscription-create",
        },
        match_info={"launch_ref": "opaque-reference"},
    )

    async def body():
        return {
            "tournament_id": str(uuid4()),
            "expected_version": 1,
            "command": "create",
            "values": {"name": "Yearly", "packet_count": 52, "playable": True},
        }

    request.json = body
    response = await http._update_subscriptions(request)
    assert response.status == 200
    parsed = gateway.requests[0]
    assert parsed.operation.tournament_id.int == 10
    assert parsed.operation.action == ActionCode.TOURNAMENT_SUBSCRIPTIONS_UPDATE
    assert parsed.metadata.idempotency_key == "subscription-create"
    assert json.loads(response.text) is not None
