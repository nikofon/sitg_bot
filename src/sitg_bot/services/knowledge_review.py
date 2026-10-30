"""Durable owner-private review snapshots and atomic selected-candidate approval."""

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import defer

from sitg_bot.domain.knowledge import FactContent, SourceLink
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.knowledge import _decode_cursor, _encode_cursor
from sitg_bot.services.knowledge_images import image_view
from sitg_bot.services.knowledge_store import PostgresKnowledgeStore
from sitg_bot.services.training_access import require_trainee
from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    KnowledgeFactCopyRecord,
    KnowledgeFactRecord,
    KnowledgeImportHashRecord,
    KnowledgeImportRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTagRecord,
    KnowledgeTagSeedRecord,
    LogicalPacketRecord,
    PacketVersionRecord,
    PlayerRecord,
)

DEFAULT_TAG_LABELS = (
    "Architecture",
    "Biology",
    "History",
    "Mathematics",
    "Physics",
    "Chemestry",
    "Religion",
    "Mythology",
    "Literature",
    "Cinema",
    "Videogames",
    "Sport",
    "Geography",
    "Astronomy",
    "Pop-culture",
    "Music",
    "Politics",
    "Art",
    "Europe",
    "Asia",
    "India",
    "Japan",
    "North America",
    "South America",
    "Africa",
    "Russia",
    "China",
    "USA",
)
TAG_LABELS = {name.casefold(): name for name in DEFAULT_TAG_LABELS}

# asyncpg allows at most 32767 bound parameters per statement; stay far below.
ATTACHMENT_METADATA_BATCH = 1000


async def seed_tags(session, actor):
    # The claim and inserts commit atomically; deletion never removes the claim.
    claimed = await session.scalar(
        insert(KnowledgeTagSeedRecord)
        .values(owner_id=actor)
        .on_conflict_do_nothing()
        .returning(KnowledgeTagSeedRecord.owner_id)
    )
    if claimed is not None:
        await session.execute(
            insert(KnowledgeTagRecord)
            .values(
                [
                    {"id": uuid4(), "owner_id": actor, "name": name.casefold(), "colour": "#64748b"}
                    for name in DEFAULT_TAG_LABELS
                ]
            )
            .on_conflict_do_nothing(index_elements=["owner_id", "name"])
        )


def content_of(candidate):
    value = candidate["content"]
    return FactContent(
        value["title"],
        value["description"],
        tuple(SourceLink(**link) for link in value.get("links", [])),
        tuple(UUID(value) for value in value.get("image_ids", [])),
        author=value.get("author", ""),
    )


def title_key(title):
    return " ".join(title.split()).casefold()


def category(candidate, hashes, titles):
    if content_of(candidate).fingerprint() in hashes:
        return "duplicates"
    return "potential_duplicates" if title_key(candidate["content"]["title"]) in titles else "new"


