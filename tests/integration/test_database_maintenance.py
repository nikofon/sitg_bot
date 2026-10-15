import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from test_initial_schema import baseline_database as baseline_database

from sitg_bot.application.contracts import (
    ActionCode,
    ApplicationPrincipal,
    CapabilitiesOperation,
    GatewayRequest,
    GatewayResponse,
    RequestMetadata,
)
from sitg_bot.application.gateway import ACTION_POLICIES, ApplicationGateway
from sitg_bot.application.telegram import DuplicateTelegramUpdate
from sitg_bot.services.database_maintenance import MAINTENANCE_LOCK, DatabaseMaintenanceService
from sitg_bot.services.reliable_delivery import DurableJobQueue, TransactionalOutbox
from sitg_bot.services.telegram_auth import TelegramUpdateAuthService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ApplicationIdempotencyRecord,
    ApplicationLaunchReferenceRecord,
    ApplicationRequestAuditRecord,
    DurableJobRecord,
    MiniAppSessionRecord,
    OutboxEventRecord,
    PlayerRecord,
    TelegramUpdateReceiptRecord,
)

pytestmark = pytest.mark.integration
NOW = datetime(2030, 1, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=40)


@pytest.fixture
def maintenance_url(baseline_database):
    url, config = baseline_database
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def database(maintenance_url):
    database = Database(maintenance_url)
    try:
        yield database
    finally:
        await database.close()


def maintenance(database, **kwargs):
    return DatabaseMaintenanceService(database, clock=lambda: NOW, **kwargs)


def job(status="succeeded", timestamp=OLD, **values):
    defaults = dict(
        id=uuid4(), kind="game.deadline", deduplication_key=f"deadline:{uuid4()}",
        payload={"game_id": str(uuid4())}, status=status, created_at=OLD,
        scheduled_at=OLD, last_error="diagnostic" if status == "failed" else None,
        completed_at=timestamp if status == "succeeded" else None,
        failed_at=timestamp if status == "failed" else None,
        lease_token=uuid4() if status == "running" else None,
        lease_expires_at=OLD if status == "running" else None,
    )
    return DurableJobRecord(**(defaults | values))


def event(status="delivered", timestamp=OLD, **values):
    defaults = dict(
        id=uuid4(), topic="game.event", deduplication_key=f"event:{uuid4()}",
        partition_key=f"chat:{uuid4()}", payload={"text": "retained until terminal cutoff"},
        status=status, created_at=OLD, available_at=OLD,
        delivered_at=timestamp if status == "delivered" else None,
        failed_at=timestamp if status == "failed" else None,
        last_error="diagnostic" if status == "failed" else None,
        lease_token=uuid4() if status == "processing" else None,
        lease_expires_at=OLD if status == "processing" else None,
    )
    return OutboxEventRecord(**(defaults | values))


def audit(outcome="succeeded", timestamp=OLD):
    return ApplicationRequestAuditRecord(
        id=uuid4(), correlation_id=uuid4(), channel="internal", action="test",
        client_name="test", client_version="1", outcome=outcome,
        completed_at=timestamp, created_at=OLD,
    )


def session_record(player_id, expires_at, **values):
    return MiniAppSessionRecord(**(dict(
        id=uuid4(), player_id=player_id, token_digest=uuid4().hex, csrf_digest=uuid4().hex,
        bot_id=42, environment="test", origin="https://example.test",
        telegram_auth_date=OLD, expires_at=expires_at, last_seen_at=OLD,
    ) | values))


def reference(player_id, expires_at, **values):
    return ApplicationLaunchReferenceRecord(**(dict(
        id=uuid4(), token_digest=uuid4().hex, route="lobby", target_id=uuid4(),
        created_by_player_id=player_id, bot_id=42, environment="test", expires_at=expires_at,
    ) | values))


async def player(database):
    async with database.transaction() as session:
        record = PlayerRecord(
            id=uuid4(), real_name="Test", public_nickname="Test", status="active",
            registration_step="complete", registration_completed_at=OLD,
        )
        session.add(record)
    return record.id


