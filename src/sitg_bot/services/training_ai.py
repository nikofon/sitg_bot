"""Provider-neutral AI analysis boundary for training mode.

Agent C module (see `docs/training-mode-plan.md`). It defines the structured
operations shared by dissection, Best buzz and Common knowledge analyses, their
validation rules, and a scripted provider used by tests and recorded fixtures.
The concrete provider/model choice is a pending Stage 0 decision; nothing in
this module contacts a network by itself, and packet content or instructions
are always data, never tools or authorization.
"""

import asyncio
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

ANALYSIS_DISSECTION = "dissection"
ANALYSIS_BEST_BUZZ = "best_buzz"
ANALYSIS_COMMON_KNOWLEDGE = "common_knowledge"
ANALYSIS_KINDS = (ANALYSIS_DISSECTION, ANALYSIS_BEST_BUZZ, ANALYSIS_COMMON_KNOWLEDGE)

DEFAULT_LANGUAGE = "en"
DEFAULT_POPULATION = "general"


class AnalysisError(Exception):
    """Base class for analysis-boundary failures with stable semantics."""


PROVIDER_CALL_METADATA: ContextVar[dict | None] = ContextVar("training_provider_call", default=None)


class SourceTooLarge(AnalysisError):
    """A single question exceeds the configured packet chunk bound."""


class AnalysisValidationError(AnalysisError):
    """Provider output failed schema, provenance or boundary validation."""


class ProviderTimeout(AnalysisError):
    """A provider call exceeded the configured deadline."""


class ProviderOutcomeUncertain(AnalysisError):
    """An upstream call may have been billed before an uncertain outcome.

    Local idempotency cannot guarantee a single provider charge for these, so
    the provider request identity must be recorded with the artifact.
    """

    def __init__(self, message: str, *, provider_request_id: str) -> None:
        super().__init__(message)
        self.provider_request_id = provider_request_id


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Server-side provider configuration; credentials are never logged."""

    provider: str
    model: str
    timeout: timedelta = timedelta(seconds=120)
    max_output_facts: int = 200
    max_chunk_characters: int = 12_000
    max_concurrent_requests: int = 2
    prompt_version: str = "1"
    schema_version: str = "1"
    tokenizer_version: str = "announcement-v1"
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise ValueError("Provider name and model are required")
        if self.timeout <= timedelta(0):
            raise ValueError("Provider timeout must be positive")
        if self.max_output_facts < 1 or self.max_output_facts > 1000:
            raise ValueError("Provider output fact bound must be between 1 and 1000")
        if self.max_chunk_characters < 100:
            raise ValueError("Provider chunk bound must allow at least 100 characters")
        if self.max_concurrent_requests < 1:
            raise ValueError("Provider concurrency must be positive")

    def redacted(self) -> dict[str, Any]:
        """Return the settings without the credential, for safe logging."""
        return {
            "provider": self.provider,
            "model": self.model,
            "timeout": self.timeout.total_seconds(),
            "max_output_facts": self.max_output_facts,
            "max_chunk_characters": self.max_chunk_characters,
            "max_concurrent_requests": self.max_concurrent_requests,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "tokenizer_version": self.tokenizer_version,
        }


def text_fingerprint(*parts: str) -> str:
    """Return a stable SHA-256 fingerprint for a tuple of text parts."""
    digest = sha256()
    for part in parts:
        digest.update(part.encode())
        digest.update(b"\x1f")
    return digest.hexdigest()


def instruction_digest(instructions: str) -> str:
    """Digest normalized instruction text; empty instructions are shared."""
    return text_fingerprint(instructions)


EMPTY_INSTRUCTION_DIGEST = instruction_digest("")


@dataclass(frozen=True, slots=True)
class QuestionText:
    """An immutable authorized question revision supplied for analysis."""

    question_revision_id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class SourceChunk:
    """A bounded batch of marked question texts for one provider scan."""

    index: int
    question_revision_ids: tuple[UUID, ...]
    text: str
    questions: tuple[QuestionText, ...] = ()


def packet_chunks(
    questions: Sequence[QuestionText], *, max_chunk_characters: int
) -> tuple[SourceChunk, ...]:
    """Pack questions deterministically into bounded, never-split batches.

    Each question is emitted as a ``[revision-id]``-marked block so provider
    output can be tied back to its provenance. A single question longer than
    the bound is rejected instead of silently truncating analysis input.
    """
    if max_chunk_characters < 1:
        raise ValueError("Chunk character bound must be positive")
    chunks: list[SourceChunk] = []
    blocks: list[str] = []
    identifiers: list[UUID] = []
    batch: list[QuestionText] = []
    length = 0
    for question in questions:
        block = f"[{question.question_revision_id}]\n{question.text}"
        if len(block) > max_chunk_characters:
            raise SourceTooLarge(
                f"Question {question.question_revision_id} exceeds the "
                f"{max_chunk_characters}-character chunk bound"
            )
        if blocks and length + len(block) + 2 > max_chunk_characters:
            chunks.append(
                SourceChunk(len(chunks), tuple(identifiers), "\n\n".join(blocks), tuple(batch))
            )
            blocks, identifiers, length = [], [], 0
            batch = []
        blocks.append(block)
        identifiers.append(question.question_revision_id)
        batch.append(question)
        length += len(block) + 2
    if blocks:
        chunks.append(
            SourceChunk(len(chunks), tuple(identifiers), "\n\n".join(blocks), tuple(batch))
        )
    return tuple(chunks)


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """A versioned fact candidate destined for B's dissection application."""

    candidate_id: str
    title: str
    description: str
    links: tuple[str, ...]
    question_revision_id: UUID


