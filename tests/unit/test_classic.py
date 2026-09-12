from decimal import Decimal
from types import SimpleNamespace

import pytest

from sitg_bot.domain.classic import (
    SCHEMES,
    balanced_groups,
    competition_points,
    order_results,
    standings,
    validate_scheme,
)


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