async def test_terminal_retention_preview_and_repeat(database):
    success_boundary = NOW - timedelta(days=7)
    failure_boundary = NOW - timedelta(days=30)
    deleted_jobs = [
        job(kind="rating.settlement", deduplication_key="periodic:rating.settlement:1"),
        job("failed", kind="matchmaking.scan", deduplication_key="periodic:matchmaking.scan:1"),
    ]
    compacted_jobs = [
        job(), job("failed"),
        job(kind="unknown", deduplication_key="periodic:unknown:1"),
        job(kind="rating.settlement", deduplication_key="periodic:classic.reconcile:1"),
        job(kind="rating.settlement", deduplication_key="periodic:rating.settlement:invalid"),
    ]
    kept_jobs = [
        job(timestamp=success_boundary), job(timestamp=NOW),
        job("failed", failure_boundary), job("failed", NOW),
        job("queued"), job("running"), job("cancelled"),
        job(payload_pruned_at=OLD),
    ]
    compacted_events = [event(), event("failed")]
    kept_events = [
        event(timestamp=success_boundary), event("failed", failure_boundary),
        event("pending", topic="lobby.event"), event("processing"),
        event(topic="telegram.chat.message"), event("failed", topic="telegram.chat.message"),
        event(payload_pruned_at=OLD),
    ]
    deleted_audits = [audit(), audit("rejected"), audit("failed")]
    kept_audits = [
        audit(timestamp=success_boundary), audit("failed", failure_boundary),
        audit("rejected", NOW - timedelta(days=29)), audit(timestamp=None),
        audit("started", None),
    ]
    async with database.transaction() as session:
        session.add_all(deleted_jobs + compacted_jobs + kept_jobs + compacted_events
                        + kept_events + deleted_audits + kept_audits)

    service = maintenance(database)
    preview = await service.run_once()
    assert preview.dry_run and preview.lock_acquired and not preview.work_limit_reached
    assert preview.categories["periodic_jobs"].eligible == len(deleted_jobs)
    assert preview.categories["jobs"].eligible == len(compacted_jobs)
    assert preview.categories["outbox_events"].eligible == len(compacted_events)
    assert all(c.deleted == c.compacted == 0 for c in preview.categories.values())
    async with database.sessions() as session:
        assert await session.get(DurableJobRecord, deleted_jobs[0].id) is not None
        assert (await session.get(DurableJobRecord, compacted_jobs[0].id)).payload

    applied = await service.run_once(dry_run=False)
    assert applied.categories["periodic_jobs"].deleted == len(deleted_jobs)
    assert applied.categories["jobs"].compacted == len(compacted_jobs)
    assert applied.categories["outbox_events"].compacted == len(compacted_events)
    assert applied.categories["request_audits"].deleted == len(deleted_audits)
    assert any(r["status"] == "pending" and r["kind_or_topic"] == "lobby.event"
               and r["count"] == 1 for r in applied.unfinished)
    async with database.sessions() as session:
        for record in deleted_jobs + deleted_audits:
            assert await session.get(type(record), record.id) is None
        for record in compacted_jobs + compacted_events:
            current = await session.get(type(record), record.id)
            assert current.payload == {} and current.last_error is None
            assert current.payload_pruned_at == NOW
            assert current.status == record.status
            assert current.deduplication_key == record.deduplication_key
            assert current.attempts == record.attempts
        for record in kept_jobs + kept_events + kept_audits:
            current = await session.get(type(record), record.id)
            assert current is not None
            if hasattr(record, "payload"):
                assert current.payload == record.payload
                assert current.payload_pruned_at == record.payload_pruned_at
    repeated = await service.run_once(dry_run=False)
    assert all(c.eligible == 0 for c in repeated.categories.values())


async def test_session_and_reference_expiry_grace(database):
    player_id = await player(database)
    old = NOW - timedelta(days=2)
    boundary = NOW - timedelta(days=1)
    deleted = [
        session_record(player_id, old), session_record(player_id, old, revoked_at=OLD),
        reference(player_id, old),
        reference(player_id, old, redeemed_at=OLD, redeemed_by_player_id=player_id),
    ]
    kept = [
        session_record(player_id, boundary), reference(player_id, boundary),
        session_record(player_id, NOW - timedelta(hours=23)),
        reference(player_id, NOW - timedelta(hours=23)),
        session_record(player_id, NOW + timedelta(days=1), revoked_at=OLD),
        reference(player_id, NOW + timedelta(days=1), redeemed_at=OLD),
    ]
    async with database.transaction() as session:
        session.add_all(deleted + kept)
    report = await maintenance(database).run_once(dry_run=False)
    assert report.categories["sessions"].deleted == 2
    assert report.categories["launch_references"].deleted == 2
    async with database.sessions() as session:
        for record in deleted:
            assert await session.get(type(record), record.id) is None
        for record in kept:
            assert await session.get(type(record), record.id) is not None


