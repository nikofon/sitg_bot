import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import create_player, seed_category
from sqlalchemy import func, select

from sitg_bot.domain.game_settings import announcement_tokens
from sitg_bot.services.analysis_jobs import (
    TRAINING_ANALYSIS_JOB_KIND,
    AnalysisFeatureDisabledError,
    AnalysisJobHandler,
    AnalysisQueuedCapExceededError,
    AnalysisQuotaExceededError,
    AnalysisService,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
)
from sitg_bot.services.entitlements import (
    FEATURE_DISSECTION,
    TRAINING_FEATURES,
    EntitlementService,
)
from sitg_bot.services.reliable_delivery import DurableJobQueue, DurableJobWorker
from sitg_bot.services.training_ai import (
    BestBuzzSource,
    BuzzBoundary,
    CommonKnowledgeSource,
    DissectionSource,
    FactCandidate,
    FakeAnalysisProvider,
    ProviderOutcomeUncertain,
    ProviderSettings,
    QuestionText,
    _parse_question_blocks,
    fact_candidate_id,
    text_fingerprint,
)
from sitg_bot.storage.models import DurableJobRecord, TrainingUsagePeriodRecord

pytestmark = pytest.mark.integration


def payload(**per_feature):
    base = {
        feature: {"enabled": True, "limit": 10, "reset_period": "none"}
        for feature in TRAINING_FEATURES
    }
    for feature, overrides in per_feature.items():
        base[feature] = base[feature] | overrides
    return base


class StubSourceLoader:
    """Owner-scoped stub returning fixed authorized question revisions."""

    def __init__(self) -> None:
        self.questions: dict[str, str] = {}
        self.calls: list[tuple[UUID, dict]] = []

    def add(self, text: str) -> UUID:
        revision_id = uuid4()
        self.questions[str(revision_id)] = text
        return revision_id

    async def load_question_texts(
        self, *, owner_player_id: UUID, source: dict
    ) -> tuple[QuestionText, ...]:
        self.calls.append((owner_player_id, dict(source)))
        return tuple(
            QuestionText(UUID(identifier), text)
            for identifier, text in self.questions.items()
        )


def default_facts(request):
    output = []
    for chunk in request.chunks:
        for index, (revision_id, _) in enumerate(_parse_question_blocks(chunk.text)):
            title = f"Fact {index}"
            description = f"Derived fact for question {revision_id}"
            links = (f"https://example.org/facts/{index}",)
            output.append(
                FactCandidate(
                    fact_candidate_id(revision_id, title, description, links),
                    title,
                    description,
                    links,
                    revision_id,
                )
            )
    return tuple(output)


async def make_stack(
    database,
    players,
    *,
    policy=None,
    chunk_characters=12_000,
    lease_duration=timedelta(minutes=10),
    max_job_attempts=12,
):
    entitlements = EntitlementService(database)
    await seed_category(database, f"cat-{uuid4().hex[:8]}", policy or payload(), players)
    jobs = DurableJobQueue(database)
    provider = FakeAnalysisProvider()
    loader = StubSourceLoader()
    settings = ProviderSettings(
        provider="fake",
        model="m1",
        max_chunk_characters=chunk_characters,
        timeout=timedelta(seconds=5),
    )
    service = AnalysisService(
        database,
        jobs,
        provider,
        settings,
        entitlements=entitlements,
        source_loader=loader,
        lease_duration=lease_duration,
        max_job_attempts=max_job_attempts,
    )
    worker = DurableJobWorker(
        jobs,
        {TRAINING_ANALYSIS_JOB_KIND: AnalysisJobHandler(service)},
        kinds=(TRAINING_ANALYSIS_JOB_KIND,),
    )
    return SimpleNamespace(
        entitlements=entitlements,
        jobs=jobs,
        provider=provider,
        loader=loader,
        settings=settings,
        service=service,
        worker=worker,
    )


