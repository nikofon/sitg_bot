from dataclasses import dataclass
from math import sqrt
from statistics import mean
from uuid import UUID

STARTING_REPUTATION = 90
MINIMUM_REPUTATION = 0
MAXIMUM_REPUTATION = 100
REPUTATION_VOTE_DELTA = 1
TOXICITY_REPORT_REPUTATION_DELTA = -10
CHEATING_REPORT_SUSPICION_DELTA = 10
MINIMUM_RATED_GAMES = 20
REPORT_TYPES = frozenset({"cheating", "toxicity"})


def apply_reputation_delta(reputation: int, delta: int) -> tuple[int, int]:
    """Return the capped reputation and the delta that actually took effect."""
    updated = min(MAXIMUM_REPUTATION, max(MINIMUM_REPUTATION, reputation + delta))
    return updated, updated - reputation


@dataclass(frozen=True, slots=True)
class SIObservation:
    player_id: UUID
    game_id: UUID
    question_id: UUID
    rating: float
    value: int
    correct: bool
    buzzed: bool
    revealed_fraction: float | None
    buzz_time_remaining_fraction: float | None


@dataclass(frozen=True, slots=True)
class SISuspicionResult:
    status: str
    raised_signals: tuple[str, ...]
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class SignalCount:
    positive: int
    eligible: int


@dataclass(frozen=True, slots=True)
class SISuspicionCounts:
    very_early_buzz: SignalCount
    very_late_buzz: SignalCount
    high_value_accuracy: SignalCount
    rare_question_accuracy: SignalCount


@dataclass(frozen=True, slots=True)
class ContinuousSignal:
    total: float
    total_squares: float
    observations: int


def evaluate_si_suspicion_counts(
    player: SISuspicionCounts,
    cohort: SISuspicionCounts,
    *,
    cohort_players: int,
    player_early_timing: ContinuousSignal | None = None,
    cohort_early_timing: ContinuousSignal | None = None,
    minimum_baseline_observations: int = 100,
    minimum_cohort_players: int = 10,
    minimum_signal_observations: int = 10,
    rate_margin: float = 0.15,
    z_threshold: float = 2.5,
) -> SISuspicionResult:
    """Evaluate compact counters without loading raw gameplay history."""
    signals: list[str] = []
    metrics: dict[str, object] = {}
    baseline_unreliable = cohort_players < minimum_cohort_players
    if player_early_timing is not None and cohort_early_timing is not None:
        if cohort_early_timing.observations < minimum_baseline_observations:
            baseline_unreliable = True
            metrics["very_early_buzz"] = {
                "status": "insufficient_baseline_data",
                "player_observations": player_early_timing.observations,
                "cohort_observations": cohort_early_timing.observations,
            }
        elif player_early_timing.observations < minimum_signal_observations:
            metrics["very_early_buzz"] = {
                "status": "insufficient_player_data",
                "player_observations": player_early_timing.observations,
                "cohort_observations": cohort_early_timing.observations,
            }
        else:
            player_mean = player_early_timing.total / player_early_timing.observations
            cohort_mean = cohort_early_timing.total / cohort_early_timing.observations
            cohort_mean_square = (
                cohort_early_timing.total_squares / cohort_early_timing.observations
            )
            cohort_variance = max(cohort_mean_square - cohort_mean**2, 0.01)
            standard_error = sqrt(cohort_variance / player_early_timing.observations)
            z_score = (cohort_mean - player_mean) / standard_error
            raised = player_mean <= cohort_mean - rate_margin and z_score >= z_threshold
            metrics["very_early_buzz"] = {
                "status": "raised" if raised else "clear",
                "player_average_revealed_fraction": player_mean,
                "cohort_average_revealed_fraction": cohort_mean,
                "player_observations": player_early_timing.observations,
                "cohort_observations": cohort_early_timing.observations,
                "z_score": z_score,
            }
            if raised:
                signals.append("very_early_buzz")
    else:
        metrics["very_early_buzz"] = {
            "status": "insufficient_baseline_data",
            "player_observations": player.very_early_buzz.eligible,
            "cohort_observations": cohort.very_early_buzz.eligible,
        }
        baseline_unreliable = True
    for name in (
        "very_late_buzz",
        "high_value_accuracy",
        "rare_question_accuracy",
    ):
        player_count = getattr(player, name)
        cohort_count = getattr(cohort, name)
        if cohort_count.eligible < minimum_baseline_observations:
            baseline_unreliable = True
            metrics[name] = {
                "status": "insufficient_baseline_data",
                "player_observations": player_count.eligible,
                "cohort_observations": cohort_count.eligible,
            }
            continue
        if player_count.eligible < minimum_signal_observations:
            metrics[name] = {
                "status": "insufficient_player_data",
                "player_observations": player_count.eligible,
                "cohort_observations": cohort_count.eligible,
            }
            continue
        player_rate = player_count.positive / player_count.eligible
        cohort_rate = cohort_count.positive / cohort_count.eligible
        standard_error = sqrt(max(cohort_rate * (1 - cohort_rate), 0.01) / player_count.eligible)
        z_score = (player_rate - cohort_rate) / standard_error
        raised = player_rate >= cohort_rate + rate_margin and z_score >= z_threshold
        metrics[name] = {
            "status": "raised" if raised else "clear",
            "player_rate": player_rate,
            "cohort_rate": cohort_rate,
            "player_observations": player_count.eligible,
            "cohort_observations": cohort_count.eligible,
            "z_score": z_score,
        }
        if raised:
            signals.append(name)
    return SISuspicionResult(
        "postponed" if baseline_unreliable else "evaluated",
        tuple(signals),
        {"cohort_players": cohort_players, "signals": metrics},
    )


