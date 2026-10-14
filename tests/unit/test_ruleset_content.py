import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine

from sitg_bot.domain.game_rulesets import ExposureClaim, SIGameRuleset
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.services import ruleset_content
from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter
from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    PlayerPacketBlockRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
)


class ContentSession:
    """Execute the adapter's column queries against disposable SQLite tables."""

    def __init__(self, connection, tables):
        self.connection = connection
        self.tables = tables
        self.queries = []
        self.returned_rows = []

    async def execute(self, query):
        self.queries.append(query)
        result = self.connection.execute(query).freeze()
        self.returned_rows.append(result.data)
        return result()

    def get_bind(self):
        return self.connection.engine

    async def scalar(self, query):
        return (await self.execute(query)).scalar()

    def insert(self, model, rows):
        self.connection.execute(self.tables[model].insert(), rows)


@pytest.fixture
def content_session():
    columns = {
        PacketVersionRecord: ("id", "packet_id"),
        ThemeRevisionRecord: ("id", "theme_id", "packet_version_id", "position"),
        PacketQuestionRecord: (
            "packet_version_id", "theme_revision_id", "question_revision_id", "position", "value",
        ),
        QuestionRevisionRecord: ("id", "question_id"),
        PlayerExposureClaimRecord: (
            "player_id", "packet_version_id", "claim_namespace", "claim_id", "state",
        ),
        PlayerPacketBlockRecord: ("player_id", "packet_id"),
    }
    metadata = MetaData()
    tables = {
        model: Table(model.__tablename__, metadata, *(
            # SQLite gives native UUID columns numeric affinity, corrupting numeric-looking IDs.
            Column(name, Uuid(native_uuid=False) if isinstance(model.__table__.c[name].type, Uuid)
                   else model.__table__.c[name].type)
            for name in names
        ))
        for model, names in columns.items()
    }
    engine = create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.connect() as connection:
            yield ContentSession(connection, tables)
    finally:
        engine.dispose()


def add_packet(session, *, theme_count=1, values=(10, 20, 30, 40, 50)):
    packet_id, version_id = uuid4(), uuid4()
    session.insert(PacketVersionRecord, [{"id": version_id, "packet_id": packet_id}])
    themes = []
    for position in range(1, theme_count + 1):
        theme_id, logical_id = uuid4(), uuid4()
        question_ids = tuple(uuid4() for _ in values)
        revision_ids = tuple(uuid4() for _ in values)
        session.insert(ThemeRevisionRecord, [{
            "id": theme_id, "theme_id": logical_id,
            "packet_version_id": version_id, "position": position,
        }])
        session.insert(QuestionRevisionRecord, [
            {"id": revision_id, "question_id": question_id}
            for revision_id, question_id in zip(revision_ids, question_ids, strict=True)
        ])
        # Insert backwards to ensure database ordering, rather than insertion order, wins.
        session.insert(PacketQuestionRecord, [
            {"packet_version_id": version_id, "theme_revision_id": theme_id,
             "question_revision_id": revision_ids[index], "position": index + 1,
             "value": values[index]}
            for index in reversed(range(len(values)))
        ])
        themes.append(SimpleNamespace(
            id=theme_id, logical_id=logical_id, question_ids=question_ids,
            revision_ids=revision_ids, position=position,
        ))
    return SimpleNamespace(id=packet_id, version_id=version_id, themes=themes)


@pytest.mark.parametrize("packet_count", [50, 100, 200])
async def test_content_queries_do_not_grow_with_packets_or_themes(content_session, packet_count):
    packets = [add_packet(content_session, theme_count=8) for _ in range(packet_count)]
    selections = [PacketSelection(packet.version_id, index + 1)
                  for index, packet in enumerate(packets)]

    units = await SIContentAdapter().available_play_units(content_session, selections, [uuid4()])

    assert len(units) == packet_count * 8
    assert len(content_session.queries) == 4
    assert all(unit.metadata["question_values"] == [10, 20, 30, 40, 50] for unit in units)


