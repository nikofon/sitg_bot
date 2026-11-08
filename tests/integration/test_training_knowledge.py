import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import create_player, seed_category
from knowledge_review_support import approve_all, send_and_approve
from sqlalchemy import event, select
from test_training import open_question, upload

from sitg_bot.domain.training import TrainingSettings
from sitg_bot.services.analysis_jobs import AnalysisService
from sitg_bot.services.entitlements import TRAINING_FEATURES, EntitlementService
from sitg_bot.services.reliable_delivery import DurableJobQueue, DurableJobWorker
from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_ai import (
    BuzzBoundary,
    FakeAnalysisProvider,
    ProviderSettings,
    chunk_questions,
    text_fingerprint,
)
from sitg_bot.services.training_knowledge import KnowledgeApplication, TrainingAnalysisAccess
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.services.training_provider import analysis_provider_from_settings
from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
    TrainingBestBuzzCompletionRecord,
    TrainingSessionRecord,
    TrainingUsagePeriodRecord,
)

pytestmark = pytest.mark.integration


async def application(database, owner, *, limit=100, cache_hit_charging=False):
    settings = ProviderSettings("fake", "fixture")
    packets = TrainingPacketService(database)
    analysis = AnalysisService(
        database,
        DurableJobQueue(database),
        FakeAnalysisProvider(),
        settings,
        entitlements=EntitlementService(database),
        source_loader=TrainingAnalysisAccess(database, packets),
    )
    app = KnowledgeApplication(database, settings=settings, analysis=analysis)
    await seed_category(
        database,
        "integration",
        {
            feature: {
                "enabled": True,
                "limit": limit,
                "reset_period": "daily",
                "cache_hit_charging": cache_hit_charging,
            }
            for feature in TRAINING_FEATURES
        },
        [owner],
    )
    return app


@pytest.mark.parametrize("completed", [False, True])
async def test_current_does_not_resurface_completed_run_after_newer_stop(database, completed):
    owner = await create_player(database, 8104, "latest-run")
    version, _ = await upload(database, owner)
    training = TrainingService(database)
    first = (await training.start(owner, version, confirm=True))["session"]
    async with database.transaction() as session:
        record = await session.get(TrainingSessionRecord, UUID(first["id"]))
        record.status, record.phase, record.deadline = "completed", "revealed", None
        record.ended_at = datetime.now(UTC)
    assert (await training.current(owner))["session"]["id"] == first["id"]
    second = (await training.start(owner, version))["session"]
    if completed:
        async with database.transaction() as session:
            record = await session.get(TrainingSessionRecord, UUID(second["id"]))
            record.status, record.phase, record.deadline = "completed", "revealed", None
            record.ended_at = datetime.now(UTC)
    await training.act(
        owner, UUID(second["id"]), "stop", expected_version=second["version"],
        question_revision_id=UUID(second["question_revision_id"]),
    )
    assert (await training.current(owner))["session"] is None
    score = (await training.current(owner, score_only=True))["session"]
    assert score["id"] == second["id"]
    assert score["status"] == ("completed" if completed else "stopped")
    lobby = (await training.lobby(owner, "create"))["lobby"]
    assert await training.current(owner) == {"session": None, "lobby": lobby}
    third = (await training.start(owner, version))["session"]
    assert (await training.current(owner))["session"]["id"] == third["id"]


@pytest.mark.parametrize("key,model", [
    (None, "fixture-model"), ("fixture-key", "unconfigured"), (None, "unconfigured"),
])
async def test_incomplete_provider_configuration_denies_preview_admission_and_statistics(
    database, key, model,
):
    from sitg_bot.services.analysis_jobs import AnalysisFeatureDisabledError

    owner = await create_player(database, 8100, "not-configured")
    app = await application(database, owner)
    app.settings = ProviderSettings("openai", model, api_key=key)
    app.analysis.provider = analysis_provider_from_settings(app.settings)
    version, _ = await upload(database, owner)
    before = await app.entitlements.capabilities(owner)
    preview = await app.best_buzz_preview(owner, version)
    assert not preview["can_begin"] and preview["eligible_theme_count"] == 0
    for kind in ("dissection", "best_buzz"):
        with pytest.raises(AnalysisFeatureDisabledError, match="not configured"):
            await app.request_analysis(
                owner, version_id=version, kind=kind, confirm_exposure=True,
                idempotency_key=f"unconfigured-{kind}",
            )
    fact = await app.mutate(owner, "create", {"title": "Fact", "description": "D"})
    await app.statistics_job({"owner_id": str(owner), "fact_id": str(fact["id"]), "version": 1})
    detail = await app.detail(owner, fact["id"])
    assert detail["statistics"]["common_knowledge"] is None
    assert detail["statistics"]["status"] == "unavailable"
    async with database.sessions() as session:
        assert not list(await session.scalars(select(TrainingAnalysisRequestRecord)))
    after = await app.entitlements.capabilities(owner)
    assert {key: value.remaining for key, value in after.items()} == {
        key: value.remaining for key, value in before.items()
    }