async def test_replay_keys_cached_response_and_receipts_survive(database):
    cached_job, cached_event = job(), event()
    gateway = ApplicationGateway(database)
    principal = ApplicationPrincipal(telegram_user_id=501)
    request = GatewayRequest(
        metadata=RequestMetadata(
            channel="telegram_bot", client_name="test", client_version="1",
            idempotency_key="old-request",
        ),
        operation=CapabilitiesOperation(action=ActionCode.CAPABILITIES),
    )
    response = GatewayResponse(
        action=ActionCode.CAPABILITIES, correlation_id=request.metadata.correlation_id,
        ok=True, data={"cached": "original result"},
    )
    key_digest = hashlib.sha256(b"old-request").hexdigest()
    cached_request = ApplicationIdempotencyRecord(
        actor_scope=hashlib.sha256(b"telegram:501").hexdigest(), channel="telegram_bot",
        action=ActionCode.CAPABILITIES.value, key_digest=key_digest,
        request_digest=gateway._request_digest(request), correlation_id=uuid4(),
        status="completed", response_payload=response.model_dump(mode="json"),
        lease_expires_at=OLD, completed_at=OLD, created_at=OLD,
    )
    receipt = TelegramUpdateReceiptRecord(
        bot_id=42, environment="test", update_id=10, correlation_id=uuid4(),
        status="completed", lease_expires_at=OLD, completed_at=OLD, created_at=OLD,
    )
    async with database.transaction() as session:
        session.add_all([cached_job, cached_event, cached_request, receipt])
    await maintenance(database).run_once(dry_run=False)
    assert await DurableJobQueue(database).schedule(
        cached_job.kind, cached_job.deduplication_key, {"new": "ignored"},
    ) == cached_job.id
    async with database.transaction() as session:
        assert await TransactionalOutbox.enqueue(
            session, topic=cached_event.topic, deduplication_key=cached_event.deduplication_key,
            partition_key=cached_event.partition_key, payload={"new": "ignored"},
        ) == cached_event.id
    result, record = await gateway._begin_idempotency(
        principal, request, policy=ACTION_POLICIES[ActionCode.CAPABILITIES], key_digest=key_digest,
    )
    assert result == response and record.id == cached_request.id
    with pytest.raises(DuplicateTelegramUpdate):
        await TelegramUpdateAuthService(database, environment="test").claim(
            bot_id=42, update_id=10, telegram_user_id=501,
        )


async def test_locks_workers_refresh_and_outbox_ordering(database):
    player_id = await player(database)
    locked_job, queued_job = job(), job("queued")
    refreshing = session_record(player_id, NOW - timedelta(days=2))
    delivered = event(partition_key="ordered", created_at=OLD - timedelta(seconds=2))
    first = event("pending", partition_key="ordered", created_at=OLD - timedelta(seconds=1))
    second = event("pending", partition_key="ordered", created_at=OLD)
    async with database.transaction() as session:
        session.add_all([locked_job, queued_job, refreshing, delivered, first, second])
    queue, outbox = DurableJobQueue(database), TransactionalOutbox(database)
    async with database.transaction() as owner:
        await owner.get(DurableJobRecord, locked_job.id, with_for_update=True)
        current_session = await owner.get(MiniAppSessionRecord, refreshing.id, with_for_update=True)
        # A concurrently locked session must be skipped; a renewed expiry must be respected.
        current_session.expires_at = NOW + timedelta(days=1)
        claims = await queue.claim(now=NOW)
        assert len(claims) == 1 and claims[0].job_id == queued_job.id
        report = await maintenance(database).run_once(dry_run=False)
        assert report.categories["jobs"].compacted == 0
        assert report.categories["sessions"].deleted == 0
        deliveries = await outbox.claim(now=NOW)
        assert [d.event_id for d in deliveries] == [first.id]
        await outbox.acknowledge(first.id, deliveries[0].lease_token)
        deliveries = await outbox.claim(now=NOW)
        assert [d.event_id for d in deliveries] == [second.id]
    report = await maintenance(database).run_once(dry_run=False)
    assert report.categories["jobs"].compacted == 1
    async with database.sessions() as session:
        assert (await session.get(DurableJobRecord, queued_job.id)).payload == queued_job.payload
        assert (await session.get(MiniAppSessionRecord, refreshing.id)).expires_at > NOW


