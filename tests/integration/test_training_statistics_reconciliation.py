from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import create_player
from sqlalchemy import select

from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.models import (
    KnowledgeFactRecord,
    KnowledgeSnapshotRecord,
    PlayerRecord,
    TrainingAnalysisArtifactRecord,
    TrainingAnalysisRequestRecord,
)

pytestmark = pytest.mark.integration


async def test_statistics_reconciliation_skips_obsolete_and_unavailable_prefixes(
    database,
):
    unavailable, owner = sorted([
        await create_player(database, 9501, "statistics-prefix"),
        await create_player(database, 9502, "statistics-current"),
    ])
    now = datetime.now(UTC)
    async with database.transaction() as session:
        player = await session.get(PlayerRecord, unavailable)
        player.status = "registration"
        artifacts = {}
        for actor in (unavailable, owner):
            artifact = TrainingAnalysisArtifactRecord(
                id=uuid4(), artifact_key=uuid4().hex, kind="common_knowledge", shared=False,
                owner_player_id=actor, source_fingerprint="fixture", instruction_digest="fixture",
                language="en", model="fixture-model", prompt_version="1", schema_version="1",
                tokenizer_version="1", status="succeeded", payload={"value": 25}, completed_at=now,
            )
            session.add(artifact)
            artifacts[actor] = artifact.id
        await session.flush()

        def add(index, actor, *, version=1, request_version=1, request_owner=None,
                status="succeeded", deleted=False, fresh=False):
            fact = KnowledgeFactRecord(
                id=UUID(int=index), owner_id=actor, title=f"Fact {index}",
                title_key=f"fact {index}", description="Description",
                content_fingerprint=str(index),
                content_version=version, statistic_status="fresh" if fresh else "pending",
                deleted_at=now if deleted else None,
            )
            session.add(fact)
            request_owner = request_owner or actor
            session.add(TrainingAnalysisRequestRecord(
                id=uuid4(), request_id=f"statistics-{index}", owner_player_id=request_owner,
                artifact_id=artifacts[request_owner], kind="common_knowledge", status=status,
                completed_at=now,
                source={"fact_id": str(fact.id), "content_version": request_version},
            ))

        for index in range(1, 112):
            add(index, unavailable)
            add(index + 1000, owner, version=2)
        for index, status in enumerate(("succeeded", "failed", "cancelled"), start=3000):
            add(index, owner, status=status)
        add(4000, owner, request_owner=unavailable)
        add(4001, owner, deleted=True)
        add(4002, owner, fresh=True)

    batches = []

    class ObservedSession:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def execute(self, statement, *args, **kwargs):
            result = await self.session.execute(statement, *args, **kwargs)
            if (
                statement.is_select
                and statement.column_descriptions[0]["entity"] is TrainingAnalysisRequestRecord
            ):
                frozen = result.freeze()
                batches.append(len(frozen().all()))
                return frozen()
            return result

    @asynccontextmanager
    async def transaction():
        async with database.transaction() as session:
            yield ObservedSession(session)

    app = KnowledgeApplication(SimpleNamespace(transaction=transaction))
    await app.reconcile_statistics()
    assert batches == [100, 14]
    async with database.sessions() as session:
        snapshots = list(await session.scalars(select(KnowledgeSnapshotRecord)))
        assert {snapshot.fact_id for snapshot in snapshots} == {
            UUID(int=index) for index in range(3000, 3003)
        }
        by_id = {row.fact_id: row for row in snapshots}
        assert by_id[UUID(int=3000)].common["value"] == 25
        assert by_id[UUID(int=3000)].common["status"] == "available"
        assert all(by_id[UUID(int=index)].common["status"] == "failed" for index in (3001, 3002))
        assert all(row.owner_id == owner and row.content_version == 1 for row in snapshots)
        facts = list(await session.scalars(select(KnowledgeFactRecord)))
        assert all(
            row.statistic_status == "pending" for row in facts if row.owner_id == unavailable
        )
        assert all(row.statistic_status == "pending" for row in facts if row.content_version == 2)
    batches.clear()
    await app.reconcile_statistics()
    assert batches == [100, 11]
    async with database.sessions() as session:
        assert len(list(await session.scalars(select(KnowledgeSnapshotRecord)))) == 3
    async with database.transaction() as session:
        player = await session.get(PlayerRecord, unavailable)
        player.status = "active"
    batches.clear()
    await app.reconcile_statistics()
    assert batches == [100, 11]
    async with database.sessions() as session:
        snapshots = list(await session.scalars(select(KnowledgeSnapshotRecord)))
        assert len(snapshots) == 114
        assert {row.fact_id for row in snapshots} == {
            UUID(int=index) for index in [*range(1, 112), *range(3000, 3003)]
        }