@pytest.mark.parametrize("preparation,playback,delay,legacy", [
    (18, 3, 1, False), (3, 18, 1, False), (18, 18, 1, False),
    (18, 3, 0, False), (18, 3, 1, True), (3, 18, 0, True),
])
async def test_persisted_best_buzz_keeps_exact_boundary_and_reads_legacy_results(
    database, preparation, playback, delay, legacy,
):
    owner = await create_player(database, 8103, "exact-buzz")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    app.analysis.provider.buzz_response = lambda request: tuple(
        BuzzBoundary(revision, text_fingerprint(text), 1, 3)
        for chunk in request.chunks for revision, text in chunk_questions(chunk)
    )
    accepted = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", token_target_chars=preparation,
        confirm_exposure=True, idempotency_key="exact-boundary",
    )
    worker = DurableJobWorker(app.analysis.jobs, {"training.analysis": app.analysis_job},
                              kinds=("training.analysis",))
    assert await worker.run_once() == 1
    async with database.transaction() as session:
        request = await session.scalar(select(TrainingAnalysisRequestRecord).where(
            TrainingAnalysisRequestRecord.request_id == accepted["request_id"],
        ))
        artifact = await session.get(TrainingAnalysisArtifactRecord, request.artifact_id)
        assert all(item["source_end"] == 3 for item in artifact.checkpoint["partial"])
        assert all(item["source_end"] == 3 for item in artifact.payload["boundaries"])
        if legacy:
            artifact.schema_version = "1"
            artifact.payload = {**artifact.payload, "boundaries": [
                {key: value for key, value in item.items() if key != "source_end"}
                for item in artifact.payload["boundaries"]
            ]}
    app = KnowledgeApplication(database, settings=app.settings, analysis=app.analysis)
    training = TrainingService(database, best_buzz=app)
    now = datetime.now(UTC)
    state = (await training.start(
        owner, version, now=now,
        settings=TrainingSettings(best_buzz=True, token_target_chars=playback, token_delay=delay),
    ))["session"]
    state, now = await open_question(training, owner, state, now)
    for _ in range(6):
        if state["phase"] == "best_buzz":
            break
        now = state["deadline"]
        state = (await training.view(owner, UUID(state["id"]), now=now))["session"]
    assert state["phase"] == "best_buzz" and state["deadline"] is None
    expected = "One two three" if legacy and preparation == 18 else "One"
    assert state["text"] == expected
    state = (await training.view(
        owner, UUID(state["id"]), now=now + timedelta(seconds=100),
    ))["session"]
    assert state["text"] == expected
    continued = (await training.act(
        owner, UUID(state["id"]), "continue", expected_version=state["version"],
        question_revision_id=UUID(state["question_revision_id"]), now=now,
    ))["session"]
    if delay == 0:
        assert continued["phase"] == "waiting"
        assert continued["text"] == "One two three <four> five"
    else:
        assert continued["phase"] == "reading" and continued["text"] == expected
    assert len(app.analysis.provider.buzz_calls) == 1