def evaluate_si_suspicion(
    player_id: UUID,
    period_observations: list[SIObservation],
    baseline_observations: list[SIObservation],
    *,
    minimum_baseline_observations: int = 100,
    minimum_cohort_players: int = 10,
    minimum_signal_observations: int = 10,
    rating_band: float = 200,
    rate_margin: float = 0.15,
    z_threshold: float = 2.5,
) -> SISuspicionResult:
    """Compare one SI player's period with similarly rated players.

    The evaluator deliberately returns ``postponed`` when a comparison would be noisy.
    Each raised signal contributes one suspicion point in the persistence service.
    """
    if not period_observations:
        return SISuspicionResult("postponed", (), {"reason": "no_period_observations"})

    target_rating = mean(item.rating for item in period_observations)
    cohort = [
        item
        for item in baseline_observations
        if item.player_id != player_id and abs(item.rating - target_rating) <= rating_band
    ]
    cohort_players = {item.player_id for item in cohort}
    if len(cohort) < minimum_baseline_observations or len(cohort_players) < minimum_cohort_players:
        return SISuspicionResult(
            "postponed",
            (),
            {
                "reason": "insufficient_cohort_data",
                "cohort_observations": len(cohort),
                "cohort_players": len(cohort_players),
            },
        )

    question_baseline = [item for item in baseline_observations if item.player_id != player_id]
    question_counts: dict[UUID, int] = {}
    question_correct: dict[UUID, int] = {}
    for item in question_baseline:
        question_counts[item.question_id] = question_counts.get(item.question_id, 0) + 1
        question_correct[item.question_id] = question_correct.get(item.question_id, 0) + int(
            item.correct
        )
    question_rates = {
        question_id: question_correct[question_id] / count
        for question_id, count in question_counts.items()
    }
    reliable_rates = sorted(
        question_rates[question_id]
        for question_id, count in question_counts.items()
        if count >= minimum_signal_observations
    )
    rare_cutoff = (
        reliable_rates[max(0, int(len(reliable_rates) * 0.2) - 1)] if reliable_rates else None
    )

    def is_slow(item: SIObservation) -> bool:
        return (
            item.buzzed
            and item.buzz_time_remaining_fraction is not None
            and item.buzz_time_remaining_fraction <= 0.1
        )

    def is_high_value(item: SIObservation) -> bool:
        return item.value in {40, 50}

    def is_rare(item: SIObservation) -> bool:
        return (
            rare_cutoff is not None
            and question_counts.get(item.question_id, 0) >= minimum_signal_observations
            and question_rates[item.question_id] <= rare_cutoff
        )

    specifications = (
        ("very_late_buzz", lambda item: item.buzzed, is_slow),
        ("high_value_accuracy", is_high_value, lambda item: item.correct),
        ("rare_question_accuracy", is_rare, lambda item: item.correct),
    )
    signals: list[str] = []
    metrics: dict[str, object] = {}
    baseline_unreliable = False
    player_early = [
        item.revealed_fraction
        for item in period_observations
        if item.buzzed and item.revealed_fraction is not None
    ]
    cohort_early = [
        item.revealed_fraction
        for item in cohort
        if item.buzzed and item.revealed_fraction is not None
    ]
    if len(cohort_early) < minimum_baseline_observations:
        metrics["very_early_buzz"] = {
            "status": "insufficient_baseline_data",
            "player_observations": len(player_early),
            "cohort_observations": len(cohort_early),
        }
        baseline_unreliable = True
    elif len(player_early) < minimum_signal_observations:
        metrics["very_early_buzz"] = {
            "status": "insufficient_player_data",
            "player_observations": len(player_early),
            "cohort_observations": len(cohort_early),
        }
    else:
        player_average = mean(player_early)
        cohort_average = mean(cohort_early)
        cohort_variance = max(
            mean(value * value for value in cohort_early) - cohort_average**2,
            0.01,
        )
        z_score = (cohort_average - player_average) / sqrt(cohort_variance / len(player_early))
        raised = player_average <= cohort_average - rate_margin and z_score >= z_threshold
        metrics["very_early_buzz"] = {
            "status": "raised" if raised else "clear",
            "player_average_revealed_fraction": player_average,
            "cohort_average_revealed_fraction": cohort_average,
            "player_observations": len(player_early),
            "cohort_observations": len(cohort_early),
            "z_score": z_score,
        }
        if raised:
            signals.append("very_early_buzz")
    for name, eligible, positive in specifications:
        if name == "rare_question_accuracy" and rare_cutoff is None:
            metrics[name] = {"status": "insufficient_baseline_data"}
            baseline_unreliable = True
            continue
        player_items = [item for item in period_observations if eligible(item)]
        cohort_items = [item for item in cohort if eligible(item)]
        if len(cohort_items) < minimum_baseline_observations:
            metrics[name] = {
                "status": "insufficient_baseline_data",
                "player_observations": len(player_items),
                "cohort_observations": len(cohort_items),
            }
            baseline_unreliable = True
            continue
        if len(player_items) < minimum_signal_observations:
            metrics[name] = {
                "status": "insufficient_player_data",
                "player_observations": len(player_items),
                "cohort_observations": len(cohort_items),
            }
            continue
        player_rate = sum(positive(item) for item in player_items) / len(player_items)
        cohort_rate = sum(positive(item) for item in cohort_items) / len(cohort_items)
        standard_error = sqrt(max(cohort_rate * (1 - cohort_rate), 0.01) / len(player_items))
        z_score = (player_rate - cohort_rate) / standard_error
        raised = player_rate >= cohort_rate + rate_margin and z_score >= z_threshold
        metrics[name] = {
            "status": "raised" if raised else "clear",
            "player_rate": player_rate,
            "cohort_rate": cohort_rate,
            "player_observations": len(player_items),
            "cohort_observations": len(cohort_items),
            "z_score": z_score,
        }
        if raised:
            signals.append(name)

    return SISuspicionResult(
        "postponed" if baseline_unreliable else "evaluated",
        tuple(signals),
        {
            "target_rating": target_rating,
            "cohort_observations": len(cohort),
            "cohort_players": len(cohort_players),
            "rare_question_correct_rate_cutoff": rare_cutoff,
            "signals": metrics,
        },
    )
