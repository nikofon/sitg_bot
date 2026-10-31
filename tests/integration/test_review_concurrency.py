import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import create_player
from sqlalchemy import event, func, select
from sqlalchemy.sql.dml import Update
from test_lobby_architecture import tournament_fixture
from test_training import upload
from test_training_knowledge import application

from sitg_bot.services import knowledge_review
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.reliable_delivery import DurableJobWorker
from sitg_bot.services.training_ai import (
    AnalysisValidationError,
    ProviderOutcomeUncertain,
    ProviderTimeout,
)
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    KnowledgeFactRecord,
    KnowledgeImportHashRecord,
    KnowledgeImportRecord,
    PacketVersionRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
    TrainingUsagePeriodRecord,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("scope", ["training", "tournament"])
async def test_approval_rechecks_source_after_concurrent_access_loss(database, monkeypatch, scope):
    if scope == "training":
        owner = await create_player(database, 9991, "approval-access")
        app = await application(database, owner)
        version, _ = await upload(database, owner)
    else:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        app = await application(database, owner)
        async with database.transaction() as session:
            assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            ))
            assignment_id = assignment.id
            version = assignment.adopted_version_id
            assignment.content_visible_by_members = True
            assignment.library_viewing_rule = "anytime"
            packet = await session.get(PacketVersionRecord, version)
            packet.library_released_at = datetime.now(UTC)
    result = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="approval-access",
    )
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    assert await worker.run_once() == 1
    detail = await app.reviews.detail(owner, result["import_id"])
    selected = [value["id"] for value in detail["sections"]["new"]]
    assert selected
    authorized, resume = asyncio.Event(), asyncio.Event()
    original = app.packets.authorize
    intercepted = False

    async def authorize(*args):
        nonlocal intercepted
        value = await original(*args)
        if not intercepted:
            intercepted = True
            authorized.set()
            await resume.wait()
        return value

    monkeypatch.setattr(app.packets, "authorize", authorize)
    approval = asyncio.create_task(app.reviews.decide(
        owner, import_id=result["import_id"], decision="approve", selected_candidate_ids=selected,
    ))
    try:
        await asyncio.wait_for(authorized.wait(), 5)
        if scope == "training":
            await app.packets.retire(owner, version)
        else:
            async with database.transaction() as session:
                session.add(TournamentPacketEntitlementRecord(
                    assignment_id=assignment_id, player_id=owner, readable_override=False,
                ))
        resume.set()
        with pytest.raises(LookupError, match="packet not found"):
            await asyncio.wait_for(approval, 5)
    finally:
        resume.set()
        await asyncio.gather(approval, return_exceptions=True)
    async with database.sessions() as session:
        review = await session.get(KnowledgeImportRecord, result["import_id"])
        assert review.status == "pending" and review.decision is None
        assert await session.scalar(select(func.count()).select_from(KnowledgeFactRecord)) == 0
        assert await session.scalar(
            select(func.count()).select_from(KnowledgeImportHashRecord)
        ) == 0


