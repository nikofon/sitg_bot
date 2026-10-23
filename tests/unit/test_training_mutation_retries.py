from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.services.training import TrainingService
from sitg_bot.storage.models import (
    ApplicationIdempotencyRecord,
    PlayerBanRecord,
    PlayerRecord,
    TrainingSessionRecord,
)

OWNER = UUID(int=1)
RESOURCE = UUID(int=2)
QUESTION = UUID(int=3)

CASES = [
    (ActionCode.KNOWLEDGE_CREATE, {"title": "Fact", "description": "Private description"}),
    (ActionCode.KNOWLEDGE_UPDATE, {"fact_id": RESOURCE, "expected_version": 1,
                                 "description": "Changed"}),
    (ActionCode.KNOWLEDGE_DELETE, {"fact_id": RESOURCE}),
    (ActionCode.KNOWLEDGE_TAG_CREATE, {"name": "History", "colour": "#123456"}),
    (ActionCode.KNOWLEDGE_TAG_RENAME, {"tag_id": RESOURCE, "name": "Past"}),
    (ActionCode.KNOWLEDGE_TAG_DELETE, {"tag_id": RESOURCE}),
    (ActionCode.TRAINING_START, {"version_id": RESOURCE, "confirm": True}),
    (ActionCode.TRAINING_ACT, {"session_id": RESOURCE, "expected_version": 1,
                             "question_revision_id": QUESTION, "command": "buzz"}),
    (ActionCode.TRAINING_UPLOAD, {"source_filename": "private.json", "source_base64": "e30="}),
    (ActionCode.TRAINING_DRAFT_DECIDE, {"draft_id": RESOURCE, "expected_version": 1,
                                      "publish": True}),
]


class MemoryIdempotencyDatabase:
    def __init__(self):
        self.record = None
        self.banned = False
        self.player = SimpleNamespace(id=OWNER, status="active")
        self.training_session = SimpleNamespace(owner_id=OWNER, packet_version_id=RESOURCE)

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, statement):
        return None

    async def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is ApplicationIdempotencyRecord:
            return self.record
        if entity is PlayerBanRecord and self.banned:
            return "Banned"
        return None

    async def get(self, model, identity, **kwargs):
        if model is ApplicationIdempotencyRecord:
            return self.record
        if model is PlayerRecord:
            return self.player
        if model is TrainingSessionRecord:
            return self.training_session
        raise AssertionError(model)

    def add(self, record):
        record.id = uuid4()
        self.record = record

    async def flush(self):
        pass


def request_for(action, values):
    return GatewayRequest.model_validate({
        "metadata": {"channel": "mini_app", "client_name": "retry-test",
                     "client_version": "1", "idempotency_key": "same-key"},
        "operation": {"action": action, **values},
    })


def stack(action):
    database = MemoryIdempotencyDatabase()
    result = {"id": str(RESOURCE), "description": "Private response"}
    if action == ActionCode.TRAINING_UPLOAD:
        result = {"drafts": [{"draft_id": str(RESOURCE), "packet": {"name": "Private"}}]}
    mutation = AsyncMock(return_value=result)
    packets = SimpleNamespace(
        import_upload=mutation, decide_draft=mutation,
        draft=AsyncMock(), authorize=AsyncMock(),
    )
    training = SimpleNamespace(
        packets=packets, start=mutation, act=mutation, _owned=TrainingService._owned,
    )
    account = SimpleNamespace(player_id=OWNER, registration_status="active")
    gateway = ApplicationGateway(
        database, training=training, knowledge=SimpleNamespace(mutate=mutation),
    )
    gateway.player_accounts = SimpleNamespace(
        lookup_by_telegram_user_id=AsyncMock(return_value=account),
    )
    gateway._start_audit = AsyncMock(return_value=uuid4())
    gateway._finish_audit = AsyncMock()
    return gateway, database, mutation, account


