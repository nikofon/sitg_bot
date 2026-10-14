from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sitg_bot.application.adapters import TelegramGatewayAdapter
from sitg_bot.application.contracts import (
    ActionCode,
    AdminAuthenticateOperation,
    ApplicationPrincipal,
    CapabilitiesOperation,
    ErrorCode,
    GatewayRequest,
    GatewayResponse,
    LobbyInfoOperation,
    NotificationReadOperation,
    PacketDraftTelegramBindOperation,
)
from sitg_bot.application.gateway import ACTION_POLICIES, ApplicationGateway
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.domain.game_rulesets import DEFAULT_RULESETS
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.services.admin_auth import PlatformAdminAuthenticationService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import (
    InvitationMatchmakingService,
    LobbyMemberSnapshot,
    LobbyReadinessError,
    LobbySnapshot,
    PacketSuggestion,
)
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


def test_lobby_info_keeps_suggestions_enabled_for_existing_callers() -> None:
    operation = LobbyInfoOperation(action=ActionCode.LOBBY_INFO, lobby_id=UUID(int=1))
    assert operation.include_packet_suggestions is True


@pytest.mark.parametrize("include_suggestions", [True, False])
async def test_lobby_info_dispatch_preserves_membership_check_and_suggestion_flag(
    include_suggestions,
):
    gateway = object.__new__(ApplicationGateway)
    gateway._require_active_principal = AsyncMock(return_value=UUID(int=2))
    gateway._reject_banned_player = AsyncMock()
    gateway._require_lobby_member = AsyncMock()
    gateway._lobby_payload = AsyncMock(return_value={})
    request = _request({
        "action": ActionCode.LOBBY_INFO, "lobby_id": str(UUID(int=1)),
        "include_packet_suggestions": include_suggestions,
    })

    await gateway._dispatch(ApplicationPrincipal(telegram_user_id=42), request)

    gateway._require_lobby_member.assert_awaited_once_with(UUID(int=1), UUID(int=2))
    gateway._lobby_payload.assert_awaited_once_with(
        UUID(int=1), 42, include_packet_suggestions=include_suggestions,
    )


@pytest.mark.parametrize("include_suggestions", [True, False])
@pytest.mark.parametrize("stored_indicator", [None, True, False])
async def test_lobby_payload_skips_hidden_suggestions_but_keeps_selected_packets(
    include_suggestions, stored_indicator,
):
    member = LobbyMemberSnapshot(42, "Owner", 1, False, ())
    selected = PacketSuggestion(UUID(int=3), UUID(int=4), "Selected", fresh_play_unit_count=2)
    lobby = LobbySnapshot(
        id=UUID(int=1), version=7, tournament_id=UUID(int=2), invitation_code="invite",
        status="assembling", max_players=4, expires_at=datetime.now(UTC) + timedelta(hours=1),
        game_id=None, searching=False, hybrid_matchmaking_available=False, search_started_at=None,
        merged_into_lobby_id=None, other_searching_lobby_count=0, other_searching_player_count=0,
        settings=GameSettings(question_fully_read_indication=stored_indicator is not False),
        selected_packets=(selected,), validation_violations=(),
        available_play_unit_count=2, members=(member,),
    )
    record = SimpleNamespace(
        creator_player_id=UUID(int=6), tournament_policy_version_id=UUID(int=5),
        settings={"theme_count": 8},
    )
    if stored_indicator is not None:
        record.settings["question_fully_read_indication"] = stored_indicator
    policy = SimpleNamespace(player_mutable_parameters=["theme_count"])
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[record, policy, SimpleNamespace(name="Tournament")]),
        scalar=AsyncMock(side_effect=[42, 3, "Owner"]),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
    )

    @asynccontextmanager
    async def sessions():
        yield session

    gateway = object.__new__(ApplicationGateway)
    gateway.database = SimpleNamespace(sessions=sessions)
    gateway.matchmaking = SimpleNamespace(
        get=AsyncMock(return_value=lobby), suggest_packets=AsyncMock(return_value=(selected,)),
    )
    gateway.tournaments = SimpleNamespace(
        context=AsyncMock(return_value=SimpleNamespace(
            ruleset_key="si", ruleset_version=1, observing_policy="forbidden",
        )), rulesets=DEFAULT_RULESETS,
    )

    payload = await gateway._lobby_payload(
        lobby.id, 42, include_packet_suggestions=include_suggestions,
    )

    assert payload["selected_packets"][0]["fresh_play_unit_count"] == 2
    assert payload["settings"]["theme_count"] == 8
    indicator = next(item for item in payload["setting_descriptors"]
                     if item["name"] == "question_fully_read_indication")
    assert indicator["value_type"] == "boolean"
    assert indicator["value"] is (stored_indicator is not False)
    assert payload["settings"]["question_fully_read_indication"] is indicator["value"]
    assert "packet_select" in payload["available_actions"]
    assert payload["last_event_sequence"] == 3
    if include_suggestions:
        gateway.matchmaking.suggest_packets.assert_awaited_once_with(lobby.id, telegram_user_id=42)
        assert payload["packet_suggestions"] == (selected,)
    else:
        gateway.matchmaking.suggest_packets.assert_not_awaited()
        assert gateway._json_value(payload)["packet_suggestions"] == []