async def submit(service, owner_id, source, key, *, instructions=""):
    async with database_transaction(service) as session:
        return await service.request(
            session,
            owner_player_id=owner_id,
            source=source,
            instructions=instructions,
            idempotency_key=key,
        )


def database_transaction(service):
    return service.database.transaction()


async def artifact_id_of(database, request_id: str) -> UUID:
    async with database.sessions() as session:
        return await session.scalar(
            select(TrainingAnalysisRequestRecord.artifact_id).where(
                TrainingAnalysisRequestRecord.request_id == request_id
            )
        )


async def artifact_of(database, artifact_id: UUID) -> TrainingAnalysisArtifactRecord:
    async with database.sessions() as session:
        return await session.get(TrainingAnalysisArtifactRecord, artifact_id)


async def test_request_executes_to_success_and_settles_usage(database):
    owner = await create_player(database, 201, "owner")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add(
        "The Eiffel Tower was built in 1889 for the World's Fair in Paris."
    )
    source = DissectionSource(uuid4(), (revision_id,))

    view = await submit(stack.service, owner, source, "d-1")
    assert view.status == "queued"
    assert view.retryable is False

    assert await stack.worker.run_once() == 1
    view = await stack.service.status(owner_player_id=owner, request_id="d-1")
    assert view.status == "succeeded"
    assert view.progress == 1.0
    assert view.error is None

    result = await stack.service.result(owner_player_id=owner, request_id="d-1")
    assert result is not None
    assert result["candidates"][0]["question_revision_id"] == str(revision_id)
    assert len(stack.provider.extract_calls) == 1
    capabilities = await stack.entitlements.capabilities(owner)
    assert capabilities[FEATURE_DISSECTION].remaining == 9


