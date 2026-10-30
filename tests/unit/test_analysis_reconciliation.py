from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sitg_bot.services.analysis_jobs import AnalysisRequestView, AnalysisService, analysis_job_key
from sitg_bot.services.training_ai import (
    BestBuzzSource,
    CommonKnowledgeSource,
    DissectionSource,
    FakeAnalysisProvider,
    ProviderOutcomeUncertain,
    ProviderSettings,
    source_descriptor,
)
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.models import (
    DurableJobRecord,
    KnowledgeDissectionRecord,
    PacketVersionRecord,
    PlayerRecord,
    TrainingAnalysisArtifactRecord,
)


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("kind", ["dissection", "best_buzz", "common_knowledge"])
async def test_production_request_admits_entitled_requests_and_replays(monkeypatch, kind, replay):
    owner, packet, question = uuid4(), uuid4(), uuid4()
    source = {
        "dissection": DissectionSource(packet, (question,)),
        "best_buzz": BestBuzzSource(packet, (question,)),
        "common_knowledge": CommonKnowledgeSource("A test fact"),
    }[kind]
    moment = datetime.now(UTC)
    descriptor = source_descriptor(source) | {"private": False, "quota_units": 1}
    existing = SimpleNamespace(
        request_id="admission", owner_player_id=owner, instructions="", source=descriptor,
        kind=kind, status="queued", error=None, created_at=moment, completed_at=None,
    )
    added = []

    async def flush():
        for row in added:
            row.status = "queued"

    session = SimpleNamespace(
        get=AsyncMock(), scalar=AsyncMock(return_value=0), scalars=AsyncMock(return_value=[]),
        add=added.append,
        flush=AsyncMock(side_effect=flush),
    )
    entitlements = SimpleNamespace(
        policy_for=AsyncMock(return_value=SimpleNamespace(enabled=True, queued_request_cap=5)),
        authorize=AsyncMock(),
    )
    loader = SimpleNamespace(authorize_in_session=AsyncMock())
    provider = FakeAnalysisProvider()
    service = AnalysisService(
        object(), object(), provider, ProviderSettings("fake", "m"),
        entitlements=entitlements, source_loader=loader,
    )
    service._request_row = AsyncMock(return_value=existing if replay else None)
    artifact = SimpleNamespace(id=uuid4(), status="pending", generation=1)
    service._artifact_for_key = AsyncMock(return_value=artifact)
    schedule = AsyncMock()
    monkeypatch.setattr(
        "sitg_bot.services.analysis_jobs.DurableJobQueue.schedule_in_session", schedule,
    )

    result = await service.request(
        session, owner_player_id=owner, source=source, idempotency_key="admission", now=moment,
    )

    assert (result.request_id, result.kind, result.status) == ("admission", kind, "queued")
    loader.authorize_in_session.assert_awaited_once_with(session, owner, descriptor)
    assert session.get.await_args_list[0].args == (PlayerRecord, owner)
    assert session.get.await_args_list[0].kwargs == {
        "with_for_update": {"key_share": True}, "populate_existing": True,
    }
    assert all(call.args == (PlayerRecord, owner) for call in session.get.await_args_list)
    if replay:
        assert added == []
        service._artifact_for_key.assert_not_awaited()
        entitlements.authorize.assert_not_awaited()
        schedule.assert_not_awaited()
        session.flush.assert_not_awaited()
    else:
        assert len(added) == 1
        assert added[0].owner_player_id == owner and added[0].source == descriptor
        entitlements.authorize.assert_awaited_once_with(
            session, player_id=owner, feature=kind, request_id="admission", units=1, now=moment,
        )
        schedule.assert_awaited_once()
        assert schedule.await_args.args[2:] == (
            analysis_job_key(artifact.id, 1),
            {"artifact_id": str(artifact.id), "generation": 1},
        )
        session.flush.assert_awaited_once()
    assert provider.extract_calls == provider.buzz_calls == provider.knowledge_calls == []


