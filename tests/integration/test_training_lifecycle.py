"""Requires a newly authorized disposable PostgreSQL DB; no Telegram transport."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import create_player
from sqlalchemy import func, select
from test_training import upload

from sitg_bot.services.training import TrainingService
from sitg_bot.storage.models import (
    GameRecord,
    OutboxEventRecord,
    RatingLedgerRecord,
    RulesetRatingLedgerRecord,
    ScoreLedgerRecord,
    TrainingAttemptRecord,
    TrainingEventRecord,
    TrainingMemberRecord,
    TrainingQuestionRecord,
    TrainingSessionRecord,
)

pytestmark = pytest.mark.integration


async def test_automatic_run_restart_event_recovery_closure_and_noncompetitive_isolation(database):
    owner = await create_player(database, 9511, "lifecycle-owner")
    other = await create_player(database, 9512, "lifecycle-other")
    packet, _ = await upload(database, owner)
    competitive = (GameRecord, ScoreLedgerRecord, RatingLedgerRecord, RulesetRatingLedgerRecord)

    async def counts():
        async with database.sessions() as session:
            return [await session.scalar(select(func.count()).select_from(model))
                    for model in competitive]

    baseline = await counts()
    now = datetime.now(UTC)
    service = TrainingService(database)
    state = (await service.start(owner, packet, confirm=True, now=now))["session"]
    run = UUID(state["id"])
    assert state["phase"] == "intermission" and state["text"] == ""
    async with database.sessions() as session:
        assert all(row.opened_at is None for row in await session.scalars(
            select(TrainingQuestionRecord).where(TrainingQuestionRecord.session_id == run),
        ))
    # Reconstruct the service on every deadline, like a restart/reclaimed job.
    for _ in range(150):
        async with database.sessions() as session:
            record = await session.get(TrainingSessionRecord, run)
            if record.status == "completed":
                ended_at = record.ended_at
                break
            deadline = record.deadline
            assert deadline is not None
        await TrainingService(database).tick(run, now=deadline)
    else:
        raise AssertionError("Solo training did not complete automatically")

    async with database.sessions() as session:
        rows = list(await session.scalars(select(TrainingEventRecord).where(
            TrainingEventRecord.session_id == run,
        ).order_by(TrainingEventRecord.sequence)))
        assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
        kinds = [row.kind for row in rows]
        assert kinds[:3] == ["game_started", "themes_announced", "theme_started"]
        assert kinds.count("question_cost_announced") == 5
        assert kinds.count("question_completed") == 5
        assert kinds[-3:] == ["theme_completed", "scoreboard", "game_completed"]
        attempts = list(await session.scalars(select(TrainingAttemptRecord).where(
            TrainingAttemptRecord.session_id == run,
        )))
        assert len(attempts) == 5 and all(row.correct is None for row in attempts)
    with pytest.raises(LookupError):
        await service.delivery(other, run)
    delivery = await service.delivery(owner, run)
    assert [row["kind"] for row in delivery["events"]] == kinds[:100]
    await service.delivery(owner, run, sequence=rows[-1].sequence)
    assert (await service.delivery(owner, run))["events"] == []
    await service.delivery(owner, run, key="question:old", value={"ids": [10, 11]})
    await service.delivery(owner, run, key="controls", value={"ids": [12]})
    await service.delivery(owner, run, key="input:13", value={"ids": [13]})
    await service.close_finished_sessions(now=ended_at + timedelta(hours=11))
    async with database.sessions() as session:
        member = await session.get(TrainingMemberRecord, (run, owner))
        assert not member.presentation.get("dismissed")
    await service.close_finished_sessions(now=ended_at + timedelta(hours=12))
    assert (await service.delivery(owner, run))["session"] is None
    async with database.sessions() as session:
        cleanup = await session.scalar(select(OutboxEventRecord).where(
            OutboxEventRecord.deduplication_key == f"training:{run}:cleanup",
        ))
        assert cleanup.payload["message_ids"] == [10, 11, 12, 13]
        assert await session.scalar(select(func.count()).select_from(TrainingAttemptRecord)
                                    .where(TrainingAttemptRecord.session_id == run)) == 5
    assert await counts() == baseline
