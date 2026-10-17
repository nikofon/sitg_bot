import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest

from sitg_bot.domain.game_rulesets import DEFAULT_RULESETS, PlayUnit
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.services.matchmaking import InvitationMatchmakingService, LobbyReadinessError
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.tournaments import (
    TournamentContext,
    TournamentService,
    hybrid_matchmaking_supported,
    normalize_tournament_policies,
)
from sitg_bot.storage.models import (
    LogicalPacketRecord,
    PacketVersionRecord,
    PlayerPacketBlockRecord,
    PregameLobbyPacketRecord,
    PregameLobbyRecord,
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


def _privacy_settings_service():
    lobby = SimpleNamespace(
        id=UUID(int=1), tournament_id=UUID(int=2), tournament_policy_version_id=UUID(int=3),
        version=1, settings=GameSettings().to_dict(), max_players=4,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(player_mutable_parameters=["theme_count"])),
        flush=AsyncMock(),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    service = object.__new__(InvitationMatchmakingService)
    service.database = SimpleNamespace(transaction=transaction)
    service.tournaments = SimpleNamespace(
        context=AsyncMock(return_value=SimpleNamespace(
            ruleset_key="si", ruleset_version=1, type_key="ladder",
        )),
        rulesets=DEFAULT_RULESETS,
    )
    service._locked_lobby = AsyncMock(return_value=lobby)
    service._require_open = AsyncMock()
    service._require_creator = AsyncMock()
    service._active_member_count = AsyncMock(return_value=1)
    service._all_active_members = AsyncMock(return_value=[])
    service._event = AsyncMock()
    service._refresh_validation = AsyncMock()
    service._snapshot = AsyncMock(return_value=lobby)
    return service, lobby


async def test_lobby_privacy_is_editable_and_preserved_when_other_settings_change():
    service, lobby = _privacy_settings_service()
    await service.set_settings(lobby.id, 42, {"is_private": True}, expected_version=1)
    assert lobby.settings["is_private"] is True
    assert lobby.version == 2
    await service.set_settings(lobby.id, 42, {"theme_count": 3})
    assert lobby.settings["is_private"] is True
    assert lobby.settings["theme_count"] == 3
    await service.set_settings(lobby.id, 42, {"is_private": False})
    assert lobby.settings["is_private"] is False
    with pytest.raises(PermissionError, match="Tournament locks parameter: ready_delay"):
        await service.set_settings(lobby.id, 42, {"ready_delay": 2})


@pytest.mark.parametrize("value", ["true", 1, None, []])
async def test_lobby_privacy_requires_a_boolean(value):
    service, lobby = _privacy_settings_service()
    with pytest.raises(ValueError, match="is_private must be true or false"):
        await service.set_settings(lobby.id, 42, {"is_private": value})
    assert "is_private" not in lobby.settings


async def test_start_rejects_a_player_who_is_not_ready():
    service, lobby = _privacy_settings_service()
    lobby.searching = False
    lobby.validation = []
    service._selected_packets = AsyncMock(return_value=[SimpleNamespace()])
    service._active_members = AsyncMock(return_value=[
        SimpleNamespace(ready=True), SimpleNamespace(ready=False),
    ])

    with pytest.raises(ValueError, match="Every lobby member must be ready"):
        await service.start(lobby.id, 42, expected_version=1)
    assert lobby.version == 1
    service._event.assert_not_awaited()


async def _readiness_service():
    service, lobby = _privacy_settings_service()
    lobby.status = "assembling"
    lobby.expires_at = datetime.now(UTC) + timedelta(hours=1)
    lobby.validation = []
    lobby.creator_player_id = UUID(int=3)
    lobby.settings = {**lobby.settings, "minimum_players": 1}
    members = [SimpleNamespace(
        player_id=UUID(int=index), role="player", ready=False, validation_violations=[],
    ) for index in (3, 4)]
    service._member = AsyncMock(return_value=members[0])
    service._active_members = AsyncMock(return_value=members)
    service._selected_packets = AsyncMock(return_value=[])
    service._available_play_units = AsyncMock(return_value=[])
    context = service.tournaments.context.return_value
    context.type_rules = {}
    context.policies = {}
    context.assembly_open = True
    async with service.database.transaction() as session:
        player = SimpleNamespace(id=members[0].player_id, public_nickname="Player", status="active")
        session.get.return_value = player
        session.scalars = AsyncMock(return_value=[player])
    return service, lobby, members, context, session


async def test_ladder_players_can_be_ready_without_packets():
    service, lobby, members, _, _ = await _readiness_service()
    for member in members:
        service._member.return_value = member
        await service.set_ready(lobby.id, 42)
    assert all(member.ready for member in members)
    assert service._event.await_count == 2
    await service.set_ready(lobby.id, 42, ready=False)
    assert not members[-1].ready


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("classic", "packet_required"),
        ("closed_stage", "tournament_stage_closed"),
        ("inactive_member", "tournament_membership_required"),
        ("player_limit", "ruleset_player_limit_exceeded"),
    ],
)
async def test_packetless_readiness_preserves_other_requirements(condition, reason):
    service, lobby, members, context, session = await _readiness_service()
    if condition == "classic":
        context.type_key = "classic"
    elif condition == "closed_stage":
        context.assembly_open = False
    elif condition == "inactive_member":
        session.get.return_value.status = "left"
    elif condition == "player_limit":
        lobby.settings = {**lobby.settings, "minimum_players": 3}
    with pytest.raises(LobbyReadinessError) as failure:
        await service.set_ready(lobby.id, 42)
    assert failure.value.reason == reason
    assert not any(member.ready for member in members)
    service._event.assert_not_awaited()


