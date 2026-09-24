from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import combinations
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sitg_bot.application.contracts import ClassicConfigurationValues
from sitg_bot.domain.classic import standings, swiss_pairings
from sitg_bot.services.classic import ClassicService
from sitg_bot.services.tournament_profiles import TournamentProfileService
from sitg_bot.storage.models import ClassicRoundRecord, ClassicStageRecord


def result(seat, points, score="0", correct_values=()):
    return {"seat": seat, "points": str(points), "score": score, "correct_values": correct_values}


def match(*results):
    return SimpleNamespace(results=list(results))


def stage(**values):
    return ClassicStageRecord(**{
        "id": uuid4(), "tournament_id": uuid4(), "kind": "first", "stage_type": "swiss",
        "round_count": 3, "players_per_game": 4, "random_seed": "swiss-test", "seeds": [],
        "place_points": ["4", "3", "2", "1"], "score_multiplier": Decimal("0.02"),
        **values,
    })


def test_opening_pairings_draw_from_rating_bands():
    seeds = [str(i) for i in range(16)]
    assert swiss_pairings(seeds, 4, []) == [
        ["0", "4", "8", "12"], ["1", "5", "9", "13"],
        ["2", "6", "10", "14"], ["3", "7", "11", "15"],
    ]


def test_later_pairings_prefer_similar_points_and_avoid_repeat_opponents():
    seeds = [str(i) for i in range(16)]
    opening = swiss_pairings(seeds, 4, [])
    matches = [match(*(result(p, 4 - index) for index, p in enumerate(g))) for g in opening]
    paired = swiss_pairings(seeds, 4, matches)
    assert paired == [seeds[i:i + 4] for i in range(0, 16, 4)]
    old_pairs = {frozenset(pair) for group in opening for pair in combinations(group, 2)}
    assert not any(frozenset(pair) in old_pairs for g in paired for pair in combinations(g, 2))
    assert paired == swiss_pairings(seeds, 4, matches)


def test_pairings_allow_unavoidable_repeats_and_preserve_chairs():
    seeds = ["a", "b", "c", "chair:1"]
    matches = [match(*(result(p, -i) for i, p in enumerate(seeds)))]
    assert swiss_pairings(seeds, 4, matches) == [seeds]


def test_pairings_require_complete_previous_round():
    with pytest.raises(ValueError, match="Finish every game"):
        swiss_pairings(["a", "b"], 2, [SimpleNamespace(results=None)])


def test_swiss_tiebreak_uses_final_point_positions_counts_repeats_and_skips_chairs():
    matches = [
        match(result("a", 5), result("c", 10), result("chair:1", 1000)),
        match(result("b", 5, correct_values=[50]), result("d", 0)),
    ] * 2
    ranking = standings(matches, quiz=False, seed="test", swiss=True)
    assert [r["seat"] for r in ranking] == ["c", "a", "b", "d"]
    assert {r["seat"]: Decimal(r["opponent_place_sum"]) for r in ranking} == {
        "a": Decimal(2), "b": Decimal(8), "c": Decimal(5), "d": Decimal(5),
    }
    assert ranking[1]["key"] == ["10", "-2"]
    assert ranking == standings(matches, quiz=False, seed="test", swiss=True)


@pytest.mark.parametrize("values", [
    {"round_count": 0}, {"round_count": 1.5}, {"round_count": True},
    {"players_per_game": 1}, {"players_per_game": 13}, {"players_per_game": False},
])
async def test_invalid_swiss_configuration_is_rejected_by_contract_and_service(values):
    values = {"stage_type": "swiss", "round_count": 6, "players_per_game": 4, **values}
    with pytest.raises(ValidationError):
        ClassicConfigurationValues(**values)
    with pytest.raises(ValueError, match="Swiss"):
        await ClassicService(None)._configure(Mock(), stage(), values)


async def test_swiss_configuration_requires_parameters_and_first_stage():
    service = ClassicService(None)
    for values in ({"stage_type": "swiss"}, {"stage_type": "swiss", "round_count": 3}):
        with pytest.raises(ValueError, match="Swiss"):
            await service._configure(Mock(), stage(), values)
    with pytest.raises(ValueError, match="Unsupported stage type"):
        await service._configure(Mock(), stage(kind="playoff"), {"stage_type": "swiss"})


