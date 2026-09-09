from __future__ import annotations

import argparse
import csv
import html
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sitg_bot.domain.rating import (
    STARTING_RATING,
    PairwiseRatingInput,
    RatingHistoryEntry,
    confidence_model,
    pairwise_rating_deltas,
)

QUESTION_VALUES = (10, 20, 30, 40, 50)
THEMES_PER_GAME = 8
TICKS_PER_QUESTION = 30
READING_TICKS = 10
STARTING_CHECK = 150
MODEL_KEYS = ("log_recent", "time_weighted")
ATTRIBUTE_NAMES = ("knowledge", "speed", "confidence", "enlightment", "envolvment")

# Adjacent values deliberately overlap: a particular 10-point question can be harder than
# a particular 20-point question. The easiest band always remains below the hardest band.
FINAL_CHECK_RANGES = {
    10: (25, 60),
    20: (35, 75),
    30: (45, 90),
    40: (55, 105),
    50: (70, 120),
}


@dataclass(frozen=True, slots=True)
class PlayerBot:
    number: int
    player_id: UUID
    knowledge: int
    speed: int
    confidence: int
    enlightment: int
    envolvment: int

    @property
    def name(self) -> str:
        return f"Bot {self.number:03d}"


@dataclass(frozen=True, slots=True)
class VirtualQuestion:
    value: int
    checks: tuple[int, ...]


@dataclass(slots=True)
class GameResult:
    game_number: int
    played_on: date
    player: PlayerBot
    score: int = 0
    points_without_penalties: int = 0
    correct: int = 0
    incorrect: int = 0
    buzzes: int = 0
    enlightment_rescues: int = 0
    correct_by_value: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    place: Decimal = Decimal(0)


@dataclass(slots=True)
class QuestionSummary:
    questions: int = 0
    buzzes: int = 0
    correct: int = 0
    incorrect: int = 0
    enlightment_rescues: int = 0
    unanswered: int = 0


@dataclass(slots=True)
class PlayerTotals:
    games: int = 0
    wins: int = 0
    score: int = 0
    correct: int = 0
    incorrect: int = 0
    buzzes: int = 0
    enlightment_rescues: int = 0


