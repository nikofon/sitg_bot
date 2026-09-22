import pytest
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from test_classic import mutate, setup
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.services.classic import ClassicService
from sitg_bot.services.tournament_profiles import TournamentProfileService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import TournamentMembershipRecord

pytestmark = pytest.mark.integration
database_url = _database_url


async def test_ladder_tournament_profile_sections_and_visibility(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(
            database, player_count=2, type_key="ladder", started=False
        )
        service = TournamentProfileService(database)

        with pytest.raises(LookupError):
            await service.profile(None, fixture.tournament_id)
        with pytest.raises(LookupError):
            await service.profile(uuid4(), fixture.tournament_id)

        profile = await service.profile(fixture.manager.id, fixture.tournament_id)
        assert profile["type_key"] == "ladder"
        assert profile["tournament"]["id"] == fixture.tournament_id
        assert profile["general"]["name"] == "Architecture tournament"
        assert profile["general"]["managers"] == [
            {"player_id": str(fixture.manager.id), "name": "Architecture Manager"}
        ]
        assert profile["participants"] is None
        assert profile["games"] == {"kind": "ladder", "items": []}

        async with database.transaction() as session:
            first = await session.get(
                TournamentMembershipRecord, (fixture.tournament_id, fixture.players[0].id)
            )
            second = await session.get(
                TournamentMembershipRecord, (fixture.tournament_id, fixture.players[1].id)
            )
            first.rating = 1500
            second.rating = 900
            second.status = "registered"

        profile = await service.profile(fixture.players[0].id, fixture.tournament_id)
        assert [item["status"] for item in profile["registrations"]] == [
            "active",
            "registered",
        ]
        leaders = profile["leaders"]
        assert leaders["kind"] == "ladder"
        assert [item["player_id"] for item in leaders["items"]] == [
            str(fixture.players[0].id)
        ]
        assert leaders["items"][0]["rating"] == 1500.0
    finally:
        await database.close()


async def test_classic_tournament_profile_browses_playoff_places(database_url):
    database = Database(database_url)
    try:
        fixture = await setup(
            database, 8, stage_type="playoff", scheme="playoff-8", kind="playoff"
        )
        await mutate(database, fixture, "start", "playoff")
        async with database.transaction() as session:
            stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
            for round_record in await ClassicService.rounds(session, stage.id):
                round_record.start_deadline = datetime.now(UTC) - timedelta(seconds=1)
        await ClassicService(database).reconcile()

        profile = await TournamentProfileService(database).profile(
            fixture.manager.id, fixture.tournament_id
        )
        assert profile["type_key"] == "classic"
        assert {item["player_id"] for item in profile["participants"]} == {
            str(player.id) for player in fixture.players
        }

        games_stage = next(
            stage for stage in profile["games"]["stages"] if stage["kind"] == "playoff"
        )
        assert [round_record["number"] for round_record in games_stage["rounds"]] == [1, 2]
        assert all(
            match["manual_results"]
            for round_record in games_stage["rounds"]
            for match in round_record["matches"]
        )

        places = next(
            stage for stage in profile["leaders"]["stages"] if stage["kind"] == "playoff"
        )["places"]
        assert [row["place"] for row in places] == [
            "1", "2", "3", "4", "5.5", "5.5", "6.5", "6.5",
        ]
        assert {row["player_id"] for row in places} == {
            str(player.id) for player in fixture.players
        }
    finally:
        await database.close()
