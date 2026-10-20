import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from sitg_bot.services.analysis_jobs import AnalysisService
from sitg_bot.services.knowledge import InMemoryKnowledgeStore, KnowledgeService
from sitg_bot.services.training_catalogue import (
    encode_packet_cursor,
    enrich_packet_cards,
    packet_cursor,
    packet_order,
)
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.services.training_packets import TrainingPacketService

OWNER, RECIPIENT, OTHER = UUID(int=1), UUID(int=2), UUID(int=3)


async def test_colours_validate_normalize_preserve_and_enforce_owner():
    service = KnowledgeService(InMemoryKnowledgeStore())
    tag = await service.create_tag(OWNER, " History ", "#AbCdEF")
    assert tag.name == "history" and tag.colour == "#abcdef"
    assert (await service.list_tags(OWNER)) == (tag,)
    renamed = await service.rename_tag(OWNER, tag.tag_id, "events")
    assert renamed.colour == "#abcdef"
    recoloured = await service.rename_tag(OWNER, tag.tag_id, "events", "#112233")
    assert recoloured.colour == "#112233"
    fact = await service.create_fact(OWNER, title="Fact", description="D", tag_names=["events"])
    assert fact.tags == (recoloured,)
    with pytest.raises(LookupError):
        await service.rename_tag(OTHER, tag.tag_id, "stolen", "#ffffff")
    for colour in ("red", "#fff", "#000000;display:none", "#zzzzzz", "#1234567"):
        with pytest.raises(ValueError, match="RGB"):
            await service.create_tag(OWNER, "invalid", colour)
    assert (await service.create_tag(OWNER, "default")).colour == "#64748b"


async def test_hash_dedup_across_senders_preserves_edits_deletion_and_recipient_independence():
    service = KnowledgeService(InMemoryKnowledgeStore())
    await service.create_tag(OWNER, "history", "#112233")
    for sender in (OWNER, OWNER, OTHER):
        await service.create_fact(
            sender, title="Same", description="Canonical", tag_names=["history"]
        )
    results = await asyncio.gather(
        service.grant_collection_access(OWNER, RECIPIENT),
        service.grant_collection_access(OTHER, RECIPIENT),
    )
    assert sum(result.copied_count for result in results) == 1
    copied = (await service.list_facts(RECIPIENT)).items[0].fact_id
    assert (await service.fact_detail(RECIPIENT, copied)).tags[0].colour == "#112233"
    await service.update_fact(RECIPIENT, copied, expected_version=1, title="Recipient edit")
    assert (await service.grant_collection_access(OTHER, RECIPIENT)).copied_count == 0
    await service.delete_fact(RECIPIENT, copied)
    assert (await service.grant_collection_access(OWNER, RECIPIENT)).copied_count == 0
    assert (await service.grant_collection_access(RECIPIENT, OTHER)).copied_count == 0
    assert (await service.grant_collection_access(OWNER, UUID(int=4))).copied_count == 1
    assert (await service.list_facts(RECIPIENT)).total == 0


@pytest.mark.parametrize(
    "order,value",
    [
        ("name_asc", "a % packet"),
        ("name_desc", "equal"),
        ("year_asc", 0),
        ("year_desc", 2025),
        ("id_asc", UUID(int=5)),
    ],
)
def test_opaque_cursor_roundtrip_tie_breaker_and_wrong_order(order, value):
    item = {"id": UUID(int=5)}
    token = encode_packet_cursor(item, value, order, "filters")
    assert packet_cursor(token, order, "filters") == (value, item["id"])
    key, descending = packet_order(order)
    assert descending == order.endswith("desc")
    assert key is not None
    if order != "id_asc":
        with pytest.raises(ValueError, match="cursor"):
            packet_cursor(token, order, "changed-filters")
        with pytest.raises(ValueError, match="cursor"):
            packet_cursor(token, "year_asc" if order.startswith("name") else "name_asc", "filters")


@pytest.mark.parametrize("token", ["!", "[]", "====", "a" * 50, "eyJpZCI6bnVsbH0"])
def test_invalid_cursor_fails_cleanly(token):
    with pytest.raises(ValueError, match="cursor"):
        packet_cursor(token, "name_asc", "filters")


