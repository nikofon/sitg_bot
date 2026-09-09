from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from sitg_bot.domain.game_deadlines import (
    INITIAL_PLAYER_JOIN_TIMEOUT,
    PAUSED_GAME_ABANDONMENT_TIMEOUT,
    REMAINING_PLAYERS_JOIN_TIMEOUT,
)
from sitg_bot.server import ConsoleApplicationServer, json_value
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


def test_bound_port_requires_a_started_server() -> None:
    server = ConsoleApplicationServer(object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="has not started"):
        _ = server.bound_port


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
