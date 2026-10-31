from datetime import UTC, datetime

import pytest
from conftest import create_player
from sqlalchemy import event
from test_training import upload
from test_training_knowledge import application

from sitg_bot.storage.models import (
    KnowledgeDissectionRecord,
    PacketVersionRecord,
    TrainingAnalysisRequestRecord,
)

pytestmark = pytest.mark.integration


async def test_dissection_page_batches_only_pending_analysis_statuses(database):
    owner = await create_player(database, 9491, "projection-owner")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    first = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="projection-seed",
    )
    async with database.transaction() as session:
        seed = await app.analysis._request_row(session, first["request_id"])
        artifact_id = seed.artifact_id
        packet = await session.get(PacketVersionRecord, version)
        packet_id = packet.packet_id
        service = app.service(session)
        for index in range(100):
            status = ("succeeded", "partially_succeeded", "failed")[index % 3]
            view = await service.request_dissection(
                owner, packet_id=packet_id, packet_revision=str(version), comment=f"past {index}",
            )
            row = await session.get(KnowledgeDissectionRecord, view.request_id)
            row.status = status
            row.analysis_request_id = f"historical-{index}"
            session.add(TrainingAnalysisRequestRecord(
                request_id=row.analysis_request_id, owner_player_id=owner,
                kind="dissection", artifact_id=artifact_id, status=status,
                completed_at=datetime.now(UTC),
            ))

    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.statement.is_select:
            queries.append(context)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        terminal = await app.dissections(owner)
        terminal_count = len(queries)
        assert len(terminal["items"]) == 100
        assert {row["status"] for row in terminal["items"]} == {
            "succeeded", "partially_succeeded", "failed",
        }
        assert terminal_count <= 4
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)

    pending = {}
    async with database.transaction() as session:
        service = app.service(session)
        for index, status in enumerate(("queued", "running", "succeeded")):
            view = await service.request_dissection(
                owner, packet_id=packet_id, packet_revision=str(version),
                comment=f"pending {index}",
            )
            row = await session.get(KnowledgeDissectionRecord, view.request_id)
            row.analysis_request_id = f"pending-{index}"
            session.add(TrainingAnalysisRequestRecord(
                request_id=row.analysis_request_id, owner_player_id=owner,
                kind="dissection", artifact_id=artifact_id, status=status,
                completed_at=datetime.now(UTC) if status == "succeeded" else None,
            ))
            pending[row.id] = status

    queries.clear()
    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        mixed = await app.dissections(owner)
        assert len(mixed["items"]) == 100
        assert {
            row["id"]: row["status"] for row in mixed["items"] if row["id"] in pending
        } == pending
        assert len(queries) == terminal_count + 1
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