@pytest.mark.parametrize("kind", ["collection", "dissection"])
@pytest.mark.parametrize(("first_decision", "second_decision", "same_selection"), [
    ("approve", "reject", False),
    ("approve", "approve", False),
    ("approve", "approve", True),
    ("reject", "approve", False),
    ("reject", "reject", True),
])
async def test_concurrent_same_import_decisions_preserve_final_identity(
    database, monkeypatch, kind, first_decision, second_decision, same_selection,
):
    recipient = await create_player(database, 9997, "final-review-recipient")
    if kind == "collection":
        owner = await create_player(database, 9998, "final-review-sender")
        app = KnowledgeApplication(database)
        for title in ("First fact", "Second fact"):
            await app.mutate(owner, "create", {"title": title, "description": "Body"})
        result = await app.mutate(owner, "grant", {"recipient": str(recipient)})
    else:
        app = await application(database, recipient)
        version, _ = await upload(database, recipient)
        result = await app.request_analysis(
            recipient, version_id=version, kind="dissection", confirm_exposure=True,
            idempotency_key="final-review",
        )
        worker = DurableJobWorker(
            app.analysis.jobs, {"training.analysis": app.analysis_job},
            kinds=("training.analysis",),
        )
        assert await worker.run_once() == 1
    import_id = result["import_id"]
    detail = await app.reviews.detail(recipient, import_id)
    candidates = detail["sections"]["new"]
    assert len(candidates) >= 2
    first_selected = [candidates[0]["id"]] if first_decision == "approve" else []
    second_selected = first_selected if same_selection else (
        [candidates[1]["id"]] if second_decision == "approve" else []
    )
    first_locked, second_preloaded, release_first = (
        asyncio.Event(), asyncio.Event(), asyncio.Event()
    )
    original = knowledge_review.require_trainee
    arrivals = 0
    second_pid = None

    async def guard(session, actor, *, lock=False):
        nonlocal arrivals, second_pid
        if not lock:
            return await original(session, actor, lock=lock)
        arrivals += 1
        if arrivals == 1:
            player = await original(session, actor, lock=True)
            first_locked.set()
            await release_first.wait()
            return player
        if arrivals == 2:
            preview = await session.get(KnowledgeImportRecord, import_id)
            assert preview.status == "pending" and preview.decision is None
            second_pid = await session.scalar(select(func.pg_backend_pid()))
            second_preloaded.set()
        return await original(session, actor, lock=True)

    monkeypatch.setattr(knowledge_review, "require_trainee", guard)
    first_task = asyncio.create_task(app.reviews.decide(
        recipient, import_id=import_id, decision=first_decision,
        selected_candidate_ids=first_selected,
    ))
    second_task = None
    try:
        await asyncio.wait_for(first_locked.wait(), 5)
        second_task = asyncio.create_task(app.reviews.decide(
            recipient, import_id=import_id, decision=second_decision,
            selected_candidate_ids=second_selected,
        ))
        await asyncio.wait_for(second_preloaded.wait(), 5)
        async with asyncio.timeout(5), database.sessions() as session:
            while not await session.scalar(select(func.pg_blocking_pids(second_pid))):
                await asyncio.sleep(0.01)
        assert not first_task.done() and not second_task.done()
        release_first.set()
        first = await asyncio.wait_for(first_task, 5)
        if same_selection:
            assert await asyncio.wait_for(second_task, 5) == first
        else:
            with pytest.raises(StaleWriteError, match="already final"):
                await asyncio.wait_for(second_task, 5)
    finally:
        release_first.set()
        await asyncio.gather(
            first_task, *([second_task] if second_task else []), return_exceptions=True,
        )
        monkeypatch.setattr(knowledge_review, "require_trainee", original)
    assert first["status"] == ("approved" if first_decision == "approve" else "rejected")
    assert first["approved_count"] == (1 if first_decision == "approve" else 0)
    assert first["skipped_count"] == 0
    assert await app.reviews.decide(
        recipient, import_id=import_id, decision=first_decision,
        selected_candidate_ids=first_selected,
    ) == first
    async with database.sessions() as session:
        review = await session.get(KnowledgeImportRecord, import_id)
        assert review.status == first["status"]
        assert review.decision == {
            "decision": first_decision, "selected_candidate_ids": sorted(first_selected),
            "approved_count": first["approved_count"], "skipped_count": 0,
        }
        facts = list(await session.scalars(select(KnowledgeFactRecord).where(
            KnowledgeFactRecord.owner_id == recipient,
        )))
        assert [fact.title for fact in facts] == (
            [candidates[0]["title"]] if first_decision == "approve" else []
        )
        assert await session.scalar(
            select(func.count()).select_from(KnowledgeImportHashRecord).where(
                KnowledgeImportHashRecord.recipient_id == recipient,
            )
        ) == first["approved_count"]


def provider_error(kind):
    if kind == "uncertain":
        return ProviderOutcomeUncertain("Old provider failed", provider_request_id="old-call")
    return {
        "validation": AnalysisValidationError,
        "timeout": ProviderTimeout,
        "value": ValueError,
        "worker": RuntimeError,
    }[kind]("Old provider failed")