async def test_batching_preserves_question_order_claims_and_seeded_assignment(content_session):
    packets = [add_packet(content_session, theme_count=2, values=(0, 10, 20, 30, 40, 50))
               for _ in range(2)]
    selections = [PacketSelection(packet.version_id, 2 - index)
                  for index, packet in enumerate(packets)]
    adapter = SIContentAdapter()
    batched = await adapter.available_play_units(content_session, selections, [])
    individual = []
    for selection in sorted(selections, key=lambda item: item.packet_version_id):
        individual.extend(await adapter.available_play_units(content_session, [selection], []))

    assert batched == individual
    by_revision = {unit.revision_id: unit for unit in batched}
    for packet in packets:
        for theme in packet.themes:
            unit = by_revision[theme.id]
            assert unit.position == theme.position
            assert unit.question_revision_ids == theme.revision_ids
            assert unit.claims == (ExposureClaim("theme", theme.logical_id),) + tuple(
                ExposureClaim("question", question_id) for question_id in theme.question_ids
            )
    ruleset = SIGameRuleset()
    common = {
        "player_count": 2, "packet_version_ids": [item.packet_version_id for item in selections],
        "parameters": GameSettings(theme_count=3), "seed": "stable-assignment",
    }
    assert ruleset.prepare_assignment(available_play_units=batched, **common) == (
        ruleset.prepare_assignment(available_play_units=individual, **common)
    )


@pytest.mark.parametrize("namespace", ["theme", "question"])
@pytest.mark.parametrize("state", ["reserved", "burnt", "released"])
async def test_exposure_matches_logical_content_across_versions(content_session, namespace, state):
    packet = add_packet(content_session)
    theme = packet.themes[0]
    player_id, other_player, old_version = uuid4(), uuid4(), uuid4()
    identity = theme.logical_id if namespace == "theme" else theme.question_ids[0]
    content_session.insert(PlayerExposureClaimRecord, [
        {"player_id": player_id, "packet_version_id": old_version,
         "claim_namespace": namespace, "claim_id": identity, "state": state},
        {"player_id": player_id, "packet_version_id": old_version,
         "claim_namespace": namespace, "claim_id": uuid4(), "state": "burnt"},
        {"player_id": player_id, "packet_version_id": old_version,
         "claim_namespace": "other", "claim_id": identity, "state": "burnt"},
        {"player_id": other_player, "packet_version_id": old_version,
         "claim_namespace": namespace, "claim_id": identity, "state": "burnt"},
    ])

    units = await SIContentAdapter().available_play_units(
        content_session, [PacketSelection(packet.version_id, 1)], [player_id]
    )

    assert len(units) == (1 if state == "released" else 0)
    assert content_session.returned_rows[-1] == (
        [] if state == "released" else [(player_id, namespace, identity)]
    )
    assert len(content_session.queries) == 4


async def test_one_players_block_only_excludes_that_packet(content_session):
    first, second = add_packet(content_session), add_packet(content_session)
    player_ids = [uuid4(), uuid4()]
    content_session.insert(PlayerPacketBlockRecord, [
        {"player_id": player_ids[1], "packet_id": first.id},
        {"player_id": player_ids[1], "packet_id": uuid4()},
    ])
    units = await SIContentAdapter().available_play_units(
        content_session,
        [PacketSelection(first.version_id, 1), PacketSelection(second.version_id, 2)],
        player_ids,
    )
    assert [unit.packet_version_id for unit in units] == [second.version_id]
    assert content_session.returned_rows[0] == [(first.version_id,)]
    assert len(content_session.returned_rows[2]) == 5
    assert len(content_session.queries) == 4


async def test_empty_selection_needs_no_queries(content_session):
    assert await SIContentAdapter().available_play_units(content_session, [], [uuid4()]) == []
    assert not content_session.queries


