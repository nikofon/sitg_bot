import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, delete, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sitg_bot.application.contracts import (
    ActionCode,
    CapabilitiesOperation,
    GatewayRequest,
    RequestMetadata,
)
from sitg_bot.application.gateway import ApplicationGateway, ApplicationPrincipal
from sitg_bot.services.reliable_delivery import DurableJobQueue, TransactionalOutbox
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ApplicationRequestAuditRecord,
    Base,
    DurableJobRecord,
    OutboxEventRecord,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def baseline_database(monkeypatch):
    source_url = os.environ.get("TEST_DATABASE_URL")
    if not source_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    name = f"sitg_baseline_{uuid4().hex}"

    async def manage(statement):
        engine = create_async_engine(source_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(manage(f'CREATE DATABASE "{name}"'))
    url = make_url(source_url).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    try:
        yield url, config
    finally:
        asyncio.run(manage(f'DROP DATABASE "{name}"'))


async def assert_schema(database_url, *, empty=False):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:

            def check(sync_connection):
                inspector = inspect(sync_connection)
                tables = set(inspector.get_table_names()) - {"alembic_version"}
                if empty:
                    assert not tables
                    return
                assert tables == set(Base.metadata.tables)
                context = MigrationContext.configure(
                    sync_connection, opts={"compare_type": True, "compare_server_default": True}
                )
                assert compare_metadata(context, Base.metadata) == []
                # Also check presence on Alembic versions without check comparison.
                for name, table in Base.metadata.tables.items():
                    assert len(inspector.get_check_constraints(name)) == sum(
                        isinstance(constraint, CheckConstraint) for constraint in table.constraints
                    ), name
                player_columns = {column["name"] for column in inspector.get_columns("players")}
                assert "display_name" not in player_columns
                tournament_columns = {
                    column["name"] for column in inspector.get_columns("tournaments")
                }
                assert "planned_ends_at" in tournament_columns
                assert {"ends_at", "price", "currency"}.isdisjoint(tournament_columns)
                request_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("tournament_creation_token_requests")
                }
                assert not request_columns["tournament_name"]["nullable"]
                game_columns = {
                    column["name"] for column in inspector.get_columns("telegram_game_views")
                }
                assert {"message_id", "render_key", "notice_sequence", "sequence"}.isdisjoint(
                    game_columns
                )
                assert {"flow_sequence", "messages", "dismissed_at", "connected"} <= game_columns
                claims = {
                    column["name"]: column
                    for column in inspector.get_columns("player_exposure_claims")
                }
                assert not claims["packet_version_id"]["nullable"]
                assert claims["game_id"]["nullable"]  # Authorship/library burns need no game.

            await connection.run_sync(check)
            if empty:
                assert await connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                return
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0001_initial_schema"
            )
            types = (
                await connection.execute(
                    text("SELECT key, version, rules FROM tournament_type_versions ORDER BY key")
                )
            ).all()
            assert [(row.key, row.version) for row in types] == [("classic", 1), ("ladder", 1)]
            assert types[0].rules["supports_hybrid_matchmaking"] is False
            assert types[1].rules["supports_hybrid_matchmaking"] is True
            assert (
                await connection.execute(text("SELECT key, version FROM game_ruleset_versions"))
            ).all() == [("si", 1)]
    finally:
        await engine.dispose()


def test_fresh_baseline_schema_seeds_and_round_trip(baseline_database):
    url, config = baseline_database
    command.upgrade(config, "head")
    asyncio.run(assert_schema(url))
    command.upgrade(config, "head")
    asyncio.run(assert_schema(url))
    command.downgrade(config, "base")
    asyncio.run(assert_schema(url, empty=True))
    command.upgrade(config, "head")
    asyncio.run(assert_schema(url))


async def _exercise_reliable_queues(database_url: str) -> tuple[str, str, int, int]:
    database = Database(database_url)
    suffix = uuid4().hex
    outbox = TransactionalOutbox(database)
    jobs = DurableJobQueue(database)
    event_id = None
    job_id = None
    try:
        async with database.transaction() as session:
            event_id = await outbox.enqueue(
                session,
                topic="integration.delivery",
                deduplication_key=f"integration-outbox:{suffix}",
                partition_key=f"integration:{suffix}",
                payload={"message_key": "integration.test", "parameters": {}},
            )
        deliveries = await outbox.claim(topics=("integration.delivery",), limit=1)
        assert len(deliveries) == 1
        await outbox.acknowledge(deliveries[0].event_id, deliveries[0].lease_token)

        job_id = await jobs.schedule(
            "integration.job",
            f"integration-job:{suffix}",
            {"value": suffix},
            scheduled_at=datetime.now(UTC),
        )
        claimed_jobs = await jobs.claim(limit=1)
        assert len(claimed_jobs) == 1
        await jobs.succeed(claimed_jobs[0].job_id, claimed_jobs[0].lease_token)

        async with database.sessions() as session:
            event = await session.get(OutboxEventRecord, event_id)
            job = await session.get(DurableJobRecord, job_id)
            assert event is not None
            assert job is not None
            return event.status, job.status, event.attempts, job.attempts
    finally:
        if event_id is not None or job_id is not None:
            async with database.transaction() as session:
                if event_id is not None:
                    await session.execute(
                        delete(OutboxEventRecord).where(OutboxEventRecord.id == event_id)
                    )
                if job_id is not None:
                    await session.execute(
                        delete(DurableJobRecord).where(DurableJobRecord.id == job_id)
                    )
        await database.close()


async def _exercise_shared_correlation(database_url: str) -> tuple[bool, bool, int]:
    database = Database(database_url)
    correlation_id = uuid4()
    gateway = ApplicationGateway(database)
    request = GatewayRequest(
        metadata=RequestMetadata(
            correlation_id=correlation_id,
            channel="internal",
            client_name="audit-correlation-test",
            client_version="1.0.0",
        ),
        operation=CapabilitiesOperation(action=ActionCode.CAPABILITIES),
    )
    try:
        first = await gateway.execute(ApplicationPrincipal(), request)
        second = await gateway.execute(ApplicationPrincipal(), request)
        async with database.transaction() as session:
            records = tuple(
                (
                    await session.execute(
                        select(ApplicationRequestAuditRecord).where(
                            ApplicationRequestAuditRecord.correlation_id == correlation_id
                        )
                    )
                ).scalars()
            )
            for record in records:
                await session.delete(record)
        return first.ok, second.ok, len(records)
    finally:
        await database.close()


def test_baseline_queue_lifecycle_and_shared_audit_correlation(baseline_database):
    url, config = baseline_database
    command.upgrade(config, "head")
    assert asyncio.run(_exercise_reliable_queues(url)) == ("delivered", "succeeded", 1, 1)
    assert asyncio.run(_exercise_shared_correlation(url)) == (True, True, 2)
