from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from sitg_bot.domain.knowledge import FactContent
from sitg_bot.services.knowledge_statistics import (
    CommonKnowledgeResult,
    CorpusInfo,
    FixtureCommonKnowledgePort,
    FixtureGotQuestionsConnector,
    KnowledgeStatisticsService,
)

OWNER = UUID(int=1)
TITLE = "Battle of Austerlitz"
ALIAS = "Battle of the Three Emperors"


def fact(title: str = TITLE, description: str = "1805 battle") -> FactContent:
    return FactContent(title=title, description=description)


def fixture_connector() -> FixtureGotQuestionsConnector:
    return FixtureGotQuestionsConnector(
        {
            "battle of austerlitz": [["q1", "q2"], ["q2", "q3"]],
            "battle of the three emperors": [["q3", "q4"]],
        },
        corpus=CorpusInfo(size=120_000, as_of=date(2026, 9, 30)),
    )


def statistics(
    connector: FixtureGotQuestionsConnector | None = None,
    common: FixtureCommonKnowledgePort | None = None,
    **overrides: object,
) -> KnowledgeStatisticsService:
    values: dict[str, object] = {
        "connector": connector or fixture_connector(),
        "common": common
        or FixtureCommonKnowledgePort(
            {TITLE: CommonKnowledgeResult(status="available", value=30, model="m")}
        ),
    }
    values.update(overrides)
    return KnowledgeStatisticsService(**values)  # type: ignore[arg-type]


async def test_fixture_connector_replays_recorded_pages_without_network():
    connector = fixture_connector()
    first = await connector.search("Battle of Austerlitz", 1)
    second = await connector.search("Battle of Austerlitz", 2)
    exhausted = await connector.search("Battle of Austerlitz", 3)
    assert first.question_ids == ("q1", "q2")
    assert second.question_ids == ("q2", "q3")
    assert exhausted.question_ids == ()
    assert (await connector.corpus()) == CorpusInfo(size=120_000, as_of=date(2026, 9, 30))


async def test_mentions_deduplicate_across_aliases_and_pages():
    service = statistics()
    mentions = await service.collect_mentions(fact(), aliases=(ALIAS,))
    assert mentions.distinct_count == 4
    assert mentions.question_ids == frozenset({"q1", "q2", "q3", "q4"})
    assert not mentions.capped
    assert mentions.complete_count() == 4
    assert mentions.corpus_size == 120_000
    assert mentions.corpus_as_of == date(2026, 9, 30)
    queries = {query.query: query for query in mentions.queries}
    assert queries["battle of austerlitz"].pages_fetched == 3
    assert queries["battle of the three emperors"].pages_fetched == 2
    # A repeated alias is issued once, not twice.
    duplicate_alias = await statistics().collect_mentions(
        fact(), aliases=(ALIAS, ALIAS.upper(), "  ")
    )
    assert len(duplicate_alias.queries) == 2


async def test_page_caps_mark_mentions_partial_never_complete():
    service = statistics(pages_per_query=2)
    mentions = await service.collect_mentions(fact(), aliases=(ALIAS,))
    title_query = mentions.queries[0]
    assert title_query.pages_fetched == 2
    assert title_query.capped
    assert mentions.capped
    assert mentions.complete_count() is None
    assert mentions.distinct_count == 4


async def test_mention_collection_is_bounded_per_query():
    connector = fixture_connector()
    service = statistics(connector, pages_per_query=1)
    await service.collect_mentions(fact(), aliases=(ALIAS,))
    # Every query issued at most pages_per_query searches.
    per_query: dict[str, int] = {}
    for query, _page in connector.calls:
        per_query[query] = per_query.get(query, 0) + 1
    assert per_query == {"battle of austerlitz": 1, "battle of the three emperors": 1}


async def test_query_results_refresh_only_after_ttl_expiry():
    connector = fixture_connector()
    current = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def clock() -> datetime:
        return current

    service = statistics(connector, clock=clock)
    await service.collect_mentions(fact())
    first_calls = len(connector.calls)
    assert first_calls == 3  # two title pages + one exhausted page
    await service.collect_mentions(fact(), aliases=(ALIAS,))
    # Only the alias query hit the connector; the title query stayed cached.
    assert len(connector.calls) == first_calls + 2
    # Advance past the freshness window; queries run again.
    current = current + timedelta(days=8)
    await service.collect_mentions(fact(), aliases=(ALIAS,))
    assert len(connector.calls) > first_calls + 2


async def test_common_knowledge_estimates_validate_and_label_metadata():
    common = FixtureCommonKnowledgePort(
        {
            TITLE: CommonKnowledgeResult(
                status="available", value=30, model="m", prompt_version="p", schema_version="s"
            ),
            "Unknown fact": CommonKnowledgeResult(status="unavailable"),
        }
    )
    service = statistics(common=common)
    estimate = await service.request_common_knowledge(fact(), language="ru", population="adults")
    assert estimate.status == "available"
    assert estimate.value == 30
    assert estimate.model == "m"
    assert estimate.prompt_version == "p"
    assert estimate.population == "adults"
    assert estimate.estimated_at is not None
    unavailable = await service.request_common_knowledge(
        fact("Unknown fact", "Never heard of it")
    )
    assert unavailable.status == "unavailable"
    assert unavailable.value is None
    invalid = FixtureCommonKnowledgePort(
        {TITLE: CommonKnowledgeResult(status="available", value=101)}
    )
    with pytest.raises(ValueError):
        await statistics(common=invalid).request_common_knowledge(fact())


async def test_fact_statistics_reports_pending_calibration_bingo():
    service = statistics()
    result = await service.fact_statistics(fact(), language="ru", population="adults")
    assert result.mentions.question_ids == frozenset({"q1", "q2", "q3"})
    assert result.mentions.complete_count() == 3
    assert result.common.value == 30
    assert result.bingo.status == "pending_calibration"
    assert result.bingo.value is None
    assert result.bingo.note is None


