import asyncio
import hashlib
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
from sitg_bot.services.token_requests import (
    TokenPlaintextUnavailable,
    TournamentTokenRequestService,
)
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ApplicationRequestAuditRecord,
    Base,
    DurableJobRecord,
    OutboxEventRecord,
    PlatformAdministratorRecord,
    PlayerRecord,
    TournamentCreationTokenDeliveryRecord,
    TournamentCreationTokenRecord,
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
                "0016_zero_point_questions"
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


@pytest.mark.parametrize(
    "start_revision", ["base", "0009_classic_chats", "0010_library_viewing_rules"]
)
def test_chat_library_merge_from_each_branch(baseline_database, start_revision):
    url, config = baseline_database
    command.upgrade(config, start_revision)
    command.upgrade(config, "head")

    async def check_merge():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                assert (await connection.execute(text(
                    "SELECT version_num FROM alembic_version"
                ))).scalars().all() == ["0016_zero_point_questions"]
                # Both branches' schema changes must be present.
                await connection.execute(text("SELECT match_id FROM classic_chats LIMIT 0"))
                await connection.execute(text(
                    "SELECT library_viewing_rule FROM tournament_packet_assignments LIMIT 0"
                ))
        finally:
            await engine.dispose()

    asyncio.run(check_merge())
    command.downgrade(config, "base")
    asyncio.run(assert_schema(url, empty=True))
    command.upgrade(config, "head")
    asyncio.run(check_merge())


def test_appeals_per_answer_migration_round_trip(baseline_database):
    url, config = baseline_database

    async def unique_columns():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(lambda sync: [
                    c["column_names"] for c in inspect(sync).get_unique_constraints("appeals")
                ])
        finally:
            await engine.dispose()

    command.upgrade(config, "0011_merge_chats_library")
    assert asyncio.run(unique_columns()) == [["game_id", "round_id"]]
    command.upgrade(config, "head")
    assert asyncio.run(unique_columns()) == [["game_id", "target_attempt_id"]]
    command.downgrade(config, "0011_merge_chats_library")
    assert asyncio.run(unique_columns()) == [["game_id", "round_id"]]
    command.upgrade(config, "head")
    assert asyncio.run(unique_columns()) == [["game_id", "target_attempt_id"]]


def test_swiss_migration_preserves_existing_stages_and_round_trips(baseline_database):
    from test_classic import first_round, mutate, setup

    from sitg_bot.services.classic import ClassicService

    url, config = baseline_database
    command.upgrade(config, "head")

    async def seed():
        database = Database(url)
        try:
            fixture = await setup(database, 3)
            await mutate(database, fixture, "seed", mode="automatic")
            first, assignment_id = await first_round(database, fixture)
            await mutate(database, fixture, "round", round_id=first["id"],
                         assignment_id=str(assignment_id))
            async with database.sessions() as session:
                return fixture.tournament_id, await ClassicService(database).snapshot(
                    session, fixture.tournament_id,
                )
        finally:
            await database.close()

    tournament_id, before = asyncio.run(seed())

    async def columns():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(lambda sync: {
                    c["name"] for c in inspect(sync).get_columns("classic_stages")
                })
        finally:
            await engine.dispose()

    async def check_preserved():
        database = Database(url)
        try:
            async with database.sessions() as session:
                after = await ClassicService(database).snapshot(session, tournament_id)
                assert after == before
                assert after["stages"][0]["round_count"] is None
                assert after["stages"][0]["players_per_game"] is None
        finally:
            await database.close()

    for _ in range(2):
        command.downgrade(config, "0012_appeals_per_answer")
        assert {"round_count", "players_per_game"}.isdisjoint(asyncio.run(columns()))
        command.upgrade(config, "head")
        assert {"round_count", "players_per_game"} <= asyncio.run(columns())
        asyncio.run(check_preserved())
        asyncio.run(assert_schema(url))


