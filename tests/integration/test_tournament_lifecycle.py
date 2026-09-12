from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PlayerNotificationRecord,
    TournamentManagerRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


@pytest.mark.parametrize("offset", [None, -1, 1])
async def test_ladder_requires_manual_start_regardless_of_schedule(database_url, offset):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        tournaments = TournamentService(database)
        now = datetime.now(UTC)
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.actual_starts_at = None
            tournament.starts_at = now + timedelta(days=offset) if offset is not None else None
            tournament.registration_open = True
        management = await tournaments.manager_management(fixture.tournament_id, fixture.manager.id)
        assert management.tournament.phase == "future"
        assert "start_tournament" in management.available_actions
        lobbies = InvitationMatchmakingService(database)
        with pytest.raises(ValueError, match="tournament_stage_closed"):
            await lobbies.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        with pytest.raises(PermissionError):
            await tournaments.start_tournament(
                fixture.tournament_id, fixture.players[0].id,
                expected_version=management.settings_version,
            )
        with pytest.raises(StaleWriteError):
            await tournaments.start_tournament(
                fixture.tournament_id, fixture.manager.id,
                expected_version=management.settings_version + 1,
            )
        started = await tournaments.start_tournament(
            fixture.tournament_id, fixture.manager.id,
            expected_version=management.settings_version,
        )
        assert started.tournament.actual_starts_at is not None
        assert started.tournament.phase == "ongoing"
        assert started.registration_open
        assert "start_tournament" not in started.available_actions
        with pytest.raises(ValueError, match="already started"):
            await tournaments.start_tournament(
                fixture.tournament_id, fixture.manager.id,
                expected_version=started.settings_version,
            )
        lobby = await lobbies.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        assert not any(v["code"] == "tournament_stage_closed" for v in lobby.validation_violations)
        await tournaments.complete_tournament(
            fixture.tournament_id, fixture.manager.id, expected_version=started.settings_version,
        )
    finally:
        await database.close()


async def test_start_reminders_are_durable_deduplicated_and_skip_started_tournaments(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        now = datetime.now(UTC)
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.actual_starts_at = None
            tournament.starts_at = now + timedelta(hours=1)
            session.add(TournamentManagerRecord(
                tournament_id=fixture.tournament_id, player_id=other.manager.id,
                granted_by_id=fixture.manager.id,
            ))
        service = TournamentService(database)

        async def notices():
            async with database.sessions() as session:
                return list(await session.scalars(select(PlayerNotificationRecord).where(
                    PlayerNotificationRecord.kind == "tournament.start_due",
                    PlayerNotificationRecord.payload["tournament_id"].astext
                    == str(fixture.tournament_id),
                )))

        await service.remind_scheduled_starts(now=now)
        assert await notices() == []
        await service.remind_scheduled_starts(now=now + timedelta(hours=1))
        await TournamentService(database).remind_scheduled_starts(now=now + timedelta(hours=2))
        assert {n.recipient_player_id for n in await notices()} == {
            fixture.manager.id, other.manager.id,
        }
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.starts_at = now + timedelta(hours=3)
        await service.remind_scheduled_starts(now=now + timedelta(hours=3))
        assert len(await notices()) == 4
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.starts_at = now + timedelta(hours=1)
        await service.remind_scheduled_starts(now=now + timedelta(hours=3))
        assert len(await notices()) == 4
        management = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        await service.start_tournament(
            fixture.tournament_id, fixture.manager.id, expected_version=management.settings_version,
        )
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.starts_at = now + timedelta(hours=4)
        await service.remind_scheduled_starts(now=now + timedelta(hours=4))
        assert len(await notices()) == 4
    finally:
        await database.close()