async def test_batched_metadata_known_zero_partial_unknown_and_user_scoping(monkeypatch):
    first, second, third = UUID(int=10), UUID(int=11), UUID(int=12)
    theme_a, theme_b = UUID(int=20), UUID(int=21)
    question = UUID(int=30)
    author, tournament = UUID(int=40), UUID(int=50)
    statements = []
    results = iter(
        [
            [(first, author, "Author")],
            [(third, "Alice")],
            [(first, tournament, "Cup")],
            [(first, theme_a), (second, theme_b)],
            [(first, question, theme_a)],
            [
                (
                    str(first),
                    "succeeded",
                    {"question_revision_ids": [str(question)]},
                    None,
                    "succeeded",
                ),
                (str(second), "partially_succeeded", None, None, None),
            ],
        ]
    )

    async def execute(statement):
        # Compile every emitted SQL query using the actual PostgreSQL dialect.
        statements.append(statement.compile(dialect=postgresql.dialect()))
        return next(results)

    async def scalars(statement):
        statements.append(statement.compile(dialect=postgresql.dialect()))
        return [first]

    session = SimpleNamespace(execute=execute, scalars=scalars)
    fresh = AsyncMock(return_value=[SimpleNamespace(packet_version_id=first, revision_id=theme_a)])
    monkeypatch.setattr(
        "sitg_bot.services.training_catalogue.SIContentAdapter.available_play_units", fresh
    )
    cards = [{"id": identity} for identity in (first, second, third)]
    await enrich_packet_cards(
        session, OWNER, cards, TrainingPacketService._library_assignments(OWNER)
    )
    assert len(statements) == 7
    assert cards[0] == {
        "id": first,
        "authors": [{"id": author, "name": "Author"}],
        "tournaments": [{"id": tournament, "name": "Cup"}],
        "total_theme_count": 1,
        "fresh_theme_count": 1,
        "dissected_theme_count": 1,
        "dissected": True,
        "best_buzz_calculated": True,
    }
    assert cards[1]["dissected_theme_count"] is None and cards[1]["dissected"]
    assert cards[2]["dissected_theme_count"] == 0 and not cards[2]["dissected"]
    assert not cards[2]["best_buzz_calculated"]
    assert cards[2]["authors"] == [{"id": None, "name": "Alice"}]
    assert fresh.await_args.args[-1] == [OWNER]
    # Owner predicates bind this user, never another trainee's results.
    assert OWNER in statements[-1].params.values()
    assert OWNER in statements[-2].params.values()


async def test_empty_page_does_not_query_metadata():
    session = SimpleNamespace(execute=AsyncMock(), scalars=AsyncMock())
    assert await enrich_packet_cards(session, OWNER, [], None) == []
    session.execute.assert_not_called()
    session.scalars.assert_not_called()


