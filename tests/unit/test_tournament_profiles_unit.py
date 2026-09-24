from decimal import Decimal

from sitg_bot.domain.classic import SCHEMES, playoff_places


def _ordered(seats: list[str]) -> list[dict]:
    return [{"seat": seat, "score": "0", "place": str(index)} for index, seat in enumerate(seats, 1)]


def test_playoff_places_match_shared_elimination_places() -> None:
    scheme = SCHEMES["playoff-8"]
    players = [str(number) for number in range(1, 9)]
    seats = {
        (1, 1): [players[0], players[3], players[4], players[7]],
        (1, 2): [players[1], players[2], players[5], players[6]],
        (2, 1): [players[0], players[3], players[1], players[2]],
    }
    results = {
        (1, 1): _ordered([players[0], players[3], players[4], players[7]]),
        (1, 2): _ordered([players[1], players[2], players[5], players[6]]),
        (2, 1): _ordered([players[1], players[0], players[2], players[3]]),
    }

    places = {row["seat"]: row["place"] for row in playoff_places(scheme, seats, results)}

    assert places == {
        players[1]: "1",
        players[0]: "2",
        players[2]: "3",
        players[3]: "4",
        players[4]: "5.5",
        players[5]: "5.5",
        players[6]: "6.5",
        players[7]: "6.5",
    }


def test_playoff_places_skip_ongoing_finalists() -> None:
    scheme = SCHEMES["playoff-8"]
    players = [str(number) for number in range(1, 9)]
    seats = {
        (1, 1): [players[0], players[3], players[4], players[7]],
        (1, 2): [players[1], players[2], players[5], players[6]],
        (2, 1): [players[0], players[3], players[1], players[2]],
    }
    results = {
        (1, 1): _ordered([players[0], players[3], players[4], players[7]]),
        (1, 2): _ordered([players[1], players[2], players[5], players[6]]),
    }

    places = [row["seat"] for row in playoff_places(scheme, seats, results)]

    assert sorted(places) == [players[4], players[5], players[6], players[7]]
    values = [row["place"] for row in playoff_places(scheme, seats, results)]
    assert sorted(Decimal(value) for value in values) == [
        Decimal("5.5"), Decimal("5.5"), Decimal("6.5"), Decimal("6.5"),
    ]


def test_playoff_places_cover_double_elimination() -> None:
    scheme = SCHEMES["de-8"]
    seats = {
        (1, 1): ["4", "1", "5", "8"],
        (1, 2): ["2", "3", "6", "7"],
        (2, 1): ["4", "1", "3", "7"],
        (2, 2): ["5", "8", "2", "6"],
        (3, 1): ["7", "3", "2", "6"],
        (4, 1): ["1", "4", "6", "2"],
    }
    results = {
        (1, 1): _ordered(["4", "1", "5", "8"]),
        (1, 2): _ordered(["3", "7", "2", "6"]),
        (2, 1): _ordered(["1", "4", "7", "3"]),
        (2, 2): _ordered(["2", "6", "5", "8"]),
        (3, 1): _ordered(["6", "2", "7", "3"]),
        (4, 1): _ordered(["1", "6", "2", "4"]),
    }

    places = [row["place"] for row in playoff_places(scheme, seats, results)]

    assert places == ["1", "2", "3", "4", "5.5", "6.5", "7.5", "8.5"]


def test_playoff_places_ignore_chairs_and_unknown_games() -> None:
    scheme = SCHEMES["playoff-8"]
    seats = {
        (1, 1): ["1", "4", "5", "8"],
        (1, 2): ["2", "3", "6", "7"],
        (2, 1): ["1", "4", "2", "3"],
    }
    results = {
        (1, 1): _ordered(["1", "4", "chair:123", "8"]),
        (1, 2): _ordered(["2", "3", "6", "7"]),
        (2, 1): _ordered(["2", "1", "3", "4"]),
        (5, 9): _ordered(["9"]),
    }

    rows = playoff_places(scheme, seats, results)

    assert all(not row["seat"].startswith("chair:") for row in rows)
    assert len(rows) == 7