def fact_candidate_id(
    question_revision_id: UUID, title: str, description: str, links: Sequence[str]
) -> str:
    """Deterministically derive a candidate ID from its provenance and content."""
    return text_fingerprint(
        "fact", str(question_revision_id), title.strip(), description.strip(), *links
    )


@dataclass(frozen=True, slots=True)
class BuzzBoundary:
    """A validated Best buzz pause boundary for one question revision."""

    question_revision_id: UUID
    text_digest: str
    token_index: int


@dataclass(frozen=True, slots=True)
class CommonKnowledgeEstimate:
    """An LLM common-knowledge estimate labeled as such (never a measurement)."""

    value: int


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    chunks: tuple[SourceChunk, ...]
    instructions: str
    language: str


@dataclass(frozen=True, slots=True)
class BuzzRequest:
    chunks: tuple[SourceChunk, ...]
    language: str
    target_chars: int = 10


@dataclass(frozen=True, slots=True)
class CommonKnowledgeRequest:
    fact_content: str
    language: str
    population: str


@dataclass(frozen=True, slots=True)
class DissectionSource:
    """An authorized packet revision to dissect into fact candidates."""

    packet_revision_id: UUID
    question_revision_ids: tuple[UUID, ...]
    language: str = DEFAULT_LANGUAGE


@dataclass(frozen=True, slots=True)
class BestBuzzSource:
    """An authorized packet revision with pinned tokenizer settings."""

    packet_revision_id: UUID
    question_revision_ids: tuple[UUID, ...]
    question_token_target_chars: int = 10
    language: str = DEFAULT_LANGUAGE


@dataclass(frozen=True, slots=True)
class CommonKnowledgeSource:
    """Fact content, language and population assumptions to estimate."""

    fact_content: str
    language: str = DEFAULT_LANGUAGE
    population: str = DEFAULT_POPULATION


AnalysisSource = DissectionSource | BestBuzzSource | CommonKnowledgeSource


def source_kind(source: AnalysisSource) -> str:
    """Return the analysis kind of a typed source."""
    if isinstance(source, DissectionSource):
        return ANALYSIS_DISSECTION
    if isinstance(source, BestBuzzSource):
        return ANALYSIS_BEST_BUZZ
    if isinstance(source, CommonKnowledgeSource):
        return ANALYSIS_COMMON_KNOWLEDGE
    raise ValueError(f"Unknown analysis source {source!r}")