async def test_reuse_validates_live_inputs_and_intersects_players_themes(content_session):
    packet = add_packet(content_session, theme_count=3)
    first, second = uuid4(), uuid4()
    for player, theme in ((first, packet.themes[0]), (second, packet.themes[1])):
        content_session.insert(PlayerExposureClaimRecord, [{
            "player_id": player, "packet_version_id": uuid4(), "claim_namespace": "theme",
            "claim_id": theme.logical_id, "state": "burnt",
        }])
    selection = [PacketSelection(packet.version_id, 1)]
    with patch.object(SIContentAdapter, "_calculate_available",
                      wraps=SIContentAdapter._calculate_available) as calculate:
        adapter = SIContentAdapter()
        assert len(await adapter.available_play_units(content_session, selection, [first])) == 2
        assert calculate.call_count == 1
        # A second adapter/listing shares the validated result in the same database.
        assert len(await SIContentAdapter().available_play_units(
            content_session, selection, [first]
        )) == 2
        assert calculate.call_count == 1
        joined = await adapter.available_play_units(content_session, selection, [first, second])
        assert [unit.revision_id for unit in joined] == [packet.themes[2].id]
        assert calculate.call_count == 2
        assert len(await adapter.available_play_units(content_session, selection, [second])) == 2
        assert calculate.call_count == 2
        table = content_session.tables[PlayerExposureClaimRecord]
        content_session.connection.execute(table.update().where(table.c.player_id == first)
                                           .values(state="released"))
        assert len(await adapter.available_play_units(
            content_session, selection, [first, second]
        )) == 2
        assert calculate.call_count == 3
    # Warm validation reads blocks and exposures, with no layout/question/count query.
    assert len(content_session.queries) == 12


async def test_blocks_skip_all_content_until_the_final_block_is_removed(content_session):
    packet = add_packet(content_session)
    first, second = uuid4(), uuid4()
    selection = [PacketSelection(packet.version_id, 1)]
    adapter = SIContentAdapter()
    with patch.object(SIContentAdapter, "_calculate_available",
                      wraps=SIContentAdapter._calculate_available) as calculate:
        assert len(await adapter.available_play_units(content_session, selection, [first])) == 1
        content_session.insert(PlayerPacketBlockRecord, [
            {"player_id": first, "packet_id": packet.id},
            {"player_id": second, "packet_id": packet.id},
        ])
        before = len(content_session.queries)
        assert await adapter.available_play_units(content_session, selection, [first, second]) == []
        assert len(content_session.queries) == before + 1
        table = content_session.tables[PlayerPacketBlockRecord]
        content_session.connection.execute(table.delete().where(table.c.player_id == first))
        assert await adapter.available_play_units(content_session, selection, [first, second]) == []
        assert len(content_session.queries) == before + 2
        content_session.connection.execute(table.delete())
        assert len(await adapter.available_play_units(
            content_session, selection, [first, second]
        )) == 1
        assert calculate.call_count == 2  # Only the newcomer needs a result.
        assert len(content_session.queries) == before + 4


async def test_coalesces_cold_layouts_and_calculations(content_session):
    packet = add_packet(content_session)
    player = uuid4()
    original = content_session.execute

    async def delayed(query):
        await asyncio.sleep(0)
        return await original(query)

    content_session.execute = delayed
    with patch.object(SIContentAdapter, "_calculate_available",
                      wraps=SIContentAdapter._calculate_available) as calculate:
        results = await asyncio.gather(*[
            SIContentAdapter().available_play_units(
                content_session, [PacketSelection(packet.version_id, order)], [player]
            ) for order in (1, 2)
        ])
        assert calculate.call_count == 1
        assert [result[0].packet_order for result in results] == [1, 2]
        assert len(content_session.queries) == 6


async def test_cache_eviction_is_bounded_and_revalidates(content_session, monkeypatch):
    monkeypatch.setattr(ruleset_content, "LAYOUT_CACHE_LIMIT", 1)
    monkeypatch.setattr(ruleset_content, "PLAYER_CACHE_LIMIT", 1)
    packets = [add_packet(content_session) for _ in range(2)]
    player = uuid4()
    adapter = SIContentAdapter()
    with patch.object(SIContentAdapter, "_calculate_available",
                      wraps=SIContentAdapter._calculate_available) as calculate:
        for packet in [*packets, packets[0]]:
            assert len(await adapter.available_play_units(
                content_session, [PacketSelection(packet.version_id, 1)], [player]
            )) == 1
        assert calculate.call_count == 3
    cache = ruleset_content._cache(content_session)
    assert len(cache.layouts) == len(cache.counts) == len(cache.players) == 1
    # Even legacy calls larger than the cache still return the complete list.
    assert len(await adapter.available_play_units(
        content_session, [PacketSelection(packet.version_id, 1) for packet in packets], [player]
    )) == 2


