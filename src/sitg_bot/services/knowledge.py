"""Trainee knowledge library service (training mode, Agent B).

Facts, tags, permanent copying and dissection application use cases. The service talks
to a narrow repository port; ``InMemoryKnowledgeStore`` serves recorded fixtures
and ``PostgresKnowledgeStore`` persists production collections (see
docs/training-knowledge-integration.md). Ownership is rechecked on every call;
copying never grants access to another trainee's source collection.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sitg_bot.domain.knowledge import (
    DISSECTION_INSTRUCTIONS_MAX,
    FACT_PAGE_SIZE,
    FACT_TAGS_MAX,
    AppliedCandidate,
    BingoScore,
    CandidateOutcome,
    CommonKnowledgeEstimate,
    DissectionApplication,
    DissectionCandidate,
    FactContent,
    FactProvenance,
    Mentions,
    SharingGrant,
    SourceLink,
    dissection_candidate_id,
    dissection_instruction_digest,
    is_base_dissection_request,
    normalize_tag_name,
    plan_candidate_application,
)

FactOrder = Literal["title_asc", "title_desc", "updated_desc", "created_desc"]
SUPPORTED_FACT_ORDERS: tuple[FactOrder, ...] = (
    "title_asc",
    "title_desc",
    "updated_desc",
    "created_desc",
)
DissectionRequestStatus = Literal["pending", "succeeded", "partially_succeeded", "failed"]
StatisticStatus = Literal["none", "pending", "fresh", "stale"]


DEFAULT_TAG_COLOUR = "#64748b"


def tag_colour(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value) is None:
        raise ValueError("Tag colour must be a six-digit RGB hex colour")
    return value.lower()


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TagView:
    tag_id: UUID
    name: str
    colour: str = DEFAULT_TAG_COLOUR


@dataclass(frozen=True, slots=True)
class FactListItem:
    fact_id: UUID
    owner_id: UUID
    title: str
    tag_names: tuple[str, ...]
    origin: str
    statistic_status: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FactPage:
    items: tuple[FactListItem, ...]
    total: int
    next_cursor: str | None
    order: FactOrder


@dataclass(frozen=True, slots=True)
class FactView:
    fact_id: UUID
    owner_id: UUID
    content: FactContent
    content_version: int
    provenance: FactProvenance
    tags: tuple[TagView, ...]
    statistic_status: str
    created_at: datetime
    updated_at: datetime
    editable: bool


@dataclass(frozen=True, slots=True)
class GrantView:
    grant_id: UUID
    owner_id: UUID
    recipient_id: UUID
    granted_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class FactCopy:
    source_fact_id: UUID
    recipient_id: UUID
    owner_id: UUID
    fact_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionCopyView:
    owner_id: UUID
    recipient_id: UUID
    copied_count: int


@dataclass(frozen=True, slots=True)
class StatisticSnapshotView:
    snapshot_id: UUID
    fact_id: UUID
    content_version: int
    mentions: Mentions | None
    common: CommonKnowledgeEstimate | None
    bingo: BingoScore | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DissectionRequestView:
    request_id: UUID
    packet_id: UUID
    packet_revision: str
    instructions: str
    comment: str
    instruction_digest: str
    base: bool
    status: DissectionRequestStatus
    created_at: datetime
    applied_at: datetime | None
    applied_fact_ids: frozenset[UUID]
    failed_candidate_ids: frozenset[str]


@dataclass
class StoredFact:
    id: UUID
    owner_id: UUID
    content: FactContent
    created_at: datetime
    updated_at: datetime
    provenance: FactProvenance = field(default_factory=FactProvenance)
    content_version: int = 1
    deleted_at: datetime | None = None
    merged_into_id: UUID | None = None
    applied_candidate_id: str | None = None
    applied_content_version: int | None = None
    statistic_status: str = "none"
    tag_ids: set[UUID] = field(default_factory=set)


@dataclass
class StoredTag:
    id: UUID
    owner_id: UUID
    name: str
    colour: str = DEFAULT_TAG_COLOUR


@dataclass
class StoredDissectionRequest:
    id: UUID
    owner_id: UUID
    packet_id: UUID
    packet_revision: str
    instructions: str
    comment: str
    instruction_digest: str
    base: bool
    status: DissectionRequestStatus
    created_at: datetime
    application: DissectionApplication | None = None


@dataclass
class StoredStatisticSnapshot:
    id: UUID
    fact_id: UUID
    owner_id: UUID
    content_version: int
    created_at: datetime
    mentions: Mentions | None = None
    common: CommonKnowledgeEstimate | None = None
    bingo: BingoScore | None = None


class InMemoryKnowledgeStore:
    """Fixture repository implementing the storage port for tests and demos.

    Agent A replaces this with a SQLAlchemy/PostgreSQL adapter exposing the
    same methods; the service never touches storage internals.
    """

    def __init__(self) -> None:
        self.facts: dict[UUID, StoredFact] = {}
        self.tags: dict[UUID, StoredTag] = {}
        self.grants: dict[UUID, SharingGrant] = {}
        self.requests: dict[UUID, StoredDissectionRequest] = {}
        self.snapshots: dict[UUID, StoredStatisticSnapshot] = {}
        self.copies: dict[tuple[UUID, UUID], FactCopy] = {}
        self.import_hashes: set[tuple[UUID, str]] = set()

    async def add_copy(self, copy: FactCopy) -> None:
        self.copies[(copy.source_fact_id, copy.recipient_id)] = copy

    async def copies_for_owner(self, owner_id: UUID) -> list[FactCopy]:
        return [copy for copy in self.copies.values() if copy.owner_id == owner_id]

    async def copies_for_recipient(self, recipient_id: UUID) -> list[FactCopy]:
        return [copy for copy in self.copies.values() if copy.recipient_id == recipient_id]

    async def add_fact(self, fact: StoredFact) -> StoredFact:
        self.facts[fact.id] = fact
        return fact

    async def get_fact(self, fact_id: UUID) -> StoredFact | None:
        return self.facts.get(fact_id)

    async def save_fact(self, fact: StoredFact) -> None:
        self.facts[fact.id] = fact

    async def facts_for_owner(self, owner_id: UUID) -> list[StoredFact]:
        return [fact for fact in self.facts.values() if fact.owner_id == owner_id]

    async def add_tag(self, tag: StoredTag) -> StoredTag:
        self.tags[tag.id] = tag
        return tag

    async def get_tag(self, tag_id: UUID) -> StoredTag | None:
        return self.tags.get(tag_id)

    async def save_tag(self, tag: StoredTag) -> None:
        self.tags[tag.id] = tag

    async def remove_tag(self, tag_id: UUID) -> None:
        self.tags.pop(tag_id, None)
        for fact in self.facts.values():
            fact.tag_ids.discard(tag_id)

    async def tags_for_owner(self, owner_id: UUID) -> list[StoredTag]:
        return [tag for tag in self.tags.values() if tag.owner_id == owner_id]

    async def add_grant(self, grant: SharingGrant) -> SharingGrant:
        self.grants[grant.grant_id] = grant
        return grant

    async def get_grant(self, grant_id: UUID) -> SharingGrant | None:
        return self.grants.get(grant_id)

    async def save_grant(self, grant: SharingGrant) -> None:
        self.grants[grant.grant_id] = grant

    async def grants_for_recipient(self, recipient_id: UUID) -> list[SharingGrant]:
        return [grant for grant in self.grants.values() if grant.recipient_id == recipient_id]

    async def grants_for_owner(self, owner_id: UUID) -> list[SharingGrant]:
        return [grant for grant in self.grants.values() if grant.owner_id == owner_id]

    async def add_request(self, request: StoredDissectionRequest) -> StoredDissectionRequest:
        self.requests[request.id] = request
        return request

    async def get_request(self, request_id: UUID) -> StoredDissectionRequest | None:
        return self.requests.get(request_id)

    async def save_request(self, request: StoredDissectionRequest) -> None:
        self.requests[request.id] = request

    async def requests_for_owner(
        self, owner_id: UUID, *, packet_id: UUID | None = None
    ) -> list[StoredDissectionRequest]:
        rows = [row for row in self.requests.values() if row.owner_id == owner_id]
        if packet_id is not None:
            rows = [row for row in rows if row.packet_id == packet_id]
        return rows

    async def add_snapshot(self, snapshot: StoredStatisticSnapshot) -> StoredStatisticSnapshot:
        self.snapshots[snapshot.id] = snapshot
        return snapshot

    async def snapshots_for_fact(self, fact_id: UUID) -> list[StoredStatisticSnapshot]:
        return [snapshot for snapshot in self.snapshots.values() if snapshot.fact_id == fact_id]


def _encode_cursor(key: tuple[str, str]) -> str:
    return base64.urlsafe_b64encode("\x00".join(key).encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = base64.urlsafe_b64decode(padded.encode()).decode()
        key, separator, fact_id = value.partition("\x00")
        if not separator or not key or not fact_id:
            raise ValueError("Malformed cursor payload")
        return key, fact_id
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Invalid facts cursor") from error


def dissection_candidate_from_payload(payload: Mapping[str, object]) -> DissectionCandidate:
    """Build a validated candidate from Agent C's extraction payload."""
    try:
        content_payload = payload["content"]
        if not isinstance(content_payload, Mapping):
            raise ValueError("Candidate content must be a mapping")
        links_payload = content_payload.get("links", ())
        if not isinstance(links_payload, Iterable) or isinstance(links_payload, (str, bytes)):
            raise ValueError("Candidate links must be a sequence")
        links = tuple(
            SourceLink(
                url=str(link["url"]),
                title=str(link.get("title", "")),
                position=int(link.get("position", index)),
            )
            for index, link in enumerate(links_payload)
            if isinstance(link, Mapping)
        )
        content = FactContent(
            title=str(content_payload["title"]),
            description=str(content_payload["description"]),
            links=links,
        )
        packet_id = UUID(str(payload["packet_id"]))
        question_id = UUID(str(payload["question_id"]))
        question_revision = str(payload["question_revision"])
        candidate_id = payload.get("candidate_id")
        if candidate_id is None:
            candidate_id = dissection_candidate_id(
                packet_id, question_id, question_revision, content
            )
        return DissectionCandidate(
            candidate_id=str(candidate_id),
            content=content,
            packet_id=packet_id,
            question_id=question_id,
            question_revision=question_revision,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid dissection candidate payload: {error}") from error


class KnowledgeService:
    """Owned facts, tags, permanent copies and dissection application."""

    def __init__(
        self,
        store: InMemoryKnowledgeStore,
        *,
        page_size: int = FACT_PAGE_SIZE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError("Knowledge page size must be positive")
        self.store = store
        self.page_size = page_size
        self.clock = clock or utcnow

    def _now(self) -> datetime:
        return self.clock()

    async def _require_view_access(self, actor_id: UUID, owner_id: UUID) -> None:
        if actor_id == owner_id:
            return
        raise PermissionError("Copies belong to the recipient; source collections remain private")

    @staticmethod
    def _require_owner(actor_id: UUID, owner_id: UUID) -> None:
        if actor_id != owner_id:
            raise PermissionError("Only the collection owner may mutate knowledge")

    async def _resolve_tag_ids(self, owner_id: UUID, tag_names: Iterable[str]) -> set[UUID]:
        names = [normalize_tag_name(name) for name in tag_names]
        if len(names) > FACT_TAGS_MAX:
            raise ValueError(f"A fact allows at most {FACT_TAGS_MAX} tags")
        existing = await self.store.tags_for_owner(owner_id)
        by_name = {tag.name: tag for tag in existing}
        resolved: set[UUID] = set()
        for name in dict.fromkeys(names):
            tag = by_name.get(name)
            if tag is None:
                tag = await self.store.add_tag(StoredTag(id=uuid4(), owner_id=owner_id, name=name))
            resolved.add(tag.id)
        return resolved

    async def _tag_views(self, owner_id: UUID, tag_ids: set[UUID]) -> tuple[TagView, ...]:
        tags = [tag for tag in await self.store.tags_for_owner(owner_id) if tag.id in tag_ids]
        tags.sort(key=lambda tag: tag.name)
        return tuple(TagView(tag.id, tag.name, tag.colour) for tag in tags)

    async def _fact_view(self, fact: StoredFact, *, editable: bool) -> FactView:
        return FactView(
            fact_id=fact.id,
            owner_id=fact.owner_id,
            content=fact.content,
            content_version=fact.content_version,
            provenance=fact.provenance,
            tags=await self._tag_views(fact.owner_id, fact.tag_ids),
            statistic_status=fact.statistic_status,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
            editable=editable,
        )

    async def _get_live_fact(self, fact_id: UUID) -> StoredFact:
        fact = await self.store.get_fact(fact_id)
        if fact is None or fact.deleted_at is not None:
            raise LookupError("Fact not found")
        return fact

    async def create_fact(
        self,
        owner_id: UUID,
        *,
        title: str,
        description: str,
        links: Iterable[SourceLink] = (),
        tag_names: Iterable[str] = (),
    ) -> FactView:
        content = FactContent(title=title, description=description, links=tuple(links))
        now = self._now()
        fact = StoredFact(
            id=uuid4(),
            owner_id=owner_id,
            content=content,
            created_at=now,
            updated_at=now,
            tag_ids=await self._resolve_tag_ids(owner_id, tag_names),
        )
        await self.store.add_fact(fact)
        return await self._fact_view(fact, editable=True)

    async def fact_detail(self, actor_id: UUID, fact_id: UUID) -> FactView:
        fact = await self._get_live_fact(fact_id)
        editable = actor_id == fact.owner_id
        if not editable:
            await self._require_view_access(actor_id, fact.owner_id)
        return await self._fact_view(fact, editable=editable)

    async def update_fact(
        self,
        owner_id: UUID,
        fact_id: UUID,
        *,
        expected_version: int,
        title: str | None = None,
        description: str | None = None,
        links: tuple[SourceLink, ...] | None = None,
        tag_names: Iterable[str] | None = None,
    ) -> FactView:
        fact = await self._get_live_fact(fact_id)
        self._require_owner(owner_id, fact.owner_id)
        if fact.content_version != expected_version:
            raise ValueError("Fact changed since it was read")
        new_content = fact.content
        if title is not None or description is not None or links is not None:
            new_content = FactContent(
                title=fact.content.title if title is None else title,
                description=fact.content.description if description is None else description,
                links=fact.content.links if links is None else links,
            )
        if new_content.changed(fact.content):
            fact.content = new_content
            fact.content_version += 1
            fact.updated_at = self._now()
            # Content changes invalidate current statistics; snapshots remain.
            fact.statistic_status = "stale"
        if tag_names is not None:
            fact.tag_ids = await self._resolve_tag_ids(owner_id, tag_names)
        await self.store.save_fact(fact)
        return await self._fact_view(fact, editable=True)

    async def delete_fact(
        self, owner_id: UUID, fact_id: UUID, *, confirm_dissection_origin: bool = False
    ) -> None:
        """Retire a fact; dissection imports need an explicit confirmation."""
        fact = await self._get_live_fact(fact_id)
        self._require_owner(owner_id, fact.owner_id)
        if fact.provenance.origin == "dissection" and not confirm_dissection_origin:
            raise ValueError(
                "Deleting a fact imported by dissection requires explicit confirmation"
            )
        fact.deleted_at = self._now()
        await self.store.save_fact(fact)

    async def merge_facts(
        self,
        owner_id: UUID,
        source_fact_id: UUID,
        target_fact_id: UUID,
        *,
        expected_target_version: int,
        confirm_source_delete: bool = False,
    ) -> FactView:
        """Explicit merge: union tags and source links, retire the source."""
        source = await self._get_live_fact(source_fact_id)
        target = await self._get_live_fact(target_fact_id)
        if source.id == target.id:
            raise ValueError("A fact cannot be merged into itself")
        self._require_owner(owner_id, source.owner_id)
        self._require_owner(owner_id, target.owner_id)
        if target.content_version != expected_target_version:
            raise ValueError("Fact changed since it was read")
        if not confirm_source_delete and source.provenance.origin == "manual":
            raise ValueError(
                "Merging away a manually maintained fact requires explicit confirmation"
            )
        target_urls = {link.url for link in target.content.links}
        added = tuple(
            link for link in source.content.links if link.url not in target_urls
        )
        merged_links = target.content.links + added
        merged_content = FactContent(
            title=target.content.title,
            description=target.content.description,
            links=merged_links,
        )
        target.tag_ids |= source.tag_ids
        if merged_content.changed(target.content):
            target.content = merged_content
            target.content_version += 1
            target.updated_at = self._now()
            target.statistic_status = "stale"
        source.deleted_at = self._now()
        source.merged_into_id = target.id
        await self.store.save_fact(source)
        await self.store.save_fact(target)
        return await self._fact_view(target, editable=True)

    async def list_facts(
        self,
        actor_id: UUID,
        *,
        owner_id: UUID | None = None,
        search: str | None = None,
        tag: str | None = None,
        order: FactOrder = "title_asc",
        cursor: str | None = None,
        limit: int | None = None,
    ) -> FactPage:
        """Filtered, sorted, cursor-paginated view of one collection."""
        view_owner = owner_id or actor_id
        await self._require_view_access(actor_id, view_owner)
        if order not in SUPPORTED_FACT_ORDERS:
            raise ValueError(f"Unsupported facts order {order!r}")
        page_limit = self.page_size if limit is None else max(1, min(limit, self.page_size))
        if hasattr(self.store, "fact_page"):
            return await self.store.fact_page(
                view_owner, search=search, tag=tag, order=order, cursor=cursor, limit=page_limit
            )
        rows = [
            fact
            for fact in await self.store.facts_for_owner(view_owner)
            if not fact.deleted_at
        ]
        if search and search.strip():
            needle = search.strip().casefold()
            rows = [
                fact
                for fact in rows
                if needle in fact.content.title.casefold()
                or needle in fact.content.description.casefold()
            ]
        if tag is not None and tag.strip():
            tag_name = normalize_tag_name(tag)
            owner_tags = await self.store.tags_for_owner(view_owner)
            matching_ids = {tag.id for tag in owner_tags if tag.name == tag_name}
            rows = [fact for fact in rows if fact.tag_ids & matching_ids]
        ascending = order == "title_asc"
        total = len(rows)

        def sort_key(fact: StoredFact) -> tuple[str, str]:
            if order in ("title_asc", "title_desc"):
                return (fact.content.title.casefold(), str(fact.id))
            if order == "updated_desc":
                return (fact.updated_at.isoformat(), str(fact.id))
            return (fact.created_at.isoformat(), str(fact.id))

        rows.sort(key=sort_key)
        if cursor is not None:
            cursor_key = _decode_cursor(cursor)
            rows = [
                fact
                for fact in rows
                if (sort_key(fact) > cursor_key if ascending else sort_key(fact) < cursor_key)
            ]
        if not ascending:
            rows.reverse()
        page = rows[:page_limit]
        has_more = len(rows) > page_limit
        next_cursor = _encode_cursor(sort_key(page[-1])) if page and has_more else None
        items = []
        for fact in page:
            tag_names = tuple(view.name for view in await self._tag_views(view_owner, fact.tag_ids))
            items.append(
                FactListItem(
                    fact_id=fact.id,
                    owner_id=fact.owner_id,
                    title=fact.content.title,
                    tag_names=tag_names,
                    origin=fact.provenance.origin,
                    statistic_status=fact.statistic_status,
                    updated_at=fact.updated_at,
                )
            )
        return FactPage(
            items=tuple(items), total=total, next_cursor=next_cursor, order=order
        )

    async def create_tag(
        self, owner_id: UUID, name: str, colour: str = DEFAULT_TAG_COLOUR
    ) -> TagView:
        normalized = normalize_tag_name(name)
        colour = tag_colour(colour)
        existing = await self.store.tags_for_owner(owner_id)
        if any(tag.name == normalized for tag in existing):
            raise ValueError("Tag already exists")
        tag = await self.store.add_tag(
            StoredTag(id=uuid4(), owner_id=owner_id, name=normalized, colour=colour)
        )
        return TagView(tag.id, tag.name, tag.colour)

    async def list_tags(
        self, actor_id: UUID, *, owner_id: UUID | None = None
    ) -> tuple[TagView, ...]:
        view_owner = owner_id or actor_id
        await self._require_view_access(actor_id, view_owner)
        tags = sorted(await self.store.tags_for_owner(view_owner), key=lambda tag: tag.name)
        return tuple(TagView(tag.id, tag.name, tag.colour) for tag in tags)

    async def rename_tag(
        self, owner_id: UUID, tag_id: UUID, new_name: str, colour: str | None = None
    ) -> TagView:
        tag = await self.store.get_tag(tag_id)
        if tag is None or tag.owner_id != owner_id:
            raise LookupError("Tag not found")
        normalized = normalize_tag_name(new_name)
        existing = await self.store.tags_for_owner(owner_id)
        if any(other.name == normalized and other.id != tag.id for other in existing):
            raise ValueError("Tag already exists")
        tag.name = normalized
        if colour is not None:
            tag.colour = tag_colour(colour)
        await self.store.save_tag(tag)
        return TagView(tag.id, tag.name, tag.colour)

    async def delete_tag(self, owner_id: UUID, tag_id: UUID) -> None:
        tag = await self.store.get_tag(tag_id)
        if tag is None or tag.owner_id != owner_id:
            raise LookupError("Tag not found")
        await self.store.remove_tag(tag_id)

    async def grant_collection_access(
        self, owner_id: UUID, recipient_id: UUID
    ) -> CollectionCopyView:
        """Send independent, permanent copies of the current collection.

        The legacy gateway operation name is retained. Repeated sends add only
        uncopied facts and never overwrite or resurrect a recipient's copy.
        """
        if owner_id == recipient_id:
            raise ValueError("A trainee cannot copy a collection to themselves")
        if hasattr(self.store, "copy_collection"):
            count = await self.store.copy_collection(owner_id, recipient_id, self._now())
            return CollectionCopyView(owner_id, recipient_id, count)
        copied = {
            copy.source_fact_id for copy in await self.store.copies_for_recipient(recipient_id)
        }
        existing = {
            fact.content.fingerprint() for fact in await self.store.facts_for_owner(recipient_id)
        }
        count = 0
        for source in await self.store.facts_for_owner(owner_id):
            if source.deleted_at is not None or source.id in copied:
                continue
            fingerprint = source.content.fingerprint()
            claim = recipient_id, fingerprint
            if claim in self.store.import_hashes:
                continue
            self.store.import_hashes.add(claim)
            if fingerprint in existing:
                continue
            existing.add(fingerprint)
            tags = await self._tag_views(owner_id, source.tag_ids)
            recipient_tags = {tag.name for tag in await self.store.tags_for_owner(recipient_id)}
            for tag in tags:
                if tag.name not in recipient_tags:
                    await self.create_tag(recipient_id, tag.name, tag.colour)
            detail = await self.create_fact(
                recipient_id,
                title=source.content.title,
                description=source.content.description,
                links=source.content.links,
                tag_names=[tag.name for tag in tags],
            )
            await self.store.add_copy(
                FactCopy(source.id, recipient_id, owner_id, detail.fact_id, self._now())
            )
            if source.statistic_status == "fresh":
                snapshots = await self.store.snapshots_for_fact(source.id)
                current = [
                    snapshot
                    for snapshot in snapshots
                    if snapshot.content_version == source.content_version
                ]
                if current:
                    latest = max(current, key=lambda snapshot: snapshot.created_at)
                    await self.record_statistics(
                        recipient_id,
                        detail.fact_id,
                        mentions=latest.mentions,
                        common=latest.common,
                        bingo=latest.bingo,
                    )
            count += 1
        return CollectionCopyView(owner_id, recipient_id, count)

    async def revoke_collection_access(self, owner_id: UUID, grant_id: UUID) -> GrantView:
        raise PermissionError("Shared copies are permanent and cannot be revoked")

    @staticmethod
    def _copy_summaries(copies: Iterable[FactCopy]) -> tuple[CollectionCopyView, ...]:
        counts: dict[tuple[UUID, UUID], int] = {}
        for copy in copies:
            key = copy.owner_id, copy.recipient_id
            counts[key] = counts.get(key, 0) + 1
        return tuple(
            CollectionCopyView(owner, recipient, count)
            for (owner, recipient), count in sorted(counts.items())
        )

    async def list_grants(self, owner_id: UUID) -> tuple[CollectionCopyView, ...]:
        if hasattr(self.store, "copy_summaries"):
            return await self.store.copy_summaries(owner_id=owner_id)
        return self._copy_summaries(await self.store.copies_for_owner(owner_id))

    async def shared_collections(self, recipient_id: UUID) -> tuple[CollectionCopyView, ...]:
        if hasattr(self.store, "copy_summaries"):
            return await self.store.copy_summaries(recipient_id=recipient_id)
        return self._copy_summaries(await self.store.copies_for_recipient(recipient_id))

    @staticmethod
    def _request_view(row: StoredDissectionRequest) -> DissectionRequestView:
        application = row.application
        return DissectionRequestView(
            request_id=row.id,
            packet_id=row.packet_id,
            packet_revision=row.packet_revision,
            instructions=row.instructions,
            comment=row.comment,
            instruction_digest=row.instruction_digest,
            base=row.base,
            status=row.status,
            created_at=row.created_at,
            applied_at=application.applied_at if application is not None else None,
            applied_fact_ids=(
                application.applied_fact_ids if application is not None else frozenset()
            ),
            failed_candidate_ids=(
                application.failed_candidate_ids if application is not None else frozenset()
            ),
        )

    async def request_dissection(
        self,
        owner_id: UUID,
        *,
        packet_id: UUID,
        packet_revision: str,
        instructions: str = "",
        comment: str = "",
    ) -> DissectionRequestView:
        """Record a dissection request; comments produce a new revision."""
        if len(instructions) > DISSECTION_INSTRUCTIONS_MAX:
            raise ValueError(
                f"Dissection instructions must be at most {DISSECTION_INSTRUCTIONS_MAX} characters"
            )
        if len(comment) > DISSECTION_INSTRUCTIONS_MAX:
            raise ValueError(
                f"Dissection comments must be at most {DISSECTION_INSTRUCTIONS_MAX} characters"
            )
        if not packet_revision.strip():
            raise ValueError("Packet revision must not be empty")
        digest = dissection_instruction_digest(instructions, comment)
        for row in await self.store.requests_for_owner(owner_id, packet_id=packet_id):
            if row.instruction_digest == digest and row.packet_revision == packet_revision:
                return self._request_view(row)
        row = StoredDissectionRequest(
            id=uuid4(),
            owner_id=owner_id,
            packet_id=packet_id,
            packet_revision=packet_revision,
            instructions=instructions,
            comment=comment,
            instruction_digest=digest,
            base=is_base_dissection_request(instructions, comment),
            status="pending",
            created_at=self._now(),
        )
        await self.store.add_request(row)
        return self._request_view(row)

    async def apply_dissection_result(
        self,
        owner_id: UUID,
        request_id: UUID,
        candidate_payloads: Iterable[Mapping[str, object]],
        *,
        excluded: Iterable[str] = (),
    ) -> DissectionRequestView:
        """Apply extraction candidates; retries never duplicate or overwrite."""
        row = await self.store.get_request(request_id)
        if row is None:
            raise LookupError("Dissection request not found")
        self._require_owner(owner_id, row.owner_id)
        if row.application is not None:
            # Idempotent retry: return the recorded application unchanged.
            return self._request_view(row)
        applied: dict[str, AppliedCandidate] = {}
        for fact in await self.store.facts_for_owner(owner_id):
            if fact.applied_candidate_id is None:
                continue
            applied[fact.applied_candidate_id] = AppliedCandidate(
                candidate_id=fact.applied_candidate_id,
                fact_id=fact.id,
                applied_content_version=fact.applied_content_version or 1,
                current_content_version=fact.content_version,
            )
        outcomes: list[CandidateOutcome] = []
        planned: dict[str, DissectionCandidate] = {}
        for payload in candidate_payloads:
            fallback_id = str(payload.get("candidate_id", "<unavailable>"))
            try:
                candidate = dissection_candidate_from_payload(payload)
                if candidate.packet_id != row.packet_id:
                    raise ValueError("Candidate belongs to another packet")
            except ValueError:
                outcomes.append(CandidateOutcome(fallback_id, "invalid"))
                continue
            planned[candidate.candidate_id] = candidate
        plan = plan_candidate_application(planned.values(), applied, excluded=excluded)
        now = self._now()
        for outcome in plan:
            if outcome.outcome != "applied":
                outcomes.append(outcome)
                continue
            candidate = planned[outcome.candidate_id]
            fact = StoredFact(
                id=uuid4(),
                owner_id=owner_id,
                content=candidate.content,
                created_at=now,
                updated_at=now,
                provenance=FactProvenance(
                    origin="dissection",
                    packet_id=candidate.packet_id,
                    question_id=candidate.question_id,
                    question_revision=candidate.question_revision,
                    request_id=row.id,
                ),
                applied_candidate_id=candidate.candidate_id,
                applied_content_version=1,
                statistic_status="none",
            )
            await self.store.add_fact(fact)
            outcomes.append(CandidateOutcome(candidate.candidate_id, "applied", fact.id))
        application = DissectionApplication(
            request_id=row.id,
            packet_id=row.packet_id,
            instruction_digest=row.instruction_digest,
            base=row.base,
            outcomes=tuple(outcomes),
            applied_at=now,
        )
        row.application = application
        row.status = "partially_succeeded" if application.failed_candidate_ids else "succeeded"
        await self.store.save_request(row)
        return self._request_view(row)

    async def mark_dissection_failed(
        self, owner_id: UUID, request_id: UUID
    ) -> DissectionRequestView:
        row = await self.store.get_request(request_id)
        if row is None:
            raise LookupError("Dissection request not found")
        self._require_owner(owner_id, row.owner_id)
        if row.application is None:
            row.status = "failed"
            await self.store.save_request(row)
        return self._request_view(row)

    async def dissection_request(
        self, owner_id: UUID, request_id: UUID
    ) -> DissectionRequestView:
        row = await self.store.get_request(request_id)
        # Requests are private to their owner; hide existence from others.
        if row is None or row.owner_id != owner_id:
            raise LookupError("Dissection request not found")
        return self._request_view(row)

    async def list_dissections(
        self, owner_id: UUID, *, packet_id: UUID | None = None
    ) -> tuple[DissectionRequestView, ...]:
        rows = await self.store.requests_for_owner(owner_id, packet_id=packet_id)
        rows.sort(key=lambda row: row.created_at)
        return tuple(self._request_view(row) for row in rows)

    async def record_statistics(
        self,
        owner_id: UUID,
        fact_id: UUID,
        *,
        mentions: Mentions | None = None,
        common: CommonKnowledgeEstimate | None = None,
        bingo: BingoScore | None = None,
    ) -> StatisticSnapshotView:
        """Persist an immutable snapshot; historical snapshots are kept."""
        fact = await self._get_live_fact(fact_id)
        self._require_owner(owner_id, fact.owner_id)
        snapshot = await self.store.add_snapshot(
            StoredStatisticSnapshot(
                id=uuid4(),
                fact_id=fact.id,
                owner_id=owner_id,
                content_version=fact.content_version,
                created_at=self._now(),
                mentions=mentions,
                common=common,
                bingo=bingo,
            )
        )
        fact.statistic_status = "fresh"
        await self.store.save_fact(fact)
        return self._snapshot_view(snapshot)

    async def statistic_snapshots(
        self, actor_id: UUID, fact_id: UUID
    ) -> tuple[StatisticSnapshotView, ...]:
        fact = await self._get_live_fact(fact_id)
        await self._require_view_access(actor_id, fact.owner_id)
        snapshots = await self.store.snapshots_for_fact(fact_id)
        snapshots.sort(key=lambda snapshot: snapshot.created_at)
        return tuple(self._snapshot_view(snapshot) for snapshot in snapshots)

    @staticmethod
    def _snapshot_view(snapshot: StoredStatisticSnapshot) -> StatisticSnapshotView:
        return StatisticSnapshotView(
            snapshot_id=snapshot.id,
            fact_id=snapshot.fact_id,
            content_version=snapshot.content_version,
            mentions=snapshot.mentions,
            common=snapshot.common,
            bingo=snapshot.bingo,
            created_at=snapshot.created_at,
        )