def source_descriptor(source: AnalysisSource) -> dict[str, Any]:
    """Serialize a typed source into its stored request descriptor."""
    kind = source_kind(source)
    if kind == ANALYSIS_DISSECTION:
        return {
            "type": kind,
            "packet_revision_id": str(source.packet_revision_id),
            "question_revision_ids": [str(item) for item in source.question_revision_ids],
            "language": source.language,
        }
    if kind == ANALYSIS_BEST_BUZZ:
        return {
            "type": kind,
            "packet_revision_id": str(source.packet_revision_id),
            "question_revision_ids": [str(item) for item in source.question_revision_ids],
            "question_token_target_chars": source.question_token_target_chars,
            "language": source.language,
        }
    return {
        "type": kind,
        "fact_content": source.fact_content,
        "language": source.language,
        "population": source.population,
    }


def source_from_descriptor(descriptor: Mapping[str, Any]) -> AnalysisSource:
    """Rebuild a typed source from its stored request descriptor."""
    kind = descriptor.get("type")
    if kind == ANALYSIS_DISSECTION:
        return DissectionSource(
            packet_revision_id=UUID(descriptor["packet_revision_id"]),
            question_revision_ids=tuple(
                UUID(item) for item in descriptor["question_revision_ids"]
            ),
            language=str(descriptor.get("language", DEFAULT_LANGUAGE)),
        )
    if kind == ANALYSIS_BEST_BUZZ:
        return BestBuzzSource(
            packet_revision_id=UUID(descriptor["packet_revision_id"]),
            question_revision_ids=tuple(
                UUID(item) for item in descriptor["question_revision_ids"]
            ),
            question_token_target_chars=int(descriptor.get("question_token_target_chars", 10)),
            language=str(descriptor.get("language", DEFAULT_LANGUAGE)),
        )
    if kind == ANALYSIS_COMMON_KNOWLEDGE:
        return CommonKnowledgeSource(
            fact_content=str(descriptor["fact_content"]),
            language=str(descriptor.get("language", DEFAULT_LANGUAGE)),
            population=str(descriptor.get("population", DEFAULT_POPULATION)),
        )
    raise ValueError(f"Unknown analysis source type {kind!r}")


def source_fingerprint_of(source: AnalysisSource) -> str:
    """Fingerprint the immutable content/revision identity of a source."""
    if isinstance(source, DissectionSource):
        return text_fingerprint(
            ANALYSIS_DISSECTION,
            str(source.packet_revision_id),
            *(str(item) for item in source.question_revision_ids),
            source.language,
        )
    if isinstance(source, BestBuzzSource):
        return text_fingerprint(
            ANALYSIS_BEST_BUZZ,
            str(source.packet_revision_id),
            *(str(item) for item in source.question_revision_ids),
            str(source.question_token_target_chars),
            source.language,
        )
    if isinstance(source, CommonKnowledgeSource):
        return text_fingerprint(
            ANALYSIS_COMMON_KNOWLEDGE, source.fact_content, source.language, source.population
        )
    raise ValueError(f"Unknown analysis source {source!r}")



class AnalysisProvider(Protocol):
    """Structured analysis operations every provider must implement.

    Implementations keep credentials server-side, expose upstream request IDs
    through the optional ``provider_request_ids`` attribute for accounting,
    and treat packet content and instructions strictly as data.
    """

    async def extract_facts(self, request: ExtractionRequest) -> tuple[FactCandidate, ...]: ...

    async def best_buzz_boundaries(self, request: BuzzRequest) -> tuple[BuzzBoundary, ...]: ...

    async def estimate_common_knowledge(
        self, request: CommonKnowledgeRequest
    ) -> CommonKnowledgeEstimate: ...