async def test_shared_base_requests_coalesce_into_one_computation(database):
    owner_a = await create_player(database, 202, "shared-a")
    owner_b = await create_player(database, 203, "shared-b")
    stack = await make_stack(database, [owner_a, owner_b])
    revision_id = stack.loader.add("Shared base fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner_a, source, "d-a")
    await submit(stack.service, owner_b, source, "d-b")
    async with database.sessions() as session:
        job_count = await session.scalar(
            select(func.count())
            .select_from(DurableJobRecord)
            .where(DurableJobRecord.kind == TRAINING_ANALYSIS_JOB_KIND)
        )
    assert job_count == 1

    assert await stack.worker.run_once() == 1
    for owner, key in ((owner_a, "d-a"), (owner_b, "d-b")):
        view = await stack.service.status(owner_player_id=owner, request_id=key)
        assert view.status == "succeeded"
    assert len(stack.provider.extract_calls) == 1
    result_a = await stack.service.result(owner_player_id=owner_a, request_id="d-a")
    result_b = await stack.service.result(owner_player_id=owner_b, request_id="d-b")
    assert result_a == result_b
    for owner in (owner_a, owner_b):
        remaining = (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining
        assert remaining == 9


async def test_registration_serializes_with_terminal_settlement(database, monkeypatch):
    first = await create_player(database, 230, "race-first")
    second = await create_player(database, 231, "race-second")
    stack = await make_stack(database, [first, second])
    source = DissectionSource(uuid4(), (stack.loader.add("A shared fact."),))
    await submit(stack.service, first, source, "race-first")
    selected = asyncio.Event()
    release = asyncio.Event()
    terminal_ready = asyncio.Event()
    allow_terminal = asyncio.Event()
    terminal_entered = asyncio.Event()
    terminal_finished = asyncio.Event()
    authorize = stack.service._authorize_request
    terminal = stack.service._write_terminal

    async def pause_registration(session, **kwargs):
        if kwargs["owner_player_id"] == second:
            selected.set()
            await release.wait()
        return await authorize(session, **kwargs)

    async def observe_terminal(*args, **kwargs):
        terminal_ready.set()
        await allow_terminal.wait()
        terminal_entered.set()
        await terminal(*args, **kwargs)
        terminal_finished.set()

    monkeypatch.setattr(stack.service, "_authorize_request", pause_registration)
    monkeypatch.setattr(stack.service, "_write_terminal", observe_terminal)
    worker = asyncio.create_task(stack.worker.run_once())
    await asyncio.wait_for(terminal_ready.wait(), 5)
    registration = asyncio.create_task(submit(stack.service, second, source, "race-second"))
    await asyncio.wait_for(selected.wait(), 5)
    allow_terminal.set()
    try:
        await asyncio.wait_for(terminal_entered.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(terminal_finished.wait(), 0.2)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(registration, worker), 10)
    for owner, key in ((first, "race-first"), (second, "race-second")):
        assert (await stack.service.status(owner_player_id=owner, request_id=key)).status == "succeeded"
        assert (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining == 9


async def test_terminal_artifact_reconciles_stranded_waiter(database):
    owner = await create_player(database, 232, "stranded")
    stack = await make_stack(database, [owner])
    source = DissectionSource(uuid4(), (stack.loader.add("A completed fact."),))
    await submit(stack.service, owner, source, "stranded")
    async with database.transaction() as session:
        artifact = await session.get(
            TrainingAnalysisArtifactRecord, await artifact_id_of(database, "stranded")
        )
        artifact.status = "failed"
        artifact.last_error = "Historical terminal write without settlement"
    assert (await stack.service.status(owner_player_id=owner, request_id="stranded")).status == "failed"
    assert (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining == 10


@pytest.mark.parametrize("insert_conflict", [False, True])
async def test_waiter_registration_serializes_with_terminal_completion(
    database, monkeypatch, insert_conflict,
):
    owner_a = await create_player(database, 9201, "terminal-a")
    owner_b = await create_player(database, 9202, "terminal-b")
    stack = await make_stack(database, [owner_a, owner_b])
    revision_id = stack.loader.add("A concurrent shared fact.")
    source = DissectionSource(uuid4(), (revision_id,))
    lease_token = uuid4()
    missing, continue_lookup = asyncio.Event(), asyncio.Event()
    selected, finish_registration = asyncio.Event(), asyncio.Event()
    original_lookup = stack.service._artifact_for_key
    waiter_task = None

    class PausedLookup:
        def __init__(self, session):
            self.session = session

        async def scalar(self, statement):
            result = await self.session.scalar(statement)
            if result is None:
                missing.set()
                await continue_lookup.wait()
            return result

        async def execute(self, statement):
            return await self.session.execute(statement)

    async def lookup(waiter_session, key, **values):
        if asyncio.current_task() is not waiter_task:
            return await original_lookup(waiter_session, key, **values)
        artifact = await original_lookup(PausedLookup(waiter_session), key, **values)
        selected.set()
        await finish_registration.wait()
        return artifact

    monkeypatch.setattr(stack.service, "_artifact_for_key", lookup)

    async def start_artifact():
        await submit(stack.service, owner_a, source, "terminal-first")
        artifact_id = await artifact_id_of(database, "terminal-first")
        async with database.transaction() as transaction:
            artifact = await transaction.get(TrainingAnalysisArtifactRecord, artifact_id)
            artifact.status = "running"
            artifact.lease_token = lease_token
            artifact.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return artifact_id

    if not insert_conflict:
        artifact_id = await start_artifact()
    waiter_task = asyncio.create_task(submit(stack.service, owner_b, source, "terminal-waiter"))
    terminal_task = None
    try:
        if insert_conflict:
            await asyncio.wait_for(missing.wait(), timeout=5)
            artifact_id = await start_artifact()
            continue_lookup.set()
        await asyncio.wait_for(selected.wait(), timeout=5)
        terminal_task = asyncio.create_task(stack.service._write_terminal(
            artifact_id, lease_token, status="succeeded", payload={"candidates": []},
        ))
        done, _ = await asyncio.wait({terminal_task}, timeout=0.25)
        assert not done
    finally:
        continue_lookup.set()
        finish_registration.set()
        await asyncio.gather(waiter_task, *([terminal_task] if terminal_task else []))

    async with database.sessions() as transaction:
        requests = list(await transaction.scalars(select(TrainingAnalysisRequestRecord)))
        assert len(requests) == 2
        assert all(row.status == "succeeded" and row.completed_at for row in requests)
        periods = list(await transaction.scalars(select(TrainingUsagePeriodRecord).where(
            TrainingUsagePeriodRecord.feature == FEATURE_DISSECTION,
        )))
        assert len(periods) == 2
        assert all(row.reserved == 0 and row.settled == 1 for row in periods)
    assert (await stack.service.result(owner_player_id=owner_b, request_id="terminal-waiter")) == {
        "candidates": [],
    }


async def test_stale_reconciliation_cannot_fail_a_replacement_generation(database):
    owner_a = await create_player(database, 9211, "generation-a")
    owner_b = await create_player(database, 9212, "generation-b")
    stack = await make_stack(database, [owner_a, owner_b])
    revision_id = stack.loader.add("An explicitly retried shared fact.")
    source = DissectionSource(uuid4(), (revision_id,))
    await submit(stack.service, owner_a, source, "generation-first")
    artifact_id = await artifact_id_of(database, "generation-first")
    async with database.transaction() as session:
        stale = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
        assert stale.generation == 1 and stale.status == "pending"
        assert await stack.service.cancel(owner_player_id=owner_a, request_id="generation-first")
        await submit(stack.service, owner_b, source, "generation-replacement")
        await stack.service._reconcile_artifact(session, stale, datetime.now(UTC))
        assert stale.generation == 2 and stale.status == "pending"
    async with database.sessions() as session:
        replacement = await stack.service._request_row(session, "generation-replacement")
        assert replacement.status == "queued" and replacement.completed_at is None
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord)))
        assert sum(row.reserved for row in periods) == 1
        assert sum(row.settled for row in periods) == 0
    assert await stack.worker.run_once() == 1
    result = await stack.service.result(owner_player_id=owner_b, request_id="generation-replacement")
    assert result is not None and len(result["candidates"]) == 1


async def test_custom_instructions_and_owners_isolate_artifacts(database):
    owner_a = await create_player(database, 204, "custom-a")
    owner_b = await create_player(database, 205, "custom-b")
    owner_c = await create_player(database, 206, "custom-c")
    stack = await make_stack(database, [owner_a, owner_b, owner_c])
    revision_id = stack.loader.add("Isolated fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner_a, source, "c-a", instructions="")
    await submit(stack.service, owner_b, source, "c-b", instructions="Focus on engineering.")
    await submit(stack.service, owner_c, source, "c-c", instructions="Focus on engineering.")

    async with database.sessions() as session:
        artifact_count = await session.scalar(
            select(func.count()).select_from(TrainingAnalysisArtifactRecord)
        )
        job_count = await session.scalar(
            select(func.count())
            .select_from(DurableJobRecord)
            .where(DurableJobRecord.kind == TRAINING_ANALYSIS_JOB_KIND)
        )
    assert artifact_count == 3
    assert job_count == 3

    assert await stack.worker.run_once() == 3
    assert len(stack.provider.extract_calls) == 3
    for owner, key in ((owner_a, "c-a"), (owner_b, "c-b"), (owner_c, "c-c")):
        view = await stack.service.status(owner_player_id=owner, request_id=key)
        assert view.status == "succeeded"

    base_artifact = await artifact_of(database, await artifact_id_of(database, "c-a"))
    custom_b = await artifact_of(database, await artifact_id_of(database, "c-b"))
    custom_c = await artifact_of(database, await artifact_id_of(database, "c-c"))
    assert base_artifact.shared and base_artifact.owner_player_id is None
    assert not custom_b.shared and custom_b.owner_player_id == owner_b
    assert not custom_c.shared and custom_c.owner_player_id == owner_c
    assert custom_b.id != custom_c.id


async def test_cache_hits_authorize_without_recomputation(database):
    owner_a = await create_player(database, 207, "cache-a")
    owner_b = await create_player(database, 208, "cache-b")
    stack = await make_stack(database, [owner_a, owner_b])
    revision_id = stack.loader.add("Cached fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner_a, source, "cache-1")
    assert await stack.worker.run_once() == 1
    assert len(stack.provider.extract_calls) == 1

    view = await submit(stack.service, owner_b, source, "cache-2")
    assert view.status == "succeeded"
    assert view.progress == 1.0
    assert await stack.worker.run_once() == 0
    assert len(stack.provider.extract_calls) == 1
    result_a = await stack.service.result(owner_player_id=owner_a, request_id="cache-1")
    result_b = await stack.service.result(owner_player_id=owner_b, request_id="cache-2")
    assert result_a == result_b
    assert (await stack.entitlements.capabilities(owner_b))[FEATURE_DISSECTION].remaining == 9


async def test_cache_hits_without_charging_still_require_enabled_features(database):
    owner_a = await create_player(database, 209, "free-a")
    owner_b = await create_player(database, 210, "free-b")
    stack = await make_stack(database, [owner_a, owner_b])
    revision_id = stack.loader.add("Free cache fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner_a, source, "free-1")
    assert await stack.worker.run_once() == 1

    no_charge = await make_stack(
        database, [owner_b], policy=payload(dissection={"cache_hit_charging": False})
    )
    view = await submit(no_charge.service, owner_b, source, "free-2")
    assert view.status == "succeeded"
    remaining = (await no_charge.entitlements.capabilities(owner_b))[FEATURE_DISSECTION].remaining
    assert remaining == 10
    assert len(stack.provider.extract_calls) == 1


async def test_quota_caps_and_disabled_features_are_enforced_at_request_time(database):
    gated_owner = await create_player(database, 211, "gated")
    limited_owner = await create_player(database, 226, "limited")
    capped_owner = await create_player(database, 227, "capped")
    revision_id = uuid4()
    source = DissectionSource(uuid4(), (revision_id,))

    disabled = await make_stack(
        database, [gated_owner], policy=payload(dissection={"enabled": False})
    )
    with pytest.raises(AnalysisFeatureDisabledError):
        await submit(disabled.service, gated_owner, source, "gated-0")

    limited = await make_stack(database, [limited_owner], policy=payload(dissection={"limit": 1}))
    await submit(limited.service, limited_owner, source, "gated-1")
    with pytest.raises(AnalysisQuotaExceededError):
        await submit(limited.service, limited_owner, source, "gated-2")

    capped = await make_stack(
        database, [capped_owner], policy=payload(dissection={"queued_request_cap": 1})
    )
    await submit(capped.service, capped_owner, source, "capped-1")
    with pytest.raises(AnalysisQueuedCapExceededError):
        await submit(capped.service, capped_owner, source, "capped-2")


async def test_cancelling_one_waiter_keeps_work_needed_by_others(database):
    owner_a = await create_player(database, 212, "cancel-a")
    owner_b = await create_player(database, 213, "cancel-b")
    stack = await make_stack(database, [owner_a, owner_b])
    revision_id = stack.loader.add("Cancellation fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner_a, source, "cancel-a")
    await submit(stack.service, owner_b, source, "cancel-b")

    assert await stack.service.cancel(owner_player_id=owner_a, request_id="cancel-a")
    view = await stack.service.status(owner_player_id=owner_a, request_id="cancel-a")
    assert view.status == "cancelled"
    assert (await stack.entitlements.capabilities(owner_a))[FEATURE_DISSECTION].remaining == 10

    assert await stack.worker.run_once() == 1
    view = await stack.service.status(owner_player_id=owner_b, request_id="cancel-b")
    assert view.status == "succeeded"
    view = await stack.service.status(owner_player_id=owner_a, request_id="cancel-a")
    assert view.status == "cancelled"
    assert len(stack.provider.extract_calls) == 1


async def test_cancelling_the_only_waiter_aborts_without_provider_calls(database):
    owner = await create_player(database, 214, "solo-cancel")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add("Solo cancellation fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner, source, "solo-1")
    assert await stack.service.cancel(owner_player_id=owner, request_id="solo-1")
    assert await stack.worker.run_once() == 0
    assert stack.provider.extract_calls == []
    view = await stack.service.status(owner_player_id=owner, request_id="solo-1")
    assert view.status == "cancelled"
    artifact = await artifact_of(database, await artifact_id_of(database, "solo-1"))
    assert artifact.status == "cancelled"
    assert (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining == 10


async def test_provider_failure_retries_from_the_persisted_checkpoint(database):
    owner = await create_player(database, 215, "retry-owner")
    stack = await make_stack(
        database,
        [owner],
        chunk_characters=120,
        lease_duration=timedelta(milliseconds=80),
    )
    first = stack.loader.add("First question with enough text to fill a chunk alone.")
    second = stack.loader.add("Second question that lands in its own chunk.")
    source = DissectionSource(uuid4(), (first, second))

    calls = {"count": 0}

    def failing_on_second_chunk(request):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("provider exploded on the second chunk")
        return default_facts(request)

    stack.provider.fact_response = failing_on_second_chunk
    await submit(stack.service, owner, source, "retry-1")
    artifact_id = await artifact_id_of(database, "retry-1")
    payload = {"artifact_id": str(artifact_id), "generation": 1}

    with pytest.raises(RuntimeError, match="provider exploded"):
        await stack.service.execute_job(payload)

    artifact = await artifact_of(database, artifact_id)
    assert artifact.status == "running"
    assert artifact.checkpoint["chunks_done"] == 1
    assert artifact.checkpoint["chunks_total"] == 2
    # Chunk 1 was processed and checkpointed; chunk 2's provider call failed.
    assert len(stack.provider.extract_calls) == 2
    view = await stack.service.status(owner_player_id=owner, request_id="retry-1")
    assert view.status == "running"

    # The crashed run's lease expires; a retry takes over and resumes.
    await asyncio.sleep(0.1)
    stack.provider.fact_response = default_facts
    await stack.service.execute_job(payload)

    view = await stack.service.status(owner_player_id=owner, request_id="retry-1")
    assert view.status == "succeeded"
    result = await stack.service.result(owner_player_id=owner, request_id="retry-1")
    assert {item["question_revision_id"] for item in result["candidates"]} == {
        str(first),
        str(second),
    }
    assert len(stack.provider.extract_calls) == 3
    assert stack.provider.extract_calls[2].chunks[0].index == 1
    assert (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining == 9


async def test_stale_compute_leases_are_fenced(database):
    owner = await create_player(database, 216, "fence-owner")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add("Fencing fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner, source, "fence-1")
    assert await stack.worker.run_once() == 1
    artifact_id = await artifact_id_of(database, "fence-1")

    with pytest.raises(RuntimeError, match="Stale analysis compute lease"):
        await stack.service._write_checkpoint(
            artifact_id, uuid4(), {"chunks_done": 1, "chunks_total": 1}
        )
    artifact = await artifact_of(database, artifact_id)
    assert artifact.status == "succeeded"


async def test_terminal_job_failure_fails_requests_and_refunds(database):
    owner = await create_player(database, 217, "doomed")
    stack = await make_stack(database, [owner], max_job_attempts=1)
    revision_id = stack.loader.add("Doomed fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    stack.provider.extract_failures.append(RuntimeError("permanent outage"))
    await submit(stack.service, owner, source, "doomed-1")
    assert await stack.worker.run_once() == 1

    view = await stack.service.status(owner_player_id=owner, request_id="doomed-1")
    assert view.status == "failed"
    assert view.retryable is True
    assert view.error is not None and "permanent outage" in view.error
    # Failures are never cached as successful empty data.
    assert await stack.service.result(owner_player_id=owner, request_id="doomed-1") is None
    assert (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining == 10

    # A new request resets the artifact generation and retries the work.
    await submit(stack.service, owner, source, "doomed-2")
    assert await stack.worker.run_once() == 1
    view = await stack.service.status(owner_player_id=owner, request_id="doomed-2")
    assert view.status == "succeeded"
    assert (await stack.entitlements.capabilities(owner))[FEATURE_DISSECTION].remaining == 9


async def test_analysis_jobs_are_isolated_from_gameplay_workers(database):
    owner = await create_player(database, 218, "isolated")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add("Isolation fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner, source, "iso-1")
    await stack.jobs.schedule("integration.gameplay", f"gameplay:{uuid4()}", {})

    async def gameplay_handler(payload: dict) -> None:
        return None

    gameplay_worker = DurableJobWorker(
        stack.jobs, {"integration.gameplay": gameplay_handler}, kinds=("integration.gameplay",)
    )
    assert await gameplay_worker.run_once() == 1
    # The gameplay worker left the analysis job untouched for analysis workers.
    claimed = await stack.jobs.claim(kinds=(TRAINING_ANALYSIS_JOB_KIND,), limit=10)
    assert len(claimed) == 1
    await stack.jobs.succeed(claimed[0].job_id, claimed[0].lease_token)
    assert await stack.jobs.claim(limit=10) == ()


async def test_uncertain_outcomes_are_recorded_and_retried(database):
    owner = await create_player(database, 219, "uncertain")
    stack = await make_stack(database, [owner], lease_duration=timedelta(milliseconds=80))
    source = CommonKnowledgeSource("An uncertain fact about electricity.")

    stack.provider.knowledge_failures.append(
        ProviderOutcomeUncertain("gateway died mid-call", provider_request_id="upstream-1")
    )
    await submit(stack.service, owner, source, "unc-1")
    artifact_id = await artifact_id_of(database, "unc-1")
    payload = {"artifact_id": str(artifact_id), "generation": 1}

    with pytest.raises(ProviderOutcomeUncertain):
        await stack.service.execute_job(payload)

    artifact = await artifact_of(database, artifact_id)
    assert "upstream-1" in artifact.provider_request_ids
    assert "upstream-1" in (artifact.last_error or "")

    await asyncio.sleep(0.1)
    await stack.service.execute_job(payload)
    view = await stack.service.status(owner_player_id=owner, request_id="unc-1")
    assert view.status == "succeeded"
    result = await stack.service.result(owner_player_id=owner, request_id="unc-1")
    assert result["value"] == 50


async def test_common_knowledge_estimates_are_cached_by_content(database):
    owner_a = await create_player(database, 220, "ck-a")
    owner_b = await create_player(database, 221, "ck-b")
    stack = await make_stack(database, [owner_a, owner_b])

    await submit(stack.service, owner_a, CommonKnowledgeSource("Water boils at 100C."), "ck-1")
    assert await stack.worker.run_once() == 1
    assert len(stack.provider.knowledge_calls) == 1

    view = await submit(
        stack.service, owner_b, CommonKnowledgeSource("Water boils at 100C."), "ck-2"
    )
    assert view.status == "succeeded"
    assert await stack.worker.run_once() == 0
    assert len(stack.provider.knowledge_calls) == 1

    await submit(stack.service, owner_b, CommonKnowledgeSource("Water boils at 90C."), "ck-3")
    assert await stack.worker.run_once() == 1
    assert len(stack.provider.knowledge_calls) == 2
    remaining = (await stack.entitlements.capabilities(owner_b))["common_knowledge"].remaining
    assert remaining == 8


async def test_best_buzz_boundaries_are_validated_against_token_streams(database):
    owner = await create_player(database, 222, "buzz")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add("alpha beta gamma delta epsilon zeta eta theta")
    source = BestBuzzSource(uuid4(), (revision_id,), question_token_target_chars=10)

    def halfway_boundaries(request):
        output = []
        for chunk in request.chunks:
            for identifier, text in _parse_question_blocks(chunk.text):
                tokens = announcement_tokens(text, 10)
                output.append(
                    BuzzBoundary(identifier, text_fingerprint(text), len(tokens) // 2)
                )
        return tuple(output)

    stack.provider.buzz_response = halfway_boundaries
    await submit(stack.service, owner, source, "buzz-1")
    assert await stack.worker.run_once() == 1
    result = await stack.service.result(owner_player_id=owner, request_id="buzz-1")
    assert result["tokenizer_version"] == stack.settings.tokenizer_version
    assert result["question_token_target_chars"] == 10
    boundary = result["boundaries"][0]
    text = "alpha beta gamma delta epsilon zeta eta theta"
    expected_tokens = announcement_tokens(text, 10)
    assert boundary["token_index"] == len(expected_tokens) // 2
    assert boundary["token_count"] == len(expected_tokens)
    assert boundary["text_digest"] == text_fingerprint(text)

    # Out-of-range provider boundaries are rejected, never silently fixed.
    invalid_stack = await make_stack(database, [owner], max_job_attempts=1)
    invalid_stack.provider.buzz_response = lambda request: tuple(
        BuzzBoundary(identifier, text_fingerprint(text), 10_000)
        for chunk in request.chunks
        for identifier, text in _parse_question_blocks(chunk.text)
    )
    different = BestBuzzSource(uuid4(), (revision_id,), question_token_target_chars=8)
    await submit(invalid_stack.service, owner, different, "buzz-2")
    assert await invalid_stack.worker.run_once() == 1
    view = await invalid_stack.service.status(owner_player_id=owner, request_id="buzz-2")
    assert view.status == "failed"
    assert view.retryable is True


async def test_status_results_and_cancellation_are_owner_scoped(database):
    owner = await create_player(database, 223, "scoped")
    intruder = await create_player(database, 224, "intruder")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add("Scoped fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner, source, "scoped-1")
    assert await stack.service.status(owner_player_id=intruder, request_id="scoped-1") is None
    assert await stack.service.result(owner_player_id=intruder, request_id="scoped-1") is None
    assert not await stack.service.cancel(owner_player_id=intruder, request_id="scoped-1")

    assert await stack.worker.run_once() == 1
    assert await stack.service.result(owner_player_id=intruder, request_id="scoped-1") is None
    assert await stack.service.result(owner_player_id=owner, request_id="scoped-1") is not None


async def test_cleanup_removes_terminal_artifacts_and_requests(database):
    owner = await create_player(database, 225, "cleanup")
    stack = await make_stack(database, [owner])
    revision_id = stack.loader.add("Cleanup fact text.")
    source = DissectionSource(uuid4(), (revision_id,))

    await submit(stack.service, owner, source, "clean-1")
    assert await stack.worker.run_once() == 1
    deleted = await stack.service.cleanup(before=datetime.now(UTC) + timedelta(days=1))
    assert deleted >= 2
    async with database.sessions() as session:
        requests = await session.scalar(
            select(func.count()).select_from(TrainingAnalysisRequestRecord)
        )
        artifacts = await session.scalar(
            select(func.count()).select_from(TrainingAnalysisArtifactRecord)
        )
    assert requests == 0
    assert artifacts == 0

    # A later request recomputes because the cache was pruned.
    await submit(stack.service, owner, source, "clean-2")
    assert await stack.worker.run_once() == 1
    assert len(stack.provider.extract_calls) == 2







