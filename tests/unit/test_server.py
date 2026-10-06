from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.domain.game_deadlines import (
    INITIAL_PLAYER_JOIN_TIMEOUT,
    PAUSED_GAME_ABANDONMENT_TIMEOUT,
    REMAINING_PLAYERS_JOIN_TIMEOUT,
)
from sitg_bot.server import ConsoleApplicationServer, json_value
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.persistent_game import PersistentGameService


@dataclass
class Payload:
    identifier: UUID
    created_at: datetime
    values: tuple[int, ...]


def test_json_value_serializes_server_payload_types() -> None:
    identifier = UUID(int=42)
    created_at = datetime(2026, 8, 20, 12, tzinfo=UTC)

    assert json_value(Payload(identifier, created_at, (1, 2))) == {
        "identifier": str(identifier),
        "created_at": created_at.isoformat(),
        "values": [1, 2],
    }


def test_game_event_payload_serializes_deadlines_for_jsonb() -> None:
    identifier = UUID(int=7)
    deadline = datetime(2026, 8, 27, 12, tzinfo=UTC)

    assert PersistentGameService._json_payload({"appeal_id": identifier, "deadline": deadline}) == {
        "appeal_id": str(identifier),
        "deadline": deadline.isoformat(),
    }


def test_server_rejects_invalid_poll_interval() -> None:
    with pytest.raises(ValueError):
        ConsoleApplicationServer(object(), poll_interval=0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "failure",
    [
        None,
        PermissionError("Manager required"),
        StaleWriteError("Tournament settings have changed"),
    ],
)
async def test_console_setup_finalization_uses_manager_and_settings_version(
    failure: Exception | None,
) -> None:
    server = ConsoleApplicationServer(object())  # type: ignore[arg-type]
    manager_id = UUID(int=20)
    tournament_id = UUID(int=30)
    finalized_at = datetime(2026, 9, 15, tzinfo=UTC)
    finalize = AsyncMock(
        return_value=SimpleNamespace(finalized_at=finalized_at, settings_version=4),
        side_effect=failure,
    )
    server.tournaments.finalize_tournament_setup = finalize
    connection = SimpleNamespace(session=SimpleNamespace(player_id=manager_id))
    params = {"tournament_id": str(tournament_id), "expected_version": 3}

    if failure is None:
        result = await server._dispatch_console(connection, "tournament_setup_finalize", params)
        assert result == {"finalized_at": finalized_at, "settings_version": 4}
    else:
        with pytest.raises(type(failure), match=str(failure)):
            await server._dispatch_console(connection, "tournament_setup_finalize", params)
    finalize.assert_awaited_once_with(tournament_id, manager_id, expected_version=3)


def test_bound_port_requires_a_started_server() -> None:
    server = ConsoleApplicationServer(object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="has not started"):
        _ = server.bound_port


async def test_lobby_presentation_requires_adapter_and_projects_active_members():
    server = ConsoleApplicationServer(object())
    params = {"telegram_user_id": 42, "lobby_id": str(UUID(int=9))}
    connection = SimpleNamespace(adapter_session=None)
    with pytest.raises(PermissionError):
        await server._dispatch(connection, "telegram.lobby.presentation", params)
    connection.adapter_session = SimpleNamespace(channel="telegram_bot")
    gateway = server.application_gateway
    gateway.matchmaking.telegram_presentation = AsyncMock(return_value={
        "active": True, "messages": {"summary": 7}, "player_id": UUID(int=1),
    })
    gateway._lobby_payload = AsyncMock(return_value={"members": []})
    gateway._lobby_reference = AsyncMock(return_value=SimpleNamespace(
        value="reference", expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    ))
    result = await server._dispatch(connection, "telegram.lobby.presentation", params)
    assert result["messages"] == {"summary": 7}
    assert result["lobby"] == {"members": []}
    assert result["launch_reference"] == "reference"
    assert "player_id" not in result
    gateway.matchmaking.telegram_presentation.return_value = {
        "active": False, "messages": {}, "player_id": UUID(int=1),
    }
    result = await server._dispatch(connection, "telegram.lobby.presentation", params)
    assert not result["active"] and "lobby" not in result
    gateway._lobby_payload.assert_awaited_once()


async def test_telegram_navigation_snapshot_requires_authenticated_bot():
    server = ConsoleApplicationServer(object())
    server.application_gateway.navigation.snapshot = AsyncMock(return_value={"context": "menu"})
    for channel in (None, "mini_app"):
        connection = SimpleNamespace(
            adapter_session=SimpleNamespace(channel=channel) if channel else None,
        )
        with pytest.raises(PermissionError):
            await server._dispatch(
                connection, "telegram.navigation.snapshot", {"telegram_user_id": 42},
            )
    connection = SimpleNamespace(adapter_session=SimpleNamespace(channel="telegram_bot"))
    result = await server._dispatch(
        connection, "telegram.navigation.snapshot", {"telegram_user_id": 42},
    )
    assert result == {"context": "menu"}
    server.application_gateway.navigation.snapshot.assert_awaited_once_with(42)


def test_console_and_application_gateway_share_domain_services() -> None:
    server = ConsoleApplicationServer(object())  # type: ignore[arg-type]

    assert server.application_gateway.matchmaking is server.matchmaking
    assert server.application_gateway.trust is server.trust


def test_game_lifecycle_deadlines_are_fixed_at_five_minutes() -> None:
    assert timedelta(minutes=5) == INITIAL_PLAYER_JOIN_TIMEOUT
    assert timedelta(minutes=5) == REMAINING_PLAYERS_JOIN_TIMEOUT
    assert timedelta(minutes=5) == PAUSED_GAME_ABANDONMENT_TIMEOUT


async def test_server_groups_live_player_connections_by_game() -> None:
    server = ConsoleApplicationServer(object())  # type: ignore[arg-type]
    game_id = UUID(int=10)
    other_game_id = UUID(int=11)
    first_player_id = UUID(int=20)
    second_player_id = UUID(int=21)
    server._connections = [  # type: ignore[assignment]
        SimpleNamespace(
            session=SimpleNamespace(player_id=first_player_id),
            adapter_session=None,
            game_ids={game_id, other_game_id},
        ),
        SimpleNamespace(
            session=SimpleNamespace(player_id=second_player_id),
            adapter_session=None,
            game_ids={game_id},
        ),
        SimpleNamespace(session=None, adapter_session=None, game_ids={game_id}),
    ]

    assert await server._connected_player_ids_by_game() == {
        game_id: {first_player_id, second_player_id},
        other_game_id: {first_player_id},
    }