class KnowledgeReview:
    def __init__(self, application):
        self.app = application
        self.database = application.database

    async def stage_collection(self, session, creator, recipient, creation_key=None):
        if creator == recipient:
            raise ValueError("A trainee cannot import their own collection")
        key_hash = hashlib.sha256(creation_key.encode()).hexdigest() if creation_key else None
        if key_hash:
            existing = await session.scalar(
                select(KnowledgeImportRecord).where(
                    KnowledgeImportRecord.owner_id == recipient,
                    KnowledgeImportRecord.creator_id == creator,
                    KnowledgeImportRecord.creation_key_hash == key_hash,
                )
            )
            if existing is not None:
                return {
                    "import_id": existing.id,
                    "status": "pending",  # replay the original staging response
                    "owner_id": creator,
                    "recipient_id": recipient,
                    "imported_count": 0,
                    "skipped_count": 0,
                }
        rows = list(
            await session.scalars(
                select(KnowledgeFactRecord)
                .where(
                    KnowledgeFactRecord.owner_id == creator,
                    KnowledgeFactRecord.deleted_at.is_(None),
                )
                .order_by(KnowledgeFactRecord.id)
                .limit(10001)
            )
        )
        if len(rows) > 10000:
            raise ValueError("Collection exceeds the 10000 fact review limit")
        tags = await PostgresKnowledgeStore(session)._tags([row.id for row in rows])
        snapshots = await self.app._latest_snapshots(session, [row.id for row in rows])
        colours = dict(
            (
                await session.execute(
                    select(
                        KnowledgeTagRecord.name,
                        KnowledgeTagRecord.colour,
                    ).where(KnowledgeTagRecord.owner_id == creator)
                )
            ).all()
        )
        candidates = []
        for row in rows:
            snapshot = snapshots.get(row.id)
            candidates.append(
                {
                    "id": str(row.id),
                    "source_fact_id": str(row.id),
                    "content": {
                        "title": row.title,
                        "description": row.description,
                        "author": row.author,
                        "links": row.links,
                        "image_ids": row.image_ids,
                    },
                    "tag_names": [name for _, name in tags.get(row.id, [])],
                    "tag_colours": {name: colours[name] for _, name in tags.get(row.id, [])},
                    "snapshot": {
                        "mentions": snapshot.mentions,
                        "common": snapshot.common,
                        "bingo": snapshot.bingo,
                    }
                    if snapshot
                    and row.statistic_status == "fresh"
                    and snapshot.content_version == row.content_version
                    else None,
                }
            )
        review = KnowledgeImportRecord(
            id=uuid4(),
            owner_id=recipient,
            creator_id=creator,
            type="collection",
            creation_key_hash=key_hash,
            status="pending",
            candidates=candidates,
        )
        session.add(review)
        await session.flush()
        return {
            "import_id": review.id,
            "status": "pending",
            "owner_id": creator,
            "recipient_id": recipient,
            "imported_count": 0,
            "skipped_count": 0,
        }

    async def _cards(self, session, actor, rows):
        if not rows:
            return []
        names = dict(
            (
                await session.execute(
                    select(
                        PlayerRecord.id,
                        PlayerRecord.public_nickname,
                    ).where(PlayerRecord.id.in_({row.creator_id for row in rows}))
                )
            ).all()
        )
        dissections = {
            row.id: row
            for row in (
                await session.execute(
                    select(
                        KnowledgeDissectionRecord.id,
                        KnowledgeDissectionRecord.packet_revision,
                        KnowledgeDissectionRecord.analysis_request_id,
                    ).where(
                        KnowledgeDissectionRecord.id.in_(
                            {row.dissection_id for row in rows if row.dissection_id}
                        )
                    )
                )
            ).all()
        }
        packet_names = (
            dict(
                (
                    await session.execute(
                        select(
                            PacketVersionRecord.id,
                            PacketVersionRecord.name,
                        )
                        .join(
                            LogicalPacketRecord,
                            LogicalPacketRecord.id == PacketVersionRecord.packet_id,
                        )
                        .where(
                            PacketVersionRecord.id.in_(
                                {UUID(row.packet_revision) for row in dissections.values()}
                            ),
                            self.app.packets._readable(actor),
                        )
                    )
                ).all()
            )
            if dissections
            else {}
        )
        cards = []
        for row in rows:
            source = dissections.get(row.dissection_id)
            cards.append(
                {
                    "id": row.id,
                    "type": row.type,
                    "status": row.status,
                    "author": names.get(row.creator_id) or "",
                    "fact_count": row.fact_count
                    if hasattr(row, "fact_count")
                    else len(row.candidates),
                    "packet_name": packet_names.get(UUID(source.packet_revision))
                    if source
                    else None,
                    "request_id": source.analysis_request_id if source else None,
                    "created_at": row.created_at,
                }
            )
        return cards

    async def list(self, actor, *, cursor=None, limit=100):
        if not 1 <= limit <= 100:
            raise ValueError("Import page size must be between 1 and 100")
        conditions = [KnowledgeImportRecord.owner_id == actor]
        if cursor:
            created_at, identity = _decode_cursor(cursor)
            moment = datetime.fromisoformat(created_at)
            if moment.tzinfo is None:
                raise ValueError("Invalid import cursor timestamp")
            conditions.append(
                tuple_(KnowledgeImportRecord.created_at, KnowledgeImportRecord.id)
                < (moment, UUID(identity))
            )
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            rows = list(
                (
                    await session.execute(
                        select(
                            KnowledgeImportRecord.id,
                            KnowledgeImportRecord.owner_id,
                            KnowledgeImportRecord.creator_id,
                            KnowledgeImportRecord.type,
                            KnowledgeImportRecord.status,
                            KnowledgeImportRecord.dissection_id,
                            KnowledgeImportRecord.created_at,
                            func.jsonb_array_length(KnowledgeImportRecord.candidates).label(
                                "fact_count"
                            ),
                        )
                        .where(*conditions)
                        .order_by(
                            KnowledgeImportRecord.created_at.desc(), KnowledgeImportRecord.id.desc()
                        )
                        .limit(limit + 1)
                    )
                ).all()
            )
            page = rows[:limit]
            next_cursor = (
                _encode_cursor((page[-1].created_at.isoformat(), str(page[-1].id)))
                if len(rows) > limit else None
            )
            return {
                "items": await self._cards(session, actor, page),
                "next_cursor": next_cursor,
            }

    @staticmethod
    async def _owned(session, actor, import_id):
        row = await session.get(KnowledgeImportRecord, import_id, with_for_update=True)
        if row is None or row.owner_id != actor:
            raise LookupError("Import not found")
        return row

    @staticmethod
    async def _dedupe(session, actor, candidates):
        hashes = {content_of(value).fingerprint() for value in candidates}
        titles = {title_key(value["content"]["title"]) for value in candidates}
        rows = (
            await session.execute(
                select(KnowledgeFactRecord.content_fingerprint, KnowledgeFactRecord.title).where(
                    KnowledgeFactRecord.owner_id == actor,
                    KnowledgeFactRecord.deleted_at.is_(None),
                    (
                        KnowledgeFactRecord.content_fingerprint.in_(hashes)
                        | KnowledgeFactRecord.title_key.in_(titles)
                    ),
                )
            )
        ).all()
        return (
            {digest for digest, _ in rows if digest in hashes},
            {title_key(name) for _, name in rows if title_key(name) in titles},
        )

    async def detail(self, actor, import_id):
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            row = await self._owned(session, actor, import_id)
            if row.dissection_id:
                dissection = await session.get(KnowledgeDissectionRecord, row.dissection_id)
                await self.app.packets.authorize(session, actor, UUID(dissection.packet_revision))
            hashes, titles = await self._dedupe(session, actor, row.candidates)
            sections = {"new": [], "potential_duplicates": [], "duplicates": []}
            image_ids = sorted({
                UUID(image)
                for value in row.candidates
                for image in value["content"].get("image_ids", [])
            })
            # Review snapshots retain attachment access without granting source
            # packet rights; metadata loads in bounded batches (asyncpg bind limit).
            from sitg_bot.storage.models import KnowledgeImageRecord

            images = {}
            for offset in range(0, len(image_ids), ATTACHMENT_METADATA_BATCH):
                batch = image_ids[offset:offset + ATTACHMENT_METADATA_BATCH]
                for image in await session.scalars(
                    select(KnowledgeImageRecord)
                    .options(defer(KnowledgeImageRecord.data))
                    .where(KnowledgeImageRecord.id.in_(batch))
                ):
                    images[image.id] = image_view(image)
            for candidate in row.candidates:
                bucket = category(candidate, hashes, titles)
                content = content_of(candidate)
                sections[bucket].append(
                    {
                        "id": candidate["id"],
                        "title": content.title,
                        "description": content.description,
                        "author": content.author,
                        "links": [asdict(link) for link in content.links],
                        "images": [images[image] for image in content.image_ids if image in images],
                        "tag_names": candidate.get("tag_names", []),
                        "selected": bucket != "duplicates",
                    }
                )
            return {
                **(await self._cards(session, actor, [row]))[0],
                "sections": sections,
                "approved_count": (row.decision or {}).get("approved_count", 0),
            }

    async def decide(self, actor, *, import_id, decision, selected_candidate_ids=None):
        async with self.database.transaction() as session:
            # Lock source grants before the account, matching packet/game entry.
            preview = await session.get(KnowledgeImportRecord, import_id)
            if preview is None or preview.owner_id != actor:
                raise LookupError("Import not found")
            if preview.dissection_id:
                dissection = await session.get(KnowledgeDissectionRecord, preview.dissection_id)
                await self.app.packets.lock_access(session, actor, UUID(dissection.packet_revision))
            await require_trainee(session, actor, lock=True)
            row = await self._owned(session, actor, import_id)
            if decision == "approve" and selected_candidate_ids is None:
                raise ValueError("Explicit selected candidate IDs are required")
            if decision not in ("approve", "reject"):
                raise ValueError("Unknown import decision")
            selected = sorted(set(selected_candidate_ids or []))
            if set(selected) - {value["id"] for value in row.candidates}:
                raise ValueError("Unknown import candidate")
            identity = {"decision": decision, "selected_candidate_ids": selected}
            if row.decision is not None:
                if any(row.decision.get(key) != value for key, value in identity.items()):
                    raise StaleWriteError("Import decision is already final")
                return {
                    "import_id": row.id,
                    "status": row.status,
                    "approved_count": row.decision["approved_count"],
                    "skipped_count": row.decision["skipped_count"],
                }
            if row.status != "pending":
                raise ValueError("Import is not ready for approval")
            approved, skipped = 0, 0
            if decision == "approve":
                approved, skipped = await self._approve(session, row, set(selected))
            row.status = "approved" if decision == "approve" else "rejected"
            row.decision = {**identity, "approved_count": approved, "skipped_count": skipped}
            return {
                "import_id": row.id,
                "status": row.status,
                "approved_count": approved,
                "skipped_count": skipped,
            }

    async def _approve(self, session, row, selected):
        service = self.app.service(session)
        candidates, skipped = [], 0
        for value in row.candidates:
            if value["id"] not in selected:
                continue
            content = content_of(value)
            digest = content.fingerprint()
            # Include tombstones and durable historical claims: never resurrect deletions.
            existing = await session.scalar(
                select(KnowledgeFactRecord.id)
                .where(
                    KnowledgeFactRecord.owner_id == row.owner_id,
                    KnowledgeFactRecord.content_fingerprint == digest,
                )
                .limit(1)
            )
            copied = (
                await session.get(
                    KnowledgeFactCopyRecord, (UUID(value["source_fact_id"]), row.owner_id)
                )
                if value.get("source_fact_id")
                else None
            )
            claimed = await session.scalar(
                insert(KnowledgeImportHashRecord)
                .values(recipient_id=row.owner_id, content_fingerprint=digest)
                .on_conflict_do_nothing()
                .returning(KnowledgeImportHashRecord.content_fingerprint)
            )
            if existing is not None or copied is not None or claimed is None:
                skipped += 1
                continue
            candidates.append(value)
        if row.type == "dissection":
            dissection = await session.get(KnowledgeDissectionRecord, row.dissection_id)
            partial = dissection.status == "partially_succeeded"
            view = await service.apply_dissection_result(
                row.owner_id, row.dissection_id, candidates,
            )
            if view.failed_candidate_ids:
                raise ValueError("Dissection candidates could not be applied")
            if partial:
                dissection.status = "partially_succeeded"
                dissection.theme_revision_ids = None
            for fact_id in view.applied_fact_ids:
                fact = await service.store.get_fact(fact_id)
                candidate = next(
                    value
                    for value in candidates
                    if content_of(value).fingerprint() == fact.content.fingerprint()
                )
                # Removed tags must stay removed; analysis cannot silently recreate them.
                names = set(candidate.get("tag_names", []))
                tags = await service.store.tags_for_owner(row.owner_id)
                fact.tag_ids = {tag.id for tag in tags if tag.name in names}
                await service.store.save_fact(fact)
                await self.app._schedule_statistics(session, row.owner_id, fact_id, 1)
            return len(view.applied_fact_ids), skipped + len(candidates) - len(
                view.applied_fact_ids
            )
        palette = {tag.name for tag in await service.store.tags_for_owner(row.owner_id)}
        seeded = await session.get(KnowledgeTagSeedRecord, row.owner_id) is not None
        for candidate in candidates:
            content = content_of(candidate)
            names = [
                name
                for name in candidate.get("tag_names", [])
                if not seeded or name not in TAG_LABELS or name in palette
            ]
            colours = {
                name: colour
                for name, colour in candidate.get("tag_colours", {}).items()
                if name in names
            }
            if colours:
                await session.execute(
                    insert(KnowledgeTagRecord)
                    .values(
                        [
                            {
                                "id": uuid4(),
                                "owner_id": row.owner_id,
                                "name": name,
                                "colour": colour,
                            }
                            for name, colour in colours.items()
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["owner_id", "name"])
                )
            detail = await service.create_fact(
                row.owner_id,
                title=content.title,
                description=content.description,
                author=content.author,
                links=content.links,
                image_ids=content.image_ids,
                tag_names=names,
            )
            session.add(
                KnowledgeFactCopyRecord(
                    source_fact_id=UUID(candidate["source_fact_id"]),
                    recipient_id=row.owner_id,
                    owner_id=row.creator_id,
                    fact_id=detail.fact_id,
                    created_at=datetime.now(UTC),
                )
            )
            if candidate.get("snapshot"):
                session.add(
                    KnowledgeSnapshotRecord(
                        id=uuid4(),
                        fact_id=detail.fact_id,
                        owner_id=row.owner_id,
                        content_version=1,
                        created_at=datetime.now(UTC),
                        **candidate["snapshot"],
                    )
                )
                fact = await session.get(KnowledgeFactRecord, detail.fact_id)
                fact.statistic_status = "fresh"
        return len(candidates), skipped