async def test_list_service_forwards_filters_and_keyset_and_enriches_only_page(monkeypatch):
    identities = [UUID(int=i) for i in (101, 102, 103)]
    rows = [
        {
            "id": identity,
            "name": "Equal",
            "packet_id": identity,
            "year": 2020,
            "scope": "training",
            "sort_value": "equal",
        }
        for identity in identities
    ]
    queries = []

    async def execute(statement):
        queries.append(statement.compile(dialect=postgresql.dialect()))
        return SimpleNamespace(mappings=lambda: rows)

    @asynccontextmanager
    async def sessions():
        yield SimpleNamespace(execute=execute)

    guard = AsyncMock()
    enrich = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.training_packets.require_trainee", guard)
    monkeypatch.setattr("sitg_bot.services.training_packets.enrich_packet_cards", enrich)
    service = TrainingPacketService(SimpleNamespace(sessions=sessions))
    page = await service.list_packets(
        OWNER,
        name=" Equal ",
        author=" Writer ",
        year_from=2019,
        publication_to=2026,
        order="name_desc",
        limit=2,
    )
    assert len(page["items"]) == 2
    assert all("sort_value" not in item for item in page["items"])
    assert packet_cursor(
        page["next_cursor"],
        "name_desc",
        hashlib.sha256(
            json.dumps(
                dict(
                    name="equal",
                    author="writer",
                    year_from=2019,
                    year_to=None,
                    publication_from=None,
                    publication_to=2026,
                ),
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16],
    ) == ("equal", identities[1])
    params = list(queries[0].params.values())
    assert "equal" in params and "writer" in params and 2019 in params and 2026 in params
    guard.assert_awaited_once()
    assert len(enrich.await_args.args[2]) == 2


async def test_best_buzz_history_records_only_success_and_persisted_version():
    session = SimpleNamespace(execute=AsyncMock())
    moment = datetime.now(UTC)
    row = SimpleNamespace(
        kind="best_buzz",
        status="succeeded",
        owner_player_id=OWNER,
        source={"packet_revision_id": str(UUID(int=7))},
    )
    await AnalysisService._record_packet_completion(session, row, moment)
    statement = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    assert OWNER in statement.params.values() and UUID(int=7) in statement.params.values()
    assert moment in statement.params.values()
    for status in ("queued", "running", "failed", "cancelled", "partially_succeeded"):
        row.status = status
        await AnalysisService._record_packet_completion(session, row, moment)
    row.kind, row.status = "dissection", "succeeded"
    await AnalysisService._record_packet_completion(session, row, moment)
    assert session.execute.await_count == 1


async def test_link_creation_retry_returns_same_deactivated_link_without_new_token(monkeypatch):
    row = SimpleNamespace(
        id=UUID(int=8),
        owner_id=OWNER,
        token="a" * 43,
        created_at=datetime.now(UTC),
        deactivated_at=datetime.now(UTC),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=row), add=AsyncMock(), flush=AsyncMock()
    )

    @asynccontextmanager
    async def transaction():
        yield session

    app = KnowledgeApplication.__new__(KnowledgeApplication)
    app.database = SimpleNamespace(transaction=transaction)
    guard = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.training_knowledge.require_trainee", guard)
    result = await app.mutate(OWNER, "link_create", {"creation_key": "same-key"})
    assert result["id"] == row.id and not result["active"]
    session.add.assert_not_called()
    guard.assert_awaited_once_with(session, OWNER, lock=True)


async def test_saved_dissection_coverage_and_best_buzz_survive_missing_analysis_requests(
    monkeypatch,
):
    version, theme = UUID(int=7), UUID(int=8)
    results = iter(
        [[], [], [], [(version, theme)], [],
         [(str(version), "succeeded", None, [str(theme)], None)]]
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=lambda _: next(results)),
        scalars=AsyncMock(return_value=[version]),
    )
    monkeypatch.setattr(
        "sitg_bot.services.training_catalogue.SIContentAdapter.available_play_units",
        AsyncMock(return_value=[]),
    )
    cards = [{"id": version}]
    await enrich_packet_cards(
        session, OWNER, cards, TrainingPacketService._library_assignments(OWNER)
    )
    assert cards[0]["dissected_theme_count"] == 1
    assert cards[0]["best_buzz_calculated"]


async def test_link_application_checks_sorted_accounts_and_rechecks_revocation(monkeypatch):
    row = SimpleNamespace(
        id=UUID(int=8),
        owner_id=OTHER,
        token="a" * 43,
        created_at=datetime.now(UTC),
        deactivated_at=None,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=row), refresh=AsyncMock())
    app = KnowledgeApplication.__new__(KnowledgeApplication)
    guard = AsyncMock()
    monkeypatch.setattr("sitg_bot.services.training_knowledge.require_trainee", guard)

    async def revoke_on_refresh(value, **kwargs):
        value.deactivated_at = datetime.now(UTC)

    session.refresh.side_effect = revoke_on_refresh
    with pytest.raises(LookupError, match="unavailable"):
        await app._import_link(session, RECIPIENT, row.token)
    assert [call.args[1] for call in guard.await_args_list] == [RECIPIENT, OTHER]
    session.refresh.assert_awaited_once_with(row, with_for_update=True)