@pytest.mark.parametrize("current_status", ["pending", "running", "succeeded", "cancelled"])
async def test_reconciliation_does_not_fail_current_generation_using_a_stale_snapshot(
    current_status,
):
    artifact_id = uuid4()
    stale = SimpleNamespace(id=artifact_id, status="pending", generation=1)
    current = SimpleNamespace(
        id=artifact_id, status=current_status, generation=2,
        lease_expires_at=None, lease_token=None, last_error=None,
    )

    async def scalar(statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is TrainingAnalysisArtifactRecord:
            return current
        assert entity is DurableJobRecord
        keys = statement.compile().params.values()
        status = "queued" if analysis_job_key(artifact_id, 2) in keys else "cancelled"
        return SimpleNamespace(status=status, last_error=None)

    session = SimpleNamespace(scalar=scalar)
    service = AnalysisService(
        object(), object(), FakeAnalysisProvider(), ProviderSettings("fake", "m"),
    )
    service._complete_requests = AsyncMock()
    await service._reconcile_artifact(session, stale, datetime.now(UTC))
    assert current.status == current_status and current.generation == 2
    if current_status in ("succeeded", "cancelled"):
        # The combined delivery also repairs historical terminal artifacts with
        # unsettled waiters; settle the freshly loaded generation, never the stale one.
        service._complete_requests.assert_awaited_once()
        assert service._complete_requests.await_args.args[1:3] == (artifact_id, current_status)
    else:
        service._complete_requests.assert_not_awaited()


async def test_reconciliation_still_fails_a_current_generation_with_a_cancelled_job():
    current = SimpleNamespace(id=uuid4(), status="pending", generation=2)
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[
        current, SimpleNamespace(status="cancelled", last_error=None),
    ]))
    service = AnalysisService(
        object(), object(), FakeAnalysisProvider(), ProviderSettings("fake", "m"),
    )
    service._complete_requests = AsyncMock()
    await service._reconcile_artifact(session, current, datetime.now(UTC))
    assert current.status == "failed" and current.generation == 2
    assert service._complete_requests.await_args.args[1:3] == (current.id, "failed")


@pytest.mark.parametrize("prior_status", ["failed", "cancelled"])
@pytest.mark.parametrize("reconciled", [False, True])
async def test_explicit_dissection_retry_links_pending_replacement(prior_status, reconciled):
    owner, packet, version, revision = (uuid4() for _ in range(4))
    row = SimpleNamespace(
        id=uuid4(), analysis_request_id="old-request", status="failed" if reconciled else "pending",
        application=None,
    )
    old_request = SimpleNamespace(status=prior_status)
    replacement = SimpleNamespace(status="queued", artifact_id=uuid4())

    async def get(model, identity, **kwargs):
        if model is KnowledgeDissectionRecord:
            return row
        if model is PacketVersionRecord:
            return SimpleNamespace(language="en")
        if model is PlayerRecord:
            return SimpleNamespace(preferred_locale="en")
        if model is TrainingAnalysisArtifactRecord:
            return SimpleNamespace(id=identity)
        raise AssertionError(model)

    added = []
    session = SimpleNamespace(
        get=get, refresh=AsyncMock(), scalars=AsyncMock(return_value=[]),
        scalar=AsyncMock(side_effect=[None, uuid4()]), add=added.append, flush=AsyncMock(),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    app = KnowledgeApplication(SimpleNamespace(transaction=transaction))
    app.packets.disclose = AsyncMock(return_value=SimpleNamespace(
        packet_id=packet, scope="training", fingerprint="fingerprint",
        questions=(SimpleNamespace(revision_id=revision, theme_revision_id=uuid4(),
                                   text="Question", answer="Answer", commentary="", source=""),),
    ))
    app.entitlements.policy_for = AsyncMock(return_value=SimpleNamespace(enabled=True))
    app.service = lambda session: SimpleNamespace(
        request_dissection=AsyncMock(return_value=SimpleNamespace(request_id=row.id)),
    )
    app.analysis = SimpleNamespace(
        provider=FakeAnalysisProvider(),
        _request_row=AsyncMock(side_effect=lambda session, key: (
            old_request if key == "old-request" else replacement
        )),
        _reconcile_artifact=AsyncMock(),
        request=AsyncMock(return_value=AnalysisRequestView(
            request_id="replacement-request", kind="dissection", status="queued", progress=None,
            error=None, retryable=False, created_at=datetime.now(UTC), completed_at=None,
        )),
    )
    result = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="explicit-retry",
    )
    created = next(value for value in added if isinstance(value, KnowledgeDissectionRecord))
    assert result["request_id"] == created.analysis_request_id == "replacement-request"
    assert created.status == "pending" and created.application is None
    assert row.analysis_request_id == "old-request"  # prior attempt remains immutable
    app.analysis.request.assert_awaited_once()


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
async def test_obsolete_worker_error_cannot_fail_or_expire_the_current_generation(error_type):
    expires = datetime.now(UTC)
    current = SimpleNamespace(status="running", generation=2, lease_expires_at=expires)
    session = SimpleNamespace(get=AsyncMock(return_value=current))

    @asynccontextmanager
    async def transaction():
        yield session

    app = KnowledgeApplication(SimpleNamespace(transaction=transaction))
    app.analysis.execute_job = AsyncMock(side_effect=error_type("Old worker failure"))
    app.analysis._fail_artifact = AsyncMock()
    app.reconcile = AsyncMock()
    payload = {"artifact_id": str(uuid4()), "generation": 1}
    if error_type is RuntimeError:
        with pytest.raises(RuntimeError):
            await app.analysis_job(payload)
    else:
        await app.analysis_job(payload)
    assert current.status == "running" and current.lease_expires_at == expires
    app.analysis._fail_artifact.assert_not_awaited()


