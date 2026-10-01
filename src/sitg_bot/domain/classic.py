"""Preset competition schemes, balanced seeding, and stage standings."""

import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from importlib.resources import files
from itertools import combinations
from statistics import mean


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


def balanced_groups(
    ratings: dict[str, Decimal], size: int, *, count: int | None = None,
    full_games: bool = False,
) -> list[list[str | None]]:
    """Snake seed, then improve the spread of group averages through pair swaps."""
    if not ratings or size < 1:
        raise ValueError("Seeding requires players and a positive group size")
    count = count or math.ceil(len(ratings) / size)
    if count * size < len(ratings) or (full_games and count != math.ceil(len(ratings) / size)):
        raise ValueError("Group count does not fit the participants")
    groups: list[list[str | None]] = [[] for _ in range(count)]
    ordered = sorted(ratings, key=lambda p: (-ratings[p], p))
    capacities = [size] * count
    if full_games:
        capacities[-1] = len(ratings) - size * (count - 1)
    index = 0
    for player in ordered:
        while True:
            band, offset = divmod(index, count)
            target_group = offset if band % 2 == 0 else count - offset - 1
            index += 1
            if len(groups[target_group]) < capacities[target_group]:
                groups[target_group].append(player)
                break
    target = mean(list(ratings.values()))

    def average(group: list) -> Decimal:
        return mean([ratings[p] for p in group]) if group else target

    def cost(group: list) -> Decimal:
        return abs(average(group) - target)

    for _ in range(8):
        improved = False
        by_average = sorted(groups, key=average)
        for left, right in zip(
            by_average[: count // 2], reversed(by_average[count // 2 :]), strict=False
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


def swiss_pairings(seeds: list[str], size: int, matches: list) -> list[list[str]]:
    """Pair saved seeds, then prefer fresh opponents with nearby stage points.

    The first round uses consecutive seats. Later rounds greedily
    minimize previous encounters, then score distance; pair swaps improve that
    same objective. Repeats remain possible when the search cannot avoid them.
    Chairs fill the configured game size but do not count as opponents.
    """
    if not 2 <= size <= 12 or not seeds or len(seeds) % size or len(set(seeds)) != len(seeds):
        raise ValueError("Swiss requires unique seeds filling games of two to twelve players")
    if any(match.results is None for match in matches):
        raise ValueError("Finish every game before pairing the next Swiss round")
    if not matches:
        return [seeds[offset:offset + size] for offset in range(0, len(seeds), size)]

    points = dict.fromkeys(seeds, Decimal(0))
    encounters: Counter = Counter()
    for match in matches:
        players = []
        for result in match.results:
            seat = result["seat"]
            if seat.startswith("chair:"):
                continue
            points[seat] += Decimal(result["points"])
            players.append(seat)
        encounters.update(frozenset(pair) for pair in combinations(players, 2))
    seed_order = {seat: index for index, seat in enumerate(seeds)}
    ordered = sorted(
        seeds, key=lambda seat: (seat.startswith("chair:"), -points[seat], seed_order[seat])
    )
    rank = {seat: index for index, seat in enumerate(ordered)}

    def pair_cost(left: str, right: str) -> tuple[int, Decimal]:
        if left.startswith("chair:") or right.startswith("chair:"):
            return 0, Decimal(0)
        return encounters[frozenset((left, right))], abs(points[left] - points[right])

    def cost(group: list[str]) -> tuple[int, Decimal]:
        costs = [pair_cost(left, right) for left, right in combinations(group, 2)]
        return sum(c[0] for c in costs), sum((c[1] for c in costs), Decimal(0))

    groups = []
    remaining = list(ordered)
    while remaining:
        group = [remaining.pop(0)]
        while len(group) < size:
            seat = min(
                remaining, key=lambda candidate: (*cost([*group, candidate]), rank[candidate])
            )
            remaining.remove(seat)
            group.append(seat)
        groups.append(group)
    for _ in range(8):
        improved = False
        for left, right in combinations(groups, 2):
            left_cost, right_cost = cost(left), cost(right)
            best_cost = tuple(a + b for a, b in zip(left_cost, right_cost, strict=True))
            best_swap = None
            for i in range(size):
                for j in range(size):
                    left[i], right[j] = right[j], left[i]
                    after = tuple(a + b for a, b in zip(cost(left), cost(right), strict=True))
                    left[i], right[j] = right[j], left[i]
                    if after < best_cost:
                        best_cost, best_swap = after, (i, j)
            if best_swap is not None:
                i, j = best_swap
                left[i], right[j] = right[j], left[i]
                improved = True
        if not improved:
            break
    return sorted(
        (sorted(group, key=rank.__getitem__) for group in groups),
        key=lambda group: rank[group[0]],
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


def standings(matches: list, *, quiz: bool, seed: str, swiss: bool = False) -> list[dict]:
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
    if swiss:
        # Opponent places use stage points alone, avoiding a circular tiebreak.
        ordered = sorted(rows, key=lambda row: Decimal(row["points"]), reverse=True)
        places = {}
        index = 0
        while index < len(ordered):
            end = index + 1
            while end < len(ordered) and Decimal(ordered[end]["points"]) == Decimal(
                ordered[index]["points"]
            ):
                end += 1
            place = Decimal(index + 1 + end) / 2
            places.update((row["seat"], place) for row in ordered[index:end])
            index = end
        opponents: dict[str, list[str]] = {seat: [] for seat in totals}
        for match in matches:
            players = [r["seat"] for r in match.results or [] if not r["seat"].startswith("chair:")]
            for player in players:
                opponents[player].extend(opponent for opponent in players if opponent != player)
        for row in rows:
            # Count every encounter, including an unavoidable repeat; exclude Chairs.
            total = sum((places[p] for p in opponents[row["seat"]]), Decimal(0))
            row["opponent_place_sum"] = str(total)
            row["key"] = [row["points"], str(-total)]
    return order_results(rows, seed=seed)
