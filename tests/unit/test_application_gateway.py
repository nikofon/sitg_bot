from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sitg_bot.application.adapters import TelegramGatewayAdapter
from sitg_bot.application.contracts import (
    ActionCode,
    AdminAuthenticateOperation,
    CapabilitiesOperation,
    ErrorCode,
    GatewayRequest,
    GatewayResponse,
)
from sitg_bot.application.gateway import ACTION_POLICIES, ApplicationGateway
from sitg_bot.services.admin_auth import PlatformAdminAuthenticationService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.telegram_auth import TelegramUpdateClaim


def _request(operation: dict[str, object], *, idempotency_key: str | None = None) -> GatewayRequest:
    return GatewayRequest.model_validate(
        {
            "metadata": {
                "channel": "mini_app",
                "client_name": "test-mini-app",
                "client_version": "1.2.3",
                "idempotency_key": idempotency_key,
            },
            "operation": operation,
        }
    )


def test_contract_selects_a_typed_versioned_operation() -> None:
    request = _request(
        {
            "action": ActionCode.LOBBY_READY_UPDATE,
            "lobby_id": str(UUID(int=1)),
            "ready": True,
            "expected_version": 7,
        },
        idempotency_key="telegram-update:42",
    )

    assert request.operation.action == ActionCode.LOBBY_READY_UPDATE
    assert request.operation.expected_version == 7  # type: ignore[union-attr]


def test_tournament_creation_contract_uses_an_owned_token_reference() -> None:
    request = _request(
        {
            "action": ActionCode.TOURNAMENT_CREATE,
            "token_id": str(UUID(int=9)),
            "name": "Autumn Open",
            "slug": "autumn-open",
            "type_key": "ladder",
            "game_ruleset_key": "si",
            "visibility": "private",
            "language": "en",
        },
        idempotency_key="telegram-update:43",
    )

    assert request.operation.action == ActionCode.TOURNAMENT_CREATE
    assert request.operation.token_id == UUID(int=9)  # type: ignore[union-attr]
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_CREATE].idempotency_required


def test_tournament_listing_contract_carries_filters_without_trusting_identity() -> None:
    request = _request(
        {
            "action": ActionCode.TOURNAMENT_LIST,
            "role": "manager",
            "phase": "ongoing",
            "relationship": "managed",
            "registration": "open",
            "search": "autumn-open",
            "order": "name_asc",
            "limit": 25,
        }
    )

    assert request.operation.action == ActionCode.TOURNAMENT_LIST
    assert request.operation.role == "manager"  # type: ignore[union-attr]
    assert request.operation.search == "autumn-open"  # type: ignore[union-attr]
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_LIST].cursor_paginated


def test_tournament_registration_is_an_idempotent_mutation() -> None:
    request = _request(
        {
            "action": ActionCode.TOURNAMENT_REGISTER,
            "tournament_id": str(UUID(int=15)),
        },
        idempotency_key="miniapp-register-15",
    )

    assert request.operation.action == ActionCode.TOURNAMENT_REGISTER
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_REGISTER].mutation
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_REGISTER].idempotency_required


def test_lobby_creation_contract_is_scoped_and_idempotent() -> None:
    request = _request(
        {
            "action": ActionCode.LOBBY_CREATE,
            "tournament_id": str(UUID(int=15)),
            "max_players": 6,
        },
        idempotency_key="telegram-lobby-create-15",
    )

    assert request.operation.action == ActionCode.LOBBY_CREATE
    assert request.operation.max_players == 6  # type: ignore[union-attr]
    assert ACTION_POLICIES[ActionCode.LOBBY_CREATE].idempotency_required


def test_manager_tournament_settings_mutations_are_versioned_and_idempotent() -> None:
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE].mutation
    assert (
        ACTION_POLICIES[ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE].stale_write_field
        == "expected_version"
    )
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_FINALIZE].idempotency_required
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_FINALIZE].stale_write_field == "expected_version"


def test_contract_rejects_unknown_fields_and_missing_stale_write_version() -> None:
    with pytest.raises(ValidationError):
        _request(
            {
                "action": ActionCode.LOBBY_READY_UPDATE,
                "lobby_id": str(UUID(int=1)),
                "ready": True,
                "raw_telegram_update": {"message": "must not enter the gateway"},
            },
            idempotency_key="telegram-update:42",
        )


