from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from sitg_bot.services.packet_blocks import PacketBlockService
from sitg_bot.services.packet_catalogue import matches_packet
from sitg_bot.storage.models import (
    PlayerPacketBlockRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentRecord,
)


async def test_tournament_pages_filter_before_freshness_and_recheck_permissions():
    assignments = [SimpleNamespace(id=UUID(int=i + 100), packet_id=UUID(int=i + 200))
                   for i in range(57)]
    versions = {assignment.id: SimpleNamespace(
        id=UUID(int=i + 300), name="Other" if i in (0, 30) else f"Match {i}",
        year=2020, published_at=datetime(2024, 1, 1, tzinfo=UTC), lead_author_id=None,
    ) for i, assignment in enumerate(assignments)}
    membership = SimpleNamespace(status="active")
    allowed = {"discoverable": True, "playable": True}

    async def get(model, identity):
        if model is TournamentRecord:
            return SimpleNamespace(name="Cup", finalized_at=datetime.now(UTC))
        if model is TournamentMembershipRecord:
            return membership
        if model is TournamentManagerRecord:
            return None
        raise AssertionError(model)

    async def scalars(query):
        if query.column_descriptions[0]["entity"] is PlayerPacketBlockRecord:
            return []
        cursor = next((value for value in query.compile().params.values()
                       if isinstance(value, UUID) and value in versions), None)
        return [assignment for assignment in assignments
                if cursor is None or assignment.id > cursor]

    session = SimpleNamespace(get=AsyncMock(side_effect=get),
                              scalars=AsyncMock(side_effect=scalars))

    @asynccontextmanager
    async def sessions():
        yield session

    service = PacketBlockService(SimpleNamespace(sessions=sessions))
    service.tournaments = SimpleNamespace(
        context=AsyncMock(return_value=SimpleNamespace(ruleset_key="si", ruleset_version=1)),
        has_assignment_access=AsyncMock(side_effect=lambda s, a, p, right: allowed[right]),
        can_read_assignment=AsyncMock(return_value=True),
    )
    service._assignment_version = AsyncMock(side_effect=lambda s, a: versions[a.id])
    service._authors = AsyncMock(return_value=[{"id": UUID(int=500), "display_name": "Ada"}])
    adapter = SimpleNamespace(
        play_unit_count=AsyncMock(return_value=3),
        available_play_units=AsyncMock(side_effect=lambda s, selections, players: [
            SimpleNamespace(packet_version_id=selection.packet_version_id)
            for selection in selections for _ in range(2)
        ]),
    )
    service.content_adapters = SimpleNamespace(get=Mock(return_value=adapter))
    cursor = None
    items = []
    sizes = []
    while True:
        page = await service.list_tournament_packets(
            UUID(int=1), UUID(int=2), limit=25, cursor=cursor,
            filters={"name": "match", "author": "ADA", "year_from": 2020,
                     "year_to": 2020, "publication_to": 2024},
        )
        sizes.append(len(page["items"]))
        items.extend(page["items"])
        if page["next_cursor"] is None:
            break
        cursor = UUID(page["next_cursor"])
    assert sizes == [25, 25, 5]
    assert len({item["packet_id"] for item in items}) == 55
    assert all(item["fresh_play_unit_count"] == 2 for item in items)
    calculated = [selection.packet_version_id
                  for call in adapter.available_play_units.await_args_list
                  for selection in call.args[1]]
    assert len(calculated) == 55
    assert versions[assignments[0].id].id not in calculated
    assert versions[assignments[30].id].id not in calculated
    allowed["playable"] = False
    page = await service.list_tournament_packets(UUID(int=1), UUID(int=2), limit=25)
    assert all(not item["playable"] for item in page["items"])
    allowed["discoverable"] = False
    page = await service.list_tournament_packets(UUID(int=1), UUID(int=2), limit=25)
    assert page["items"] == []
    assert adapter.available_play_units.await_args.args[1] == []
    membership.status = "suspended"
    with pytest.raises(PermissionError):
        await service.list_tournament_packets(UUID(int=1), UUID(int=2), limit=25)


def test_unknown_years_only_match_unrestricted_ranges():
    version = SimpleNamespace(name=" Alpha ", year=None, published_at=None)
    assert matches_packet(version, ["Анна", "Борис"], {"author": "анна"})
    assert not matches_packet(version, [], {"year_from": 2020})
    assert not matches_packet(version, [], {"publication_to": 2026})
