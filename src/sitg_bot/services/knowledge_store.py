"""Session-scoped PostgreSQL repository for the knowledge service."""

import json
from dataclasses import asdict
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from sitg_bot.domain.knowledge import (
    BingoScore,
    CandidateOutcome,
    CommonKnowledgeEstimate,
    DissectionApplication,
    FactContent,
    FactProvenance,
    Mentions,
    QueryProvenance,
    SharingGrant,
    SourceLink,
    normalize_tag_name,
)
from sitg_bot.services.knowledge import (
    CollectionCopyView,
    FactListItem,
    FactPage,
    StoredDissectionRequest,
    StoredFact,
    StoredStatisticSnapshot,
    StoredTag,
    _decode_cursor,
    _encode_cursor,
)
from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    KnowledgeFactCopyRecord,
    KnowledgeFactRecord,
    KnowledgeFactTagRecord,
    KnowledgeGrantRecord,
    KnowledgeImportHashRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTagRecord,
)


def json_data(value):
    def encode(item):
        if isinstance(item, (UUID, datetime, date)):
            return str(item) if isinstance(item, UUID) else item.isoformat()
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError(type(item).__name__)

    return json.loads(json.dumps(asdict(value), default=encode))


def provenance(payload):
    values = dict(payload)
    for key in ("packet_id", "question_id", "request_id"):
        if values.get(key):
            values[key] = UUID(values[key])
    return FactProvenance(**values)


def application(payload):
    if payload is None:
        return None
    return DissectionApplication(
        request_id=UUID(payload["request_id"]),
        packet_id=UUID(payload["packet_id"]),
        instruction_digest=payload["instruction_digest"],
        base=payload["base"],
        applied_at=datetime.fromisoformat(payload["applied_at"]),
        outcomes=tuple(
            CandidateOutcome(
                item["candidate_id"],
                item["outcome"],
                UUID(item["fact_id"]) if item.get("fact_id") else None,
            )
            for item in payload["outcomes"]
        ),
    )


def snapshot_value(row):
    mentions = None
    if row.mentions:
        value = dict(row.mentions)
        value["retrieval_at"] = datetime.fromisoformat(value["retrieval_at"])
        value["question_ids"] = frozenset(value["question_ids"])
        value["corpus_as_of"] = (
            date.fromisoformat(value["corpus_as_of"]) if value.get("corpus_as_of") else None
        )
        value["queries"] = tuple(
            QueryProvenance(**{**item, "question_ids": frozenset(item["question_ids"])})
            for item in value["queries"]
        )
        mentions = Mentions(**value)
    common = None
    if row.common:
        value = dict(row.common)
        value["estimated_at"] = (
            datetime.fromisoformat(value["estimated_at"]) if value.get("estimated_at") else None
        )
        common = CommonKnowledgeEstimate(**value)
    return StoredStatisticSnapshot(
        row.id,
        row.fact_id,
        row.owner_id,
        row.content_version,
        row.created_at,
        mentions,
        common,
        BingoScore(**row.bingo) if row.bingo else None,
    )