@pytest.mark.parametrize("playable", [True, False])
async def test_selected_packets_still_require_access_and_fresh_content(playable):
    service, lobby, members, _, _ = await _readiness_service()
    service._selected_packets.return_value = [SimpleNamespace(
        packet_id=UUID(int=10), assignment_id=UUID(int=11), packet_version_id=UUID(int=12),
    )]
    service._packet_selection_violation = AsyncMock(return_value=None)
    service.tournaments.has_assignment_access = AsyncMock(return_value=playable)

    with pytest.raises(LobbyReadinessError) as failure:
        await service.set_ready(lobby.id, 42)

    assert failure.value.reason == (
        "insufficient_fresh_content" if playable else "packet_not_playable"
    )
    assert not any(member.ready for member in members)


@pytest.mark.parametrize("initial_selection", [True, False])
@pytest.mark.parametrize("bulk", [True, False])
async def test_only_initial_bulk_packet_selection_preserves_readiness(initial_selection, bulk):
    service, lobby, members, _, session = await _readiness_service()
    for member in members:
        member.ready = True
    packet_id = UUID(int=10)
    version = SimpleNamespace(id=UUID(int=11), name="Packet")
    assignment = SimpleNamespace(id=UUID(int=12), adopted_version_id=version.id)
    packet = SimpleNamespace(id=packet_id, retired_at=None)
    session.get.side_effect = lambda model, identity: (
        packet if model is LogicalPacketRecord else version
    )
    session.scalar = AsyncMock(return_value=assignment)
    session.add = Mock()
    service.tournaments.has_assignment_access = AsyncMock(return_value=True)
    service._all_active_members = AsyncMock(return_value=members)
    service._validate_lobby = AsyncMock()
    if not initial_selection:
        service._selected_packets.return_value = [SimpleNamespace(
            packet_id=UUID(int=20), selection_order=1,
        )]

    if bulk:
        await service.select_packets(lobby.id, 42, [packet_id, UUID(int=13)])
    else:
        await service.select_packet(lobby.id, 42, packet_id)

    assert all(member.ready == (initial_selection and bulk) for member in members)


