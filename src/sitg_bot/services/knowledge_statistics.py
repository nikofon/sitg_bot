"""Automatic knowledge statistics (training mode, Agent B).

GotQuestions mentions collection behind a replaceable connector protocol,
Common knowledge% requests through Agent C's port, and the versioned Bingo
scoring interface. The fixture connector replays recorded pages so tests and
development run without live provider calls or throttled traffic; the real
connector must first verify a supported programmatic access method against
the official FAQ (https://gotquestions.online/faq).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sitg_bot.domain.knowledge import (
    BingoScore,
    BingoScoring,
    CommonKnowledgeEstimate,
    FactContent,
    Mentions,
    QueryProvenance,
    build_mentions,
    validate_common_knowledge_value,
)

DEFAULT_PAGES_PER_QUERY = 5
DEFAULT_QUERY_TTL = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class GotQuestionsPage:
    """One connector page of search results for a query."""

    question_ids: tuple[str, ...]
    total: int | None
    capped: bool
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class CorpusInfo:
    size: int | None
    as_of: date | None


class GotQuestionsConnector(Protocol):
    """Replaceable search boundary; the fixture never performs network I/O."""

    async def search(self, query: str, page: int) -> GotQuestionsPage: ...

    async def corpus(self) -> CorpusInfo | None: ...


class FixtureGotQuestionsConnector:
    """Deterministic recorded fixture: query -> sequence of result pages."""

    def __init__(
        self,
        pages: Mapping[str, Sequence[Sequence[str]]] | None = None,
        *,
        corpus: CorpusInfo | None = None,
    ) -> None:
        self.pages = {
            query: [tuple(ids) for ids in value] for query, value in (pages or {}).items()
        }
        self.corpus_info = corpus
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, page: int) -> GotQuestionsPage:
        self.calls.append((query, page))
        recorded = self.pages.get(query.strip().casefold(), [])
        index = page - 1
        ids = recorded[index] if 0 <= index < len(recorded) else ()
        return GotQuestionsPage(
            question_ids=ids,
            total=None,
            capped=False,
            retrieved_at=datetime.now(UTC),
        )

    async def corpus(self) -> CorpusInfo | None:
        return self.corpus_info


@dataclass(frozen=True, slots=True)
class CommonKnowledgeResult:
    """Agent C's raw estimate before validation and metadata attachment."""

    status: str
    value: int | None = None
    model: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None


class CommonKnowledgePort(Protocol):
    """Agent C boundary for Common knowledge% estimation."""

    async def estimate(
        self, *, content: str, language: str, population: str
    ) -> CommonKnowledgeResult: ...


class FixtureCommonKnowledgePort:
    """Deterministic fixture keyed by fact title."""

    def __init__(self, results: Mapping[str, CommonKnowledgeResult] | None = None) -> None:
        self.results = {key.casefold(): value for key, value in (results or {}).items()}
        self.calls: list[str] = []

    async def estimate(
        self, *, content: str, language: str, population: str
    ) -> CommonKnowledgeResult:
        title = content.splitlines()[0].strip().casefold() if content else ""
        self.calls.append(title)
        if title in self.results:
            return self.results[title]
        return CommonKnowledgeResult(status="unavailable")


@dataclass(frozen=True, slots=True)
class FactStatistics:
    """Computed statistics for one fact at one point in time."""

    mentions: Mentions
    common: CommonKnowledgeEstimate
    bingo: BingoScore


@dataclass
class _CachedQuery:
    provenance: QueryProvenance
    retrieved_at: datetime


class KnowledgeStatisticsService:
    """Mentions, Common knowledge% and Bingo for one fact at a time.

    Mention queries are cached with a freshness window; every collection is
    bounded per query, so one dissection cannot fan out unbounded provider
    traffic. Statistics run as separate bounded jobs scheduled by the caller.
    """

    def __init__(
        self,
        connector: GotQuestionsConnector,
        common: CommonKnowledgePort,
        *,
        bingo: BingoScoring | None = None,
        pages_per_query: int = DEFAULT_PAGES_PER_QUERY,
        query_ttl: timedelta = DEFAULT_QUERY_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if pages_per_query < 1:
            raise ValueError("Pages per query must be positive")
        self.connector = connector
        self.common = common
        self.bingo = bingo or BingoScoring()
        self.pages_per_query = pages_per_query
        self.query_ttl = query_ttl
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._query_cache: dict[str, _CachedQuery] = {}

    def _now(self) -> datetime:
        return self.clock()

    async def _run_query(self, query: str) -> QueryProvenance:
        """Page through one query until exhaustion or the page cap."""
        ids: set[str] = set()
        pages = 0
        capped = False
        while pages < self.pages_per_query:
            page = await self.connector.search(query, pages + 1)
            pages += 1
            ids.update(page.question_ids)
            if not page.question_ids or page.capped:
                capped = capped or page.capped
                break
        else:
            # The cap stopped pagination; the result is partial, never complete.
            capped = True
        return QueryProvenance(
            query=query,
            pages_fetched=pages,
            question_ids=frozenset(ids),
            capped=capped,
        )

    async def _query_provenance(self, query: str) -> QueryProvenance:
        normalized = query.strip().casefold()
        cached = self._query_cache.get(normalized)
        now = self._now()
        if cached is not None and now - cached.retrieved_at <= self.query_ttl:
            return cached.provenance
        provenance = await self._run_query(normalized)
        self._query_cache[normalized] = _CachedQuery(provenance, now)
        return provenance

    async def collect_mentions(
        self,
        fact: FactContent,
        *,
        aliases: tuple[str, ...] = (),
    ) -> Mentions:
        """Distinct question IDs deduplicated across aliases and pages."""
        queries = [fact.title]
        queries.extend(alias for alias in aliases if alias.strip())
        provenance: list[QueryProvenance] = []
        seen: set[str] = set()
        for query in queries:
            normalized = query.strip().casefold()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            provenance.append(await self._query_provenance(query))
        corpus = await self.connector.corpus()
        return build_mentions(
            provenance,
            retrieval_at=self._now(),
            corpus_size=corpus.size if corpus is not None else None,
            corpus_as_of=corpus.as_of if corpus is not None else None,
        )

    async def request_common_knowledge(
        self,
        fact: FactContent,
        *,
        language: str = "ru",
        population: str = "",
    ) -> CommonKnowledgeEstimate:
        """Request, validate and label the LLM estimate with its metadata."""
        content = f"{fact.title}\n{fact.description}"
        result = await self.common.estimate(
            content=content, language=language, population=population
        )
        value = None
        if result.status == "available":
            value = validate_common_knowledge_value(result.value)
        return CommonKnowledgeEstimate(
            status=result.status,  # type: ignore[arg-type]
            value=value,
            model=result.model,
            prompt_version=result.prompt_version,
            schema_version=result.schema_version,
            population=population,
            estimated_at=self._now() if result.status == "available" else None,
        )

    async def fact_statistics(
        self,
        fact: FactContent,
        *,
        aliases: tuple[str, ...] = (),
        language: str = "ru",
        population: str = "",
    ) -> FactStatistics:
        mentions = await self.collect_mentions(fact, aliases=aliases)
        common = await self.request_common_knowledge(
            fact, language=language, population=population
        )
        return FactStatistics(
            mentions=mentions,
            common=common,
            bingo=self.bingo.score(mentions, common),
        )

