import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import create_player, seed_category
from sqlalchemy import event, select
from test_training import upload

from sitg_bot.domain.training import TrainingSettings
from sitg_bot.services.analysis_jobs import AnalysisService
from sitg_bot.services.entitlements import TRAINING_FEATURES, EntitlementService
from sitg_bot.services.reliable_delivery import DurableJobQueue, DurableJobWorker
from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_ai import FakeAnalysisProvider, ProviderSettings
from sitg_bot.services.training_knowledge import KnowledgeApplication, TrainingAnalysisAccess
from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
    TrainingBestBuzzCompletionRecord,
    TrainingUsagePeriodRecord,
)

pytestmark = pytest.mark.integration


async def application(database, owner, *, limit=100):
    settings = ProviderSettings("fake", "fixture")
    app = KnowledgeApplication(database, settings=settings)
    app.analysis = AnalysisService(
        database,
        DurableJobQueue(database),
        FakeAnalysisProvider(),
        settings,
        entitlements=EntitlementService(database),
        source_loader=TrainingAnalysisAccess(database, app.packets),
    )
    await seed_category(
        database,
        "integration",
        {
            feature: {
                "enabled": True,
                "limit": limit,
                "reset_period": "daily",
                "cache_hit_charging": False,
            }
            for feature in TRAINING_FEATURES
        },
        [owner],
    )
    return app


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
    await app.mutate(owner, "grant", {"recipient": "reader"})
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
    await app.mutate(owner, "grant", {"recipient": "reader"})
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
    assert second["dissection_id"] == first["dissection_id"]
    assert second["request_id"] != first["request_id"]
    await worker.run_once()
    await app.reconcile()
    assert (await app.dissections(owner))["items"][0]["status"] == "succeeded"
    assert (await app.facts(owner))["total"] == 5
    await app.reconcile()
    assert (await app.facts(owner))["total"] == 5


async def test_dissection_automatic_application_and_best_buzz_bridge(database):
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
            await app.analysis._fail_artifact(session, artifact, "Fixture failure", datetime.now(UTC))
    if reconciled:
        await app.reconcile()
    retried = await app.request_analysis(
        owner, version_id=version, kind="dissection", idempotency_key="retry-dissection",
    )
    assert retried["request_id"] != first["request_id"]
    assert retried["dissection_id"] == first["dissection_id"]
    async with database.sessions() as session:
        row = await session.get(KnowledgeDissectionRecord, first["dissection_id"])
        assert row.status == "pending" and row.application is None
        assert row.analysis_request_id == retried["request_id"]
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    for _ in range(3):
        await worker.run_once()
    facts = await app.facts(owner)
    assert facts["total"] == 5
    await app.mutate(owner, "delete", {
        "fact_id": facts["items"][0]["id"], "confirm_dissection_origin": True,
    })
    repeated = await app.request_analysis(
        owner, version_id=version, kind="dissection", idempotency_key="after-application",
    )
    assert repeated["request_id"] == retried["request_id"]
    await app.reconcile()
    assert (await app.facts(owner))["total"] == 4
    async with database.sessions() as session:
        row = await session.get(KnowledgeDissectionRecord, first["dissection_id"])
        assert row.status == "succeeded" and row.application is not None
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord).where(
            TrainingUsagePeriodRecord.feature == "dissection",
        )))
        assert len(periods) == 1 and periods[0].reserved == 0 and periods[0].settled == 1


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
    original_lookup = app.analysis._artifact_for_key

    async def complete(*args):
        terminal_locked.set()
        await waiter_lookup.wait()
        await original_complete(*args)

    async def lookup(*args, **kwargs):
        waiter_lookup.set()
        return await original_lookup(*args, **kwargs)

    monkeypatch.setattr(app.analysis, "_complete_requests", complete)
    monkeypatch.setattr(app.analysis, "_artifact_for_key", lookup)
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
        assert result["status"] == "succeeded"
    finally:
        waiter_lookup.set()
        for task in (terminal, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(terminal, *([waiter] if waiter else []), return_exceptions=True)
    async with database.sessions() as session:
        requests = list(await session.scalars(select(TrainingAnalysisRequestRecord)))
        assert len(requests) == 2 and all(row.status == "succeeded" for row in requests)
        periods = list(await session.scalars(select(TrainingUsagePeriodRecord)))
        assert len(periods) == 1 and periods[0].reserved == 0 and periods[0].settled == 1
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


async def test_statistics_lock_owners_in_the_same_order_as_collection_imports(database, monkeypatch):
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
        assert all(row.common["model"] == "fixture" and row.common["prompt_version"] == "1"
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
    assert grant.ok
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
    await app.request_analysis(
        owner, version_id=version, kind="best_buzz", idempotency_key="new-buzz"
    )
    await worker.run_once()
    cached = await app.request_analysis(
        owner, version_id=version, kind="best_buzz", idempotency_key="cached-buzz"
    )
    assert cached["status"] == "succeeded"
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
            "best_buzz": (12, 0),
        }
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
