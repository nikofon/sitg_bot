"""Preset competition schemes, balanced seeding, and stage standings."""

import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from importlib.resources import files
from statistics import median


def scheme_library() -> dict[str, dict]:
    return json.loads(files(__package__).joinpath("classic_schemes.json").read_text())


SCHEMES = scheme_library()


def validate_scheme(scheme: dict) -> None:
    games = {(g["round"], g["game"]): g for g in scheme["games"]}
    if len(games) != len(scheme["games"]):
        raise ValueError("Duplicate scheme game")
    for (round_number, _), game in games.items():
        if not 1 <= len(game["sources"]) <= 12:
            raise ValueError("Invalid scheme game size")
        for source in game["sources"]:
            if isinstance(source, int):
                if not 1 <= source <= scheme["size"]:
                    raise ValueError("Invalid seed reference")
            else:
                previous_round, previous_game, place = source
                previous = games.get((previous_round, previous_game))
                if (
                    previous is None
                    or previous_round >= round_number
                    or not 1 <= place <= len(previous["sources"])
                ):
                    raise ValueError("Invalid advancement reference")
    if scheme["kind"] == "groups":
        for round_number in range(1, scheme["round_count"] + 1):
            seats = [s for g in games.values() if g["round"] == round_number for s in g["sources"]]
            if sorted(seats) != list(range(1, scheme["size"] + 1)):
                raise ValueError("Every group player must play once per round")


for _scheme in SCHEMES.values():
    validate_scheme(_scheme)


def balanced_groups(ratings: dict[str, Decimal], size: int) -> list[list[str | None]]:
    """Snake seed, then improve the spread of group medians through pair swaps."""
    if not ratings or size < 1:
        raise ValueError("Seeding requires players and a positive group size")
    count = math.ceil(len(ratings) / size)
    groups: list[list[str | None]] = [[] for _ in range(count)]
    ordered = sorted(ratings, key=lambda p: (-ratings[p], p))
    for index, player in enumerate(ordered):
        band, offset = divmod(index, count)
        groups[offset if band % 2 == 0 else count - offset - 1].append(player)
    target = median(list(ratings.values()))

    def cost(group: list) -> Decimal:
        return abs(median([ratings[p] for p in group]) - target)

    for _ in range(8):
        improved = False
        by_median = sorted(groups, key=lambda g: median([ratings[p] for p in g]))
        for left, right in zip(
            by_median[: count // 2], reversed(by_median[count // 2 :]), strict=False
        ):
            before = cost(left) + cost(right)
            best = None
            for i in range(len(left)):
                for j in range(len(right)):
                    left[i], right[j] = right[j], left[i]
                    after = cost(left) + cost(right)
                    left[i], right[j] = right[j], left[i]
                    if after < before:
                        best, before = (i, j), after
            if best is not None:
                i, j = best
                left[i], right[j] = right[j], left[i]
                improved = True
        if not improved:
            break
    return [g + [None] * (size - len(g)) for g in groups]


def competition_points(place: Decimal, occupied: int, points: list[str]) -> Decimal:
    # Rulesets share the mean of occupied places when every criterion is tied.
    first = int(place - Decimal(occupied - 1) / 2)
    return (
        sum(
            (
                Decimal(points[p - 1]) if p <= len(points) else Decimal(0)
                for p in range(first, first + occupied)
            ),
            Decimal(0),
        )
        / occupied
    )


def order_results(results: list[dict], *, seed: str) -> list[dict]:
    """Resolve any remaining equality reproducibly for unique bracket positions."""
    ordered = list(results)
    random.Random(seed).shuffle(ordered)
    return sorted(ordered, key=lambda r: tuple(Decimal(str(v)) for v in r["key"]), reverse=True)


def playoff_places(
    scheme: dict,
    seats: Mapping[tuple[int, int], Sequence[str | None]],
    results: Mapping[tuple[int, int], Sequence[Mapping[str, object]]],
) -> list[dict]:
    """Final places for a play-off scheme from ordered per-game result lists.

    ``seats`` maps every scheme game ``(round, game)`` to its resolved
    participants and ``results`` maps played games to their ordered finishers
    (each entry carries ``seat``). The last round keeps its in-game order: the
    winner takes 1st place and the other finalists keep their finishing
    positions. A player eliminated in an earlier round with several parallel
    games shares one place: ``survivors + position - 1.5``, where ``survivors``
    counts the distinct players that continue after that round.
    """
    games = {(game["round"], game["game"]): game for game in scheme["games"]}
    last_round = max(round_number for round_number, _ in games)
    advancing: set[tuple[int, int, int]] = set()
    for game in games.values():
        for source in game["sources"]:
            if isinstance(source, list):
                advancing.add((int(source[0]), int(source[1]), int(source[2])))
    survivors: dict[int, int] = {}
    for round_number in {round_number for round_number, _ in games}:
        distinct = {
            seat
            for (game_round, _), game_seats in seats.items()
            if game_round > round_number
            for seat in game_seats or ()
            if seat and not seat.startswith("chair:")
        }
        survivors[round_number] = len(distinct)
    ranking: dict[str, Decimal] = {}
    for (round_number, game_number), ordered in results.items():
        if (round_number, game_number) not in games:
            continue
        for position, entry in enumerate(ordered, start=1):
            seat = str(entry["seat"])
            if seat.startswith("chair:"):
                continue
            if round_number == last_round:
                ranking[seat] = Decimal(position)
            elif (round_number, game_number, position) in advancing:
                continue
            else:
                ranking[seat] = (
                    Decimal(survivors.get(round_number, 0))
                    + Decimal(position)
                    - Decimal("1.5")
                )
    return [
        {"seat": seat, "place": str(place)}
        for seat, place in sorted(ranking.items(), key=lambda item: (item[1], item[0]))
    ]


def standings(matches: list, *, quiz: bool, seed: str) -> list[dict]:
    totals: dict[str, dict] = {}
    for match in matches:
        for result in match.results or []:
            seat = result["seat"]
            if seat.startswith("chair:"):
                continue
            item = totals.setdefault(
                seat,
                {"seat": seat, "points": Decimal(0), "score": Decimal(0), "correct": Counter()},
            )
            item["points"] += Decimal(result["points"])
            item["score"] += Decimal(result["score"])
            item["correct"].update(result.get("correct_values", []))
    values = sorted({v for t in totals.values() for v in t["correct"]}, reverse=True)
    rows = []
    for item in totals.values():
        correct = item.pop("correct")
        item["key"] = [
            str(item["score"] if quiz else item["points"]),
            str(sum(v * n for v, n in correct.items())),
            *[str(correct[v]) for v in values],
        ]
        item["points"], item["score"] = str(item["points"]), str(item["score"])
        rows.append(item)
    return order_results(rows, seed=seed)
