from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from sitg_bot.domain.rating import (
    DEFAULT_CONFIDENCE_MODEL_KEY,
    LogRecentConfidenceModel,
    PairwiseRatingInput,
    RatingHistoryEntry,
    TimeWeightedConfidenceModel,
    pairwise_rating_deltas,
)
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.storage.models import GameParticipantRecord


def _history(days_and_deltas: list[tuple[int, str]]) -> list[RatingHistoryEntry]:
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        RatingHistoryEntry(Decimal(delta), origin + timedelta(days=day - 1))
        for day, delta in days_and_deltas
    ]


def test_time_weighted_is_the_production_default() -> None:
    assert DEFAULT_CONFIDENCE_MODEL_KEY == "time_weighted"


def test_log_recent_confidence_uses_lifetime_and_recent_games() -> None:
    model = LogRecentConfidenceModel()
    history = _history([(day, "1") for day in range(1, 7)])

    result = model.calculate(
        current_rating=Decimal("1006"),
        history=history,
        as_of=datetime(2026, 1, 30, tzinfo=UTC),
    )

    assert result.confidence == Decimal("0.756770")
    assert result.k_factor == Decimal("28.6485")
    assert result.effective_games == Decimal("6.0000")


def test_time_weighted_confidence_matches_discussed_example() -> None:
    model = TimeWeightedConfidenceModel()
    history = _history([(1, "20"), (5, "-10"), (24, "15"), (25, "-5"), (29, "12"), (30, "8")])

    result = model.calculate(
        current_rating=Decimal("1040"),
        history=history,
        as_of=datetime(2026, 1, 30, tzinfo=UTC),
    )

    assert result.effective_games == Decimal("4.5000")
    assert result.time_weighted_rating == Decimal("1030.1667")
    assert result.confidence == Decimal("0.362153")
    assert result.k_factor == Decimal("34.5677")


def test_both_models_approach_k_25_after_about_sixty_active_games() -> None:
    as_of = datetime(2026, 3, 1, tzinfo=UTC)
    log_history = [
        RatingHistoryEntry(Decimal(0), as_of - timedelta(days=index % 30)) for index in range(60)
    ]
    time_history = [RatingHistoryEntry(Decimal(0), as_of) for _ in range(60)]

    log_result = LogRecentConfidenceModel().calculate(
        current_rating=Decimal(1000), history=log_history, as_of=as_of
    )
    time_result = TimeWeightedConfidenceModel().calculate(
        current_rating=Decimal(1000), history=time_history, as_of=as_of
    )

    assert Decimal("25") < log_result.k_factor < Decimal("26")
    assert Decimal("25") < time_result.k_factor < Decimal("25.1")


def test_pairwise_elo_averages_opponents_and_treats_shared_place_as_draw() -> None:
    players = [
        PairwiseRatingInput(UUID(int=1), Decimal(1100), Decimal(1), Decimal(40)),
        PairwiseRatingInput(UUID(int=2), Decimal(1000), Decimal("2.5"), Decimal(40)),
        PairwiseRatingInput(UUID(int=3), Decimal(1000), Decimal("2.5"), Decimal(40)),
        PairwiseRatingInput(UUID(int=4), Decimal(900), Decimal(4), Decimal(40)),
    ]

    deltas = pairwise_rating_deltas(players)

    assert deltas[UUID(int=2)] == deltas[UUID(int=3)] == Decimal("0.0000")
    assert deltas[UUID(int=1)] == Decimal("12.8016")
    assert deltas[UUID(int=4)] == Decimal("-12.8016")


def test_pairwise_elo_applies_ruleset_tournament_weight() -> None:
    players = [
        PairwiseRatingInput(UUID(int=1), Decimal(1000), Decimal(1), Decimal(40)),
        PairwiseRatingInput(UUID(int=2), Decimal(1000), Decimal(2), Decimal(40)),
    ]

    assert pairwise_rating_deltas(players, weight=Decimal("0.5")) == {
        UUID(int=1): Decimal("10.0000"),
        UUID(int=2): Decimal("-10.0000"),
    }
    with pytest.raises(ValueError, match="finite positive"):
        pairwise_rating_deltas(players, weight=Decimal(0))


def test_equal_non_random_ranking_metrics_split_places() -> None:
    participants = [GameParticipantRecord(id=UUID(int=index), seat=index) for index in range(1, 5)]
    metrics = {
        UUID(int=1): (Decimal(100), 2),
        UUID(int=2): (Decimal(50), 1),
        UUID(int=3): (Decimal(50), 1),
        UUID(int=4): (Decimal(0), 0),
    }

    PersistentGameService._assign_shared_places(participants, metrics)

    assert [item.final_place for item in participants] == [
        Decimal(1),
        Decimal("2.5"),
        Decimal("2.5"),
        Decimal(4),
    ]