async def test_persistent_crud_pagination_and_permanent_independent_copies(database):
    owner = await create_player(database, 8101, "owner")
    recipient = await create_player(database, 8102, "reader")
    app = KnowledgeApplication(database)
    created = await app.mutate(
        owner,
        "create",
        {
            "title": "Alpha",
            "description": "<kept> fact",
            "tag_names": [" History "],
            "links": [{"url": "https://example.org/source", "title": "Source"}],
        },
    )
    fact_id = created["id"]
    await app.mutate(owner, "create", {"title": "Beta", "description": "Second"})
    page = await app.facts(owner, limit=1)
    assert page["total"] == 2 and page["next_cursor"]
    other = await app.facts(owner, limit=1, cursor=page["next_cursor"])
    assert other["items"][0]["title"] == "Beta"
    with pytest.raises(PermissionError):
        await app.detail(recipient, fact_id)
    await send_and_approve(app, owner, "grant", {"recipient": "reader"})
    copied_id = (await app.facts(recipient))["items"][0]["id"]
    shared = await app.detail(recipient, copied_id)
    assert copied_id != fact_id and shared["editable"] is True and "provenance" not in shared
    assert shared["tags"][0]["name"] == "history"
    from sitg_bot.services.concurrency import StaleWriteError

    await app.mutate(
        owner, "update", {"fact_id": fact_id, "expected_version": 1, "title": "Changed"}
    )
    with pytest.raises(StaleWriteError):
        await app.mutate(
            owner, "update", {"fact_id": fact_id, "expected_version": 1, "title": "Stale"}
        )
    grants = (await app.sharing(owner))["grants"]
    with pytest.raises(PermissionError, match="permanent"):
        await app.mutate(owner, "revoke", {"grant_id": grants[0]["id"]})
    with pytest.raises(PermissionError):
        await app.detail(recipient, fact_id)
    await send_and_approve(app, owner, "grant", {"recipient": "reader"})
    assert (await app.detail(recipient, copied_id))["title"] == "Alpha"
    await app.mutate(
        recipient,
        "update",
        {"fact_id": copied_id, "expected_version": 1, "description": "Recipient edit"},
    )
    assert (await app.detail(owner, fact_id))["description"] == "<kept> fact"
    assert (await app.facts(recipient))["total"] == 2
    # Missing provider produces nullable values, not invented zeroes.
    await app.statistics_job({"owner_id": str(owner), "fact_id": str(fact_id), "version": 2})
    statistics = (await app.detail(owner, fact_id))["statistics"]
    assert statistics["common_knowledge"] is None
    assert statistics["bingo"]["status"] == "pending_calibration"


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_dissection_retry_applies_replacement_result(database, outcome):
    from sitg_bot.services.training_ai import AnalysisValidationError

    owner = await create_player(database, 8203, "retry-trainee")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    first = await app.request_analysis(
        owner, version_id=version, kind="dissection", instructions="Keep history",
        idempotency_key="first", confirm_exposure=True,
    )
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    if outcome == "cancelled":
        await app.analysis.cancel(owner_player_id=owner, request_id=first["request_id"])
    else:
        app.analysis.provider.extract_failures.append(AnalysisValidationError("Invalid result"))
        await worker.run_once()
    await app.reconcile()
    assert (await app.dissections(owner))["items"][0]["status"] == "failed"
    assert (await app.facts(owner))["total"] == 0
    second = await app.request_analysis(
        owner, version_id=version, kind="dissection", instructions="Keep history",
        idempotency_key="replacement",
    )
    assert second["dissection_id"] != first["dissection_id"]
    assert second["request_id"] != first["request_id"]
    await worker.run_once()
    await app.reconcile()
    assert (await app.dissections(owner))["items"][0]["status"] == "succeeded"
    assert (await app.facts(owner))["total"] == 0
    await approve_all(app, owner, second)
    assert (await app.facts(owner))["total"] == 5
    await app.reconcile()
    assert (await app.facts(owner))["total"] == 5