def validate_http_url(url: str) -> str:
    """Accept only absolute http(s) URLs as authoritative references."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AnalysisValidationError(
            f"Reference link is not an absolute http(s) URL: {url!r}"
        )
    return parsed.geturl()


def validate_fact_candidates(
    candidates: Sequence[FactCandidate],
    *,
    allowed_question_revision_ids: Collection[UUID],
    max_output_facts: int,
) -> tuple[FactCandidate, ...]:
    """Validate provider fact candidates against provenance and bounds."""
    if len(candidates) > max_output_facts:
        raise AnalysisValidationError(
            f"Provider returned {len(candidates)} facts, above the "
            f"{max_output_facts}-fact output bound"
        )
    validated: list[FactCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        title = candidate.title.strip()
        description = candidate.description.strip()
        if not title or not description:
            raise AnalysisValidationError(
                "Fact candidates require a title and a description"
            )
        if len(title) > 200 or len(description) > 4000 or len(candidate.links) > 16:
            raise AnalysisValidationError("Fact candidate text exceeds the length bound")
        if candidate.question_revision_id not in allowed_question_revision_ids:
            raise AnalysisValidationError(
                "Fact candidate cites unknown question revision "
                f"{candidate.question_revision_id}"
            )
        links = tuple(validate_http_url(link) for link in candidate.links)
        if len(set(links)) != len(links):
            raise AnalysisValidationError("Fact candidate repeats a reference link")
        expected = fact_candidate_id(candidate.question_revision_id, title, description, links)
        if candidate.candidate_id != expected:
            raise AnalysisValidationError(
                f"Fact candidate ID {candidate.candidate_id!r} is not deterministic"
            )
        if expected in seen:
            raise AnalysisValidationError("Duplicate fact candidate content")
        seen.add(expected)
        validated.append(
            FactCandidate(expected, title, description, links, candidate.question_revision_id)
        )
    return tuple(validated)


def validate_buzz_boundaries(
    boundaries: Sequence[BuzzBoundary],
    *,
    token_counts: Mapping[UUID, int],
    text_digests: Mapping[UUID, str],
) -> tuple[BuzzBoundary, ...]:
    """Validate Best buzz boundaries against the announcement token stream."""
    covered: set[UUID] = set()
    for boundary in boundaries:
        revision_id = boundary.question_revision_id
        if revision_id not in token_counts:
            raise AnalysisValidationError(
                f"Best buzz boundary cites unknown question revision {revision_id}"
            )
        if revision_id in covered:
            raise AnalysisValidationError(
                f"Duplicate Best buzz boundary for question revision {revision_id}"
            )
        covered.add(revision_id)
        if boundary.text_digest != text_digests[revision_id]:
            raise AnalysisValidationError(
                f"Best buzz boundary digest does not match question revision {revision_id}"
            )
        if (
            isinstance(boundary.token_index, bool)
            or not isinstance(boundary.token_index, int)
            or not 0 <= boundary.token_index <= token_counts[revision_id]
        ):
            raise AnalysisValidationError(
                f"Best buzz boundary index {boundary.token_index} is outside the "
                f"{token_counts[revision_id]}-token question stream"
            )
    missing = set(token_counts) - covered
    if missing:
        raise AnalysisValidationError(
            f"Best buzz boundaries are missing {len(missing)} question revision(s)"
        )
    return tuple(boundaries)


def validate_common_knowledge(value: int) -> int:
    """Validate a common-knowledge estimate as an integer percentage."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise AnalysisValidationError(
            f"Common knowledge estimate {value!r} is outside the 0-100 range"
        )
    return value


def chunk_questions(chunk: SourceChunk) -> tuple[tuple[UUID, str], ...]:
    if chunk.questions:
        return tuple((question.question_revision_id, question.text) for question in chunk.questions)
    return tuple(_parse_question_blocks(chunk.text))


def _parse_question_blocks(text: str) -> list[tuple[UUID, str]]:
    questions: list[tuple[UUID, str]] = []
    for block in text.split("\n\n"):
        header, _, body = block.partition("\n")
        try:
            revision_id = UUID(header.removeprefix("[").removesuffix("]"))
        except ValueError as error:
            raise AnalysisValidationError(
                "Chunk block is missing its revision marker"
            ) from error
        questions.append((revision_id, body))
    return questions