async def test_configure_rebuilds_rounds_only_when_structure_changes():
    session = SimpleNamespace(execute=AsyncMock(), add_all=Mock())
    configured = stage(seeds=[["a", "b", "c", "d"]])
    values = {"stage_type": "swiss", "round_count": 3, "players_per_game": 4,
              "score_multiplier": "0.1"}
    await ClassicService(None)._configure(session, configured, values)
    session.execute.assert_not_called()
    assert configured.seeds
    assert configured.score_multiplier == Decimal("0.1")
    await ClassicService(None)._configure(session, configured, {**values, "round_count": 6})
    assert configured.seeds == []
    assert [r.number for r in session.add_all.call_args.args[0]] == list(range(1, 7))


async def test_seeding_uses_global_rating_for_associated_ruleset_and_defaults_unrated():
    players = [SimpleNamespace(id=UUID(int=i)) for i in range(1, 4)]
    service = ClassicService(None)
    service.eligible = AsyncMock(return_value=players)
    session = SimpleNamespace(execute=AsyncMock(return_value=Mock(all=Mock(return_value=[
        (players[0].id, Decimal(900)), (players[2].id, Decimal(1200)),
    ]))))
    configured = stage()
    assert await service._seed(session, configured, "si", {}) == [[
        str(players[2].id), str(players[1].id), str(players[0].id), None,
    ]]
    statement = session.execute.call_args.args[0]
    assert statement.compile().params["ruleset_key_1"] == "si"
    for mode in ("manual", "random"):
        with pytest.raises(ValueError, match="automatic"):
            await service._seed(session, configured, "si", {"mode": mode})


async def test_start_refreshes_swiss_seeds_and_creates_only_first_round():
    service = ClassicService(None)
    seeds = [str(uuid4()) for _ in range(8)]
    configured = stage(seeds=[[str(uuid4())]])
    rounds = [ClassicRoundRecord(id=uuid4(), number=n) for n in range(1, 4)]
    service._seed = AsyncMock(return_value=[seeds])
    service.rounds = AsyncMock(return_value=rounds)
    service._reconcile = AsyncMock()
    added = []
    session = SimpleNamespace(add=added.append, get=AsyncMock(return_value=SimpleNamespace()),
                              flush=AsyncMock())
    tournament = SimpleNamespace(id=configured.tournament_id, actual_starts_at=None)
    await service._start(session, tournament, configured, [configured], "si", uuid4())
    assert configured.seeds == [seeds]
    assert len(added) == 2
    assert all(m.round_id == rounds[0].id for m in added)
    assert {p for m in added for p in m.seats} == set(seeds)
    assert configured.started_at == tournament.actual_starts_at
    assert tournament.registration_open is False


async def test_reconciliation_waits_for_all_final_results_and_is_idempotent():
    service = ClassicService(None)
    configured = stage(started_at=datetime.now(UTC), seeds=[[str(i) for i in range(8)]])
    rounds = [ClassicRoundRecord(id=uuid4(), number=n) for n in range(1, 4)]
    matches = []
    session = SimpleNamespace(add=matches.append, flush=AsyncMock(), get=AsyncMock())
    service.stages = AsyncMock(return_value=[configured])
    service.rounds = AsyncMock(return_value=rounds)
    service.matches = AsyncMock(side_effect=lambda *_: list(matches))
    service._pair_swiss_round(session, configured, rounds[0], [])
    matches[0].results = [result(p, 1) for p in matches[0].seats]
    matches[1].game_id = uuid4()
    session.get.return_value = SimpleNamespace(status="completed")
    await service._reconcile(session, configured.tournament_id)
    assert len(matches) == 2
    assert configured.completed_at is None

    async def collect(_session, _stage, current, _game):
        current.results = [result(p, 1) for p in current.seats]

    service._collect_results = AsyncMock(side_effect=collect)
    session.get.return_value.status = "finalized"
    await service._reconcile(session, configured.tournament_id)
    assert len(matches) == 4
    assert configured.completed_at is None
    await service._reconcile(session, configured.tournament_id)
    assert len(matches) == 4
    for current in matches:
        current.results = [result(p, 1) for p in current.seats]
    await service._reconcile(session, configured.tournament_id)
    assert len(matches) == 6
    assert configured.completed_at is None
    for current in matches:
        current.results = [result(p, 1) for p in current.seats]
    await service._reconcile(session, configured.tournament_id)
    assert configured.completed_at is not None
    service._collect_results.assert_awaited_once()
    await service._reconcile(session, configured.tournament_id)
    assert len(matches) == 6