async def test_dissection_explicit_approval_and_best_buzz_bridge(database):
    owner = await create_player(database, 8201, "trainee")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    result = await app.request_analysis(
        owner,
        version_id=version,
        kind="dissection",
        instructions="  before 1500 A.C.\n",
        idempotency_key="dissect-integration",
        confirm_exposure=True,
    )
    worker = DurableJobWorker(
        app.analysis.jobs,
        {
            "training.analysis": app.analysis_job,
            "knowledge.statistics": app.statistics_job,
        },
        kinds=("training.analysis", "knowledge.statistics"),
        batch_size=1,
    )
    await worker.run_once()
    assert (await app.facts(owner))["total"] == 0
    await approve_all(app, owner, result)
    page = await app.facts(owner)
    assert page["total"] == 5
    async with database.transaction() as session:
        row = await session.get(KnowledgeDissectionRecord, result["dissection_id"])
        assert row.instructions == "  before 1500 A.C.\n"
        assert row.status == "succeeded"
        artifact = await session.scalar(select(TrainingAnalysisArtifactRecord))
        assert artifact.shared is False and artifact.owner_player_id == owner
    await app.reconcile()
    assert (await app.facts(owner))["total"] == 5
    await app.request_analysis(
        owner,
        version_id=version,
        kind="best_buzz",
        idempotency_key="buzz-integration",
        token_target_chars=7,
    )
    # Other jobs can run first; none is claimed by a gameplay-only worker.
    for _ in range(15):
        await worker.run_once()
        await app.reconcile_statistics()
    trained = await TrainingService(database, best_buzz=app).start(
        owner, version, settings=TrainingSettings(best_buzz=True, token_target_chars=18)
    )
    assert trained["session"] is not None
    await app.packets.retire(owner, version)
    with pytest.raises((PermissionError, LookupError)):
        await app.analysis.result(owner_player_id=owner, request_id=result["request_id"])
    await app.cleanup_retired()
    from sitg_bot.storage.models import TrainingAnalysisRequestRecord

    async with database.transaction() as session:
        assert not list(
            await session.scalars(
                select(TrainingAnalysisRequestRecord).where(
                    TrainingAnalysisRequestRecord.source["packet_revision_id"].astext
                    == str(version)
                )
            )
        )
        # Extracted knowledge remains the owner's collection after retiring its packet.
        assert (await app.facts(owner))["total"] == 5


@pytest.mark.parametrize("prior_status", ["failed", "cancelled"])
@pytest.mark.parametrize("reconciled", [False, True])
async def test_explicit_dissection_retry_applies_facts_without_resurrecting_them(
    database, prior_status, reconciled,
):
    owner = await create_player(database, 8221, "retry-dissection")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    first = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="first-dissection",
    )
    if prior_status == "cancelled":
        assert await app.analysis.cancel(owner_player_id=owner, request_id=first["request_id"])
    else:
        async with database.transaction() as session:
            request = await app.analysis._request_row(session, first["request_id"])
            artifact = await session.get(
                TrainingAnalysisArtifactRecord, request.artifact_id,
                with_for_update={"key_share": True},
            )
            await app.analysis._fail_artifact(
                session, artifact, "Fixture failure", datetime.now(UTC)
            )
    if reconciled:
        await app.reconcile()
    retried = await app.request_analysis(
        owner, version_id=version, kind="dissection", idempotency_key="retry-dissection",
    )
    assert retried["request_id"] != first["request_id"]
    assert retried["dissection_id"] != first["dissection_id"]
    async with database.sessions() as session:
        row = await session.get(KnowledgeDissectionRecord, retried["dissection_id"])
        assert row.status == "pending" and row.application is None
        assert row.analysis_request_id == retried["request_id"]
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    for _ in range(3):
        await worker.run_once()
    await approve_all(app, owner, retried)
    facts = await app.facts(owner)
    assert facts["total"] == 5
    await app.mutate(owner, "delete", {
        "fact_id": facts["items"][0]["id"], "confirm_dissection_origin": True,
    })
    repeated = await app.request_analysis(
        owner, version_id=version, kind="dissection", idempotency_key="after-application",
    )
    assert repeated["request_id"] != retried["request_id"]
    for _ in range(3):
        await worker.run_once()
    await approve_all(app, owner, repeated)
    await app.reconcile()
    assert (await app.facts(owner))["total"] == 4
    async with database.sessions() as session:
        row = await session.get(KnowledgeDissectionRecord, retried["dissection_id"])
        assert row.status == "succeeded" and row.application is not None
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord).where(
            TrainingUsagePeriodRecord.feature == "dissection",
        )))
        assert len(periods) == 1 and periods[0].reserved == 0 and periods[0].settled == 2