def test_swiss_migration_refuses_to_discard_existing_swiss_stages(baseline_database):
    from sqlalchemy.exc import IntegrityError
    from test_classic_swiss import swiss_setup

    from sitg_bot.services.classic import ClassicService

    url, config = baseline_database
    command.upgrade(config, "head")

    async def seed():
        database = Database(url)
        try:
            fixture = await swiss_setup(database)
            return fixture.tournament_id
        finally:
            await database.close()

    tournament_id = asyncio.run(seed())
    with pytest.raises(IntegrityError):
        command.downgrade(config, "0012_appeals_per_answer")

    async def check_retained():
        database = Database(url)
        try:
            async with database.sessions() as session:
                view = await ClassicService(database).snapshot(session, tournament_id)
                assert view["stages"][0]["stage_type"] == "swiss"
                assert view["stages"][0]["round_count"] == 3
                assert view["stages"][0]["players_per_game"] == 4
                assert len(view["stages"][0]["rounds"]) == 3
                assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "0016_zero_point_questions"
                )
        finally:
            await database.close()

    asyncio.run(check_retained())


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


def test_player_limits_migration_initializes_existing_tournaments(baseline_database):
    from test_lobby_architecture import tournament_fixture

    from sitg_bot.services.matchmaking import InvitationMatchmakingService
    from sitg_bot.storage.models import (
        PregameLobbyRecord,
        TournamentPacketAssignmentRecord,
        TournamentPolicyVersionRecord,
        TournamentRecord,
    )

    url, config = baseline_database
    command.upgrade(config, "head")

    async def seed():
        database = Database(url)
        try:
            fixture = await tournament_fixture(database, player_count=1)
            lobby = await InvitationMatchmakingService(database).create_lobby(
                fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=6,
            )
            async with database.transaction() as session:
                policy = await session.scalar(select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id,
                ))
                policy.default_parameters = {"theme_count": 1}
                row = await session.get(PregameLobbyRecord, lobby.id)
                row.settings = {"theme_count": 1}
                tournament = await session.get(TournamentRecord, fixture.tournament_id)
                tournament.registration_open = True
                tournament.registration_open_override = True
                assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                ))
                assignment.library_viewing_rule = "never"
                assignment.playable_by_members = False
            return fixture.tournament_id, lobby.id
        finally:
            await database.close()

    tournament_id, lobby_id = asyncio.run(seed())
    command.downgrade(config, "0008_theme_commentary")

    async def prepare_legacy():
        database = Database(url)
        try:
            async with database.transaction() as session:
                await session.execute(text("""
                    UPDATE tournament_packet_assignments SET access_level_by_members = 'no-access'
                """))
        finally:
            await database.close()

    asyncio.run(prepare_legacy())

    async def check(upgraded):
        database = Database(url)
        try:
            async with database.sessions() as session:
                policy = await session.scalar(select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == tournament_id,
                ))
                lobby = await session.get(PregameLobbyRecord, lobby_id)
                for values in (policy.default_parameters, lobby.settings):
                    if upgraded:
                        assert values["minimum_players"] == values["maximum_players"] == 4
                    else:
                        assert "minimum_players" not in values and "maximum_players" not in values
                assert lobby.max_players == 4
                if upgraded:
                    # Raw SQL: the ORM includes columns added by later revisions.
                    row = (await session.execute(text("""
                        SELECT registration_open, registration_open_override
                        FROM tournaments WHERE id = :id
                    """), {"id": tournament_id})).one()
                    assert not row.registration_open
                    assert row.registration_open_override
                    rule, playable = (await session.execute(text("""
                        SELECT access_level_by_members, playable_by_members
                        FROM tournament_packet_assignments WHERE tournament_id = :id
                    """), {"id": tournament_id})).one()
                    assert rule == "read-after-play"
                    assert not playable
        finally:
            await database.close()

    command.upgrade(config, "0009_tournament_player_limits")
    asyncio.run(check(True))
    command.downgrade(config, "0008_theme_commentary")
    asyncio.run(check(False))
    command.upgrade(config, "0009_tournament_player_limits")
    asyncio.run(check(True))