@pytest.mark.parametrize("kind", ["validation", "timeout", "uncertain", "value", "worker"])
async def test_same_generation_stale_errors_preserve_replacement_and_reservation(
    database, monkeypatch, kind,
):
    owner = await create_player(database, 9992, "same-generation-error")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    result = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="same-generation-error",
    )
    async with database.sessions() as session:
        request = await app.analysis._request_row(session, result["request_id"])
        artifact_id = request.artifact_id
    payload = {"artifact_id": str(artifact_id), "generation": 1}
    old_started, old_release = asyncio.Event(), asyncio.Event()
    new_started, new_release = asyncio.Event(), asyncio.Event()
    extract = app.analysis.provider.extract_facts
    calls = 0

    async def blocked(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            old_started.set()
            await old_release.wait()
            raise provider_error(kind)
        new_started.set()
        await new_release.wait()
        return await extract(request)

    monkeypatch.setattr(app.analysis.provider, "extract_facts", blocked)
    old_task = asyncio.create_task(app.analysis_job(payload))
    new_task = None
    try:
        await asyncio.wait_for(old_started.wait(), 5)
        async with database.transaction() as session:
            artifact = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
            old_token = artifact.lease_token
            artifact.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        new_task = asyncio.create_task(app.analysis_job(payload))
        await asyncio.wait_for(new_started.wait(), 5)
        async with database.sessions() as session:
            before = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
            assert before.generation == 1 and before.lease_token != old_token
            usage = await session.scalar(select(TrainingUsagePeriodRecord).where(
                TrainingUsagePeriodRecord.player_id == owner,
                TrainingUsagePeriodRecord.feature == "dissection",
            ))
            reserved = usage.reserved
            assert reserved > 0 and usage.settled == 0
        old_release.set()
        if kind == "worker":
            with pytest.raises(RuntimeError, match="Old provider failed"):
                await asyncio.wait_for(old_task, 5)
        else:
            await asyncio.wait_for(old_task, 5)
        async with database.sessions() as session:
            after = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
            fields = (
                "status", "generation", "lease_token", "lease_expires_at", "last_error",
                "provider_usage", "provider_request_ids", "checkpoint", "payload",
            )
            assert tuple(getattr(after, key) for key in fields) == tuple(
                getattr(before, key) for key in fields
            )
            request = await app.analysis._request_row(session, result["request_id"])
            review = await session.get(KnowledgeImportRecord, result["import_id"])
            assert request.status == "running" and review.status == "working"
            usage = await session.scalar(select(TrainingUsagePeriodRecord).where(
                TrainingUsagePeriodRecord.player_id == owner,
                TrainingUsagePeriodRecord.feature == "dissection",
            ))
            assert usage.reserved == reserved and usage.settled == 0
        new_release.set()
        await asyncio.wait_for(new_task, 5)
        assert (await app.reviews.detail(owner, result["import_id"]))["status"] == "pending"
        async with database.sessions() as session:
            request = await app.analysis._request_row(session, result["request_id"])
            assert request.status == "succeeded"
            usage = await session.scalar(select(TrainingUsagePeriodRecord).where(
                TrainingUsagePeriodRecord.player_id == owner,
                TrainingUsagePeriodRecord.feature == "dissection",
            ))
            assert usage.reserved == 0 and usage.settled == reserved
    finally:
        old_release.set()
        new_release.set()
        await asyncio.gather(old_task, *([new_task] if new_task else []), return_exceptions=True)


@pytest.mark.parametrize("kind", ["validation", "timeout", "uncertain", "value", "worker"])
async def test_current_worker_errors_fail_or_release_only_the_owned_computation(
    database, monkeypatch, kind,
):
    owner = await create_player(database, 9994, "current-worker-error")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    result = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="current-worker-error",
    )
    async with database.sessions() as session:
        request = await app.analysis._request_row(session, result["request_id"])
        artifact_id = request.artifact_id
    payload = {"artifact_id": str(artifact_id), "generation": 1}
    extract = app.analysis.provider.extract_facts

    async def fail(request):
        raise provider_error(kind)

    monkeypatch.setattr(app.analysis.provider, "extract_facts", fail)
    if kind == "worker":
        with pytest.raises(RuntimeError, match="Old provider failed"):
            await app.analysis_job(payload)
    else:
        await app.analysis_job(payload)
    async with database.sessions() as session:
        artifact = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
        request = await app.analysis._request_row(session, result["request_id"])
        review = await session.get(KnowledgeImportRecord, result["import_id"])
        usage = await session.scalar(select(TrainingUsagePeriodRecord).where(
            TrainingUsagePeriodRecord.player_id == owner,
            TrainingUsagePeriodRecord.feature == "dissection",
        ))
        if kind == "worker":
            assert artifact.status == request.status == "running" and review.status == "working"
            assert artifact.lease_expires_at <= datetime.now(UTC)
            assert usage.reserved > 0 and usage.settled == 0
        else:
            assert artifact.status == request.status == review.status == "failed"
            assert artifact.lease_token is None and artifact.lease_expires_at is None
            assert usage.reserved == usage.settled == 0
            if kind == "uncertain":
                assert artifact.provider_request_ids == ["old-call"]
    if kind == "worker":
        monkeypatch.setattr(app.analysis.provider, "extract_facts", extract)
        await app.analysis_job(payload)
        assert (await app.reviews.detail(owner, result["import_id"]))["status"] == "pending"


