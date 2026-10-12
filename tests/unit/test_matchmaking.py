from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from uuid import UUID

import pytest

from sitg_bot.domain.game_rulesets import DEFAULT_RULESETS
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.tournaments import (
    TournamentContext,
    TournamentService,
    hybrid_matchmaking_supported,
    normalize_tournament_policies,
)


@pytest.mark.parametrize(
    ("theme_count", "packet_counts", "expected"),
    [("max", (), 1), ("max", (3, 5), 8), (6, (), 6)],
)
async def test_lobby_theme_count_resolves_maximum(theme_count, packet_counts, expected) -> None:
    service = object.__new__(InvitationMatchmakingService)
    service._packet_play_unit_count = AsyncMock(side_effect=packet_counts)
    session = object()
    context = object()
    lobby = SimpleNamespace(settings={
        "theme_count": theme_count, "minimum_players": 2, "maximum_players": 4,
    })
    selected = [
        SimpleNamespace(packet_version_id=UUID(int=index))
        for index in range(1, len(packet_counts) + 1)
    ]

    settings = await service._lobby_parameters(
        session, lobby, selected, context, DEFAULT_RULESETS.get("si", 1),
    )

    assert settings.to_dict()["theme_count"] == expected
    assert lobby.settings["theme_count"] == theme_count
    assert service._packet_play_unit_count.await_args_list == [
        call(session, item.packet_version_id, context) for item in selected
    ]


def test_matchmaking_tolerance_widens_in_steps_and_honors_optional_cap() -> None:
    service = object.__new__(InvitationMatchmakingService)
    service.tolerance_step_interval = timedelta(seconds=30)
    service.maximum_tolerance = 225
    started_at = datetime(2026, 8, 20, tzinfo=UTC)

    assert service._tolerance(started_at, started_at, 100, 50) == 100
    assert service._tolerance(started_at, started_at + timedelta(seconds=29), 100, 50) == 100
    assert service._tolerance(started_at, started_at + timedelta(seconds=30), 100, 50) == 150
    assert service._tolerance(started_at, started_at + timedelta(minutes=5), 100, 50) == 225
    service.maximum_tolerance = None
    assert service._tolerance(started_at, started_at + timedelta(minutes=5), 100, 50) == 600


def test_matchmaking_tolerance_is_zero_before_search_starts() -> None:
    service = object.__new__(InvitationMatchmakingService)
    service.tolerance_step_interval = timedelta(seconds=30)
    service.maximum_tolerance = 500

    assert service._tolerance(None, datetime.now(UTC), 100, 50) == 0


def test_merge_direction_prefers_oldest_lobby_that_has_capacity() -> None:
    started = datetime(2026, 8, 20, tzinfo=UTC)
    oldest = SimpleNamespace(
        id=UUID(int=1), search_started_at=started, created_at=started, max_players=2
    )
    newer = SimpleNamespace(
        id=UUID(int=2),
        search_started_at=started + timedelta(seconds=1),
        created_at=started,
        max_players=4,
    )

    assert InvitationMatchmakingService._merge_direction(oldest, newer, 2) == (
        oldest,
        newer,
    )
    assert InvitationMatchmakingService._merge_direction(oldest, newer, 3) == (
        newer,
        oldest,
    )
    assert InvitationMatchmakingService._merge_direction(oldest, newer, 5) == (
        None,
        None,
    )


def test_hybrid_matchmaking_requires_type_support_and_tournament_opt_in() -> None:
    assert hybrid_matchmaking_supported({"supports_hybrid_matchmaking": True})
    assert not hybrid_matchmaking_supported({"supports_hybrid_matchmaking": False})
    assert normalize_tournament_policies({"supports_hybrid_matchmaking": True}, None) == {
        "library_viewing_rule_default": "after-play",
        "auto_approve_registrations": False,
        "member_uploads": False,
        "hybrid_matchmaking_enabled": False,
        "ruleset_rating_weight": 1,
        "observing": "forbidden",
        "packets_per_lobby": "one",
        "packets_discoverable_by_default": True,
        "packets_playable_by_default": False,
        "packets_readable_by_default": False,
        "packets_released_by_default": False,
        "packet_notifications_enabled": True,
    }


@pytest.mark.parametrize("value", ("unlimited", "burnt-only", "forbidden"))
def test_tournament_observing_policy_accepts_supported_modes(value: str) -> None:
    policies = normalize_tournament_policies({}, {"observing": value})

    assert policies["observing"] == value


@pytest.mark.parametrize("value", ("one", "any"))
def test_tournament_packets_per_lobby_policy_accepts_supported_modes(value: str) -> None:
    policies = normalize_tournament_policies({}, {"packets_per_lobby": value})

    assert policies["packets_per_lobby"] == value
    descriptors = {
        item.name: item for item in TournamentService._manager_policy_descriptors(policies)
    }
    assert descriptors["packets_per_lobby"].value == value
    assert descriptors["packets_per_lobby"].value_type == "enum"
    assert set(descriptors["packets_per_lobby"].options) == {"one", "any"}


@pytest.mark.parametrize("value", (None, "single", "unlimited", 1, [], True))
def test_tournament_packets_per_lobby_policy_rejects_unknown_mode(value: object) -> None:
    with pytest.raises(ValueError, match="packets_per_lobby must be one or any"):
        normalize_tournament_policies({}, {"packets_per_lobby": value})


@pytest.mark.parametrize("value", ("friends-only", True, ["unlimited"]))
def test_tournament_observing_policy_rejects_unknown_mode(value: object) -> None:
    with pytest.raises(ValueError, match="unlimited, burnt-only, or forbidden"):
        normalize_tournament_policies({}, {"observing": value})
    assert (
        normalize_tournament_policies(
            {"supports_hybrid_matchmaking": True}, {"hybrid_matchmaking_enabled": True}
        )["hybrid_matchmaking_enabled"]
        is True
    )

    with pytest.raises(ValueError, match="does not support"):
        normalize_tournament_policies(
            {"supports_hybrid_matchmaking": False},
            {"hybrid_matchmaking_enabled": True},
        )
    with pytest.raises(ValueError, match="must be a boolean"):
        normalize_tournament_policies(
            {"supports_hybrid_matchmaking": True},
            {"hybrid_matchmaking_enabled": "yes"},
        )
    assert (
        normalize_tournament_policies(
            {"supports_hybrid_matchmaking": True}, {"ruleset_rating_weight": 0.5}
        )["ruleset_rating_weight"]
        == 0.5
    )
    with pytest.raises(ValueError, match="finite and positive"):
        normalize_tournament_policies(
            {"supports_hybrid_matchmaking": True}, {"ruleset_rating_weight": 0}
        )


def test_context_reports_effective_hybrid_matchmaking_availability() -> None:
    context = TournamentContext(
        UUID(int=1),
        UUID(int=2),
        "ladder",
        UUID(int=3),
        "si",
        1,
        UUID(int=4),
        1,
        SimpleNamespace(),
        frozenset(),
        {"hybrid_matchmaking_enabled": True},
        {"supports_hybrid_matchmaking": True},
        True,
    )

    assert context.type_supports_hybrid_matchmaking
    assert context.hybrid_matchmaking_enabled