@pytest.mark.parametrize(
    ("reason", "details"),
    [
        ("insufficient_fresh_content", {"available": 1, "required": 3}),
        ("ruleset_player_limit_exceeded", {"actual": 2, "minimum": 4, "maximum": 4}),
        ("tournament_stage_closed", {}),
    ],
)
async def test_start_preserves_the_validation_failure_reason_and_details(reason, details):
    service, lobby = _privacy_settings_service()
    lobby.searching = False
    lobby.validation = [{"code": reason, "details": details}]
    service._selected_packets = AsyncMock(return_value=[SimpleNamespace()])
    service._active_members = AsyncMock(return_value=[SimpleNamespace(ready=True)])

    with pytest.raises(LobbyReadinessError) as failure:
        await service.start(lobby.id, 42, expected_version=1)

    assert failure.value.reason == reason
    assert failure.value.details == details
    service._event.assert_not_awaited()


async def test_private_lobbies_are_excluded_before_projecting_ongoing_cards():
    tournament_id = UUID(int=2)
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status="active")),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [SimpleNamespace(
            tournament_id=tournament_id, settings={"is_private": True},
        )])),
    )

    @asynccontextmanager
    async def sessions():
        yield session

    service = object.__new__(InvitationMatchmakingService)
    service.database = SimpleNamespace(sessions=sessions)
    service.tournaments = SimpleNamespace(player_tournament_map=AsyncMock(
        return_value=({tournament_id: "Public tournament"}, set()),
    ))
    service._snapshot = AsyncMock()
    assert await service.ongoing_lobbies(UUID(int=1)) == ()
    service._snapshot.assert_not_awaited()


async def test_private_games_are_excluded_before_projecting_observable_cards():
    service = object.__new__(PersistentGameService)
    game = SimpleNamespace(assignment_plan={"is_private": True})
    assert await service._observable_card(None, game, UUID(int=1)) is None


async def test_search_pool_is_batched_cached_and_scoped_to_each_tournament(monkeypatch):
    ticks = [0.0]
    monkeypatch.setattr("sitg_bot.services.matchmaking.time", SimpleNamespace(
        monotonic=lambda: ticks[0],
    ))
    started = datetime.now(UTC)
    first, second, third = (UUID(int=i) for i in (1, 2, 3))
    rows = [
        (first, UUID(int=10), started, 2),
        (second, UUID(int=10), started, 3),
        (third, UUID(int=20), started, 4),
    ]
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    service = InvitationMatchmakingService(object())
    pools = await asyncio.gather(*(service._search_pool(session) for _ in range(12)))
    assert session.execute.await_count == 1
    assert all(pool[first]["other_searching_lobby_count"] == 1 for pool in pools)
    assert pools[0][first]["other_searching_player_count"] == 3
    assert pools[0][second]["other_searching_player_count"] == 2
    assert pools[0][third]["other_searching_lobby_count"] == 0
    assert pools[0][third]["other_searching_player_count"] == 0
    ticks[0] = 29.9
    await service._search_pool(session)
    assert session.execute.await_count == 1
    rows.clear()
    ticks[0] = 30.0
    assert await service._search_pool(session) == {}
    assert session.execute.await_count == 2


async def test_search_refresh_throttles_reads_and_queues_only_changed_participants(monkeypatch):
    ticks = [0.0]
    monkeypatch.setattr("sitg_bot.services.matchmaking.time", SimpleNamespace(
        monotonic=lambda: ticks[0],
    ))
    enqueue = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.matchmaking.TransactionalOutbox.enqueue", enqueue)
    lobby_id = UUID(int=1)
    started = datetime.now(UTC) - timedelta(minutes=2)
    pool_rows = [(lobby_id, UUID(int=2), started, 2)]
    status = InvitationMatchmakingService._search_status(started, {})
    recipients = [
        (lobby_id, None, UUID(int=3), 42, "en"),
        (lobby_id, status, UUID(int=4), 43, "ru"),
    ]
    session = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(all=lambda: pool_rows), SimpleNamespace(all=lambda: recipients),
        SimpleNamespace(all=lambda: pool_rows), SimpleNamespace(all=lambda: recipients),
    ]))

    @asynccontextmanager
    async def transaction():
        yield session

    service = InvitationMatchmakingService(SimpleNamespace(transaction=transaction))
    await service.refresh_search_status()
    assert session.execute.await_count == 2
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["payload"]["recipient_telegram_user_id"] == 42
    ticks[0] = 29.9
    await service.refresh_search_status()
    assert session.execute.await_count == 2
    recipients[0] = (lobby_id, status, UUID(int=3), 42, "en")
    ticks[0] = 30.0
    await service.refresh_search_status()
    assert session.execute.await_count == 4
    assert enqueue.await_count == 1