@dataclass(slots=True)
class RatingState:
    rating: Decimal = STARTING_RATING
    history: list[RatingHistoryEntry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RatingRecord:
    model: str
    game_number: int
    played_on: date
    player: PlayerBot
    place: Decimal
    score: int
    rating_before: Decimal
    delta: Decimal
    rating_after: Decimal
    confidence: Decimal
    k_factor: Decimal
    effective_games: Decimal
    time_weighted_rating: Decimal | None


@dataclass(slots=True)
class SimulationResult:
    players: list[PlayerBot]
    games: list[list[GameResult]]
    ratings: list[RatingRecord]
    rating_states: dict[str, dict[UUID, RatingState]]
    player_totals: dict[UUID, PlayerTotals]
    question_summaries: dict[int, QuestionSummary]
    days: int
    seed: int
    start_date: date
    players_per_game: int


def _roll_dice(rng: random.Random, count: int, sides: int) -> int:
    return sum(rng.randint(1, sides) for _ in range(count))


def _rounded_up_division(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def perceived_check(question: VirtualQuestion, tick: int, speed: int) -> int:
    delay = 20 - speed
    return question.checks[max(0, tick - delay)]


def generate_question(rng: random.Random, value: int) -> VirtualQuestion:
    low, high = FINAL_CHECK_RANGES[value]
    final_check = rng.randint(low, high)
    intermediate = sorted(rng.sample(range(final_check + 1, STARTING_CHECK), 8), reverse=True)
    reading = (STARTING_CHECK, *intermediate, final_check)
    checks = reading + (final_check,) * (TICKS_PER_QUESTION - READING_TICKS)
    return VirtualQuestion(value=value, checks=checks)


def generate_players(count: int, rng: random.Random) -> list[PlayerBot]:
    return [
        PlayerBot(
            number=index,
            player_id=UUID(int=index),
            knowledge=rng.randint(1, 20),
            speed=rng.randint(1, 20),
            confidence=rng.randint(1, 20),
            enlightment=rng.randint(1, 20),
            envolvment=rng.randint(1, 20),
        )
        for index in range(1, count + 1)
    ]


def group_daily_players(
    players: Sequence[PlayerBot], players_per_game: int, rng: random.Random
) -> list[list[PlayerBot]]:
    eligible = [player for player in players if player.envolvment > rng.randint(1, 20)]
    rng.shuffle(eligible)
    return [
        eligible[index : index + players_per_game]
        for index in range(0, len(eligible), players_per_game)
    ]


def _buzz_roll(player: PlayerBot, rng: random.Random) -> int:
    dice = _rounded_up_division(player.knowledge, 2)
    dice += _rounded_up_division(player.confidence, 2)
    return _roll_dice(rng, dice, 6)


def _answers_correctly(
    player: PlayerBot, question: VirtualQuestion, tick: int, rng: random.Random
) -> tuple[bool, bool]:
    if _roll_dice(rng, player.knowledge, 6) >= question.checks[tick]:
        return True, False
    rescue_dice = _rounded_up_division(player.enlightment + player.knowledge, 5)
    rescued = any(rng.randint(1, 20) == 20 for _ in range(rescue_dice))
    return rescued, rescued


def _assign_places(results: Sequence[GameResult]) -> None:
    def ranking_key(result: GameResult) -> tuple[int, ...]:
        tie_break_correct = tuple(
            result.correct_by_value[value] for value in reversed(QUESTION_VALUES[1:])
        )
        return (result.score, result.points_without_penalties, *tie_break_correct)

    ordered = sorted(results, key=ranking_key, reverse=True)
    index = 0
    while index < len(ordered):
        tied_until = index + 1
        while tied_until < len(ordered) and ranking_key(ordered[tied_until]) == ranking_key(
            ordered[index]
        ):
            tied_until += 1
        first_place = Decimal(index + 1)
        last_place = Decimal(tied_until)
        shared_place = (first_place + last_place) / Decimal(2)
        for result in ordered[index:tied_until]:
            result.place = shared_place
        index = tied_until


def simulate_game(
    players: Sequence[PlayerBot],
    *,
    game_number: int,
    played_on: date,
    rng: random.Random,
    question_summaries: dict[int, QuestionSummary],
) -> list[GameResult]:
    results = {player.player_id: GameResult(game_number, played_on, player) for player in players}
    for _theme in range(THEMES_PER_GAME):
        for value in QUESTION_VALUES:
            question = generate_question(rng, value)
            summary = question_summaries[value]
            summary.questions += 1
            eligible = {player.player_id: player for player in players}
            answered = False
            for tick in range(TICKS_PER_QUESTION):
                buzzers = [
                    player
                    for player in eligible.values()
                    if _buzz_roll(player, rng) >= perceived_check(question, tick, player.speed)
                ]
                if not buzzers:
                    continue
                best_speed = max(player.speed for player in buzzers)
                fastest = [player for player in buzzers if player.speed == best_speed]
                player = rng.choice(fastest)
                player_result = results[player.player_id]
                player_result.buzzes += 1
                summary.buzzes += 1
                correct, rescued = _answers_correctly(player, question, tick, rng)
                if correct:
                    player_result.score += value
                    player_result.points_without_penalties += value
                    player_result.correct += 1
                    player_result.correct_by_value[value] += 1
                    player_result.enlightment_rescues += int(rescued)
                    summary.correct += 1
                    summary.enlightment_rescues += int(rescued)
                    answered = True
                    break
                player_result.score -= value
                player_result.incorrect += 1
                summary.incorrect += 1
                del eligible[player.player_id]
                if not eligible:
                    break
            if not answered:
                summary.unanswered += 1
    result_list = list(results.values())
    _assign_places(result_list)
    return result_list


def _apply_ratings(
    game_results: Sequence[GameResult],
    *,
    played_at: datetime,
    rating_states: dict[str, dict[UUID, RatingState]],
) -> list[RatingRecord]:
    if len(game_results) < 2:
        return []
    records: list[RatingRecord] = []
    for model_key in MODEL_KEYS:
        model = confidence_model(model_key)
        inputs: list[PairwiseRatingInput] = []
        confidence_results = {}
        states = rating_states[model_key]
        for result in game_results:
            state = states[result.player.player_id]
            confidence = model.calculate(
                current_rating=state.rating, history=state.history, as_of=played_at
            )
            confidence_results[result.player.player_id] = confidence
            inputs.append(
                PairwiseRatingInput(
                    result.player.player_id,
                    state.rating,
                    result.place,
                    confidence.k_factor,
                )
            )
        deltas = pairwise_rating_deltas(inputs)
        for result in game_results:
            state = states[result.player.player_id]
            confidence = confidence_results[result.player.player_id]
            before = state.rating
            delta = deltas[result.player.player_id]
            state.rating += delta
            state.history.append(RatingHistoryEntry(delta, played_at))
            records.append(
                RatingRecord(
                    model=model_key,
                    game_number=result.game_number,
                    played_on=result.played_on,
                    player=result.player,
                    place=result.place,
                    score=result.score,
                    rating_before=before,
                    delta=delta,
                    rating_after=state.rating,
                    confidence=confidence.confidence,
                    k_factor=confidence.k_factor,
                    effective_games=confidence.effective_games,
                    time_weighted_rating=confidence.time_weighted_rating,
                )
            )
    return records


def simulate(
    *,
    player_count: int,
    days: int,
    players_per_game: int = 4,
    seed: int = 20260828,
    start_date: date = date(2025, 1, 1),
) -> SimulationResult:
    if player_count < 1:
        raise ValueError("player_count must be at least 1")
    if days < 1:
        raise ValueError("days must be at least 1")
    if not 1 <= players_per_game <= 12:
        raise ValueError("players_per_game must be between 1 and the SI maximum of 12")

    rng = random.Random(seed)
    players = generate_players(player_count, rng)
    rating_states = {
        model: {player.player_id: RatingState() for player in players} for model in MODEL_KEYS
    }
    player_totals = {player.player_id: PlayerTotals() for player in players}
    question_summaries = {value: QuestionSummary() for value in QUESTION_VALUES}
    games: list[list[GameResult]] = []
    ratings: list[RatingRecord] = []
    game_number = 0

    for day_offset in range(days):
        played_on = start_date + timedelta(days=day_offset)
        daily_groups = group_daily_players(players, players_per_game, rng)
        for daily_game_index, group in enumerate(daily_groups):
            game_number += 1
            game_results = simulate_game(
                group,
                game_number=game_number,
                played_on=played_on,
                rng=rng,
                question_summaries=question_summaries,
            )
            games.append(game_results)
            for result in game_results:
                totals = player_totals[result.player.player_id]
                totals.games += 1
                totals.wins += int(result.place == 1)
                totals.score += result.score
                totals.correct += result.correct
                totals.incorrect += result.incorrect
                totals.buzzes += result.buzzes
                totals.enlightment_rescues += result.enlightment_rescues
            played_at = datetime.combine(played_on, time(tzinfo=UTC)) + timedelta(
                seconds=daily_game_index
            )
            ratings.extend(
                _apply_ratings(
                    game_results,
                    played_at=played_at,
                    rating_states=rating_states,
                )
            )

    return SimulationResult(
        players=players,
        games=games,
        ratings=ratings,
        rating_states=rating_states,
        player_totals=player_totals,
        question_summaries=question_summaries,
        days=days,
        seed=seed,
        start_date=start_date,
        players_per_game=players_per_game,
    )


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)


def write_csv_reports(result: SimulationResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    settings: list[tuple[str, object]] = [
        ("seed", result.seed),
        ("players", len(result.players)),
        ("days", result.days),
        ("start_date", result.start_date.isoformat()),
        ("players_per_game", result.players_per_game),
        ("themes_per_game", THEMES_PER_GAME),
        ("questions_per_theme", len(QUESTION_VALUES)),
        ("ticks_per_question", TICKS_PER_QUESTION),
        ("reading_ticks", READING_TICKS),
        ("starting_check", STARTING_CHECK),
    ]
    settings.extend(
        (f"final_check_range_{value}", f"{low}-{high}")
        for value, (low, high) in FINAL_CHECK_RANGES.items()
    )
    _write_csv(output_dir / "simulation_settings.csv", ("setting", "value"), settings)
    _write_csv(
        output_dir / "players.csv",
        ("player", *ATTRIBUTE_NAMES),
        (
            (
                player.name,
                player.knowledge,
                player.speed,
                player.confidence,
                player.enlightment,
                player.envolvment,
            )
            for player in result.players
        ),
    )
    _write_csv(
        output_dir / "game_results.csv",
        (
            "game",
            "date",
            "player",
            "players_in_game",
            "place",
            "score",
            "points_without_penalties",
            "buzzes",
            "correct",
            "incorrect",
            "enlightment_rescues",
        ),
        (
            (
                item.game_number,
                item.played_on.isoformat(),
                item.player.name,
                len(game),
                item.place,
                item.score,
                item.points_without_penalties,
                item.buzzes,
                item.correct,
                item.incorrect,
                item.enlightment_rescues,
            )
            for game in result.games
            for item in game
        ),
    )
    _write_csv(
        output_dir / "rating_history.csv",
        (
            "model",
            "game",
            "date",
            "player",
            "place",
            "score",
            "rating_before",
            "delta",
            "rating_after",
            "confidence",
            "k_factor",
            "effective_games",
            "time_weighted_rating",
        ),
        (
            (
                item.model,
                item.game_number,
                item.played_on.isoformat(),
                item.player.name,
                item.place,
                item.score,
                item.rating_before,
                item.delta,
                item.rating_after,
                item.confidence,
                item.k_factor,
                item.effective_games,
                item.time_weighted_rating if item.time_weighted_rating is not None else "",
            )
            for item in result.ratings
        ),
    )
    ranks = _model_ranks(result)
    _write_csv(
        output_dir / "final_ratings.csv",
        (
            "player",
            *ATTRIBUTE_NAMES,
            "games",
            "wins",
            "average_score",
            "answer_accuracy",
            "log_recent_rating",
            "log_recent_rank",
            "time_weighted_rating",
            "time_weighted_rank",
            "rating_difference",
        ),
        (
            _final_player_row(result, player, ranks)
            for player in sorted(result.players, key=lambda item: item.number)
        ),
    )
    _write_csv(
        output_dir / "question_summary.csv",
        (
            "value",
            "questions",
            "buzzes",
            "correct",
            "incorrect",
            "enlightment_rescues",
            "unanswered",
            "answer_rate",
        ),
        (
            (
                value,
                summary.questions,
                summary.buzzes,
                summary.correct,
                summary.incorrect,
                summary.enlightment_rescues,
                summary.unanswered,
                _ratio(summary.correct, summary.questions),
            )
            for value, summary in result.question_summaries.items()
        ),
    )


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


def _model_ranks(result: SimulationResult) -> dict[str, dict[UUID, int]]:
    ranks: dict[str, dict[UUID, int]] = {}
    for model in MODEL_KEYS:
        ordered = sorted(
            result.players,
            key=lambda player: result.rating_states[model][player.player_id].rating,
            reverse=True,
        )
        ranks[model] = {player.player_id: index for index, player in enumerate(ordered, 1)}
    return ranks


def _final_player_row(
    result: SimulationResult, player: PlayerBot, ranks: dict[str, dict[UUID, int]]
) -> tuple[object, ...]:
    totals = result.player_totals[player.player_id]
    log_rating = result.rating_states["log_recent"][player.player_id].rating
    time_rating = result.rating_states["time_weighted"][player.player_id].rating
    return (
        player.name,
        player.knowledge,
        player.speed,
        player.confidence,
        player.enlightment,
        player.envolvment,
        totals.games,
        totals.wins,
        round(_ratio(totals.score, totals.games), 4),
        round(_ratio(totals.correct, totals.correct + totals.incorrect), 6),
        log_rating,
        ranks["log_recent"][player.player_id],
        time_rating,
        ranks["time_weighted"][player.player_id],
        time_rating - log_rating,
    )


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2 or len(set(left)) < 2 or len(set(right)) < 2:
        return 0.0
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_spread = sum((x - left_mean) ** 2 for x in left)
    right_spread = sum((y - right_mean) ** 2 for y in right)
    return numerator / math.sqrt(left_spread * right_spread)


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        tied_until = index + 1
        while tied_until < len(ordered) and values[ordered[tied_until]] == values[ordered[index]]:
            tied_until += 1
        rank = (index + 1 + tied_until) / 2
        for original_index in ordered[index:tied_until]:
            ranks[original_index] = rank
        index = tied_until
    return ranks


def _svg_scatter(
    points: Sequence[tuple[float, float, str]], *, width: int = 620, height: int = 420
) -> str:
    margin = 52
    xs = [point[0] for point in points] or [0, 1]
    ys = [point[1] for point in points] or [0, 1]
    low = min(xs + ys) - 10
    high = max(xs + ys) + 10
    span = high - low or 1

    def x(value: float) -> float:
        return margin + (value - low) / span * (width - 2 * margin)

    def y(value: float) -> float:
        return height - margin - (value - low) / span * (height - 2 * margin)

    circles = "".join(
        f'<circle cx="{x(px):.1f}" cy="{y(py):.1f}" r="3.5"><title>'
        f"{html.escape(label)}: {px:.1f}, {py:.1f}</title></circle>"
        for px, py, label in points
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Final rating model scatter plot">'
        f'<line class="axis" x1="{margin}" y1="{height - margin}" x2="{width - margin}" '
        f'y2="{height - margin}"/><line class="axis" x1="{margin}" y1="{margin}" '
        f'x2="{margin}" y2="{height - margin}"/><line class="reference" x1="{x(low):.1f}" '
        f'y1="{y(low):.1f}" x2="{x(high):.1f}" y2="{y(high):.1f}"/>{circles}'
        f'<text x="{width / 2}" y="{height - 8}" text-anchor="middle">log_recent</text>'
        f'<text x="15" y="{height / 2}" transform="rotate(-90 15 {height / 2})" '
        'text-anchor="middle">time_weighted</text></svg>'
    )


def _svg_bars(values: Sequence[tuple[str, float]], *, width: int = 620, height: int = 330) -> str:
    margin = 55
    max_value = max((abs(value) for _, value in values), default=1) or 1
    plot_width = width - 2 * margin
    row_height = (height - 2 * margin) / max(len(values), 1)
    zero = margin + plot_width / 2
    content = []
    for index, (label, value) in enumerate(values):
        center_y = margin + (index + 0.5) * row_height
        bar_width = abs(value) / max_value * (plot_width / 2)
        bar_x = zero if value >= 0 else zero - bar_width
        anchor = "start" if value >= 0 else "end"
        content.append(
            f'<text x="{margin - 8}" y="{center_y + 4:.1f}" text-anchor="end">'
            f"{html.escape(label)}</text>"
            f'<rect class="bar" x="{bar_x:.1f}" y="{center_y - row_height * 0.3:.1f}" '
            f'width="{bar_width:.1f}" height="{row_height * 0.6:.1f}"><title>'
            f"{html.escape(label)}: {value:.3f}</title></rect>"
            f'<text x="{bar_x + bar_width + (5 if value >= 0 else -5):.1f}" '
            f'y="{center_y + 4:.1f}" text-anchor="{anchor}">{value:.3f}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Attribute correlation bar chart">'
        f'<line class="axis" x1="{zero:.1f}" y1="{margin / 2}" x2="{zero:.1f}" '
        f'y2="{height - margin / 2}"/>{"".join(content)}</svg>'
    )


def _svg_trajectories(
    result: SimulationResult, selected: Sequence[PlayerBot], *, width: int = 900, height: int = 440
) -> str:
    margin = 55
    selected_ids = {player.player_id for player in selected}
    series: dict[tuple[str, UUID], list[tuple[int, float]]] = defaultdict(list)
    for record in result.ratings:
        if record.player.player_id in selected_ids:
            series[(record.model, record.player.player_id)].append(
                (record.game_number, float(record.rating_after))
            )
    all_points = [point for points in series.values() for point in points]
    if not all_points:
        return "<p>No multiplayer rating records were produced.</p>"
    min_game = min(point[0] for point in all_points)
    max_game = max(point[0] for point in all_points)
    min_rating = min(point[1] for point in all_points) - 10
    max_rating = max(point[1] for point in all_points) + 10
    game_span = max_game - min_game or 1
    rating_span = max_rating - min_rating or 1
    colors = ("#2563eb", "#dc2626", "#059669", "#7c3aed", "#d97706", "#0891b2")

    def x(value: int) -> float:
        return margin + (value - min_game) / game_span * (width - 2 * margin)

    def y(value: float) -> float:
        return height - margin - (value - min_rating) / rating_span * (height - 2 * margin)

    paths = []
    legend = []
    for player_index, player in enumerate(selected):
        color = colors[player_index % len(colors)]
        for model in MODEL_KEYS:
            points = series[(model, player.player_id)]
            if not points:
                continue
            coordinates = " ".join(f"{x(game):.1f},{y(rating):.1f}" for game, rating in points)
            dash = ' stroke-dasharray="6 4"' if model == "time_weighted" else ""
            paths.append(
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
                f'stroke-width="1.8"{dash}><title>{html.escape(player.name)} — {model}</title>'
                "</polyline>"
            )
        legend.append(f'<span><i style="background:{color}"></i>{html.escape(player.name)}</span>')
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Selected player rating trajectories">'
        f'<line class="axis" x1="{margin}" y1="{height - margin}" x2="{width - margin}" '
        f'y2="{height - margin}"/><line class="axis" x1="{margin}" y1="{margin}" '
        f'x2="{margin}" y2="{height - margin}"/>{"".join(paths)}'
        f'<text x="{width / 2}" y="{height - 8}" text-anchor="middle">game number</text>'
        f'<text x="15" y="{height / 2}" transform="rotate(-90 15 {height / 2})" '
        'text-anchor="middle">rating</text></svg>'
        f'<div class="legend">{"".join(legend)}<span>solid: log_recent</span>'
        "<span>dashed: time_weighted</span></div>"
    )


def write_html_report(result: SimulationResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ranks = _model_ranks(result)
    log_values = [
        float(result.rating_states["log_recent"][player.player_id].rating)
        for player in result.players
    ]
    time_values = [
        float(result.rating_states["time_weighted"][player.player_id].rating)
        for player in result.players
    ]
    pearson = _pearson(log_values, time_values)
    rank_correlation = _pearson(_average_ranks(log_values), _average_ranks(time_values))
    mean_absolute_difference = statistics.fmean(
        abs(left - right) for left, right in zip(log_values, time_values, strict=True)
    )
    scatter = _svg_scatter(
        [
            (log_rating, time_rating, player.name)
            for player, log_rating, time_rating in zip(
                result.players, log_values, time_values, strict=True
            )
        ]
    )
    attribute_correlations = []
    for attribute in ATTRIBUTE_NAMES:
        attribute_values = [float(getattr(player, attribute)) for player in result.players]
        attribute_correlations.append((attribute, _pearson(attribute_values, log_values)))
    bars = _svg_bars(attribute_correlations)
    selected = sorted(
        result.players,
        key=lambda player: sum(
            float(result.rating_states[model][player.player_id].rating) for model in MODEL_KEYS
        ),
        reverse=True,
    )[:6]
    trajectories = _svg_trajectories(result, selected)
    final_rows = []
    for player in sorted(result.players, key=lambda item: ranks["log_recent"][item.player_id]):
        row = _final_player_row(result, player, ranks)
        final_rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
        )
    question_rows = []
    for value, summary in result.question_summaries.items():
        question_rows.append(
            "<tr>"
            f"<td>{value}</td><td>{summary.questions}</td><td>{summary.buzzes}</td>"
            f"<td>{summary.correct}</td><td>{summary.incorrect}</td>"
            f"<td>{summary.enlightment_rescues}</td><td>{summary.unanswered}</td>"
            f"<td>{_ratio(summary.correct, summary.questions):.1%}</td></tr>"
        )
    total_participations = sum(len(game) for game in result.games)
    multiplayer_games = sum(len(game) >= 2 for game in result.games)
    report = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SITG rating simulation</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1200px; margin: auto; padding: 2rem; line-height: 1.45; }}
