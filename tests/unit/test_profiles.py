from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sitg_bot.services.profiles import (
    PLACEMENT_KINDS,
    canonical_question_values,
    identity_projection,
    placement_summary,
)
from sitg_bot.storage.models import PlayerRecord


def player_record() -> PlayerRecord:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    return PlayerRecord(
        id=UUID(int=1),
        telegram_user_id=42,
        real_name="Иван Иванов",
        public_nickname="Player One",
        telegram_username="player_one",
        telegram_public=False,
        preferred_locale="ru",
        registration_step="complete",
        registration_completed_at=now,
        profile_version=0,
        profile_updated_at=now,
        status="active",
    )


def test_placement_summary_buckets_ties_and_low_places() -> None:
    summary = placement_summary(
        [Decimal(value) for value in ("1", "2", "2.5", "2.5", "4", "5")]
    )

    assert [item["kind"] for item in summary] == list(PLACEMENT_KINDS)
    counts = {item["kind"]: item["count"] for item in summary}
    assert counts == {
        "place_1": 1,
        "place_2": 1,
        "place_3": 0,
        "place_4": 1,
        "draw": 2,
        "below_4": 1,
    }
    percents = {item["kind"]: item["percent"] for item in summary}
    assert percents["draw"] == 33.3
    assert sum(item["percent"] for item in summary) >= 99.5


def test_placement_summary_is_zeroed_without_games() -> None:
    summary = placement_summary([])

    assert all(item["count"] == 0 and item["percent"] == 0.0 for item in summary)


def test_canonical_question_values_maps_any_tournament_scale() -> None:
    standard = canonical_question_values([10, 20, 30, 40, 50])
    assert standard == {10: 10, 20: 20, 30: 30, 40: 40, 50: 50}

    scaled = canonical_question_values([1, 2, 3, 4, 5])
    assert scaled == {1: 10, 2: 20, 3: 30, 4: 40, 5: 50}


def test_identity_projection_hides_private_fields_from_other_viewers() -> None:
    player = player_record()

    public = identity_projection(player, privileged=False)
    assert public["nickname"] == "Player One"
    assert "real_name" not in public
    assert "telegram_username" not in public
    assert public["viewer_privileged"] is False

    player.telegram_public = True
    shared = identity_projection(player, privileged=False)
    assert shared["telegram_username"] == "player_one"
    assert "real_name" not in shared


def test_identity_projection_shows_sensitive_fields_to_privileged_viewers() -> None:
    player = player_record()

    privileged = identity_projection(player, privileged=True)
    assert privileged["real_name"] == "Иван Иванов"
    assert privileged["telegram_username"] == "player_one"
    assert privileged["telegram_public"] is False
    assert privileged["viewer_privileged"] is True