@pytest.mark.parametrize("field", [
    None, "model", "prompt_version", "schema_version", "tokenizer_version",
])
@pytest.mark.parametrize("status", ["pending", "running"])
@pytest.mark.parametrize("kind", ["dissection", "best_buzz", "common_knowledge"])
async def test_execution_profile_is_checked_before_provider_computation(field, status, kind):
    settings = ProviderSettings("fake", "model-b")
    artifact = SimpleNamespace(
        id=uuid4(), generation=1, status=status, kind=kind, provider_usage=None,
        lease_expires_at=datetime.now(UTC) if status == "running" else None,
        lease_token=uuid4() if status == "running" else None,
        checkpoint={"chunks_done": 0, "partial": []},
        **{name: getattr(settings, name) for name in (
            "model", "prompt_version", "schema_version", "tokenizer_version",
        )},
    )
    if field is not None:
        setattr(artifact, field, "prior-profile")
    request = SimpleNamespace(owner_player_id=uuid4(), instructions="", source={}, status="queued")
    session = SimpleNamespace(
        get=AsyncMock(return_value=artifact),
        execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [request]),
        )),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    provider = FakeAnalysisProvider()
    service = AnalysisService(
        SimpleNamespace(transaction=transaction), object(), provider, settings
    )
    service._complete_requests = AsyncMock()
    service._run_packet_scan = AsyncMock()
    service._run_common_knowledge = AsyncMock()
    await service.execute_job({"artifact_id": str(artifact.id), "generation": 1})
    if field is None:
        assert artifact.status == "running" and request.status == "running"
        service._complete_requests.assert_not_awaited()
        if kind == "common_knowledge":
            service._run_common_knowledge.assert_awaited_once()
        else:
            service._run_packet_scan.assert_awaited_once()
        return
    assert artifact.status == "failed" and artifact.lease_token is None
    assert artifact.last_error == "Analysis execution profile changed"
    assert service._complete_requests.await_args.args[1:3] == (artifact.id, "failed")
    assert provider.extract_calls == provider.buzz_calls == provider.knowledge_calls == []
    service._run_packet_scan.assert_not_awaited()
    service._run_common_knowledge.assert_not_awaited()


