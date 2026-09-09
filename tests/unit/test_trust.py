from datetime import UTC, datetime
from uuid import UUID

import pytest

from sitg_bot.domain.trust import (
    ContinuousSignal,
    SignalCount,
    SIObservation,
    SISuspicionCounts,
    apply_reputation_delta,
    evaluate_si_suspicion,
    evaluate_si_suspicion_counts,
)
from sitg_bot.services.trust import TrustService


def _observation(
    player: int,
    game: int,
    *,
    correct: bool,
    revealed_fraction: float,
    remaining_fraction: float,
) -> SIObservation:
    return SIObservation(
        player_id=UUID(int=player),
        game_id=UUID(int=game),
        question_id=UUID(int=999),
        rating=1000,
        value=50,
        correct=correct,
        buzzed=True,
        revealed_fraction=revealed_fraction,
        buzz_time_remaining_fraction=remaining_fraction,
    )


def test_reputation_delta_is_capped_and_reports_applied_delta() -> None:
    assert apply_reputation_delta(90, 1) == (91, 1)
    assert apply_reputation_delta(100, 1) == (100, 0)
    assert apply_reputation_delta(3, -10) == (0, -3)


def test_suspicion_period_is_the_previous_completed_utc_week() -> None:
    start, end = TrustService.completed_week(datetime(2026, 9, 1, 12, tzinfo=UTC))

    assert start == datetime(2026, 8, 24, tzinfo=UTC)
    assert end == datetime(2026, 8, 31, tzinfo=UTC)


def test_compact_si_counts_raise_supported_outlier_signals() -> None:
    player = SISuspicionCounts(
        SignalCount(10, 10),
        SignalCount(10, 10),
        SignalCount(10, 10),
        SignalCount(10, 10),
    )
    cohort = SISuspicionCounts(
        SignalCount(0, 100),
        SignalCount(0, 100),
        SignalCount(0, 100),
        SignalCount(0, 100),
    )

    result = evaluate_si_suspicion_counts(
        player,
        cohort,
        cohort_players=10,
        player_early_timing=ContinuousSignal(1.0, 0.1, 10),
        cohort_early_timing=ContinuousSignal(80.0, 64.0, 100),
    )

    assert result.status == "evaluated"
    assert set(result.raised_signals) == {
        "very_early_buzz",
        "very_late_buzz",
        "high_value_accuracy",
        "rare_question_accuracy",
    }


def test_early_buzz_signal_compares_average_reveal_fraction() -> None:
    empty_rates = SISuspicionCounts(
        SignalCount(0, 10),
        SignalCount(0, 100),
        SignalCount(0, 100),
        SignalCount(0, 100),
    )

    result = evaluate_si_suspicion_counts(
        empty_rates,
        empty_rates,
        cohort_players=10,
        player_early_timing=ContinuousSignal(3.0, 0.9, 10),
        cohort_early_timing=ContinuousSignal(60.0, 38.0, 100),
    )

    assert "very_early_buzz" in result.raised_signals
    early = result.details["signals"]["very_early_buzz"]
    assert early["player_average_revealed_fraction"] == pytest.approx(0.3)
    assert early["cohort_average_revealed_fraction"] == pytest.approx(0.6)


def test_incremental_worker_rejects_unbounded_zero_sized_batches() -> None:
    with pytest.raises(ValueError, match="batch sizes"):
        TrustService(object(), evaluation_batch_size=0)  # type: ignore[arg-type]


def test_si_evaluation_postpones_without_a_reliable_rating_cohort() -> None:
    player_id = UUID(int=1)
    observations = [
        _observation(1, index + 1, correct=True, revealed_fraction=0.1, remaining_fraction=0.05)
        for index in range(10)
    ]

    result = evaluate_si_suspicion(player_id, observations, observations)

    assert result.status == "postponed"
    assert result.details["reason"] == "insufficient_cohort_data"


def test_si_evaluation_raises_all_supported_outlier_signals() -> None:
    target = [
        _observation(1, index + 1, correct=True, revealed_fraction=0.1, remaining_fraction=0.05)
        for index in range(10)
    ]
    cohort = [
        _observation(
            player,
            player * 100 + index,
            correct=False,
            revealed_fraction=1,
            remaining_fraction=1,
        )
        for player in range(2, 12)
        for index in range(10)
    ]

    result = evaluate_si_suspicion(UUID(int=1), target, target + cohort)

    assert result.status == "evaluated"
    assert set(result.raised_signals) == {
        "very_early_buzz",
        "very_late_buzz",
        "high_value_accuracy",
        "rare_question_accuracy",
    }