class FakeAnalysisProvider:
    """Scripted, deterministic provider for tests and recorded fixtures.

    Failures and uncertain outcomes are injected through the ``*_failures``
    queues and consumed one per call. Optional ``*_response`` hooks script
    outputs; the defaults derive deterministic results from the request.
    """

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.extract_calls: list[ExtractionRequest] = []
        self.buzz_calls: list[BuzzRequest] = []
        self.knowledge_calls: list[CommonKnowledgeRequest] = []
        self.extract_failures: list[BaseException] = []
        self.buzz_failures: list[BaseException] = []
        self.knowledge_failures: list[BaseException] = []
        self.fact_response: (
            Callable[[ExtractionRequest], tuple[FactCandidate, ...]] | None
        ) = None
        self.buzz_response: Callable[[BuzzRequest], tuple[BuzzBoundary, ...]] | None = None
        self.knowledge_response: (
            Callable[[CommonKnowledgeRequest], CommonKnowledgeEstimate] | None
        ) = None
        self.provider_request_ids: list[str] = []
        self._in_flight = 0
        self.max_in_flight = 0

    def _enter(self) -> str:
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        request_id = uuid.uuid4().hex
        self.provider_request_ids.append(request_id)
        return request_id

    def _exit(self) -> None:
        self._in_flight -= 1

    @staticmethod
    def _next_failure(failures: list[BaseException]) -> BaseException | None:
        if failures:
            return failures.pop(0)
        return None

    async def _settle(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)

    @staticmethod
    def _uncertain(
        failure: BaseException, provider_request_id: str
    ) -> BaseException:
        _ = provider_request_id
        return failure


    async def extract_facts(self, request: ExtractionRequest) -> tuple[FactCandidate, ...]:
        self.extract_calls.append(request)
        request_id = self._enter()
        try:
            await self._settle()
            failure = self._next_failure(self.extract_failures)
            if failure is not None:
                raise self._uncertain(failure, request_id)
            if self.fact_response is not None:
                return self.fact_response(request)
            candidates: list[FactCandidate] = []
            for chunk in request.chunks:
                for index, (revision_id, _) in enumerate(chunk_questions(chunk)):
                    title = f"Fact {index}"
                    description = f"Derived fact for question {revision_id}"
                    links = (f"https://example.org/facts/{index}",)
                    candidates.append(
                        FactCandidate(
                            fact_candidate_id(revision_id, title, description, links),
                            title,
                            description,
                            links,
                            revision_id,
                        )
                    )
            return tuple(candidates)
        finally:
            self._exit()

    async def best_buzz_boundaries(self, request: BuzzRequest) -> tuple[BuzzBoundary, ...]:
        self.buzz_calls.append(request)
        request_id = self._enter()
        try:
            await self._settle()
            failure = self._next_failure(self.buzz_failures)
            if failure is not None:
                raise self._uncertain(failure, request_id)
            if self.buzz_response is not None:
                return self.buzz_response(request)
            boundaries: list[BuzzBoundary] = []
            for chunk in request.chunks:
                for revision_id, text in chunk_questions(chunk):
                    boundaries.append(BuzzBoundary(revision_id, text_fingerprint(text), 0))
            return tuple(boundaries)
        finally:
            self._exit()

    async def estimate_common_knowledge(
        self, request: CommonKnowledgeRequest
    ) -> CommonKnowledgeEstimate:
        self.knowledge_calls.append(request)
        request_id = self._enter()
        try:
            await self._settle()
            failure = self._next_failure(self.knowledge_failures)
            if failure is not None:
                raise self._uncertain(failure, request_id)
            if self.knowledge_response is not None:
                return self.knowledge_response(request)
            return CommonKnowledgeEstimate(50)
        finally:
            self._exit()