@pytest.mark.parametrize("termination", ["cancel", "timeout"])
async def test_advisory_lock_serialization_and_release(database, monkeypatch, termination):
    async with database.transaction() as session:
        session.add(job())
    service = maintenance(database, max_runtime=0.5 if termination == "timeout" else 10)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_batch", blocked)
    task = asyncio.create_task(service.run_once(dry_run=False))
    await asyncio.wait_for(entered.wait(), timeout=3)
    skipped = await maintenance(database).run_once(dry_run=False)
    assert not skipped.lock_acquired
    assert all(c.eligible == 0 for c in skipped.categories.values())
    if termination == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).work_limit_reached
    report = await maintenance(database).run_once(dry_run=False)
    assert report.lock_acquired and report.categories["jobs"].compacted == 1
    # Explicitly verify pooled connections do not retain the session lock.
    async with database.engine.connect() as connection:
        assert await connection.scalar(select(func.pg_try_advisory_lock(MAINTENANCE_LOCK)))
        await connection.execute(select(func.pg_advisory_unlock(MAINTENANCE_LOCK)))


async def test_bounded_round_robin_and_preview_pagination(database):
    player_id = await player(database)
    async with database.transaction() as session:
        session.add_all([
            *(job() for _ in range(5)), *(audit() for _ in range(2)),
            session_record(player_id, OLD), reference(player_id, OLD),
        ])
    small = maintenance(database, batch_size=1, max_batches=8)
    preview = await small.run_once()
    assert preview.work_limit_reached
    assert preview.categories["jobs"].eligible == 1
    assert preview.categories["request_audits"].eligible == 1
    assert preview.categories["sessions"].eligible == 1
    assert preview.categories["launch_references"].eligible == 1
    preview = await maintenance(database, batch_size=1).run_once()
    assert not preview.work_limit_reached
    assert preview.categories["jobs"].eligible == 5
    assert preview.categories["request_audits"].eligible == 2
    applied = await small.run_once(dry_run=False)
    assert applied.work_limit_reached and applied.categories["jobs"].compacted == 1
    final = await maintenance(database).run_once(dry_run=False)
    assert final.categories["jobs"].compacted == 4
    assert final.categories["request_audits"].deleted == 1


def test_maintenance_migration_preserves_existing_rows(baseline_database):
    url, config = baseline_database
    command.upgrade(config, "0019_lobby_messages")
    identifier = uuid4()

    async def seed():
        database = Database(url)
        try:
            async with database.transaction() as session:
                await session.execute(text("""
                    INSERT INTO durable_jobs (id, kind, deduplication_key, payload, status,
                                             completed_at)
                    VALUES (:id, 'rating.settlement', :key, '{"keep": true}', 'succeeded', :old)
                """), {"id": identifier, "key": "periodic:rating.settlement:1", "old": OLD})
        finally:
            await database.close()

    async def verify(upgraded):
        database = Database(url)
        try:
            async with database.sessions() as session:
                row = (await session.execute(text(
                    "SELECT * FROM durable_jobs WHERE id = :id"
                ), {"id": identifier})).mappings().one()
                assert row["payload"] == {"keep": True} and row["status"] == "succeeded"
                if upgraded:
                    assert row["payload_pruned_at"] is None
                else:
                    assert "payload_pruned_at" not in row
        finally:
            await database.close()

    asyncio.run(seed())
    command.upgrade(config, "head")
    asyncio.run(verify(True))
    command.downgrade(config, "0019_lobby_messages")
    asyncio.run(verify(False))
    command.upgrade(config, "head")
    asyncio.run(verify(True))