async def test_search_presentation_shares_summary_and_skips_unchanged_versions(monkeypatch):
    now = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr("sitg_bot.services.matchmaking.datetime", Clock)
    lobby = SimpleNamespace(
        id=UUID(int=1), tournament_id=UUID(int=2), version=1, status="assembling", searching=True,
        expires_at=now[0] + timedelta(hours=1),
        search_started_at=now[0] - timedelta(minutes=2),
    )
    member = SimpleNamespace(active=True, player_id=UUID(int=2), telegram_messages={})
    session = SimpleNamespace(scalar=AsyncMock(return_value=member))

    @asynccontextmanager
    async def transaction():
        yield session

    service = InvitationMatchmakingService(SimpleNamespace(transaction=transaction))
    service._locked_lobby = AsyncMock(return_value=lobby)
    service._search_pool = AsyncMock(return_value={lobby.id: {
        "other_searching_lobby_count": 3, "other_searching_player_count": 6,
    }})
    service._telegram_summary = AsyncMock(return_value={"members": [], "selected_packets": []})
    first = await service.telegram_presentation(lobby.id, 42)
    assert first["lobby"]["other_searching_lobby_count"] == 3
    # Another recipient reuses the lobby's displayed data.
    await service.telegram_presentation(lobby.id, 43)
    assert service._telegram_summary.await_count == 1
    member.telegram_messages = {
        "summary": 1, "settings": 2, "summary_text": "Summary", "summary_version": 1,
        "search_status": first["search_status"],
    }
    assert "lobby" not in await service.telegram_presentation(lobby.id, 42)
    # A retry after saving the summary still needs capabilities for the settings prompt.
    del member.telegram_messages["settings"]
    assert "lobby" in await service.telegram_presentation(lobby.id, 42)
    member.telegram_messages["settings"] = 2
    now[0] += timedelta(minutes=1)
    elapsed = await service.telegram_presentation(lobby.id, 42)
    assert elapsed["lobby"]["search_elapsed_minutes"] == 3
    assert service._telegram_summary.await_count == 1
    lobby.searching = False
    stopped = await service.telegram_presentation(lobby.id, 42)
    assert stopped["search_status"] is None
    assert stopped["lobby"]["searching"] is False
    assert service._telegram_summary.await_count == 2


@pytest.mark.parametrize("kind", [
    "matchmaking_search_started", "matchmaking_search_cancelled",
    "matchmaking_search_completed", "lobby_merged",
])
async def test_search_state_events_refresh_all_active_participants(monkeypatch, kind):
    enqueue = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.matchmaking.TransactionalOutbox.enqueue", enqueue)
    monkeypatch.setattr(
        "sitg_bot.services.matchmaking.TransactionalOutbox.enqueue_lobby_event", AsyncMock(),
    )
    recipients = [SimpleNamespace(id=UUID(int=i), telegram_user_id=40 + i, preferred_locale="en")
                  for i in (1, 2)]
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=0), add=Mock(), flush=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: recipients)),
    )
    await InvitationMatchmakingService._event(session, UUID(int=3), kind, {})
    assert enqueue.await_count == 2
    assert all(args.kwargs["topic"] == "telegram.lobby.refresh" for args in enqueue.await_args_list)


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