async def test_swiss_deadline_results_advance_to_next_round():
    service = ClassicService(None)
    configured = stage(started_at=datetime.now(UTC), seeds=[["a", "b", "c", "d"]])
    rounds = [ClassicRoundRecord(id=uuid4(), number=n,
                                start_deadline=datetime.now(UTC) - timedelta(seconds=1))
              for n in range(1, 4)]
    matches = []
    session = SimpleNamespace(add=matches.append, flush=AsyncMock())
    service.stages = AsyncMock(return_value=[configured])
    service.rounds = AsyncMock(return_value=rounds)
    service.matches = AsyncMock(side_effect=lambda *_: list(matches))
    service._pair_swiss_round(session, configured, rounds[0], [])
    for _ in range(3):
        await service._reconcile(session, configured.tournament_id)
    assert len(matches) == 3
    assert configured.completed_at is not None
    assert all(m.randomized and len(m.results) == 4 for m in matches)
    assert all(sum(Decimal(r["points"]) for r in m.results) == 10 for m in matches)


async def test_swiss_points_include_shared_place_awards_and_negative_game_scores():
    participants = [SimpleNamespace(id=uuid4(), seat=i, final_place=Decimal("1.5"), score=-10)
                    for i in (1, 2)]
    session = SimpleNamespace(scalars=AsyncMock(side_effect=[participants, [], []]))
    current = SimpleNamespace(id=uuid4(), seats=["a", "b"])
    game = SimpleNamespace(id=uuid4(), assignment_plan={
        "ruleset_key": "si", "ruleset_version": 1, "parameters": {},
    })
    await ClassicService._collect_results(session, stage(), current, game)
    assert [Decimal(r["points"]) for r in current.results] == [Decimal("3.3")] * 2


async def test_swiss_public_leaders_include_opponent_place_sum():
    service = TournamentProfileService(None)
    configured = stage()
    service._stage_rows = AsyncMock(return_value=[configured])
    service._stage_matches = AsyncMock(return_value=[match(result("a", 5), result("b", 3))])
    projection = await service._leaders_projection(
        Mock(), SimpleNamespace(id=configured.tournament_id),
        [("a", "Alice", "active", None), ("b", "Bob", "active", None)], True,
    )
    assert projection["stages"][0]["stage_type"] == "swiss"
    assert projection["stages"][0]["standings"][0] == {
        "player_id": "a", "nickname": "Alice", "points": "5", "score": "0",
        "opponent_place_sum": "2",
    }


async def test_playoff_qualification_uses_swiss_tiebreak():
    service = ClassicService(None)
    seeds = [str(uuid4()) for _ in range(8)]
    a, b, c, d, *others = seeds
    matches = [match(result(a, 5), result(c, 10)), match(result(b, 5), result(d, 0)),
               match(*(result(player, -1) for player in others))]
    first = stage(completed_at=datetime.now(UTC), started_at=datetime.now(UTC))
    playoff = stage(kind="playoff", stage_type="playoff", scheme_key="playoff-8")
    service.matches = AsyncMock(return_value=matches)
    service.rounds = AsyncMock(return_value=[ClassicRoundRecord(id=uuid4(), number=n)
                                           for n in (1, 2)])
    service._reconcile = AsyncMock()
    session = SimpleNamespace(add=Mock(), get=AsyncMock(return_value=SimpleNamespace()),
                              flush=AsyncMock())
    tournament = SimpleNamespace(id=first.tournament_id, actual_starts_at=first.started_at)
    await service._start(session, tournament, playoff, [first, playoff], "si", uuid4())
    assert playoff.seeds[0][:4] == [c, a, b, d]
