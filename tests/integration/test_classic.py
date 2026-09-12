import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.services.classic import ClassicService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService, LobbyReadinessError
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.telegram_game import TelegramGameService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ClassicMatchRecord,
    GameParticipantRecord,
    GameRecord,
    OutboxEventRecord,
    RulesetRatingLedgerRecord,
    ScoreLedgerRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def mutate(database, fixture, command, kind="first", **values):
    async with database.sessions() as session:
        tournament = await session.get(TournamentRecord, fixture.tournament_id)
        version = tournament.settings_version
    await ClassicService(database).mutate(
        fixture.tournament_id,
        fixture.manager.id,
        expected_version=version,
        command=command,
        kind=kind,
        values=values,
    )


async def setup(database, count, stage_type="groups", scheme="groups-9-4", kind="first"):
    fixture = await tournament_fixture(
        database, player_count=count, type_key="classic", hybrid_matchmaking_enabled=False
    )
    await mutate(database, fixture, "configure", kind, stage_type=stage_type, scheme_key=scheme)
    return fixture


async def first_round(database, fixture, kind="first"):
    view = await TournamentService(database).manager_management(
        fixture.tournament_id, fixture.manager.id
    )
    stage = next(s for s in view.classic["stages"] if s["kind"] == kind)
    return stage["rounds"][0], view.packets[0].assignment_id


async def test_classic_prescribed_game_chairs_scoring_and_no_replay(database_url):
    database = Database(database_url)
    try:
        fixture = await setup(database, 2)
        ids = [str(p.id) for p in fixture.players]
        await mutate(database, fixture, "seed", mode="manual", seeds=[ids + [None] * 7])
        await mutate(database, fixture, "start")
        round_record, assignment_id = await first_round(database, fixture)
        await mutate(
            database,
            fixture,
            "round",
            round_id=round_record["id"],
            assignment_id=str(assignment_id),
            discoverable=True,
            playable=True,
        )
        service = InvitationMatchmakingService(database)
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        lobby = await service.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        assert any(
            v["code"] == "classic_participants_required" for v in lobby.validation_violations
        )
        with pytest.raises(LobbyReadinessError):
            await service.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        await service.join(lobby.invitation_code, fixture.inputs[1])
        for player in fixture.inputs:
            await service.set_ready(lobby.id, player.telegram_user_id)
        result = await service.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert result.started
        games = PersistentGameService(database)
        for player in fixture.inputs:
            await games.join(result.game.id, player.telegram_user_id)
        async with database.transaction() as session:
            game = await session.get(GameRecord, result.game.id)
            assert game.status == "active"
            participants = list(
                await session.scalars(
                    select(GameParticipantRecord)
                    .where(GameParticipantRecord.game_id == game.id)
                    .order_by(GameParticipantRecord.seat)
                )
            )
            assert len(participants) == 3
            chair = participants[2]
            assert chair.is_chair and chair.joined and chair.ready and not chair.active
            for index, participant in enumerate(participants[:2], 1):
                session.add(
                    ScoreLedgerRecord(
                        game_id=game.id,
                        participant_id=participant.id,
                        delta=-10 * index,
                        reason="admin_correction",
                    )
                )
            await session.flush()
            await games._finalize(session, game)
            assert chair.score == 0 and chair.final_place == 1
        await ClassicService(database).reconcile()
        view = await TelegramGameService(database).view(
            fixture.inputs[0].telegram_user_id, result.game.id
        )
        assert view["participants"][2]["is_chair"]
        assert view["participants"][2]["score"] == 0
        async with database.sessions() as session:
            match = await session.scalar(
                select(ClassicMatchRecord).where(ClassicMatchRecord.game_id == result.game.id)
            )
            assert match.results[0]["seat"].startswith("chair:")
            assert Decimal(match.results[1]["points"]) == Decimal("2.8")
            assert not await session.scalar(
                select(RulesetRatingLedgerRecord.id).where(
                    RulesetRatingLedgerRecord.player_id == chair.player_id
                )
            )
            assert not await ClassicService.assignment_access(
                session, assignment_id, fixture.players[0].id, "playable"
            )
            notices = list(
                await session.scalars(
                    select(OutboxEventRecord).where(
                        OutboxEventRecord.topic == "telegram.lobby.notice",
                        OutboxEventRecord.aggregate_id == lobby.id,
                    )
                )
            )
            assert any(n.payload.get("classic_players", [])[-1:] == ["Chair"] for n in notices)
        with pytest.raises(ValueError, match="locked"):
            await mutate(database, fixture, "configure", stage_type="quiz")
    finally:
        await database.close()