def test_manager_tournament_settings_mutations_are_versioned_and_idempotent() -> None:
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE].mutation
    assert (
        ACTION_POLICIES[ActionCode.TOURNAMENT_MANAGER_SETTINGS_UPDATE].stale_write_field
        == "expected_version"
    )
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_FINALIZE].idempotency_required
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_FINALIZE].stale_write_field == "expected_version"


def test_packet_rule_contract_requires_version_and_packet_scope():
    operation = {
        "action": ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE,
        "assignment_id": str(UUID(int=1)), "right": "library_viewing_rule",
        "library_viewing_rule": "after-play", "expected_version": 3,
    }
    request = _request(operation, idempotency_key="packet-rule")
    assert request.operation.expected_version == 3
    assert ACTION_POLICIES[ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE].idempotency_required
    for changes in ({"expected_version": None}, {"player_id": str(UUID(int=2))},
                    {"library_viewing_rule": "invalid"}):
        with pytest.raises(ValidationError):
            _request({**operation, **changes}, idempotency_key="packet-rule")


def test_packet_default_access_contract_is_assignment_wide():
    operation = {
        "action": ActionCode.TOURNAMENT_PACKET_ACCESS_UPDATE,
        "assignment_id": str(UUID(int=1)), "right": "playable",
        "enabled": True, "scope": "default",
    }
    request = _request(operation, idempotency_key="packet-default")
    assert request.operation.scope == "default"  # type: ignore[union-attr]
    assert request.operation.player_id is None  # type: ignore[union-attr]
    for changes in ({"player_id": str(UUID(int=2))}, {"scope": "invalid"},
                    {"right": "library_viewing_rule"}):
        with pytest.raises(ValidationError):
            _request({**operation, **changes}, idempotency_key="packet-default")


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


