from datetime import date
from itertools import pairwise
from random import Random

from sitg_bot.rating_simulation import (
    FINAL_CHECK_RANGES,
    MODEL_KEYS,
    QUESTION_VALUES,
    TICKS_PER_QUESTION,
    PlayerBot,
    VirtualQuestion,
    generate_players,
    generate_question,
    group_daily_players,
    perceived_check,
    simulate,
    write_csv_reports,
    write_html_report,
)


def test_generated_players_have_d20_attributes_and_are_reproducible() -> None:
    first = generate_players(20, Random(7))
    second = generate_players(20, Random(7))

    assert first == second
    assert all(
        1 <= value <= 20
        for player in first
        for value in (
            player.knowledge,
            player.speed,
            player.confidence,
            player.enlightment,
            player.envolvment,
        )
    )


def test_question_curve_and_overlapping_difficulty_bands() -> None:
    rng = Random(19)
    questions = [generate_question(rng, value) for value in QUESTION_VALUES for _ in range(20)]

    assert all(len(question.checks) == TICKS_PER_QUESTION for question in questions)
    assert all(question.checks[0] == 150 for question in questions)
    assert all(
        all(left >= right for left, right in pairwise(question.checks)) for question in questions
    )
    assert all(
        FINAL_CHECK_RANGES[question.value][0]
        <= question.checks[-1]
        <= FINAL_CHECK_RANGES[question.value][1]
        for question in questions
    )
    assert FINAL_CHECK_RANGES[10][1] < FINAL_CHECK_RANGES[50][0]
    assert FINAL_CHECK_RANGES[10][1] > FINAL_CHECK_RANGES[20][0]


def test_speed_ten_delays_perception_by_ten_ticks() -> None:
    checks = tuple(range(150, 120, -1))
    question = VirtualQuestion(10, checks)

    assert [perceived_check(question, tick, 10) for tick in range(13)] == [
        150,
        150,
        150,
        150,
        150,
        150,
        150,
        150,
        150,
        150,
        150,
        149,
        148,
    ]
    assert perceived_check(question, 5, 20) == 145


def test_daily_remainder_creates_an_additional_game() -> None:
    class AllEligibleRandom(Random):
        def randint(self, _start: int, _end: int) -> int:
            return 1

        def shuffle(self, _values: list[PlayerBot]) -> None:
            return None

    players = [
        PlayerBot(index, generate_players(index, Random(index))[-1].player_id, 10, 10, 10, 10, 20)
        for index in range(1, 36)
    ]

    groups = group_daily_players(players, 4, AllEligibleRandom())

    assert [len(group) for group in groups] == [4] * 8 + [3]


def test_simulation_replays_games_through_both_models_and_writes_reports(tmp_path) -> None:
    result = simulate(
        player_count=12,
        days=4,
        players_per_game=4,
        seed=42,
        start_date=date(2025, 1, 1),
    )

    assert result.games
    assert {record.model for record in result.ratings} == set(MODEL_KEYS)
    assert len(result.ratings) == 2 * sum(len(game) for game in result.games if len(game) >= 2)

    write_csv_reports(result, tmp_path)
    report = write_html_report(result, tmp_path)

    assert report.exists()
    assert "Model comparison" in report.read_text(encoding="utf-8")
    assert {path.name for path in tmp_path.iterdir()} == {
        "simulation_settings.csv",
        "players.csv",
        "game_results.csv",
        "rating_history.csv",
        "final_ratings.csv",
        "question_summary.csv",
        "report.html",
    }