class PostgresKnowledgeStore:
    async def copy_summaries(self, *, owner_id=None, recipient_id=None):
        model = KnowledgeFactCopyRecord
        condition = (
            model.owner_id == owner_id
            if owner_id is not None
            else model.recipient_id == recipient_id
        )
        rows = await self.session.execute(
            select(model.owner_id, model.recipient_id, func.count())
            .where(condition)
            .group_by(model.owner_id, model.recipient_id)
            .order_by(model.owner_id, model.recipient_id)
        )
        return tuple(
            CollectionCopyView(owner, recipient, count) for owner, recipient, count in rows
        )

    async def copy_collection(self, owner_id, recipient_id, now):
        """Copy in bounded batches under the caller's two-account lock."""
        total = 0
        cursor = None
        model = KnowledgeFactRecord
        while True:
            statement = select(model).where(
                model.owner_id == owner_id,
                model.deleted_at.is_(None),
                ~select(KnowledgeFactCopyRecord.source_fact_id)
                .where(
                    KnowledgeFactCopyRecord.source_fact_id == model.id,
                    KnowledgeFactCopyRecord.recipient_id == recipient_id,
                )
                .exists(),
            )
            if cursor is not None:
                statement = statement.where(model.id > cursor)
            rows = list(await self.session.scalars(statement.order_by(model.id).limit(100)))
            if not rows:
                break
            cursor = rows[-1].id
            # A durable unique recipient/hash claim is the concurrency boundary.
            # The caller also serializes account mutations using sorted account locks.
            hashes = {row.content_fingerprint for row in rows}
            existing = set(await self.session.scalars(
                select(model.content_fingerprint).where(
                    model.owner_id == recipient_id, model.content_fingerprint.in_(hashes)
                )
            ))
            claimed = set(await self.session.scalars(
                insert(KnowledgeImportHashRecord).values([
                    {"recipient_id": recipient_id, "content_fingerprint": digest}
                    for digest in sorted(hashes)
                ]).on_conflict_do_nothing().returning(KnowledgeImportHashRecord.content_fingerprint)
            ))
            selected = []
            for row in rows:
                if row.content_fingerprint in claimed and row.content_fingerprint not in existing:
                    selected.append(row)
                    claimed.remove(row.content_fingerprint)
            rows = selected
            if not rows:
                continue
            tags = await self._tags([row.id for row in rows])
            names = {name for source_tags in tags.values() for _, name in source_tags}
            if names:
                colours = dict((await self.session.execute(
                    select(KnowledgeTagRecord.name, KnowledgeTagRecord.colour).where(
                        KnowledgeTagRecord.owner_id == owner_id, KnowledgeTagRecord.name.in_(names)
                    )
                )).all())
                await self.session.execute(
                    insert(KnowledgeTagRecord)
                    .values(
                        [
                            {"id": uuid4(), "owner_id": recipient_id, "name": name,
                             "colour": colours[name]}
                            for name in sorted(names)
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["owner_id", "name"])
                )
            tag_ids = dict(
                (
                    await self.session.execute(
                        select(KnowledgeTagRecord.name, KnowledgeTagRecord.id).where(
                            KnowledgeTagRecord.owner_id == recipient_id,
                            KnowledgeTagRecord.name.in_(names),
                        )
                    )
                ).all()
            )
            snapshots = {
                row.fact_id: row
                for row in await self.session.scalars(
                    select(KnowledgeSnapshotRecord)
                    .where(KnowledgeSnapshotRecord.fact_id.in_([row.id for row in rows]))
                    .distinct(KnowledgeSnapshotRecord.fact_id)
                    .order_by(
                        KnowledgeSnapshotRecord.fact_id,
                        KnowledgeSnapshotRecord.created_at.desc(),
                        KnowledgeSnapshotRecord.id.desc(),
                    )
                )
            }
            facts, copies, associations, statistics = [], [], [], []
            for row in rows:
                copied_id = uuid4()
                snapshot = snapshots.get(row.id)
                fresh = (
                    row.statistic_status == "fresh"
                    and snapshot is not None
                    and snapshot.content_version == row.content_version
                )
                facts.append(
                    {
                        "id": copied_id,
                        "owner_id": recipient_id,
                        "title": row.title,
                        "title_key": row.title_key,
                        "description": row.description,
                        "links": row.links,
                        "provenance": {"origin": "manual"},
                        "content_fingerprint": row.content_fingerprint,
                        "content_version": 1,
                        "statistic_status": "fresh" if fresh else "none",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                copies.append(
                    {
                        "source_fact_id": row.id,
                        "recipient_id": recipient_id,
                        "owner_id": owner_id,
                        "fact_id": copied_id,
                        "created_at": now,
                    }
                )
                associations.extend(
                    {"fact_id": copied_id, "tag_id": tag_ids[name], "owner_id": recipient_id}
                    for _, name in tags.get(row.id, [])
                )
                if fresh:
                    statistics.append(
                        {
                            "id": uuid4(),
                            "fact_id": copied_id,
                            "owner_id": recipient_id,
                            "content_version": 1,
                            "created_at": now,
                            "mentions": snapshot.mentions,
                            "common": snapshot.common,
                            "bingo": snapshot.bingo,
                        }
                    )
            await self.session.execute(insert(KnowledgeFactRecord), facts)
            await self.session.execute(insert(KnowledgeFactCopyRecord), copies)
            if associations:
                await self.session.execute(insert(KnowledgeFactTagRecord), associations)
            if statistics:
                await self.session.execute(insert(KnowledgeSnapshotRecord), statistics)
            total += len(rows)
        return total

    def __init__(self, session):
        self.session = session

    async def _tags(self, fact_ids):
        if not fact_ids:
            return {}
        result = {}
        rows = await self.session.execute(
            select(
                KnowledgeFactTagRecord.fact_id,
                KnowledgeTagRecord.id,
                KnowledgeTagRecord.name,
            )
            .join(KnowledgeTagRecord, KnowledgeTagRecord.id == KnowledgeFactTagRecord.tag_id)
            .where(KnowledgeFactTagRecord.fact_id.in_(fact_ids))
            .order_by(KnowledgeTagRecord.name)
        )
        for fact_id, tag_id, name in rows:
            result.setdefault(fact_id, []).append((tag_id, name))
        return result

    @staticmethod
    def _fact(row, tags):
        return StoredFact(
            id=row.id,
            owner_id=row.owner_id,
            content=FactContent(
                row.title, row.description, tuple(SourceLink(**link) for link in row.links)
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
            provenance=provenance(row.provenance),
            content_version=row.content_version,
            deleted_at=row.deleted_at,
            merged_into_id=row.merged_into_id,
            applied_candidate_id=row.applied_candidate_id,
            applied_content_version=row.applied_content_version,
            statistic_status=row.statistic_status,
            tag_ids={item[0] for item in tags},
        )

    async def get_fact(self, fact_id):
        row = await self.session.get(KnowledgeFactRecord, fact_id)
        if row is None:
            return None
        tags = await self._tags([row.id])
        return self._fact(row, tags.get(row.id, []))

    async def facts_for_owner(self, owner_id):
        rows = list(
            await self.session.scalars(
                select(KnowledgeFactRecord).where(KnowledgeFactRecord.owner_id == owner_id)
            )
        )
        tags = await self._tags([row.id for row in rows])
        return [self._fact(row, tags.get(row.id, [])) for row in rows]

    async def add_fact(self, fact):
        self.session.add(KnowledgeFactRecord(id=fact.id, owner_id=fact.owner_id))
        await self.save_fact(fact)
        return fact

    async def save_fact(self, fact):
        with self.session.no_autoflush:
            row = await self.session.get(KnowledgeFactRecord, fact.id)
            if row is None:
                row = next(
                    item
                    for item in self.session.new
                    if isinstance(item, KnowledgeFactRecord) and item.id == fact.id
                )
        row.title, row.title_key, row.description = (
            fact.content.title,
            fact.content.title.casefold(),
            fact.content.description,
        )
        row.links = [asdict(link) for link in fact.content.links]
        row.provenance = json_data(fact.provenance)
        row.content_fingerprint, row.content_version = (
            fact.content.fingerprint(),
            fact.content_version,
        )
        row.created_at, row.updated_at = fact.created_at, fact.updated_at
        row.deleted_at, row.merged_into_id = fact.deleted_at, fact.merged_into_id
        row.applied_candidate_id, row.applied_content_version = (
            fact.applied_candidate_id,
            fact.applied_content_version,
        )
        row.statistic_status = fact.statistic_status
        await self.session.flush()
        await self.session.execute(
            delete(KnowledgeFactTagRecord).where(KnowledgeFactTagRecord.fact_id == fact.id)
        )
        self.session.add_all(
            KnowledgeFactTagRecord(
                fact_id=fact.id,
                tag_id=tag_id,
                owner_id=fact.owner_id,
            )
            for tag_id in fact.tag_ids
        )
        await self.session.flush()

    async def fact_page(self, owner_id, *, search, tag, order, cursor, limit):
        model = KnowledgeFactRecord
        conditions = [model.owner_id == owner_id, model.deleted_at.is_(None)]
        if search and search.strip():
            needle = (
                search.strip()
                .casefold()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            conditions.append(
                func.lower(model.title + " " + model.description).like(
                    "%" + needle + "%", escape="\\"
                )
            )
        if tag and tag.strip():
            conditions.append(
                select(KnowledgeFactTagRecord.fact_id)
                .join(KnowledgeTagRecord, KnowledgeTagRecord.id == KnowledgeFactTagRecord.tag_id)
                .where(
                    KnowledgeFactTagRecord.fact_id == model.id,
                    KnowledgeTagRecord.name == normalize_tag_name(tag),
                )
                .exists()
            )
        total = await self.session.scalar(
            select(func.count()).select_from(model).where(*conditions)
        )
        key = (
            model.title_key
            if order.startswith("title")
            else (model.updated_at if order == "updated_desc" else model.created_at)
        )
        ascending = order == "title_asc"
        if cursor:
            raw, fact_id = _decode_cursor(cursor)
            value = raw if order.startswith("title") else datetime.fromisoformat(raw)
            pair = tuple_(key, model.id)
            conditions.append(
                pair > (value, UUID(fact_id)) if ascending else pair < (value, UUID(fact_id))
            )
        direction = (lambda column: column.asc()) if ascending else (lambda column: column.desc())
        rows = list(
            await self.session.scalars(
                select(model)
                .where(*conditions)
                .order_by(direction(key), direction(model.id))
                .limit(limit + 1)
            )
        )
        page = rows[:limit]
        tags = await self._tags([row.id for row in page])
        last = page[-1] if page else None
        next_cursor = None
        if last is not None and len(rows) > limit:
            sort_value = (
                last.title_key
                if order.startswith("title")
                else (last.updated_at if order == "updated_desc" else last.created_at).isoformat()
            )
            next_cursor = _encode_cursor((sort_value, str(last.id)))
        return FactPage(
            tuple(
                FactListItem(
                    row.id,
                    row.owner_id,
                    row.title,
                    tuple(item[1] for item in tags.get(row.id, [])),
                    row.provenance["origin"],
                    row.statistic_status,
                    row.updated_at,
                )
                for row in page
            ),
            total or 0,
            next_cursor,
            order,
        )

    async def add_tag(self, tag):
        self.session.add(KnowledgeTagRecord(
            id=tag.id, owner_id=tag.owner_id, name=tag.name, colour=tag.colour
        ))
        await self.session.flush()
        return tag

    async def get_tag(self, tag_id):
        row = await self.session.get(KnowledgeTagRecord, tag_id)
        return StoredTag(row.id, row.owner_id, row.name, row.colour) if row else None

    async def save_tag(self, tag):
        row = await self.session.get(KnowledgeTagRecord, tag.id)
        row.name, row.colour = tag.name, tag.colour
        await self.session.flush()

    async def remove_tag(self, tag_id):
        await self.session.execute(
            delete(KnowledgeTagRecord).where(KnowledgeTagRecord.id == tag_id)
        )

    async def tags_for_owner(self, owner_id):
        return [
            StoredTag(row.id, row.owner_id, row.name, row.colour)
            for row in await self.session.scalars(
                select(KnowledgeTagRecord).where(KnowledgeTagRecord.owner_id == owner_id)
            )
        ]

    @staticmethod
    def _grant(row):
        return SharingGrant(row.id, row.owner_id, row.recipient_id, row.granted_at, row.revoked_at)

    async def add_grant(self, grant):
        row = (
            await self.session.execute(
                insert(KnowledgeGrantRecord)
                .values(
                    id=grant.grant_id,
                    owner_id=grant.owner_id,
                    recipient_id=grant.recipient_id,
                    granted_at=grant.granted_at,
                )
                .on_conflict_do_update(
                    index_elements=["owner_id", "recipient_id"],
                    set_={
                        "revoked_at": None,
                        "granted_at": grant.granted_at,
                    },
                )
                .returning(KnowledgeGrantRecord)
            )
        ).scalar_one()
        return self._grant(row)

    async def get_grant(self, grant_id):
        row = await self.session.get(KnowledgeGrantRecord, grant_id)
        return self._grant(row) if row else None

    async def save_grant(self, grant):
        row = await self.session.get(KnowledgeGrantRecord, grant.grant_id)
        row.revoked_at = grant.revoked_at
        await self.session.flush()

    async def grants_for_owner(self, owner_id):
        return [
            self._grant(row)
            for row in await self.session.scalars(
                select(KnowledgeGrantRecord).where(KnowledgeGrantRecord.owner_id == owner_id)
            )
        ]

    async def grants_for_recipient(self, recipient_id):
        return [
            self._grant(row)
            for row in await self.session.scalars(
                select(KnowledgeGrantRecord).where(
                    KnowledgeGrantRecord.recipient_id == recipient_id
                )
            )
        ]

    @staticmethod
    def _request(row):
        return StoredDissectionRequest(
            row.id,
            row.owner_id,
            row.packet_id,
            row.packet_revision,
            row.instructions,
            row.comment,
            row.instruction_digest,
            row.base,
            row.status,
            row.created_at,
            application(row.application),
        )

    async def add_request(self, request):
        self.session.add(
            KnowledgeDissectionRecord(
                id=request.id,
                owner_id=request.owner_id,
                packet_id=request.packet_id,
                packet_revision=request.packet_revision,
                instructions=request.instructions,
                comment=request.comment,
                instruction_digest=request.instruction_digest,
                base=request.base,
                status=request.status,
                created_at=request.created_at,
            )
        )
        await self.session.flush()
        return request

    async def get_request(self, request_id):
        row = await self.session.get(KnowledgeDissectionRecord, request_id)
        return self._request(row) if row else None

    async def save_request(self, request):
        row = await self.session.get(KnowledgeDissectionRecord, request.id)
        row.status = request.status
        row.application = json_data(request.application) if request.application else None
        await self.session.flush()

    async def requests_for_owner(self, owner_id, *, packet_id=None):
        statement = select(KnowledgeDissectionRecord).where(
            KnowledgeDissectionRecord.owner_id == owner_id
        )
        if packet_id:
            statement = statement.where(KnowledgeDissectionRecord.packet_id == packet_id)
        # Requests are operational history, bounded independently from fact pagination.
        return [
            self._request(row)
            for row in await self.session.scalars(
                statement.order_by(KnowledgeDissectionRecord.created_at.desc()).limit(100)
            )
        ]

    async def add_snapshot(self, snapshot):
        self.session.add(
            KnowledgeSnapshotRecord(
                id=snapshot.id,
                fact_id=snapshot.fact_id,
                owner_id=snapshot.owner_id,
                content_version=snapshot.content_version,
                created_at=snapshot.created_at,
                mentions=json_data(snapshot.mentions) if snapshot.mentions else None,
                common=json_data(snapshot.common) if snapshot.common else None,
                bingo=json_data(snapshot.bingo) if snapshot.bingo else None,
            )
        )
        await self.session.flush()
        return snapshot

    async def snapshots_for_fact(self, fact_id):
        rows = await self.session.scalars(
            select(KnowledgeSnapshotRecord)
            .where(KnowledgeSnapshotRecord.fact_id == fact_id)
            .order_by(KnowledgeSnapshotRecord.created_at.desc())
            .limit(100)
        )
        return [snapshot_value(row) for row in rows]
