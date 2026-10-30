"""Pure rules for the trainee knowledge library (training mode, Agent B).

Facts, tags, permanent copy ownership, mention statistics, Common
knowledge estimates, Bingo scoring and dissection candidate application rules.
Storage, gateway operations and adapters are integrated centrally by Agent A
per docs/training-knowledge-integration.md; these rules stay dependency-free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

FACT_TITLE_MAX = 200
FACT_DESCRIPTION_MAX = 4000
FACT_LINKS_MAX = 16
FACT_TAGS_MAX = 32
SOURCE_URL_MAX = 2000
SOURCE_TITLE_MAX = 200
TAG_NAME_MAX = 64
DISSECTION_INSTRUCTIONS_MAX = 4000
FACT_PAGE_SIZE = 25
LINK_SCHEMES = ("http://", "https://")

MENTIONS_POLICY_VERSION = "gq-phrase-dedup-v1"
BINGO_SCORING_VERSION = "bingo-pending-calibration-v0"

TagOrigin = Literal["manual", "dissection"]
EstimateStatus = Literal["available", "pending", "unavailable", "denied", "failed"]
BingoStatus = Literal[
    "scored",
    "pending_calibration",
    "unknown",
    "denied",
    "failed",
    "insufficient_data",
]
ApplicationOutcome = Literal[
    "applied", "duplicate", "manual_edit_preserved", "excluded", "invalid"
]

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_tag_name(name: str) -> str:
    """Single canonical form per trainee: collapsed, casefolded, trimmed."""
    normalized = _WHITESPACE_RE.sub(" ", name).strip().casefold()
    if not normalized:
        raise ValueError("Tag name must not be empty")
    if len(normalized) > TAG_NAME_MAX:
        raise ValueError(f"Tag name must be at most {TAG_NAME_MAX} characters")
    return normalized


def validate_source_url(url: str) -> str:
    value = url.strip()
    if not value.startswith(LINK_SCHEMES) or not urlsplit(value).netloc:
        raise ValueError("Source link must use the http or https scheme")
    if len(value) > SOURCE_URL_MAX or any(character.isspace() for character in value):
        raise ValueError("Source link must be a bounded URL without whitespace")
    return value


@dataclass(frozen=True, slots=True)
class SourceLink:
    """Ordered external reference; ``origin`` tracks how it was added."""

    url: str
    title: str = ""
    position: int = 0
    origin: TagOrigin = "manual"

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", validate_source_url(self.url))
        title = self.title.strip()
        if len(title) > SOURCE_TITLE_MAX:
            raise ValueError(f"Source title must be at most {SOURCE_TITLE_MAX} characters")
        object.__setattr__(self, "title", title)
        if (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or self.position < 0
        ):
            raise ValueError("Source link position must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class FactContent:
    """Editable text of a fact; statistics and provenance live elsewhere."""

    title: str
    description: str
    links: tuple[SourceLink, ...] = ()
    image_ids: tuple[UUID, ...] = ()
    author: str = ""

    def __post_init__(self) -> None:
        author = _WHITESPACE_RE.sub(" ", self.author).strip()
        if len(author) > 300:
            raise ValueError("Fact author must be at most 300 characters")
        object.__setattr__(self, "author", author)
        title = _WHITESPACE_RE.sub(" ", self.title).strip()
        description = self.description.strip()
        if not title:
            raise ValueError("Fact title must not be empty")
        if len(title) > FACT_TITLE_MAX:
            raise ValueError(f"Fact title must be at most {FACT_TITLE_MAX} characters")
        if not description:
            raise ValueError("Fact description must not be empty")
        if len(description) > FACT_DESCRIPTION_MAX:
            raise ValueError(f"Fact description must be at most {FACT_DESCRIPTION_MAX} characters")
        links = tuple(sorted(self.links, key=lambda link: (link.position, link.url)))
        if len(links) > FACT_LINKS_MAX:
            raise ValueError(f"A fact allows at most {FACT_LINKS_MAX} source links")
        urls = [link.url for link in links]
        if len(set(urls)) != len(urls):
            raise ValueError("Source links must not repeat the same URL")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "links", links)
        images = tuple(self.image_ids)
        if len(images) > 8 or len(set(images)) != len(images):
            raise ValueError("A fact allows at most eight distinct images")
        if any(not isinstance(image, UUID) for image in images):
            raise ValueError("Images must be attachment UUIDs")
        object.__setattr__(self, "image_ids", images)

    def fingerprint(self) -> str:
        """Stable digest of editable content; drives statistic invalidation."""
        payload = json.dumps(
            {
                "title": self.title,
                "description": self.description,
                "links": [
                    {"url": link.url, "title": link.title, "origin": link.origin}
                    for link in self.links
                ],
                **({"author": self.author} if self.author else {}),
                # Preserve existing no-image fingerprints and import claims.
                **({"image_ids": [str(image) for image in self.image_ids]}
                   if self.image_ids else {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return sha256(payload.encode()).hexdigest()

    def changed(self, other: FactContent) -> bool:
        return self.fingerprint() != other.fingerprint()


@dataclass(frozen=True, slots=True)
class FactProvenance:
    """Where a fact came from; dissection facts keep question attribution."""

    origin: TagOrigin = "manual"
    packet_id: UUID | None = None
    question_id: UUID | None = None
    question_revision: str | None = None
    request_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.origin == "manual":
            return
        missing = [
            name
            for name, value in (
                ("packet_id", self.packet_id),
                ("question_id", self.question_id),
                ("question_revision", self.question_revision),
                ("request_id", self.request_id),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"Dissection provenance requires {', '.join(missing)}")


@dataclass(frozen=True, slots=True)
class SharingGrant:
    """Historical grant record, retained only for upgrade compatibility."""

    grant_id: UUID
    owner_id: UUID
    recipient_id: UUID
    granted_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.recipient_id == self.owner_id:
            raise ValueError("A trainee cannot share a collection with themselves")

    def active(self) -> bool:
        return self.revoked_at is None


def can_view_collection(
    actor_id: UUID, owner_id: UUID, active_grants: Iterable[SharingGrant]
) -> bool:
    """A recipient owns independent copies; source collections stay private."""
    return actor_id == owner_id


def can_mutate_collection(actor_id: UUID, owner_id: UUID) -> bool:
    """Only the owner edits a collection, including independently received copies."""
    return actor_id == owner_id


@dataclass(frozen=True, slots=True)
class QueryProvenance:
    """One executed alias query: pages fetched and whether it was cut short."""

    query: str
    pages_fetched: int
    question_ids: frozenset[str]
    capped: bool


@dataclass(frozen=True, slots=True)
class Mentions:
    """Deduplicated GotQuestions matches for one fact, with query provenance."""

    question_ids: frozenset[str]
    retrieval_at: datetime
    policy_version: str = MENTIONS_POLICY_VERSION
    queries: tuple[QueryProvenance, ...] = ()
    capped: bool = False
    corpus_size: int | None = None
    corpus_as_of: date | None = None

    @property
    def distinct_count(self) -> int:
        return len(self.question_ids)

    def complete_count(self) -> int | None:
        """A capped/partial result is never presented as a complete count."""
        return None if self.capped else len(self.question_ids)


def build_mentions(
    per_query: Iterable[QueryProvenance],
    *,
    retrieval_at: datetime,
    policy_version: str = MENTIONS_POLICY_VERSION,
    corpus_size: int | None = None,
    corpus_as_of: date | None = None,
) -> Mentions:
    """Distinct matching question IDs, deduplicated across aliases and pages."""
    queries = tuple(per_query)
    if queries:
        question_ids = frozenset().union(*(query.question_ids for query in queries))
    else:
        question_ids = frozenset()
    return Mentions(
        question_ids=question_ids,
        retrieval_at=retrieval_at,
        policy_version=policy_version,
        queries=queries,
        capped=any(query.capped for query in queries),
        corpus_size=corpus_size,
        corpus_as_of=corpus_as_of,
    )


def validate_common_knowledge_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Common knowledge percentage must be an integer")
    if not 0 <= value <= 100:
        raise ValueError("Common knowledge percentage must be within 0..100")
    return value


@dataclass(frozen=True, slots=True)
class CommonKnowledgeEstimate:
    """LLM estimate with retained model/prompt/version/time metadata."""

    status: EstimateStatus
    value: int | None = None
    model: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    population: str = ""
    estimated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status == "available":
            if self.value is None or self.estimated_at is None:
                raise ValueError("An available estimate requires a value and a timestamp")
            validate_common_knowledge_value(self.value)
        elif self.value is not None:
            raise ValueError("Only available estimates carry a value")


@dataclass(frozen=True, slots=True)
class BingoScore:
    """Versioned bingo value; unknown, denied and failed are never zero."""

    status: BingoStatus
    value: int | None = None
    scoring_version: str = BINGO_SCORING_VERSION
    note: str | None = None

    def __post_init__(self) -> None:
        if self.status == "scored":
            if self.value is None:
                raise ValueError("A scored bingo result requires a value")
            validate_common_knowledge_value(self.value)
        elif self.value is not None:
            raise ValueError("Only scored bingo results carry a value")


class BingoScoring:
    """Versioned interface consuming Mentions and Common knowledge%.

    The final percentage formula awaits calibration against the supplied
    bingo examples; until then every result is ``pending_calibration`` and
    never a number. A calibrated formula is injected explicitly.
    """

    def __init__(
        self,
        *,
        version: str = BINGO_SCORING_VERSION,
        formula: object | None = None,
    ) -> None:
        if formula is not None and not callable(formula):
            raise ValueError("A calibrated bingo formula must be callable")
        self.version = version
        self.formula = formula

    def score(
        self, mentions: Mentions, common: CommonKnowledgeEstimate
    ) -> BingoScore:
        if common.status == "denied":
            return BingoScore(status="denied", scoring_version=self.version)
        if common.status == "failed":
            return BingoScore(status="failed", scoring_version=self.version)
        if common.status in ("pending", "unavailable"):
            # Zero knowledge and missing estimates are explicit, never zero.
            return BingoScore(
                status="insufficient_data",
                scoring_version=self.version,
                note=f"common knowledge {common.status}",
            )
        if common.value == 0 and not mentions.question_ids:
            return BingoScore(
                status="insufficient_data",
                scoring_version=self.version,
                note="zero knowledge and no mentions",
            )
        if self.formula is None:
            note = (
                None
                if mentions.corpus_size is not None
                else "corpus size unknown; frequency not normalized"
            )
            return BingoScore(
                status="pending_calibration",
                scoring_version=self.version,
                note=note,
            )
        value = validate_common_knowledge_value(self.formula(mentions, common))
        return BingoScore(status="scored", value=value, scoring_version=self.version)


def dissection_instruction_digest(instructions: str, comment: str = "") -> str:
    """Instruction identity: comments create a new dissection revision."""
    return sha256(f"{instructions}\n{comment}".encode()).hexdigest()[:16]


def is_base_dissection_request(instructions: str, comment: str = "") -> bool:
    """Only empty instructions and comments use the shared base artifact."""
    return instructions == "" and comment == ""


def dissection_candidate_id(
    packet_id: UUID, question_id: UUID, question_revision: str, content: FactContent
) -> str:
    """Deterministic candidate identity: retries map to the same candidate."""
    digest = sha256(
        (
            f"{packet_id}\n{question_id}\n{question_revision}\n{content.fingerprint()}"
        ).encode()
    ).hexdigest()[:16]
    return f"dc_{digest}"


@dataclass(frozen=True, slots=True)
class DissectionCandidate:
    """Versioned fact candidate produced by extraction (Agent C)."""

    candidate_id: str
    content: FactContent
    packet_id: UUID
    question_id: UUID
    question_revision: str

    def __post_init__(self) -> None:
        expected = dissection_candidate_id(
            self.packet_id, self.question_id, self.question_revision, self.content
        )
        if self.candidate_id != expected:
            raise ValueError("Candidate ID does not match its provenance and content")
        if not self.question_revision.strip():
            raise ValueError("Candidate question revision must not be empty")


@dataclass(frozen=True, slots=True)
class AppliedCandidate:
    """Candidate-to-fact mapping; detects later manual edits."""

    candidate_id: str
    fact_id: UUID
    applied_content_version: int
    current_content_version: int

    def __post_init__(self) -> None:
        if min(self.applied_content_version, self.current_content_version) < 1:
            raise ValueError("Applied candidates reference positive content versions")

    def manually_edited(self) -> bool:
        return self.current_content_version > self.applied_content_version


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    candidate_id: str
    outcome: ApplicationOutcome
    fact_id: UUID | None = None


def plan_candidate_application(
    candidates: Iterable[DissectionCandidate],
    applied: Mapping[str, AppliedCandidate],
    *,
    excluded: Iterable[str] = frozenset(),
) -> tuple[CandidateOutcome, ...]:
    """Idempotent plan: duplicates and manual edits are never overwritten.

    ``applied`` maps candidate IDs already applied to this collection;
    ``excluded`` lists candidate IDs the trainee declined. Within one batch a
    repeated candidate ID is a duplicate.
    """
    excluded_ids = frozenset(excluded)
    seen: set[str] = set()
    outcomes: list[CandidateOutcome] = []
    for candidate in candidates:
        if candidate.candidate_id in seen:
            outcomes.append(
                CandidateOutcome(candidate.candidate_id, "duplicate")
            )
            continue
        seen.add(candidate.candidate_id)
        if candidate.candidate_id in excluded_ids:
            outcomes.append(CandidateOutcome(candidate.candidate_id, "excluded"))
            continue
        existing = applied.get(candidate.candidate_id)
        if existing is None:
            outcomes.append(CandidateOutcome(candidate.candidate_id, "applied"))
        elif existing.manually_edited():
            outcomes.append(
                CandidateOutcome(
                    candidate.candidate_id, "manual_edit_preserved", existing.fact_id
                )
            )
        else:
            outcomes.append(
                CandidateOutcome(candidate.candidate_id, "duplicate", existing.fact_id)
            )
    return tuple(outcomes)


@dataclass(frozen=True, slots=True)
class DissectionApplication:
    """Snapshot of one application run; partial work can be retried safely."""

    request_id: UUID
    packet_id: UUID
    instruction_digest: str
    base: bool
    outcomes: tuple[CandidateOutcome, ...]
    applied_at: datetime

    @property
    def applied_fact_ids(self) -> frozenset[UUID]:
        return frozenset(
            outcome.fact_id
            for outcome in self.outcomes
            if outcome.outcome == "applied" and outcome.fact_id is not None
        )

    @property
    def failed_candidate_ids(self) -> frozenset[str]:
        return frozenset(
            outcome.candidate_id for outcome in self.outcomes if outcome.outcome == "invalid"
        )

    @property
    def status(self) -> str:
        if self.failed_candidate_ids:
            return "partially_succeeded"
        return "succeeded"