async def test_count_only_reads_are_cached_and_do_not_load_questions(content_session):
    packet = add_packet(content_session, theme_count=2)
    adapter = SIContentAdapter()
    assert await adapter.play_unit_count(content_session, packet.version_id) == 2
    assert await adapter.play_unit_count(content_session, packet.version_id) == 2
    assert len(content_session.queries) == 1


async def test_warm_gameplay_reads_bypass_unrelated_layout_loads(content_session):
    packet = add_packet(content_session)
    adapter = SIContentAdapter()
    player = uuid4()
    selection = [PacketSelection(packet.version_id, 1)]
    await adapter.available_play_units(content_session, selection, [player])
    cache = ruleset_content._cache(content_session)
    async with cache.layout_lock:
        assert len(await asyncio.wait_for(
            adapter.available_play_units(content_session, selection, [player]), timeout=1,
        )) == 1
        assert await asyncio.wait_for(
            adapter.play_unit_count(content_session, packet.version_id), timeout=1,
        ) == 1


async def test_cached_results_are_isolated_by_database(content_session):
    packet = add_packet(content_session)
    player = uuid4()
    content_session.insert(PlayerExposureClaimRecord, [{
        "player_id": player, "packet_version_id": packet.version_id,
        "claim_namespace": "theme", "claim_id": packet.themes[0].logical_id, "state": "burnt",
    }])
    adapter = SIContentAdapter()
    selection = [PacketSelection(packet.version_id, 1)]
    assert await adapter.available_play_units(content_session, selection, [player]) == []
    engine = create_engine("sqlite://")
    try:
        next(iter(content_session.tables.values())).metadata.create_all(engine)
        with engine.connect() as connection:
            other = ContentSession(connection, content_session.tables)
            for model, table in content_session.tables.items():
                if model not in (PlayerExposureClaimRecord, PlayerPacketBlockRecord):
                    rows = list(content_session.connection.execute(table.select()).mappings())
                    if rows:
                        other.insert(model, rows)
            assert len(await adapter.available_play_units(other, selection, [player])) == 1
    finally:
        engine.dispose()


async def test_adopting_a_revision_reuses_logical_claims_and_has_its_own_layout(content_session):
    packet = add_packet(content_session)
    theme = packet.themes[0]
    player = uuid4()
    adapter = SIContentAdapter()
    assert len(await adapter.available_play_units(
        content_session, [PacketSelection(packet.version_id, 1)], [player]
    )) == 1
    version, revision, question_revision = uuid4(), uuid4(), uuid4()
    content_session.insert(PacketVersionRecord, [{"id": version, "packet_id": packet.id}])
    content_session.insert(ThemeRevisionRecord, [{
        "id": revision, "theme_id": theme.logical_id, "packet_version_id": version, "position": 1,
    }])
    content_session.insert(QuestionRevisionRecord, [{
        "id": question_revision, "question_id": theme.question_ids[0],
    }])
    content_session.insert(PacketQuestionRecord, [{
        "packet_version_id": version, "theme_revision_id": revision,
        "question_revision_id": question_revision, "position": 1, "value": 50,
    }])
    content_session.insert(PlayerExposureClaimRecord, [{
        "player_id": player, "packet_version_id": packet.version_id,
        "claim_namespace": "question", "claim_id": theme.question_ids[0], "state": "reserved",
    }])
    selection = [PacketSelection(version, 1)]
    assert await adapter.available_play_units(content_session, selection, [player]) == []
    table = content_session.tables[PlayerExposureClaimRecord]
    content_session.connection.execute(table.update().values(state="released"))
    unit, = await adapter.available_play_units(content_session, selection, [player])
    assert unit.revision_id == revision
    assert unit.question_revision_ids == (question_revision,)
    assert unit.metadata["question_values"] == [50]