@pytest.mark.parametrize("rollback", [False, True])
async def test_retirement_atomically_fails_working_reviews_and_preserves_completed_reviews(
    database, rollback,
):
    owner = await create_player(database, 9993, "retired-review")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    results = {}
    for status in ("working", "pending", "approved", "rejected"):
        results[status] = await app.request_analysis(
            owner, version_id=version, kind="dissection", confirm_exposure=True,
            instructions=status, idempotency_key=f"retirement-{status}",
        )
        if status != "working":
            async with database.sessions() as session:
                request = await app.analysis._request_row(session, results[status]["request_id"])
                artifact_id = request.artifact_id
            await app.analysis_job({"artifact_id": str(artifact_id), "generation": 1})
            if status != "pending":
                detail = await app.reviews.detail(owner, results[status]["import_id"])
                await app.reviews.decide(
                    owner, import_id=detail["id"], decision="approve" if status == "approved"
                    else "reject", selected_candidate_ids=[
                        value["id"] for value in detail["sections"]["new"]
                    ],
                )
    async with database.sessions() as session:
        snapshots = {
            row.id: (row.candidates, row.decision) for row in await session.scalars(
                select(KnowledgeImportRecord)
            )
        }
    await app.packets.retire(owner, version)
    injected = False

    def fail_review_update(connection, cursor, statement, parameters, context, executemany):
        nonlocal injected
        compiled = context.compiled
        if (
            compiled is not None and isinstance(compiled.statement, Update)
            and compiled.statement.table.name == KnowledgeImportRecord.__tablename__
        ):
            injected = True
            raise RuntimeError("Review update failed")

    if rollback:
        event.listen(database.engine.sync_engine, "before_cursor_execute", fail_review_update)
    try:
        await app.cleanup_retired()
    finally:
        if rollback:
            event.remove(database.engine.sync_engine, "before_cursor_execute", fail_review_update)
    assert injected == rollback
    async with database.sessions() as session:
        for status, result in results.items():
            review = await session.get(KnowledgeImportRecord, result["import_id"])
            dissection = await session.get(KnowledgeDissectionRecord, result["dissection_id"])
            assert review.status == ("failed" if status == "working" and not rollback else status)
            expected_status = "pending" if rollback else "failed"
            assert dissection.status == (expected_status if status == "working" else "succeeded")
            assert (review.candidates, review.decision) == snapshots[review.id]
        expected = 4 if rollback else 0
        assert await session.scalar(
            select(func.count()).select_from(TrainingAnalysisArtifactRecord)
        ) == expected
        assert await session.scalar(
            select(func.count()).select_from(TrainingAnalysisRequestRecord)
        ) == expected
        assert await session.scalar(select(func.count()).select_from(KnowledgeFactRecord)) == 5