def test_every_gateway_mutation_requires_an_idempotency_key() -> None:
    assert all(
        policy.idempotency_required for policy in ACTION_POLICIES.values() if policy.mutation
    )


def test_admin_authentication_credential_is_typed_but_hidden_from_repr() -> None:
    operation = AdminAuthenticateOperation(
        action=ActionCode.ADMIN_AUTHENTICATE,
        credential="server-held-secret",
    )

    assert "server-held-secret" not in repr(operation)
    assert ACTION_POLICIES[ActionCode.ADMIN_AUTHENTICATE].idempotency_required


def test_admin_authentication_capability_requires_server_configuration() -> None:
    unavailable = ApplicationGateway(object())  # type: ignore[arg-type]
    configured = ApplicationGateway(
        object(),  # type: ignore[arg-type]
        admin_authentication=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert ActionCode.ADMIN_AUTHENTICATE not in {
        item.action for item in unavailable.capabilities().actions
    }
    assert ActionCode.ADMIN_AUTHENTICATE in {
        item.action for item in configured.capabilities().actions
    }


def test_admin_authentication_requires_a_strong_server_credential() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        PlatformAdminAuthenticationService(
            object(),
            credential="too-short",  # type: ignore[arg-type]
        )


def test_capabilities_are_sorted_and_omit_unconfigured_token_workflow() -> None:
    gateway = ApplicationGateway(object())  # type: ignore[arg-type]

    capabilities = gateway.capabilities()
    actions = tuple(item.action for item in capabilities.actions)

    assert actions == tuple(sorted(actions, key=lambda action: action.value))
    assert ActionCode.CAPABILITIES in actions
    assert ActionCode.TOKEN_CLAIM not in actions
    assert ActionCode.LOBBY_CREATE not in actions
    lobby_ready = next(
        item for item in capabilities.actions if item.action == ActionCode.LOBBY_READY_UPDATE
    )
    assert lobby_ready.idempotency_required
    assert lobby_ready.stale_write_field == "expected_version"


def test_audit_metadata_is_an_allowlist_without_sensitive_values() -> None:
    request = _request(
        {
            "action": ActionCode.PLAYER_REPORT,
            "game_id": str(UUID(int=2)),
            "target_telegram_user_id": 123456,
            "kind": "toxicity",
            "details": "private report text",
            "expected_version": 4,
        },
        idempotency_key="mini-app-mutation:7",
    )

    assert ApplicationGateway._resource_metadata(request) == {"game_id": str(UUID(int=2))}


def test_gateway_maps_concurrency_failure_to_a_stable_error_code() -> None:
    error = ApplicationGateway._error(
        StaleWriteError("translated or internal text does not matter"),
        action=ActionCode.LOBBY_READY_UPDATE,
    )

    assert error.code == ErrorCode.STALE_WRITE
    assert error.message_key == "error.stale_write"


def test_lobby_version_guard_rejects_a_stale_snapshot() -> None:
    lobby = SimpleNamespace(version=8)

    with pytest.raises(StaleWriteError):
        InvitationMatchmakingService._require_version(lobby, 7)

    InvitationMatchmakingService._require_version(lobby, 8)


@pytest.mark.asyncio
async def test_telegram_adapter_derives_update_idempotency_without_storage_access() -> None:
    class CapturingGateway:
        request: GatewayRequest | None = None

        async def execute(self, principal: object, request: GatewayRequest) -> GatewayResponse:
            self.request = request
            return GatewayResponse(
                action=ActionCode.CAPABILITIES,
                correlation_id=request.metadata.correlation_id,
                ok=True,
                data={},
            )

    gateway = CapturingGateway()
    adapter = TelegramGatewayAdapter(  # type: ignore[arg-type]
        gateway,
        client_version="2.0.0",
        bot_id=99,
        environment="test",
    )
    claim = TelegramUpdateClaim(
        receipt_id=uuid4(),
        correlation_id=uuid4(),
        bot_id=99,
        environment="test",
        update_id=456,
        telegram_user_id=123,
    )

    await adapter.execute_update(
        claim,
        CapabilitiesOperation(action=ActionCode.CAPABILITIES),
    )

    assert gateway.request is not None
    assert gateway.request.metadata.channel == "telegram_bot"
    assert gateway.request.metadata.idempotency_key == "telegram-update:99:test:456"
    assert gateway.request.metadata.correlation_id == claim.correlation_id
