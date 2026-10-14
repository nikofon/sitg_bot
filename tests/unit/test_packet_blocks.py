"""SI content adapter unit tests for player packet blocks."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from sitg_bot.services.packet_blocks import PacketBlockService
from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter
from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PlayerPacketBlockRecord,
    ThemeRevisionRecord,
)


def _block_service(access_results, *, record=None):
    assignments = [SimpleNamespace(id=uuid4()) for _ in access_results]
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[SimpleNamespace(retired_at=None), record]),
        scalars=AsyncMock(return_value=assignments),
        add=Mock(),
        delete=AsyncMock(),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    service = PacketBlockService(SimpleNamespace(transaction=transaction))
    service.tournaments.has_assignment_access = AsyncMock(side_effect=access_results)
    return service, session, assignments


@pytest.mark.parametrize("access_results", ([True], [False, True], [True, False]))
async def test_block_packet_with_access_through_any_tournament(access_results) -> None:
    service, session, assignments = _block_service(access_results)
    player_id, packet_id = uuid4(), uuid4()

    result = await service.set_blocked(player_id, packet_id, blocked=True)

    assert result == {"packet_id": str(packet_id), "blocked": True}
    session.add.assert_called_once()
    block = session.add.call_args.args[0]
    assert isinstance(block, PlayerPacketBlockRecord)
    assert (block.player_id, block.packet_id) == (player_id, packet_id)
    access = service.tournaments.has_assignment_access
    assert access.await_count == access_results.index(True) + 1
    access.assert_awaited_with(
        session, assignments[access_results.index(True)], player_id, "discoverable"
    )


@pytest.mark.parametrize("access_results", ([], [False, False]))
async def test_block_packet_without_tournament_access_is_rejected(access_results) -> None:
    service, session, _ = _block_service(access_results)

    with pytest.raises(PermissionError, match="Packet access through an active tournament"):
        await service.set_blocked(uuid4(), uuid4(), blocked=True)

    session.add.assert_not_called()


async def test_unblock_packet_does_not_require_tournament_access() -> None:
    player_id, packet_id = uuid4(), uuid4()
    record = PlayerPacketBlockRecord(player_id=player_id, packet_id=packet_id)
    service, session, _ = _block_service([], record=record)

    result = await service.set_blocked(player_id, packet_id, blocked=False)

    assert result == {"packet_id": str(packet_id), "blocked": False}
    session.delete.assert_awaited_once_with(record)
    service.tournaments.has_assignment_access.assert_not_awaited()


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def tuples(self):
        return list(self._rows)

    def scalars(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        return self._results.pop(0)

    def get_bind(self):
        return self


def _theme(version_id, position=1):
    return ThemeRevisionRecord(
        id=uuid4(),
        theme_id=uuid4(),
        packet_version_id=version_id,
        revision_number=1,
        position=position,
        name="Theme",
    )


def _theme_row(theme, packet_id):
    return SimpleNamespace(
        id=theme.id, theme_id=theme.theme_id, packet_version_id=theme.packet_version_id,
        position=theme.position, packet_id=packet_id,
    )


async def test_available_play_units_skips_themes_of_blocked_packets() -> None:
    version_id = uuid4()
    session = FakeSession([
        FakeResult([version_id]),  # block resolution must precede all content reads
    ])
    units = await SIContentAdapter().available_play_units(
        session, [PacketSelection(version_id, 1)], [uuid4()]
    )
    assert units == []
    assert len(session.queries) == 1


async def test_available_play_units_keeps_unblocked_themes() -> None:
    version_id = uuid4()
    packet_id = uuid4()
    theme = _theme(version_id)
    question_revision_id = uuid4()
    placement = PacketQuestionRecord(
        packet_version_id=version_id,
        theme_revision_id=theme.id,
        question_revision_id=question_revision_id,
        position=1,
        value=10,
    )
    session = FakeSession([
        FakeResult([]),
        FakeResult([_theme_row(theme, packet_id)]),
        FakeResult([(theme.id, placement.question_revision_id, placement.value, uuid4())]),
        FakeResult([]),
    ])
    units = await SIContentAdapter().available_play_units(
        session, [PacketSelection(version_id, 1)], [uuid4()]
    )
    assert len(units) == 1
    assert units[0].question_revision_ids == (question_revision_id,)