@pytest.mark.parametrize(
    ("action", "failure", "reason", "code"),
    [
        (ActionCode.LOBBY_START, ValueError("Every lobby member must be ready"),
         "players_not_ready", ErrorCode.VALIDATION_FAILED),
        (ActionCode.LOBBY_START, ValueError("Cancel Find players before starting the lobby"),
         "search_active", ErrorCode.VALIDATION_FAILED),
        (ActionCode.LOBBY_START, ValueError("Select at least one packet before starting"),
         "packet_required", ErrorCode.VALIDATION_FAILED),
        (ActionCode.LOBBY_JOIN, ValueError("Lobby is full"),
         "full", ErrorCode.VALIDATION_FAILED),
        (ActionCode.LOBBY_CANCEL, PermissionError("Only the lobby creator may do this"),
         "creator_required", ErrorCode.FORBIDDEN),
        (ActionCode.LOBBY_INFO, PermissionError("Active lobby membership is required"),
         "membership_required", ErrorCode.FORBIDDEN),
        (ActionCode.LOBBY_JOIN, LookupError("Invitation not found"),
         "invitation_not_found", ErrorCode.NOT_FOUND),
        (ActionCode.LOBBY_INVITE, LookupError("Registered Telegram user not found"),
         "invitee_not_found", ErrorCode.NOT_FOUND),
        (ActionCode.LOBBY_PACKET_REMOVE, LookupError("Packet is not selected"),
         "packet_not_selected", ErrorCode.NOT_FOUND),
        (ActionCode.LOBBY_SEARCH_START, ValueError("The lobby has no viable packet selection"),
         "no_viable_packets", ErrorCode.VALIDATION_FAILED),
        (ActionCode.LOBBY_SETTINGS_UPDATE,
         PermissionError("Tournament locks parameter: ready_delay"),
         "setting_locked", ErrorCode.FORBIDDEN),
        (ActionCode.LOBBY_SETTINGS_UPDATE,
         ValueError("Maximum players cannot be below the current lobby size"),
         "capacity_too_small", ErrorCode.VALIDATION_FAILED),
    ],
)
def test_lobby_failures_have_localized_reasons(action, failure, reason, code) -> None:
    error = ApplicationGateway._error(failure, action=action)

    assert error.code == code
    assert error.message_key == f"lobby.error.{reason}"
    localization = LocalizationService()
    for locale in ("en", "ru"):
        assert localization.text(error.message_key, locale, **error.details)


@pytest.mark.parametrize(
    ("action", "failure", "key"),
    [
        (ActionCode.LOBBY_START, ValueError("private internal details"), "error.validation_failed"),
        (ActionCode.LOBBY_START, RuntimeError("Lobby is full"), "error.internal_error"),
        (ActionCode.GAME_ACT, ValueError("Lobby is full"), "error.validation_failed"),
    ],
)
def test_lobby_error_mapping_keeps_unknown_and_unrelated_errors_generic(action, failure, key):
    error = ApplicationGateway._error(failure, action=action)

    assert error.message_key == key
    assert not error.details


def test_start_validation_error_preserves_content_counts_and_filters_private_details() -> None:
    error = ApplicationGateway._error(
        LobbyReadinessError("insufficient_fresh_content", {
            "available": 1, "required": 3, "packet_id": "private", "actual": "invalid",
        }),
        action=ActionCode.LOBBY_START,
    )

    assert error.message_key == "lobby.readiness.insufficient_fresh_content"
    assert error.details == {"available": 1, "required": 3}


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

    keys = []
    for draft_id in (UUID(int=1), UUID(int=2), UUID(int=1)):
        await adapter.execute_update(claim, PacketDraftTelegramBindOperation(
            action=ActionCode.PACKET_DRAFT_TELEGRAM_BIND,
            draft_id=draft_id, chat_id=123, message_id=456, locale="ru",
        ))
        keys.append(gateway.request.metadata.idempotency_key)
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]


@pytest.mark.asyncio
async def test_telegram_adapter_keys_each_notification_read_independently() -> None:
    class CapturingGateway:
        request: GatewayRequest | None = None

        async def execute(self, principal: object, request: GatewayRequest) -> GatewayResponse:
            self.request = request
            return GatewayResponse(
                action=ActionCode.NOTIFICATIONS_READ,
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

    keys = []
    for notification_id in (UUID(int=1), UUID(int=2), UUID(int=1)):
        await adapter.execute_update(
            claim,
            NotificationReadOperation(
                action=ActionCode.NOTIFICATIONS_READ, notification_id=notification_id
            ),
        )
        keys.append(gateway.request.metadata.idempotency_key)

    # One Telegram update marks several displayed notifications as read; each
    # mutation needs its own idempotency key so retries stay conflict-free.
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]
    assert all(key.startswith("telegram-update:99:test:456:notification:") for key in keys)