async def test_quiz_solo_deadline_and_playoff_seeding(database_url):
    database = Database(database_url)
    try:
        fixture = await setup(database, 3, stage_type="quiz", scheme=None)
        await mutate(
            database, fixture, "configure", "playoff", stage_type="playoff", scheme_key="playoff-8"
        )
        with pytest.raises(ValueError, match="Finish the first stage"):
            await mutate(database, fixture, "start", "playoff")
        await mutate(database, fixture, "start")
        round_record, assignment_id = await first_round(database, fixture)
        await mutate(
            database,
            fixture,
            "round",
            round_id=round_record["id"],
            assignment_id=str(assignment_id),
            discoverable=True,
            playable=True,
        )
        service = InvitationMatchmakingService(database)
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        await service.select_packet(lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id)
        await service.join(lobby.invitation_code, fixture.inputs[1])
        with pytest.raises(LobbyReadinessError):
            await service.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        await service.leave(lobby.id, fixture.inputs[1].telegram_user_id)
        await service.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        game_result = await service.start(lobby.id, fixture.inputs[0].telegram_user_id)
        await mutate(
            database,
            fixture,
            "round",
            round_id=round_record["id"],
            assignment_id=str(assignment_id),
            discoverable=True,
            playable=True,
            start_deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        await ClassicService(database).reconcile()
        async with database.sessions() as session:
            matches = list(
                await session.scalars(
                    select(ClassicMatchRecord).where(
                        ClassicMatchRecord.round_id == round_record["id"]
                    )
                )
            )
            assert sum(m.randomized for m in matches) == 2
            assert all(r["score"] == "0" for m in matches if m.results for r in m.results)
            assert (await session.get(GameRecord, game_result.game.id)).status == "lobby"
        games = PersistentGameService(database)
        await games.join(game_result.game.id, fixture.inputs[0].telegram_user_id)
        async with database.transaction() as session:
            game = await session.get(GameRecord, game_result.game.id)
            participant = await session.scalar(
                select(GameParticipantRecord).where(GameParticipantRecord.game_id == game.id)
            )
            session.add(
                ScoreLedgerRecord(
                    game_id=game.id,
                    participant_id=participant.id,
                    delta=50,
                    reason="admin_correction",
                )
            )
            await session.flush()
            await games._finalize(session, game)
        await ClassicService(database).reconcile()
        await mutate(database, fixture, "start", "playoff")
        view = await TournamentService(database).manager_management(
            fixture.tournament_id, fixture.manager.id
        )
        playoff = next(s for s in view.classic["stages"] if s["kind"] == "playoff")
        assert playoff["seeds"][0][0] == str(fixture.players[0].id)
        assert sum(s.startswith("chair:") for s in playoff["seeds"][0]) == 5
    finally:
        await database.close()


@pytest.mark.parametrize("scheme", ["playoff-8", "de-8", "de-16", "de-32"])
async def test_brackets_advance_once_after_deadlines(database_url, scheme):
    database = Database(database_url)
    try:
        fixture = await setup(database, 8, stage_type="playoff", scheme=scheme, kind="playoff")
        await mutate(database, fixture, "start", "playoff")
        async with database.transaction() as session:
            stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
            for round_record in await ClassicService.rounds(session, stage.id):
                round_record.start_deadline = datetime.now(UTC) - timedelta(seconds=1)
        await ClassicService(database).reconcile()
        async with database.sessions() as session:
            matches = await ClassicService.matches(session, stage.id)
            results = [m.results for m in matches]
            assert all(results)
            assert (await session.get(type(stage), stage.id)).completed_at is not None
            rounds = {r.id: r.number for r in await ClassicService.rounds(session, stage.id)}
            lookup = {(rounds[m.round_id], m.number): m for m in matches}
            for match in matches:
                if rounds[match.round_id] == 1:
                    continue
                assert match.seats == [
                    lookup[r, g].results[p - 1]["seat"] for r, g, p in match.sources
                ]
        await ClassicService(database).reconcile()
        async with database.sessions() as session:
            assert [m.results for m in await ClassicService.matches(session, stage.id)] == results
            snapshot = await ClassicService(database).snapshot(session, fixture.tournament_id)
            assert snapshot["stages"][0]["standings"] == []
            assert snapshot["stages"][0]["rounds"][-1]["matches"][0]["results"]
    finally:
        await database.close()


async def test_classic_mutations_require_manager_and_current_version(database_url):
    database = Database(database_url)
    try:
        fixture = await setup(database, 2)
        with pytest.raises(PermissionError):
            await ClassicService(database).mutate(
                fixture.tournament_id,
                fixture.players[0].id,
                expected_version=1,
                command="start",
                kind="first",
                values={},
            )
        with pytest.raises(StaleWriteError):
            await ClassicService(database).mutate(
                fixture.tournament_id,
                fixture.manager.id,
                expected_version=1,
                command="start",
                kind="first",
                values={},
            )
        with pytest.raises(ValueError, match="exactly once"):
            await mutate(
                database, fixture, "seed", mode="manual", seeds=[[str(fixture.players[0].id)] * 9]
            )
        async with database.sessions() as session:
            version = (await session.get(TournamentRecord, fixture.tournament_id)).settings_version
        results = await asyncio.gather(
            *(
                ClassicService(database).mutate(
                    fixture.tournament_id,
                    fixture.manager.id,
                    expected_version=version,
                    command="start",
                    kind="first",
                    values={},
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(r is None for r in results) == 1
        assert sum(isinstance(r, StaleWriteError) for r in results) == 1
        async with database.sessions() as session:
            stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
            assert len(await ClassicService.matches(session, stage.id)) == 12
    finally:
        await database.close()