h1, h2 {{ line-height: 1.15; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 1rem; }}
.card, .chart {{ border: 1px solid #8886; border-radius: 10px; padding: 1rem; }}
.card strong {{ display: block; font-size: 1.5rem; }}
.charts {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(420px,1fr)); gap: 1rem; }}
svg {{ width: 100%; height: auto; }}
svg circle, .bar {{ fill: #2563eb; opacity: .65; }}
.axis {{ stroke: currentColor; stroke-width: 1; }}
.reference {{ stroke: #888; stroke-dasharray: 5 4; }}
table {{ border-collapse: collapse; width: 100%; font-size: .88rem; }}
th, td {{ border-bottom: 1px solid #8885; padding: .4rem .55rem; text-align: right;
white-space: nowrap; }}
th:first-child, td:first-child {{ text-align: left; }}
.scroll {{ overflow: auto; max-height: 700px; }}
.legend {{ display: flex; gap: 1rem; flex-wrap: wrap; font-size: .85rem; }}
.legend i {{ display: inline-block; width: .8rem; height: .8rem; margin-right: .3rem; }}
code {{ background: #8882; padding: .12rem .3rem; border-radius: 4px; }}
</style>
</head>
<body>
<h1>SITG rating simulation</h1>
<p>Seed <code>{result.seed}</code>; {len(result.players)} bots over {result.days} simulated days
starting {result.start_date.isoformat()}, targeting {result.players_per_game} players per game.
Both confidence models replay the same game results.</p>
<div class="cards">
<div class="card"><strong>{len(result.games):,}</strong>games</div>
<div class="card"><strong>{multiplayer_games:,}</strong>rated games</div>
<div class="card"><strong>{total_participations:,}</strong>player-games</div>
<div class="card"><strong>{pearson:.4f}</strong>rating Pearson correlation</div>
<div class="card"><strong>{rank_correlation:.4f}</strong>rank correlation</div>
<div class="card"><strong>{mean_absolute_difference:.2f}</strong>
mean absolute rating difference</div>
</div>
<h2>Model comparison</h2>
<div class="charts">
<div class="chart"><h3>Final ratings</h3>{scatter}</div>
<div class="chart"><h3>log_recent rating vs attributes</h3>{bars}</div>
</div>
<div class="chart"><h3>Top combined-rating trajectories</h3>{trajectories}</div>
<h2>Question outcomes</h2>
<div class="scroll"><table><thead><tr><th>Value</th><th>Questions</th><th>Buzzes</th>
<th>Correct</th><th>Incorrect</th><th>Enlightment rescues</th><th>Unanswered</th>
<th>Answer rate</th></tr></thead><tbody>{"".join(question_rows)}</tbody></table></div>
<h2>Final player ratings</h2>
<div class="scroll"><table><thead><tr>
<th>Player</th><th>Knowledge</th><th>Speed</th><th>Confidence</th><th>Enlightment</th>
<th>Envolvment</th><th>Games</th><th>Wins</th><th>Average score</th><th>Accuracy</th>
<th>log_recent</th><th>LR rank</th><th>time_weighted</th><th>TW rank</th><th>TW − LR</th>
</tr></thead><tbody>{"".join(final_rows)}</tbody></table></div>
<h2>Data files</h2>
<ul>
<li><a href="simulation_settings.csv">simulation_settings.csv</a></li>
<li><a href="players.csv">players.csv</a></li>
<li><a href="game_results.csv">game_results.csv</a></li>
<li><a href="rating_history.csv">rating_history.csv</a></li>
<li><a href="final_ratings.csv">final_ratings.csv</a></li>
<li><a href="question_summary.csv">question_summary.csv</a></li>
</ul>
<p>The answer check uses the actual question check at the buzz tick. A wrong answer removes
that bot from the question, after which the remaining bots may buzz. Ties use SI score,
points before penalties, then correct-answer counts from 50 through 20.</p>
</body></html>
"""
    path = output_dir / "report.html"
    path.write_text(report, encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate virtual SI games and compare the implemented rating models."
    )
    parser.add_argument("--players", type=int, default=100, help="number of Player_bots")
    parser.add_argument("--days", type=int, default=365, help="simulated calendar days")
    parser.add_argument(
        "--players-per-game",
        type=int,
        default=4,
        help="target game size (remainder forms a game)",
    )
    parser.add_argument("--seed", type=int, default=20260828, help="random seed")
    parser.add_argument(
        "--start-date", type=date.fromisoformat, default=date(2025, 1, 1), help="YYYY-MM-DD"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("rating-simulation-output"), help="report directory"
    )
    return parser


def main(args: argparse.Namespace) -> Path:
    result = simulate(
        player_count=args.players,
        days=args.days,
        players_per_game=args.players_per_game,
        seed=args.seed,
        start_date=args.start_date,
    )
    write_csv_reports(result, args.output)
    return write_html_report(result, args.output)


def run() -> None:
    args = build_parser().parse_args()
    report = main(args)
    print(f"Simulated reports written to {report}")


if __name__ == "__main__":
    run()
