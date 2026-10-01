"""PostgreSQL coverage for Swiss configuration, progression and qualification."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import combinations
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_classic import first_round, mutate, setup
from test_lobby_architecture import database_url as _database_url

from sitg_bot.services.classic import ClassicService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService, LobbyReadinessError
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.tournament_profiles import TournamentProfileService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ClassicStageRecord,
    GameParticipantRecord,
    GameRecord,
    PlayerRecord,
    RulesetRatingRecord,
    ScoreLedgerRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def swiss_setup(database, count=8, rounds=3, size=4, themes=1):
    fixture = await setup(database, count, stage_type="none", scheme=None, themes=themes)
    await mutate(database, fixture, "configure", stage_type="swiss",
                 round_count=rounds, players_per_game=size)
    return fixture


async def snapshot(database, fixture):
    async with database.sessions() as session:
        return await ClassicService(database).snapshot(session, fixture.tournament_id)


@pytest.mark.parametrize("mode", ["manual", "random"])
async def test_swiss_custom_seeding_is_used_by_opening_matches(database_url, mode):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database, count=7)
        ids = [str(p.id) for p in reversed(fixture.players)]
        await mutate(database, fixture, "seed", mode=mode, seeds=[ids])
        saved = (await snapshot(database, fixture))["stages"][0]["seeds"][0]
        assert set(saved) == set(ids)
        if mode == "manual":
            assert saved == ids
        await mutate(database, fixture, "start")
        async with database.sessions() as session:
            stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
            matches = await ClassicService.matches(session, stage.id)
            assert matches[0].seats == saved[:4]
            assert matches[1].seats[:3] == saved[4:]
            assert matches[1].seats[3].startswith("chair:")
    finally:
        await database.close()


async def test_swiss_configuration_persists_and_reconfiguration_is_atomic(database_url):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database)
        before = (await snapshot(database, fixture))["stages"][0]
        assert before["round_count"] == 3 and before["players_per_game"] == 4
        assert [r["number"] for r in before["rounds"]] == [1, 2, 3]
        assert all(not r["matches"] for r in before["rounds"])
        first, assignment_id = await first_round(database, fixture)
        await mutate(database, fixture, "round", round_id=first["id"],
                     assignment_id=str(assignment_id))
        await mutate(database, fixture, "seed", mode="automatic")
        await mutate(database, fixture, "configure", stage_type="swiss", round_count=3,
                     players_per_game=4, place_points=["8", "5", "2", "0"], score_multiplier="0.1")
        saved = (await snapshot(database, fixture))["stages"][0]
        assert saved["rounds"][0]["id"] == first["id"]
        assert saved["rounds"][0]["assignment_id"] == str(assignment_id)
        assert saved["seeds"] and saved["place_points"] == ["8", "5", "2", "0"]
        assert Decimal(saved["score_multiplier"]) == Decimal("0.1")
        for values in ({"round_count": 0}, {"players_per_game": 13}):
            with pytest.raises(ValueError, match="Swiss"):
                await mutate(database, fixture, "configure", **{
                    "stage_type": "swiss", "round_count": 3, "players_per_game": 4, **values,
                })
            assert (await snapshot(database, fixture))["stages"][0] == saved
        with pytest.raises(ValueError, match="slots"):
            await mutate(database, fixture, "seed", mode="manual", seeds=[])
        async with database.sessions() as session:
            version = (await session.get(TournamentRecord, fixture.tournament_id)).settings_version
        for actor, expected_version, error in (
            (fixture.players[0].id, version, PermissionError),
            (fixture.manager.id, version - 1, StaleWriteError),
        ):
            with pytest.raises(error):
                await ClassicService(database).mutate(
                    fixture.tournament_id, actor, expected_version=expected_version,
                    command="configure", kind="first",
                    values={"stage_type": "swiss", "round_count": 2, "players_per_game": 3},
                )
        assert (await snapshot(database, fixture))["stages"][0] == saved
        await mutate(database, fixture, "configure", stage_type="swiss", round_count=2,
                     players_per_game=3)
        changed = (await snapshot(database, fixture))["stages"][0]
        assert changed["seeds"] == [] and len(changed["rounds"]) == 2
        assert {r["id"] for r in changed["rounds"]}.isdisjoint(r["id"] for r in saved["rounds"])
        assert all(r["assignment_id"] is None for r in changed["rounds"])
    finally:
        await database.close()


async def test_swiss_seeding_preserves_saved_order_and_persists_chairs(database_url):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database, count=7)
        players = fixture.players
        ratings = [Decimal(value) for value in (1100, 1400, 900, 1300, 1200, 800, 1000)]
        async with database.transaction() as session:
            # Player seven is unrated in SI; their unrelated rating must be ignored.
            session.add_all(RulesetRatingRecord(ruleset_key="si", player_id=p.id, rating=rating)
                            for p, rating in zip(players[:-1], ratings[:-1], strict=True))
            session.add(RulesetRatingRecord(ruleset_key="other", player_id=players[-1].id,
                                           rating=9999))
        await mutate(database, fixture, "seed", mode="automatic")
        preview = (await snapshot(database, fixture))["stages"][0]
        ordered = sorted(range(7), key=lambda i: (-ratings[i], str(players[i].id)))
        assert preview["seeds"] == [[str(players[i].id) for i in ordered]]
        async with database.transaction() as session:
            rating = await session.get(RulesetRatingRecord, ("si", players[0].id))
            rating.rating = ratings[0] = Decimal(1600)
        await mutate(database, fixture, "start")
        started = (await snapshot(database, fixture))["stages"][0]
        assert started["seeds"][0][:-1] == [str(players[i].id) for i in ordered]
        chair = started["seeds"][0][-1]
        assert chair.startswith("chair:")
        assert len(started["rounds"][0]["matches"]) == 2
        assert all(not r["matches"] for r in started["rounds"][1:])
        async with database.sessions() as session:
            row = await session.get(PlayerRecord, UUID(chair[6:]))
            assert row.status == "anonymized" and row.public_nickname == "Chair"
            stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
            matches = await ClassicService.matches(session, stage.id)
            assert [m.seats for m in matches] == [started["seeds"][0][i:i + 4] for i in (0, 4)]
        for command, values in (
            ("configure", {"stage_type": "swiss", "round_count": 4, "players_per_game": 4}),
            ("seed", {"mode": "automatic"}),
        ):
            with pytest.raises(ValueError):
                await mutate(database, fixture, command, **values)
        assert (await snapshot(database, fixture))["stages"][0] == started
    finally:
        await database.close()


async def test_swiss_real_games_wait_for_finalization_before_pairing(database_url):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database, rounds=2, themes=8)
        await mutate(database, fixture, "start")
        first, assignment_id = await first_round(database, fixture)
        await mutate(database, fixture, "round", round_id=first["id"],
                     assignment_id=str(assignment_id), discoverable=True, playable=True)
        async with database.sessions() as session:
            stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
            matches = await ClassicService.matches(session, stage.id)
        inputs = {str(p.id): item for p, item in zip(fixture.players, fixture.inputs, strict=True)}
        lobbies = InvitationMatchmakingService(database)
        games = PersistentGameService(database)
        game_ids = []
        for match in matches:
            roster = [inputs[seat] for seat in match.seats]
            host = roster[0]
            lobby = await lobbies.create_lobby(host, tournament_id=fixture.tournament_id)
            await lobbies.select_packet(lobby.id, host.telegram_user_id, fixture.packet_id)
            if not game_ids:
                outsider = next(item for seat, item in inputs.items() if seat not in match.seats)
                await lobbies.join(lobby.invitation_code, outsider)
                with pytest.raises(LobbyReadinessError):
                    await lobbies.set_ready(lobby.id, host.telegram_user_id)
                await lobbies.leave(lobby.id, outsider.telegram_user_id)
            for player in roster[1:]:
                await lobbies.join(lobby.invitation_code, player)
            for player in roster:
                await lobbies.set_ready(lobby.id, player.telegram_user_id)
            assigned = await lobbies.start(lobby.id, host.telegram_user_id)
            assert assigned.started
            game_ids.append(assigned.game.id)
            for player in roster:
                await games.join(assigned.game.id, player.telegram_user_id)
            async with database.sessions() as session:
                game = await session.get(GameRecord, assigned.game.id)
                assert game.assignment_plan["parameters"]["theme_count"] == 8
                assert len(game.assignment_plan["play_units"]) == 8
        for index, game_id in enumerate(game_ids):
            if index == 1:
                # Model the completed-but-unfinalized state while settlement/appeals wait.
                async with database.transaction() as session:
                    game = await session.get(GameRecord, game_id)
                    game.status = "completed"
                await ClassicService(database).reconcile()
                pending = (await snapshot(database, fixture))["stages"][0]
                assert pending["rounds"][0]["matches"][0]["results"]
                assert pending["rounds"][0]["matches"][1]["results"] is None
                assert pending["rounds"][1]["matches"] == []
                assert pending["completed_at"] is None
            async with database.transaction() as session:
                game = await session.get(GameRecord, game_id)
                participants = list(await session.scalars(
                    select(GameParticipantRecord).where(GameParticipantRecord.game_id == game_id)
                    .order_by(GameParticipantRecord.seat)
                ))
                for participant, score in zip(participants, (100, 100, 0, -50), strict=True):
                    session.add(ScoreLedgerRecord(game_id=game_id, participant_id=participant.id,
                                                 delta=score, reason="admin_correction"))
                await session.flush()
                await games._finalize(session, game)
                assert game.status == "finalized"
            await ClassicService(database).reconcile()
        await asyncio.gather(*(ClassicService(database).reconcile() for _ in range(2)))
        saved = (await snapshot(database, fixture))["stages"][0]
        assert len(saved["rounds"][1]["matches"]) == 2 and saved["completed_at"] is None
        async with database.sessions() as session:
            persisted = await ClassicService.matches(session, stage.id)
            for match in persisted[:2]:
                assert [Decimal(r["place"]) for r in match.results] == [
                    Decimal("1.5"), Decimal("1.5"), Decimal(3), Decimal(4),
                ]
                assert [Decimal(r["points"]) for r in match.results] == [
                    Decimal("5.5"), Decimal("5.5"), Decimal(2), Decimal(0),
                ]
            pairs = {frozenset(pair) for m in persisted[:2] for pair in combinations(m.seats, 2)}
            assert sorted(seat for m in persisted[2:] for seat in m.seats) == sorted(inputs)
            # Eight players in four-player games necessarily meet some opponents again.
            assert len({frozenset(pair) for m in persisted[2:]
                        for pair in combinations(m.seats, 2)} - pairs) > 0
        await database.close()
        database = Database(database_url)
        await ClassicService(database).reconcile()
        assert (await snapshot(database, fixture))["stages"][0] == saved
    finally:
        await database.close()


async def test_swiss_deadlines_advance_once_under_concurrent_reconciliation(database_url):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database, count=7)
        await mutate(database, fixture, "start")
        previous_results = {}
        for index in range(3):
            current = (await snapshot(database, fixture))["stages"][0]
            await mutate(database, fixture, "round", round_id=current["rounds"][index]["id"],
                         start_deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
            await asyncio.gather(*(ClassicService(database).reconcile() for _ in range(2)))
            current = (await snapshot(database, fixture))["stages"][0]
            assert bool(current["completed_at"]) == (index == 2)
            for round_number, round_record in enumerate(current["rounds"]):
                assert len(round_record["matches"]) == (2 if round_number <= index + 1 else 0)
                for match in round_record["matches"]:
                    if match["id"] in previous_results:
                        assert match["results"] == previous_results[match["id"]]
                    if round_number <= index:
                        assert match["randomized"] and len(match["results"]) == 4
                        assert all(Decimal(r["score"]) == 0 for r in match["results"])
                        assert sum(Decimal(r["points"]) for r in match["results"]) == 10
                        previous_results[match["id"]] = match["results"]
            assert len(current["standings"]) == 7
            assert all(not r["seat"].startswith("chair:") for r in current["standings"])
        await ClassicService(database).reconcile()
        assert (await snapshot(database, fixture))["stages"][0] == current
    finally:
        await database.close()


async def test_swiss_opponent_places_drive_persisted_standings_and_playoff_seeding(database_url):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database, count=4, rounds=1, size=2)
        await mutate(database, fixture, "configure", "playoff", stage_type="playoff",
                     scheme_key="playoff-8")
        await mutate(database, fixture, "start")
        with pytest.raises(ValueError, match="Finish the first stage"):
            await mutate(database, fixture, "start", "playoff")
        async with database.transaction() as session:
            first = next(s for s in await ClassicService.stages(session, fixture.tournament_id)
                         if s.kind == "first")
            matches = await ClassicService.matches(session, first.id)
            a, c = matches[0].seats
            b, d = matches[1].seats
            # Persist authoritative game-result snapshots with deliberately tied stage points.
            for match, scores in zip(matches, ((100, 300), (50, -150)), strict=True):
                ordered = sorted(zip(match.seats, scores, strict=True), key=lambda r: -r[1])
                match.results = [
                    {"seat": seat, "points": str(Decimal(5 - place) + Decimal(score) / 50),
                     "score": str(score), "place": str(place), "correct_values": [],
                     "key": [str(score)]}
                    for place, (seat, score) in enumerate(ordered, 1)
                ]
        await ClassicService(database).reconcile()
        first_view = next(s for s in (await snapshot(database, fixture))["stages"]
                          if s["kind"] == "first")
        assert first_view["completed_at"] is not None
        assert [r["seat"] for r in first_view["standings"]] == [c, a, b, d]
        assert {r["seat"]: Decimal(r["opponent_place_sum"]) for r in first_view["standings"]} == {
            a: Decimal(1), b: Decimal(4), c: Decimal("2.5"), d: Decimal("2.5"),
        }
        profile = await TournamentProfileService(database).profile(
            fixture.players[0].id, fixture.tournament_id,
        )
        leaders = profile["leaders"]["stages"][0]["standings"]
        assert [r["player_id"] for r in leaders] == [c, a, b, d]
        assert [r["opponent_place_sum"] for r in leaders] == [
            r["opponent_place_sum"] for r in first_view["standings"]
        ]
        await mutate(database, fixture, "start", "playoff")
        playoff = next(s for s in (await snapshot(database, fixture))["stages"]
                       if s["kind"] == "playoff")
        assert playoff["seeds"][0][:4] == [c, a, b, d]
        assert all(p.startswith("chair:") for p in playoff["seeds"][0][4:])
    finally:
        await database.close()


@pytest.mark.parametrize("invalid", [
    {"kind": "playoff"}, {"round_count": None}, {"round_count": 0},
    {"players_per_game": None}, {"players_per_game": 1}, {"players_per_game": 13},
])
async def test_swiss_database_constraint_rejects_invalid_configuration(database_url, invalid):
    database = Database(database_url)
    try:
        fixture = await swiss_setup(database)
        before = (await snapshot(database, fixture))["stages"][0]
        with pytest.raises(IntegrityError) as error:
            async with database.transaction() as session:
                stage = (await ClassicService.stages(session, fixture.tournament_id))[0]
                for name, value in invalid.items():
                    setattr(stage, name, value)
                await session.flush()
        assert error.value.orig.sqlstate == "23514"
        assert "swiss_configuration" in str(error.value.orig)
        assert (await snapshot(database, fixture))["stages"][0] == before
        async with database.sessions() as session:
            assert await session.scalar(select(ClassicStageRecord.id).where(
                ClassicStageRecord.tournament_id == fixture.tournament_id,
            )) is not None
    finally:
        await database.close()