async def test_statistics_snapshot_uses_producing_artifact_metadata(monkeypatch):
    request = SimpleNamespace(
        id=uuid4(), status="succeeded", source={"content_version": 1}, artifact_id=uuid4()
    )
    fact = SimpleNamespace(owner_id=uuid4(), id=uuid4(), content_version=1,
                           statistic_status="pending", deleted_at=None)
    artifact = SimpleNamespace(payload={"value": 25}, model="producing-model",
                               prompt_version="producing-prompt", schema_version="producing-schema")
    session = SimpleNamespace(
        execute=AsyncMock(return_value=[(request, fact)]), refresh=AsyncMock(),
        get=AsyncMock(return_value=artifact),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    app = KnowledgeApplication(SimpleNamespace(transaction=transaction))
    record = AsyncMock()
    app.service = lambda session: SimpleNamespace(record_statistics=record)
    monkeypatch.setattr("sitg_bot.services.training_knowledge.require_trainee", AsyncMock())
    await app.reconcile_statistics()
    common = record.await_args.kwargs["common"]
    assert common.value == 25 and common.model == "producing-model"
    assert common.prompt_version == "producing-prompt"
    assert common.schema_version == "producing-schema"


async def test_statistics_reconciliation_advances_past_unavailable_owner_batches(monkeypatch):
    unavailable, owner = uuid4(), uuid4()
    blocked = [(
        SimpleNamespace(id=uuid4(), source={"content_version": 1}),
        SimpleNamespace(id=uuid4(), owner_id=unavailable),
    ) for _ in range(100)]
    fact = SimpleNamespace(
        id=uuid4(), owner_id=owner, content_version=1, statistic_status="pending", deleted_at=None,
    )
    request = SimpleNamespace(
        id=uuid4(), status="succeeded", source={"content_version": 1}, artifact_id=uuid4(),
    )
    artifact = SimpleNamespace(
        payload={"value": 25}, model="model", prompt_version="1", schema_version="1",
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[blocked, [(request, fact)]]), refresh=AsyncMock(),
        get=AsyncMock(return_value=artifact),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    async def guard(session, player_id, *, lock=False):
        if player_id == unavailable:
            raise PermissionError("Unavailable owner")

    monkeypatch.setattr("sitg_bot.services.training_knowledge.require_trainee", guard)
    app = KnowledgeApplication(SimpleNamespace(transaction=transaction))
    record = AsyncMock()
    app.service = lambda session: SimpleNamespace(record_statistics=record)
    await app.reconcile_statistics()
    record.assert_awaited_once()
    assert record.await_args.args == (owner, fact.id)
    assert record.await_args.kwargs["common"].value == 25
    assert session.execute.await_count == 2
    session.refresh.assert_awaited_once_with(fact)


async def test_uncertain_provider_error_is_written_with_the_current_compute_token():
    settings = ProviderSettings("fake", "m")
    artifact = SimpleNamespace(
        id=uuid4(), generation=1, status="pending", kind="common_knowledge",
        lease_token=None, lease_expires_at=None, provider_usage=None,
        **{name: getattr(settings, name) for name in (
            "model", "prompt_version", "schema_version", "tokenizer_version",
        )},
    )
    request = SimpleNamespace(
        owner_player_id=uuid4(), instructions="", source={}, status="queued",
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=artifact),
        execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [request]),
        )),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    service = AnalysisService(
        SimpleNamespace(transaction=transaction), object(), FakeAnalysisProvider(), settings,
    )
    error = ProviderOutcomeUncertain("Disconnected", provider_request_id="old-call")
    service._run_common_knowledge = AsyncMock(side_effect=error)
    service._record_uncertain = AsyncMock()
    with pytest.raises(ProviderOutcomeUncertain):
        await service.execute_job({"artifact_id": str(artifact.id), "generation": 1})
    token = service._run_common_knowledge.await_args.args[1]
    assert token is not None and token == artifact.lease_token
    service._record_uncertain.assert_awaited_once_with(artifact.id, token, error)


async def test_applied_dissection_is_not_reset_or_requeued_by_an_explicit_retry():
    row = SimpleNamespace(status="succeeded", analysis_request_id="original", application=object())
    app = KnowledgeApplication(object())
    app.analysis._request_row = AsyncMock()
    await app._apply_ready(SimpleNamespace(), row)
    assert row.status == "succeeded" and row.analysis_request_id == "original"
    assert row.application is not None
    app.analysis._request_row.assert_not_awaited()