@pytest.mark.parametrize("limit,charged", [(1, False), (1, True), (None, False), (0, False)])
async def test_best_buzz_preview_is_non_charging_and_matches_cache_policy(database, limit, charged):
    from test_lobby_architecture import tournament_fixture

    from sitg_bot.storage.models import (
        PlayerExposureClaimRecord,
        TournamentPacketAssignmentRecord,
    )

    fixture = await tournament_fixture(database, player_count=1)
    owner = fixture.manager.id
    app = await application(database, owner, limit=limit, cache_hit_charging=charged)
    async with database.sessions() as session:
        version = await session.scalar(select(TournamentPacketAssignmentRecord.adopted_version_id)
            .where(TournamentPacketAssignmentRecord.packet_id == fixture.packet_id))
        exposure_before = set(await session.scalars(select(PlayerExposureClaimRecord.id)))
    before = await app.best_buzz_preview(owner, version)
    assert before["theme_count"] == 1
    assert before["eligible_theme_count"] == (0 if limit == 0 else 1)
    assert before["can_begin"] is (limit != 0)
    # Opening a dialog does not disclose, reserve, create requests or call upstream.
    async with database.sessions() as session:
        assert set(await session.scalars(select(PlayerExposureClaimRecord.id))) == exposure_before
        assert not list(await session.scalars(select(TrainingAnalysisRequestRecord)))
    assert app.analysis.provider.buzz_calls == []
    other = fixture.players[0].id
    private, _ = await upload(database, owner)
    with pytest.raises(LookupError):
        await app.best_buzz_preview(other, private)
    if limit == 0:
        return
    accepted = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", confirm_exposure=True,
        idempotency_key="preview-buzz",
    )
    active = await app.best_buzz_preview(owner, version)
    assert active["active_request_id"] == accepted["request_id"]
    card = (await app.packets.list_packets(owner))["items"]
    assert next(item for item in card if item["id"] == version)["best_buzz_request_id"] \
        == accepted["request_id"]
    worker = DurableJobWorker(app.analysis.jobs, {"training.analysis": app.analysis_job},
                              kinds=("training.analysis",))
    assert await worker.run_once() == 1
    after = await app.best_buzz_preview(owner, version)
    assert after["cache_hit"] is True
    assert after["can_begin"] is (not charged or limit is None)
    # Another tokenization is not the same cache, and private packets never get
    # cross-request cache eligibility from the public packet's artifact.
    different = await app.best_buzz_preview(owner, version, token_target_chars=7)
    assert different["cache_hit"] is False
    private_preview = await app.best_buzz_preview(owner, private)
    assert private_preview["cache_hit"] is False
    assert private_preview["can_begin"] is (limit is None)
    assert len(app.analysis.provider.buzz_calls) == 1


async def test_best_buzz_duplicate_concurrent_preparation_is_suppressed(database):
    owner = await create_player(database, 8232, "buzz-dedup")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    first = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", confirm_exposure=True,
        idempotency_key="buzz-dedup-1",
    )
    assert first["status"] == "queued"
    # A second concurrent preparation of the same packet (double click, new
    # idempotency key) returns the active request, not a duplicate job.
    second = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", confirm_exposure=True,
        idempotency_key="buzz-dedup-2",
    )
    assert second["request_id"] == first["request_id"]
    assert second["status"] == "queued"
    async with database.sessions() as session:
        rows = list(await session.scalars(
            select(TrainingAnalysisRequestRecord).where(
                TrainingAnalysisRequestRecord.owner_player_id == owner
            )
        ))
        assert len(rows) == 1
    # Quota is reserved once: the packet has one theme and the limit is 100.
    capabilities = await app.entitlements.capabilities(owner)
    assert capabilities["best_buzz"].remaining == 99
    # An independent packet is never blocked by the same-packet guard.
    other, _ = await upload(database, owner)
    third = await app.request_analysis(
        owner, version_id=other, kind="best_buzz", confirm_exposure=True,
        idempotency_key="buzz-other-packet",
    )
    assert third["request_id"] != first["request_id"]
    # Cancelling the active preparation frees the packet for an explicit retry.
    assert await app.analysis.cancel(owner_player_id=owner, request_id=first["request_id"])
    retried = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", confirm_exposure=True,
        idempotency_key="buzz-dedup-3",
    )
    assert retried["request_id"] != first["request_id"]