def _packet_listing_service(*, type_key="ladder", theme_count=1):
    lobby = SimpleNamespace(
        id=UUID(int=1), tournament_id=UUID(int=2), creator_player_id=UUID(int=3),
        settings={"theme_count": theme_count}, version=1, invitation_code="invite",
        status="assembling", max_players=4, expires_at=datetime.now(UTC) + timedelta(hours=1),
        game_id=None, searching=False, search_started_at=None, merged_into_lobby_id=None,
        validation=[],
    )
    context = SimpleNamespace(
        ruleset_key="si", ruleset_version=1, type_key=type_key, hybrid_matchmaking_enabled=False,
    )
    versions = [SimpleNamespace(
        id=UUID(int=10 + index), name=name, lead_author_id=None, year=None, published_at=None,
    ) for index, name in enumerate(("Zulu", "Alpha", "Hidden"))]
    assignments = [SimpleNamespace(
        id=UUID(int=20 + index), adopted_version_id=version.id,
    ) for index, version in enumerate(versions)]
    selections = [PregameLobbyPacketRecord(
        lobby_id=lobby.id, packet_id=UUID(int=30 + index), packet_version_id=version.id,
        assignment_id=assignment.id, selection_order=index + 1,
    ) for index, (version, assignment) in enumerate(zip(versions, assignments, strict=True))]
    members = [SimpleNamespace(
        player_id=UUID(int=3 + index), join_order=index + 1, ready=False,
        validation_violations=[], role="player", fresh_content_confirmed=False,
    ) for index in range(2)]
    units = [PlayUnit(
        kind="si_theme", logical_id=UUID(int=100 + index), revision_id=UUID(int=200 + index),
        packet_version_id=versions[0 if index < 2 else 1].id, packet_order=1, position=index + 1,
        question_revision_ids=(), claims=(), metadata={"question_values": [10, 20, 30, 40, 50]},
    ) for index in range(3)]

    async def get(model, identity):
        if model is PregameLobbyRecord:
            return lobby
        records = versions if model is PacketVersionRecord else assignments
        return next(record for record in records if record.id == identity)

    async def access(session, assignment, player_id, right):
        if right == "discoverable":
            return assignment.id != assignments[2].id
        return not (assignment.id == assignments[1].id and player_id == members[1].player_id)

    async def scalars(query):
        return [selections[1].packet_id] if (
            query.column_descriptions[0]["entity"] is PlayerPacketBlockRecord
        ) else []

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get), scalar=AsyncMock(return_value=0),
        execute=AsyncMock(), scalars=AsyncMock(side_effect=scalars),
    )

    @asynccontextmanager
    async def sessions():
        yield session

    service = object.__new__(InvitationMatchmakingService)
    service.database = SimpleNamespace(sessions=sessions)
    service.tournaments = SimpleNamespace(
        context=AsyncMock(return_value=context), rulesets=DEFAULT_RULESETS,
        has_assignment_access=AsyncMock(side_effect=access),
    )
    service._active_members = AsyncMock(return_value=members)
    service._member = AsyncMock(return_value=members[1])
    service._available_play_units = AsyncMock(return_value=units)
    service._packet_play_unit_count = AsyncMock(side_effect=[3, 2, 3, 2])
    service._assignment_versions = AsyncMock(side_effect=lambda s, items: {
        item.id: next(version for version in versions if version.id == item.adopted_version_id)
        for item in items
    })
    service._packet_authors = AsyncMock(side_effect=lambda s, ids: {key: [] for key in ids})
    return service, session, lobby, context, selections, versions, members


