from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sitg_bot.application.contracts import ClassicSeedingValues
from sitg_bot.domain.classic import (
    SCHEMES,
    balanced_groups,
    competition_points,
    order_results,
    standings,
    validate_scheme,
)
from sitg_bot.services.classic import ClassicService


def test_seeding_contract_rejects_unknown_automatic_strategy():
    with pytest.raises(ValidationError):
        ClassicSeedingValues.model_validate({"mode": "automatic", "strategy": "invalid"})


@pytest.mark.parametrize("kind,expected", [("first", "6"), ("playoff", "0")])
async def test_result_points_apply_only_to_first_stage(kind, expected):
    participant = SimpleNamespace(id=uuid4(), seat=1, final_place=1, score=100)
    session = SimpleNamespace(scalars=AsyncMock(side_effect=[[participant], []]))
    stage = SimpleNamespace(
        kind=kind, place_points=["4", "3", "2", "1"], score_multiplier=Decimal("0.02"),
        random_seed="test",
    )
    match = SimpleNamespace(id=uuid4(), seats=[str(uuid4())])
    game = SimpleNamespace(id=uuid4(), assignment_plan={
        "ruleset_key": "si", "ruleset_version": 1, "parameters": {},
    })
    await ClassicService._collect_results(session, stage, match, game)
    assert Decimal(match.results[0]["points"]) == Decimal(expected)
    assert match.results[0]["score"] == "100"


@pytest.mark.parametrize("scheme", SCHEMES.values(), ids=SCHEMES.keys())
def test_scheme_references_and_player_counts(scheme):
    validate_scheme(scheme)
    if scheme["kind"] == "playoff":
        assert sorted(s for g in scheme["games"] if g["round"] == 1 for s in g["sources"]) == list(
            range(1, scheme["size"] + 1)
        )
        for round_number in range(1, scheme["round_count"] + 1):
            sources = [
                tuple(s) if isinstance(s, list) else s
                for g in scheme["games"]
                if g["round"] == round_number
                for s in g["sources"]
            ]
            assert len(sources) == len(set(sources))


def test_top32_de_corrected_lower_bracket():
    games = {g["game"]: g for g in SCHEMES["de-32"]["games"] if g["round"] == 4}
    assert games[3]["sources"] == [[3, 3, 1], [3, 4, 2], [3, 1, 3], [3, 2, 4]]
    assert games[4]["sources"] == [[3, 3, 2], [3, 4, 1], [3, 1, 4], [3, 2, 3]]


def test_balanced_groups_preserve_every_player_and_pad_with_chairs():
    ratings = {str(i): Decimal(1000 + i * 10) for i in range(31)}
    groups = balanced_groups(ratings, 16)
    assert len(groups) == 2 and all(len(g) == 16 for g in groups)
    assert sorted(s for g in groups for s in g if s is not None) == sorted(ratings)
    assert sum(s is None for g in groups for s in g) == 1
    assert groups == balanced_groups(ratings, 16)


@pytest.mark.parametrize("stage_type,scheme,size,count", [
    ("groups", "groups-9-4", 9, 18),
    ("playoff", "top-16", 4, 16),
    ("swiss", None, 4, 12),
])
async def test_automatic_strategies_use_global_ratings_and_prescribed_games(
    stage_type, scheme, size, count,
):
    # Resolve the library ID without depending on the display name of play-off schemes.
    if stage_type == "playoff":
        scheme = next(s["id"] for s in SCHEMES.values()
                      if s["kind"] == "playoff" and s["size"] == count)
    players = [SimpleNamespace(id=uuid4()) for _ in range(count)]
    ids = [str(p.id) for p in players]
    ratings = {p.id: Decimal(2000 - i * 50) for i, p in enumerate(players)}
    service = ClassicService(None)
    service.eligible = AsyncMock(return_value=players)
    session = SimpleNamespace(execute=AsyncMock(return_value=Mock(
        all=Mock(return_value=list(ratings.items())),
    )))
    stage = SimpleNamespace(stage_type=stage_type, scheme_key=scheme, players_per_game=size,
                            kind="playoff" if stage_type == "playoff" else "first",
                            tournament_id=uuid4())

    def groups(seeds):
        if stage_type == "playoff":
            return [[seeds[0][source - 1] for source in game["sources"]]
                    for game in SCHEMES[scheme]["games"] if game["round"] == 1]
        if stage_type == "swiss":
            return [seeds[0][i:i + size] for i in range(0, count, size)]
        return seeds

    best = await service._seed(session, stage, "si", {"mode": "automatic", "strategy": "best"})
    assert groups(best) == [ids[i:i + size] for i in range(0, count, size)]
    average = await service._seed(
        session, stage, "si", {"mode": "automatic", "strategy": "average"},
    )
    by_id = {str(p): rating for p, rating in ratings.items()}
    means = [sum(by_id[p] for p in g) / len(g) for g in groups(average)]
    assert max(means) - min(means) <= 50
    assert sorted(p for g in average for p in g) == sorted(ids)
    for mode in ("manual", "random"):
        seeded = await service._seed(session, stage, "si", {"mode": mode, "seeds": best})
        assert sorted(p for g in seeded for p in g) == sorted(ids)
    with pytest.raises(ValueError, match="strategy"):
        await service._seed(session, stage, "si", {"strategy": "invalid"})


def test_average_swiss_fills_consecutive_games_and_playoff_keeps_empty_games():
    ratings = {str(i): Decimal(1000 + i * 100) for i in range(9)}
    groups = balanced_groups(ratings, 4, full_games=True)
    assert [sum(p is not None for p in g) for g in groups] == [4, 4, 1]
    sparse = balanced_groups({"a": Decimal(1500)}, 4, count=4)
    assert sparse == [["a", None, None, None], [None] * 4, [None] * 4, [None] * 4]


def test_place_awards_include_split_places_and_score_multiplier():
    assert competition_points(Decimal("1.5"), 2, ["4", "3", "2", "1"]) == Decimal("3.5")
    assert competition_points(Decimal(2), 1, ["4", "3", "2", "1"]) - Decimal("10") * Decimal(
        "0.02"
    ) == Decimal("2.8")


def test_stage_tiebreaks_sum_correct_answers_across_games():
    def result(player, points, values):
        return {"seat": player, "points": points, "score": "100", "correct_values": values}

    matches = [
        SimpleNamespace(results=[result("a", "5", [50]), result("b", "5", [40, 10])]),
        SimpleNamespace(
            results=[result("a", "3", [10]), result("b", "3", [10]), result("chair:1", "9", [])]
        ),
    ]
    ranking = standings(matches, quiz=False, seed="test")
    assert [r["seat"] for r in ranking] == ["a", "b"]
    assert ranking[0]["points"] == "8"
    assert ranking[0]["score"] == "200"
    assert ranking[0]["key"][:3] == ["8", "60", "1"]
    assert order_results(ranking, seed="test") == ranking


@pytest.mark.parametrize("quiz,swiss", [(False, False), (True, False), (False, True)])
def test_stage_tiebreaks_ignore_zero_point_answers(quiz, swiss):
    def matches(values):
        return [SimpleNamespace(results=[
            {"seat": "a", "points": "5", "score": "100", "correct_values": values},
            {"seat": "b", "points": "5", "score": "100", "correct_values": [50]},
        ])]

    assert standings(matches([0, 0, 50]), quiz=quiz, swiss=swiss, seed="zero") == standings(
        matches([50]), quiz=quiz, swiss=swiss, seed="zero",
    )