async def test_best_buzz_completion_and_real_disclosure_have_compatible_fk_locks(
    database, monkeypatch,
):
    owner = await create_player(database, 8231, "fk-waiter")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    first = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", confirm_exposure=True,
        idempotency_key="first-best-buzz",
    )
    lease = uuid4()
    async with database.transaction() as session:
        request = await app.analysis._request_row(session, first["request_id"])
        artifact_id = request.artifact_id
        artifact = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
        artifact.status = "running"
        artifact.lease_token = lease
        artifact.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    terminal_locked, waiter_lookup = asyncio.Event(), asyncio.Event()
    original_complete = app.analysis._complete_requests
    original_lookup = app.analysis._active_duplicate

    async def complete(*args):
        terminal_locked.set()
        await waiter_lookup.wait()
        await original_complete(*args)

    async def lookup(*args, **kwargs):
        waiter_lookup.set()
        return await original_lookup(*args, **kwargs)

    monkeypatch.setattr(app.analysis, "_complete_requests", complete)
    monkeypatch.setattr(app.analysis, "_active_duplicate", lookup)
    terminal = asyncio.create_task(app.analysis._write_terminal(
        artifact_id, lease, status="succeeded", payload={"boundaries": []},
    ))
    waiter = None
    try:
        await asyncio.wait_for(terminal_locked.wait(), timeout=5)
        waiter = asyncio.create_task(app.request_analysis(
            owner, version_id=version, kind="best_buzz", idempotency_key="second-best-buzz",
        ))
        _, result = await asyncio.wait_for(asyncio.gather(terminal, waiter), timeout=10)
        assert result["status"] == "queued"  # private packets have no cross-request cache
    finally:
        waiter_lookup.set()
        for task in (terminal, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(terminal, *([waiter] if waiter else []), return_exceptions=True)
    async with database.sessions() as session:
        requests = list(await session.scalars(select(TrainingAnalysisRequestRecord)))
        assert len(requests) == 2 and {row.status for row in requests} == {"succeeded", "queued"}
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord)))
        assert len(periods) == 1 and periods[0].reserved == 1 and periods[0].settled == 1
        assert len(list(await session.scalars(select(TrainingBestBuzzCompletionRecord)))) == 1


async def test_exact_dissection_identity_survives_bounded_persisted_history(database):
    from sitg_bot.storage.models import PacketVersionRecord

    owner = await create_player(database, 8241, "history-owner")
    version, _ = await upload(database, owner)
    app = KnowledgeApplication(database)
    async with database.transaction() as session:
        packet = await session.get(PacketVersionRecord, version)
        service = app.service(session)
        first = await service.request_dissection(
            owner, packet_id=packet.packet_id, packet_revision=str(version),
        )
        applied = await service.apply_dissection_result(owner, first.request_id, [])
        for index in range(101):
            await service.request_dissection(
                owner, packet_id=packet.packet_id, packet_revision=str(version),
                comment=f"History revision {index}",
            )
        history = await service.store.requests_for_owner(owner, packet_id=packet.packet_id)
        assert len(history) == 100 and first.request_id not in {row.id for row in history}
        retry = await service.request_dissection(
            owner, packet_id=packet.packet_id, packet_revision=str(version),
        )
        assert retry == applied
        assert len(list(await session.scalars(select(KnowledgeDissectionRecord)))) == 102


async def test_statistics_lock_owners_in_the_same_order_as_collection_imports(
    database, monkeypatch,
):
    from dataclasses import replace

    from sitg_bot.services import training_knowledge
    from sitg_bot.storage.models import KnowledgeSnapshotRecord, TrainingCategoryRecord

    owner = await create_player(database, 8251, "statistics-owner")
    other = await create_player(database, 8252, "statistics-other")
    app = await application(database, owner)
    async with database.transaction() as session:
        category = await session.scalar(select(TrainingCategoryRecord))
        await app.entitlements.assign_category(session, player_id=other, category_id=category.id)
    for actor in sorted([owner, other], reverse=True):
        fact = await app.mutate(actor, "create", {"title": str(actor), "description": "Fact"})
        await app.statistics_job({"owner_id": str(actor), "fact_id": str(fact["id"]), "version": 1})
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    for _ in range(3):
        await worker.run_once()
    app.settings = replace(app.settings, model="new-runtime-model", prompt_version="new-prompt")
    acquired = []
    guard = training_knowledge.require_trainee

    async def recording_guard(session, player_id, *, lock=False):
        result = await guard(session, player_id, lock=lock)
        if lock:
            acquired.append(player_id)
        return result

    monkeypatch.setattr(training_knowledge, "require_trainee", recording_guard)
    await app.reconcile_statistics()
    assert acquired == sorted([owner, other])
    async with database.sessions() as session:
        snapshots = list(await session.scalars(select(KnowledgeSnapshotRecord)))
        assert len(snapshots) == 2
        assert all(row.common["model"] == "fixture"
                   and row.common["prompt_version"] == app.analysis.settings.prompt_version
                   for row in snapshots)
    link = await app.mutate(owner, "link_create", {})
    acquired.clear()
    await app.mutate(other, "import", {"token": link["token"]})
    assert acquired == sorted([owner, other])
    acquired.clear()
    await app.mutate(owner, "grant", {"recipient": str(other)})
    assert acquired == sorted([owner, other])