@pytest.mark.parametrize("type_key", ["ladder", "classic"])
@pytest.mark.parametrize("theme_count", [1, "max"])
async def test_suggestions_batch_discoverable_packets_and_preserve_individual_cards(
    type_key, theme_count,
):
    service, session, lobby, context, selections, versions, members = _packet_listing_service(
        type_key=type_key, theme_count=theme_count,
    )
    session.execute.return_value = SimpleNamespace(all=lambda: [
        (SimpleNamespace(id=selection.packet_id), assignment)
        for selection, assignment in zip(selections, [
            SimpleNamespace(id=item.assignment_id, adopted_version_id=item.packet_version_id)
            for item in selections
        ], strict=True)
    ])

    suggestions = await service.suggest_packets(lobby.id, telegram_user_id=42)

    assert [item.name for item in suggestions] == ["Alpha", "Zulu"]
    alpha, zulu = suggestions
    assert (alpha.total_play_unit_count, alpha.fresh_play_unit_count) == (2, 1)
    assert (zulu.total_play_unit_count, zulu.fresh_play_unit_count) == (3, 2)
    assert alpha.blocked and not alpha.playable_for_all
    assert not zulu.blocked and zulu.playable_for_all
    required = 3 if theme_count == "max" or type_key == "classic" else 1
    assert zulu.validation_violations == (
        ({"code": "insufficient_fresh_content", "details": {"required": 3, "available": 2}},)
        if required == 3 else ()
    )
    service._available_play_units.assert_awaited_once()
    args = service._available_play_units.await_args.args
    assert [item.packet_version_id for item in args[1]] == [item.id for item in versions[:2]]
    assert args[2:] == (members, context)
    assert not any(call.args == (PacketVersionRecord, versions[2].id)
                   for call in session.get.await_args_list)


@pytest.mark.parametrize("theme_count", [1, "max"])
@pytest.mark.parametrize("searching", [True, False])
async def test_snapshot_reuses_availability_and_excludes_observers(theme_count, searching):
    service, session, lobby, context, selections, _, members = _packet_listing_service(
        theme_count=theme_count,
    )
    observer = SimpleNamespace(**{**vars(members[0]), "role": "observer", "join_order": 3})
    context.hybrid_matchmaking_enabled = True
    lobby.searching = searching
    counts = {"other_searching_lobby_count": 2, "other_searching_player_count": 3}
    service._search_pool = AsyncMock(return_value={lobby.id: counts} if searching else {})
    service._search_pool_totals = {lobby.tournament_id: counts}
    rows = [(member, SimpleNamespace(
        id=member.player_id, public_nickname="Player", telegram_user_id=42 + index,
    )) for index, member in enumerate([*members, observer])]
    session.execute.return_value = SimpleNamespace(all=lambda: rows)
    service._selected_packets = AsyncMock(return_value=selections[:2])

    snapshot = await service._snapshot(session, lobby)

    assert [packet.fresh_play_unit_count for packet in snapshot.selected_packets] == [2, 1]
    assert snapshot.available_play_unit_count == 3
    assert snapshot.settings.theme_count == (5 if theme_count == "max" else 1)
    service._available_play_units.assert_awaited_once_with(
        session, selections[:2], members, context,
    )
    assert snapshot.members[-1].role == "observer"
    assert snapshot.other_searching_lobby_count == 2
    assert snapshot.other_searching_player_count == 3


async def test_snapshot_rechecks_availability_if_players_change_after_validation():
    service, session, lobby, context, selections, _, members = _packet_listing_service()
    session.execute.return_value = SimpleNamespace(all=lambda: [
        (member, SimpleNamespace(id=member.player_id, public_nickname="Player",
                                 telegram_user_id=42))
        for member in members
    ])
    service._selected_packets = AsyncMock(return_value=selections[:2])
    validation = SimpleNamespace(
        members=members[:1], selected=selections[:2], context=context,
        parameters=GameSettings(theme_count=1), available=[],
        playable={(item.assignment_id, members[0].player_id): True for item in selections[:2]},
    )

    snapshot = await service._snapshot(session, lobby, validation=validation)

    assert snapshot.available_play_unit_count == 3
    service._available_play_units.assert_awaited_once_with(
        session, selections[:2], members, context,
    )