@pytest.mark.parametrize(("action", "values"), CASES)
async def test_completed_private_mutation_replays_original_success_once(action, values):
    gateway, database, mutation, _ = stack(action)
    principal = ApplicationPrincipal(player_id=OWNER, telegram_user_id=42)
    first_request = request_for(action, values)
    first = await gateway.execute(principal, first_request)
    retry_request = request_for(action, values)
    retry = await gateway.execute(principal, retry_request)
    assert first.ok and retry.ok
    assert retry.data == first.data
    assert retry.correlation_id == retry_request.metadata.correlation_id
    assert retry.correlation_id != first.correlation_id
    assert database.record.status == "completed"
    mutation.assert_awaited_once()


@pytest.mark.parametrize(("action", "values"), CASES)
@pytest.mark.parametrize("revocation", ["ban", "registration", "identity"])
async def test_private_replay_rechecks_actor_without_destroying_original(
    action, values, revocation,
):
    gateway, database, mutation, account = stack(action)
    principal = ApplicationPrincipal(player_id=OWNER, telegram_user_id=42)
    request = request_for(action, values)
    first = await gateway.execute(principal, request)
    assert first.ok
    if revocation == "ban":
        database.banned = True
    elif revocation == "registration":
        account.registration_status = "registration"
    else:
        principal = ApplicationPrincipal(player_id=QUESTION, telegram_user_id=42)
    denied = await gateway.execute(principal, request)
    assert not denied.ok and denied.data is None
    assert denied.error.code.value in {"forbidden", "authentication_required"}
    assert database.record.status == "completed"
    database.banned = False
    account.registration_status = "active"
    restored = await gateway.execute(
        ApplicationPrincipal(player_id=OWNER, telegram_user_id=42), request,
    )
    assert restored.ok and restored.data == first.data
    mutation.assert_awaited_once()


@pytest.mark.parametrize(("action", "values"), CASES[7:])
async def test_training_replay_rechecks_source_and_draft_authorization(action, values):
    gateway, database, mutation, _ = stack(action)
    principal = ApplicationPrincipal(player_id=OWNER, telegram_user_id=42)
    request = request_for(action, values)
    assert (await gateway.execute(principal, request)).ok
    gateway.training.packets.authorize.side_effect = LookupError("Packet unavailable")
    gateway.training.packets.draft.side_effect = LookupError("Draft unavailable")
    denied = await gateway.execute(principal, request)
    assert not denied.ok and denied.error.code.value == "not_found"
    assert denied.data is None
    assert database.record.status == "completed"
    mutation.assert_awaited_once()


async def test_stop_retry_remains_available_after_source_retirement():
    values = {**next(values for action, values in CASES if action == ActionCode.TRAINING_ACT),
              "command": "stop"}
    gateway, _, mutation, _ = stack(ActionCode.TRAINING_ACT)
    principal = ApplicationPrincipal(player_id=OWNER, telegram_user_id=42)
    request = request_for(ActionCode.TRAINING_ACT, values)
    first = await gateway.execute(principal, request)
    gateway.training.packets.authorize.side_effect = LookupError("Packet retired")
    retry = await gateway.execute(principal, request)
    assert first.ok and retry.ok and first.data == retry.data
    mutation.assert_awaited_once()
    gateway.training.packets.authorize.assert_not_awaited()


async def test_legacy_completion_marker_does_not_redispatch_private_mutation():
    action, values = CASES[0]
    gateway, database, mutation, _ = stack(action)
    principal = ApplicationPrincipal(player_id=OWNER, telegram_user_id=42)
    request = request_for(action, values)
    assert (await gateway.execute(principal, request)).ok
    database.record.response_payload = {"completed": True}
    retry = await gateway.execute(principal, request)
    assert not retry.ok and retry.error.code.value == "request_indeterminate"
    mutation.assert_awaited_once()


async def test_changed_payload_with_same_key_conflicts_without_mutating():
    action, values = CASES[0]
    gateway, _, mutation, _ = stack(action)
    principal = ApplicationPrincipal(player_id=OWNER, telegram_user_id=42)
    assert (await gateway.execute(principal, request_for(action, values))).ok
    retry = await gateway.execute(principal, request_for(action, {**values, "title": "Other"}))
    assert not retry.ok and retry.error.code.value == "idempotency_conflict"
    mutation.assert_awaited_once()
