from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import UUID

RATING_QUANTUM = Decimal("0.0001")
CONFIDENCE_QUANTUM = Decimal("0.000001")
K_QUANTUM = Decimal("0.0001")
STARTING_RATING = Decimal("1000")
K_MIN = Decimal("25")
K_MULTIPLIER = Decimal("15")
K_MAX = K_MIN + K_MULTIPLIER
DEFAULT_CONFIDENCE_MODEL_KEY = "time_weighted"


@dataclass(frozen=True, slots=True)
class RatingHistoryEntry:
    delta: Decimal
    played_at: datetime


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    confidence: Decimal
    k_factor: Decimal
    effective_games: Decimal
    time_weighted_rating: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PairwiseRatingInput:
    player_id: UUID
    rating: Decimal
    place: Decimal
    k_factor: Decimal


class ConfidenceModel(Protocol):
    key: str

    def calculate(
        self,
        *,
        current_rating: Decimal,
        history: Sequence[RatingHistoryEntry],
        as_of: datetime,
    ) -> ConfidenceResult: ...


def _result(
    confidence: float,
    effective_games: float,
    *,
    time_weighted_rating: Decimal | None = None,
) -> ConfidenceResult:
    bounded = min(1.0, max(0.0, confidence))
    confidence_value = Decimal(str(bounded)).quantize(CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP)
    k_factor = (K_MIN + K_MULTIPLIER * (Decimal(1) - confidence_value)).quantize(
        K_QUANTUM, rounding=ROUND_HALF_UP
    )
    return ConfidenceResult(
        confidence=confidence_value,
        k_factor=k_factor,
        effective_games=Decimal(str(effective_games)).quantize(
            RATING_QUANTUM, rounding=ROUND_HALF_UP
        ),
        time_weighted_rating=time_weighted_rating,
    )


class LogRecentConfidenceModel:
    """Diminishing lifetime evidence plus activity in the preceding 30 days."""

    key = "log_recent"
    recent_window = timedelta(days=30)

    def calculate(
        self,
        *,
        current_rating: Decimal,
        history: Sequence[RatingHistoryEntry],
        as_of: datetime,
    ) -> ConfidenceResult:
        del current_rating
        reference = max(
            _aware_utc(as_of),
            max((_aware_utc(item.played_at) for item in history), default=_aware_utc(as_of)),
        )
        cutoff = reference - self.recent_window
        total = len(history)
        recent = sum(1 for item in history if cutoff <= _aware_utc(item.played_at) <= reference)
        evidence = math.log1p(total) / math.log(21) + math.sqrt(recent / 10)
        confidence = 1 - math.exp(-evidence)
        return _result(confidence, float(total))


class TimeWeightedConfidenceModel:
    """Confidence from recency-weighted evidence and recent/full-history stability."""

    key = "time_weighted"
    decay_threshold_days = 5
    evidence_scale = 10
    drift_scale = Decimal("400")

    def calculate(
        self,
        *,
        current_rating: Decimal,
        history: Sequence[RatingHistoryEntry],
        as_of: datetime,
    ) -> ConfidenceResult:
        if not history:
            return _result(0, 0, time_weighted_rating=STARTING_RATING)

        reference_day = _aware_utc(as_of).date()
        earliest_day = min(_aware_utc(item.played_at).date() for item in history)
        latest_day = max(
            reference_day,
            max(_aware_utc(item.played_at).date() for item in history),
        )
        span = Decimal(1 + (latest_day - earliest_day).days)
        effective_games = Decimal(0)
        weighted_delta = Decimal(0)
        for item in history:
            played_day = _aware_utc(item.played_at).date()
            fully_weighted_until = played_day + timedelta(days=self.decay_threshold_days)
            capped_day = min(latest_day, fully_weighted_until)
            weight = Decimal(1 + (capped_day - earliest_day).days) / span
            effective_games += weight
            weighted_delta += item.delta * weight

        time_weighted_rating = (STARTING_RATING + weighted_delta).quantize(
            RATING_QUANTUM, rounding=ROUND_HALF_UP
        )
        evidence = 1 - math.exp(-float(effective_games) / self.evidence_scale)
        drift = (current_rating - time_weighted_rating) / self.drift_scale
        stability = Decimal(1) / (Decimal(1) + drift * drift)
        return _result(
            evidence * float(stability),
            float(effective_games),
            time_weighted_rating=time_weighted_rating,
        )


CONFIDENCE_MODELS: dict[str, ConfidenceModel] = {
    model.key: model for model in (LogRecentConfidenceModel(), TimeWeightedConfidenceModel())
}


def confidence_model(key: str) -> ConfidenceModel:
    try:
        return CONFIDENCE_MODELS[key]
    except KeyError as error:
        raise ValueError(f"Unknown rating confidence model: {key}") from error


def expected_score(rating: Decimal, opponent_rating: Decimal) -> Decimal:
    exponent = float((opponent_rating - rating) / Decimal(400))
    return Decimal(str(1 / (1 + 10**exponent)))


def pairwise_rating_deltas(
    participants: Sequence[PairwiseRatingInput],
    *,
    weight: Decimal = Decimal(1),
) -> dict[UUID, Decimal]:
    if not weight.is_finite() or weight <= 0:
        raise ValueError("Rating weight must be a finite positive number")
    if len(participants) < 2:
        return {item.player_id: Decimal(0) for item in participants}

    divisor = Decimal(len(participants) - 1)
    deltas: dict[UUID, Decimal] = {}
    for player in participants:
        residual = Decimal(0)
        for opponent in participants:
            if opponent.player_id == player.player_id:
                continue
            if player.place < opponent.place:
                actual = Decimal(1)
            elif player.place > opponent.place:
                actual = Decimal(0)
            else:
                actual = Decimal("0.5")
            residual += actual - expected_score(player.rating, opponent.rating)
        deltas[player.player_id] = (player.k_factor * weight * residual / divisor).quantize(
            RATING_QUANTUM, rounding=ROUND_HALF_UP
        )
    return deltas


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Rating timestamps must include a timezone")
    return value.astimezone(UTC)