async def test_packet_pages_filter_before_calculating_and_return_complete_matches():
    service, session, lobby, _, selections, versions, _ = _packet_listing_service()
    rows = [(SimpleNamespace(id=selection.packet_id), SimpleNamespace(
        id=selection.assignment_id, adopted_version_id=selection.packet_version_id,
    )) for selection in selections]

    async def execute(query):
        cursor = next((value for value in query.compile().params.values()
                       if isinstance(value, UUID) and value in [row[1].id for row in rows]), None)
        return SimpleNamespace(all=lambda: [
            row for row in rows if cursor is None or row[1].id > cursor
        ][:query._limit_clause.value])

    session.execute.side_effect = execute
    service._packet_play_unit_count.side_effect = None
    service._packet_play_unit_count.return_value = 3
    first = await service.packet_page(lobby.id, telegram_user_id=42, limit=1)
    assert [item.name for item in first["items"]] == ["Zulu"]
    assert first["next_cursor"] == str(selections[0].assignment_id)
    assert not any(call.args[1].id == selections[1].assignment_id and call.args[3] == "playable"
                   for call in service.tournaments.has_assignment_access.await_args_list)
    second = await service.packet_page(
        lobby.id, telegram_user_id=42, limit=1, cursor=UUID(first["next_cursor"]),
    )
    assert [item.name for item in second["items"]] == ["Alpha"]
    assert second["next_cursor"] == str(selections[2].assignment_id)
    last = await service.packet_page(
        lobby.id, telegram_user_id=42, limit=1, cursor=UUID(second["next_cursor"]),
    )
    assert last["items"] == () and last["next_cursor"] is None
    service._available_play_units.reset_mock()
    filtered = await service.packet_page(
        lobby.id, telegram_user_id=42, filters={"name": " ALPHA "},
    )
    assert [item.name for item in filtered["items"]] == ["Alpha"]
    calculated = service._available_play_units.await_args.args[1]
    assert [item.packet_version_id for item in calculated] == [versions[1].id]


async def test_sparse_lobby_packet_pages_bound_work_and_advance_past_rejected_rows():
    service, session, lobby, _, _, versions, _ = _packet_listing_service()
    rows = [(SimpleNamespace(id=UUID(int=10000 + i)), SimpleNamespace(
        id=UUID(int=1000 + i), adopted_version_id=versions[0].id,
    )) for i in range(1000)]

    async def execute(query):
        assert query._limit_clause.value == 26
        cursor = next((value for value in query.compile().params.values()
                       if isinstance(value, UUID) and value in {row[1].id for row in rows}), None)
        return SimpleNamespace(all=lambda: [
            row for row in rows if cursor is None or row[1].id > cursor
        ][:query._limit_clause.value])

    session.execute.side_effect = execute
    cursor = None
    for index in range(2):
        service.tournaments.has_assignment_access.reset_mock()
        page = await service.packet_page(
            lobby.id, telegram_user_id=42, limit=25, cursor=cursor, filters={"name": "absent"},
        )
        assert page["items"] == ()
        assert page["next_cursor"] == str(rows[(index + 1) * 26 - 1][1].id)
        assert service.tournaments.has_assignment_access.await_count == 26
        cursor = UUID(page["next_cursor"])
    assert session.execute.await_count == 2
    assert service._available_play_units.await_args.args[1] == []


async def test_listing_services_share_a_background_limiter_without_blocking_other_work():
    from sitg_bot.services.packet_blocks import PacketBlockService

    loop = asyncio.get_running_loop()
    starts, finishes = [], []

    async def page(*args, **kwargs):
        starts.append(loop.time())
        await asyncio.sleep(0.01)
        finishes.append(loop.time())
        return {"items": [], "next_cursor": None}

    lobby_service = object.__new__(InvitationMatchmakingService)
    lobby_service._packet_page = page
    tournament_service = object.__new__(PacketBlockService)
    tournament_service._packet_page = page
    first = asyncio.create_task(lobby_service.packet_page(UUID(int=1), telegram_user_id=42))
    second = asyncio.create_task(tournament_service.list_tournament_packets(
        UUID(int=2), UUID(int=3), limit=25,
    ))
    await asyncio.sleep(0.03)
    assert not second.done()
    assert first.done()
    await asyncio.gather(first, second)
    assert starts[1] - finishes[0] >= 0.19