async def test_fact_page_query_count_does_not_scale_per_card(database):
    owner = await create_player(database, 8301, "page-owner")
    app = KnowledgeApplication(database)
    for index in range(10):
        await app.mutate(
            owner,
            "create",
            {"title": f"Fact {index:02d}", "description": "Description", "tag_names": ["tag"]},
        )
    counts = []

    def count(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counts.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", count)
    try:
        await app.facts(owner, limit=1)
        small = len(counts)
        counts.clear()
        await app.facts(owner, limit=10)
        assert len(counts) == small
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", count)


async def test_gateway_shares_owned_copies_and_rejects_revocation(database):
    from sitg_bot.application.contracts import ApplicationPrincipal, GatewayRequest
    from sitg_bot.application.gateway import ApplicationGateway

    owner = await create_player(database, 8401, "gateway-owner")
    reader = await create_player(database, 8402, "gateway-reader")
    gateway = ApplicationGateway(database)

    async def execute(player_id, telegram_id, operation, key):
        return await gateway.execute(
            ApplicationPrincipal(player_id=player_id, telegram_user_id=telegram_id),
            GatewayRequest.model_validate(
                {
                    "metadata": {
                        "channel": "mini_app",
                        "client_name": "test",
                        "client_version": "1.0.0",
                        "idempotency_key": key,
                    },
                    "operation": operation,
                }
            ),
        )

    created = await execute(
        owner,
        8401,
        {"action": "knowledge.fact.create.v1", "title": "Title", "description": "Fact"},
        "gateway-create-fact",
    )
    assert created.ok
    fact_id = created.data["id"]
    grant = await execute(
        owner,
        8401,
        {"action": "knowledge.grant.create.v1", "recipient": "gateway-reader"},
        "gateway-grant-fact",
    )
    assert grant.ok and grant.data["status"] == "pending"
    staged = await execute(reader, 8402, {
        "action": "knowledge.import.view.v1", "import_id": grant.data["import_id"],
    }, None)
    approved = await execute(reader, 8402, {
        "action": "knowledge.import.decide.v1", "import_id": grant.data["import_id"],
        "decision": "approve", "selected_candidate_ids": [
            value["id"] for values in staged.data["sections"].values() for value in values
        ],
    }, "gateway-approve-copy")
    assert approved.ok and approved.data["approved_count"] == 1
    shared = await execute(
        reader, 8402, {"action": "knowledge.fact.view.v1", "fact_id": fact_id}, None
    )
    assert shared.error.code.value == "forbidden"
    copies = await execute(reader, 8402, {"action": "knowledge.facts.view.v1"}, None)
    copied_id = copies.data["items"][0]["id"]
    copied = await execute(
        reader, 8402, {"action": "knowledge.fact.view.v1", "fact_id": copied_id}, None
    )
    assert copied.ok and copied.data["editable"] is True
    grants = await execute(owner, 8401, {"action": "knowledge.sharing.view.v1"}, None)
    revoked = await execute(
        owner,
        8401,
        {"action": "knowledge.grant.revoke.v1", "grant_id": grants.data["grants"][0]["id"]},
        "gateway-revoke-fact",
    )
    assert revoked.error.code.value == "forbidden"
    denied = await execute(
        reader, 8402, {"action": "knowledge.fact.view.v1", "fact_id": fact_id}, None
    )
    assert denied.error.code.value == "forbidden"


async def test_application_server_initializes_policies_and_separates_worker_kinds(
    database, monkeypatch
):
    from sitg_bot.server import ConsoleApplicationServer

    monkeypatch.delenv("SITG_TRAINING_AI_API_KEY", raising=False)
    monkeypatch.delenv("SITG_TRAINING_AI_MODEL", raising=False)
    server = ConsoleApplicationServer(database, port=0)
    try:
        await server.start()
        assert "training.analysis" not in server.job_worker.kinds
        assert set(server.analysis_worker.kinds) == {"training.analysis", "knowledge.statistics"}
        owner = await create_player(database, 8501, "new-default")
        capabilities = await server.application_gateway.knowledge.entitlements.capabilities(owner)
        assert all(not feature.enabled for feature in capabilities.values())
    finally:
        await server.close()


async def upload_theme_packet(app, owner, count):
    import json
    from uuid import UUID

    result = await app.packets.import_upload(
        owner,
        json.dumps(
            {
                "name": f"Packet with {count} themes",
                "themes": [
                    {
                        "name": f"Theme {theme}",
                        "questions": [
                            {
                                "value": value,
                                "text": f"Question for theme {theme} and value {value}",
                                "answer": "Paris",
                            }
                            for value in (10, 20, 30, 40, 50)
                        ],
                    }
                    for theme in range(count)
                ],
            }
        ).encode(),
        "themes.json",
    )
    draft = result["drafts"][0]
    published = await app.packets.decide_draft(
        owner, UUID(draft["draft_id"]), expected_version=draft["version"], publish=True
    )
    return UUID(published["version_id"])


async def test_packet_analysis_charges_themes_with_independent_24_theme_daily_budgets(database):
    from sitg_bot.services.analysis_jobs import AnalysisQuotaExceededError
    from sitg_bot.services.training_ai import AnalysisValidationError
    from sitg_bot.storage.models import TrainingUsagePeriodRecord

    owner = await create_player(database, 8601, "theme-quota")
    app = await application(database, owner, limit=24)
    version = await upload_theme_packet(app, owner, 12)
    over_budget = await upload_theme_packet(app, owner, 13)
    dissect = await app.request_analysis(
        owner,
        version_id=version,
        kind="dissection",
        idempotency_key="twelve-themes-dissection",
        confirm_exposure=True,
    )
    buzz = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", idempotency_key="twelve-themes-buzz"
    )
    capabilities = await app.entitlements.capabilities(owner)
    assert capabilities["dissection"].remaining == capabilities["best_buzz"].remaining == 12
    assert capabilities["dissection"].unit == "themes"
    with pytest.raises(AnalysisQuotaExceededError):
        await app.request_analysis(
            owner,
            version_id=over_budget,
            kind="dissection",
            idempotency_key="cannot-queue-thirteen",
            confirm_exposure=True,
        )
    await app.analysis.cancel(owner_player_id=owner, request_id=buzz["request_id"])
    assert (await app.entitlements.capabilities(owner))["best_buzz"].remaining == 24
    worker = DurableJobWorker(
        app.analysis.jobs,
        {"training.analysis": app.analysis_job},
        kinds=("training.analysis",),
        batch_size=1,
    )
    await worker.run_once()
    assert (
        await app.analysis.status(owner_player_id=owner, request_id=dissect["request_id"])
    ).status == "succeeded"
    await approve_all(app, owner, dissect)
    await app.request_analysis(
        owner, version_id=version, kind="best_buzz", idempotency_key="new-buzz"
    )
    await worker.run_once()
    cached = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", idempotency_key="cached-buzz"
    )
    assert cached["status"] == "queued"  # new private requests must recompute
    async with database.transaction() as session:
        periods = list(
            await session.scalars(
                select(TrainingUsagePeriodRecord).where(
                    TrainingUsagePeriodRecord.player_id == owner
                )
            )
        )
        assert {period.feature: (period.settled, period.reserved) for period in periods} == {
            "dissection": (12, 0),
            "best_buzz": (12, 12),
        }
    await app.analysis.cancel(owner_player_id=owner, request_id=cached["request_id"])
    small = await upload_theme_packet(app, owner, 2)
    app.analysis.provider.buzz_failures.append(AnalysisValidationError("Malformed provider output"))
    failed = await app.request_analysis(
        owner,
        version_id=small,
        kind="best_buzz",
        idempotency_key="two-themes-fail",
        confirm_exposure=True,
    )
    assert (await app.entitlements.capabilities(owner))["best_buzz"].remaining == 10
    await worker.run_once()
    assert (await app.entitlements.capabilities(owner))["best_buzz"].remaining == 12
    assert (
        await app.analysis.status(owner_player_id=owner, request_id=failed["request_id"])
    ).status == "failed"

    # Cache expiration does not remove saved facts, packet revisions or pinned sessions.
    from datetime import UTC, datetime, timedelta

    training = TrainingService(database, best_buzz=app)
    started = await training.start(owner, version, settings=TrainingSettings(best_buzz=True))
    before = (await app.facts(owner))["total"]
    assert before == 60
    assert await app.analysis.cleanup(before=datetime.now(UTC) + timedelta(days=1)) > 0
    assert (await app.facts(owner))["total"] == before
    assert (await training.view(owner, UUID(started["session"]["id"])))["session"] is not None