@pytest.mark.parametrize("old_rule,new_rule", [
    ("no-access", "never"), ("play-only", "never"),
    ("read-after-play", "after-play"), ("read-or-play", "anytime"),
])
def test_library_viewing_migration_preserves_play_permissions(
    baseline_database, old_rule, new_rule
):
    from test_lobby_architecture import tournament_fixture

    from sitg_bot.services.tournaments import TournamentService
    from sitg_bot.storage.models import (
        TournamentPacketAssignmentRecord,
        TournamentPacketEntitlementRecord,
        TournamentPolicyVersionRecord,
    )

    url, config = baseline_database
    command.upgrade(config, "head")

    async def seed():
        database = Database(url)
        try:
            fixture = await tournament_fixture(database, player_count=3)
            async with database.transaction() as session:
                assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                ))
                for player, playable in zip(fixture.players, (False, True, None), strict=True):
                    session.add(TournamentPacketEntitlementRecord(
                        assignment_id=assignment.id, player_id=player.id, playable=playable,
                    ))
            return fixture, assignment.id
        finally:
            await database.close()

    fixture, assignment_id = asyncio.run(seed())
    command.downgrade(config, "0009_tournament_player_limits")

    async def legacy_values():
        database = Database(url)
        try:
            async with database.transaction() as session:
                await session.execute(text("""
                    UPDATE tournament_packet_assignments SET access_level_by_members = :rule
                """), {"rule": old_rule})
                await session.execute(text("""
                    UPDATE tournament_policy_versions SET policies = jsonb_build_object(
                        'packet_access_rule_default', CAST(:rule AS text))
                """), {"rule": old_rule})
                await session.execute(text("""
                    UPDATE tournament_packet_entitlements SET playable = false,
                        access_level = 'read-or-play' WHERE player_id = :id
                """), {"id": fixture.players[1].id})
        finally:
            await database.close()

    asyncio.run(legacy_values())
    command.upgrade(config, "head")

    async def check():
        database = Database(url)
        try:
            async with database.sessions() as session:
                assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
                assert assignment.library_viewing_rule == new_rule
                assert assignment.playable_by_members
                policy = await session.scalar(select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id,
                ))
                assert policy.policies == {"library_viewing_rule_default": new_rule}
                for player, playable in zip(fixture.players, (False, True, True), strict=True):
                    assert await TournamentService.has_assignment_access(
                        session, assignment, player.id, "playable"
                    ) is playable
            async with database.engine.connect() as connection:
                await connection.run_sync(assert_schema_without_defaults)
        finally:
            await database.close()

    def assert_schema_without_defaults(connection):
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []

    asyncio.run(check())


def test_token_approval_and_one_time_delivery_after_baseline_upgrade(baseline_database):
    url, config = baseline_database
    command.upgrade(config, "0001_initial_schema")
    command.upgrade(config, "head")

    async def exercise():
        database = Database(url)
        try:
            async with database.transaction() as session:
                admin, requester = [
                    PlayerRecord(
                        real_name=name, public_nickname=name, status="active",
                        registration_step="complete", registration_completed_at=datetime.now(UTC),
                    )
                    for name in ("Admin", "Requester")
                ]
                session.add_all([admin, requester])
                await session.flush()
                session.add(PlatformAdministratorRecord(player_id=admin.id))
            service = TournamentTokenRequestService(
                database, delivery_encryption_key="test-only-token-delivery-key-32-characters"
            )
            request = await service.create_request(requester.id, tournament_name="Test tournament")
            approved = await service.decide_request(request.request_id, admin.id, approve=True)
            assert approved.status == "approved"
            async with database.sessions() as session:
                delivery = await session.get(
                    TournamentCreationTokenDeliveryRecord, approved.token_id
                )
                assert delivery.status == "pending"
                encrypted = delivery.encrypted_token
                assert encrypted
            delivered = await service.claim_token_once(request.request_id, requester.id)
            async with database.sessions() as session:
                token = await session.get(TournamentCreationTokenRecord, approved.token_id)
                assert token.token_digest == hashlib.sha256(delivered.token.encode()).hexdigest()
                assert delivered.token.encode() not in encrypted
                delivery = await session.get(
                    TournamentCreationTokenDeliveryRecord, approved.token_id
                )
                assert delivery.status == "delivered"
                assert delivery.encrypted_token is None
            with pytest.raises(TokenPlaintextUnavailable):
                await service.claim_token_once(request.request_id, requester.id)
        finally:
            await database.close()

    asyncio.run(exercise())


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
