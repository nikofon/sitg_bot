"""Requires a disposable PostgreSQL TEST_DATABASE_URL; never production runtime."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture
from test_training import upload

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.training import TrainingService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameRecord,
    OutboxEventRecord,
    PlayerRecord,
    RatingLedgerRecord,
    RulesetRatingLedgerRecord,
    ScoreLedgerRecord,
    SIPlayerSuspicionAggregateRecord,
    SIQuestionSuspicionAggregateRecord,
    TrainingAttemptRecord,
    TrainingQuestionRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def test_training_score_appeal_buzz_disclosure_and_no_competitive_writes(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        owner, other = (player.id for player in fixture.players)
        packet, _ = await upload(database, owner)
        global_models = (GameRecord, RatingLedgerRecord, RulesetRatingLedgerRecord,
                         ScoreLedgerRecord, SIPlayerSuspicionAggregateRecord,
                         SIQuestionSuspicionAggregateRecord, AuthorRecord)

        async def global_facts():
            async with database.sessions() as session:
                return [tuple((await session.execute(select(*model.__table__.c))).all())
                        for model in global_models]

        baseline = await global_facts()
        now = datetime.now(UTC)
        service = TrainingService(database)
        state = (await service.start(owner, packet, confirm=True, now=now))["session"]
        run, revision = UUID(state["id"]), UUID(state["question_revision_id"])

        async def act(command, *, seconds=0, **values):
            nonlocal state
            state = (await service.act(
                owner, run, command, expected_version=state["version"],
                question_revision_id=UUID(state["question_revision_id"]),
                now=now + timedelta(seconds=seconds), **values,
            ))["session"]

        with pytest.raises(LookupError):
            await service.act(other, run, "buzz", expected_version=state["version"],
                              question_revision_id=revision, now=now)
        async with database.sessions() as session:
            questions = (await session.scalars(select(TrainingQuestionRecord).where(
                TrainingQuestionRecord.session_id == run,
            ).order_by(TrainingQuestionRecord.position))).all()
            assert questions[0].opened_at == now
            assert all(q.opened_at is None for q in questions[1:])
        await act("pause", seconds=.1)
        await act("resume", seconds=10.1)
        version = state["version"]
        await act("buzz", seconds=10.5)
        async with database.sessions() as session:
            question = await session.get(TrainingQuestionRecord, (run, 0))
            assert question.buzzed_at == now + timedelta(seconds=10.5)
            assert float(question.buzz_delay_seconds) == pytest.approx(.5)
            assert question.buzz_token_cursor == 0
            assert not await session.scalar(select(TrainingAttemptRecord.id).where(
                TrainingAttemptRecord.session_id == run,
            ))
        with pytest.raises(StaleWriteError):
            await service.act(owner, run, "buzz", expected_version=version,
                              question_revision_id=revision, now=now + timedelta(seconds=10.5))
        await act("answer", seconds=11, answer="London")
        assert state["correct"] is False and state["score"] == -10
        await act("appeal", seconds=12)
        await act("vote", seconds=13, approve=True)
        assert state["correct"] is True and state["score"] == 10
        assert state["appeal_status"] == "accepted" and state["status"] == "active"
        with pytest.raises(ValueError):
            await service.act(owner, run, "appeal", expected_version=state["version"],
                              question_revision_id=revision, now=now + timedelta(seconds=14))
        async with database.sessions() as session:
            attempt = await session.scalar(select(TrainingAttemptRecord).where(
                TrainingAttemptRecord.session_id == run,
            ))
            assert attempt.original_correct is False and attempt.correct is True
            assert attempt.appealed_at == now + timedelta(seconds=12)
            assert attempt.appeal_resolved_at == now + timedelta(seconds=13)
        await act("next", seconds=14)
        await act("buzz", seconds=14)
        await service.tick(run, now=now + timedelta(seconds=30))
        state = (await service.view(owner, run, now=now + timedelta(seconds=30)))["session"]
        assert state["outcome"] == "timeout" and state["correct"] is False
        assert state["score"] == -10  # +10 from first, -20 from second.
        await service.act(owner, run, "stop", expected_version=state["version"],
                          question_revision_id=UUID(state["question_revision_id"]),
                          now=now + timedelta(seconds=31))
        latest = await TrainingService(database).current(
            owner, score_only=True, now=now + timedelta(seconds=31),
        )
        assert latest["session"]["score"] == -10
        await service.packets.retire(owner, packet)
        score_only = await service.current(owner, score_only=True)
        assert score_only["session"]["score"] == -10
        assert set(score_only["session"]) == {"id", "status", "score"}
        assert baseline == await global_facts()
    finally:
        await database.close()


async def test_lobby_changes_enqueue_refresh_and_retain_one_presentation(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        packet, _ = await upload(database, owner)
        service = TrainingService(database)
        lobby = (await service.lobby(owner, "create"))["lobby"]
        await service.lobby_delivery(owner, presentation={"id": 123, "text": "Initial"})
        lobby = (await service.lobby(owner, "save", expected_version=lobby["version"],
                                    version_id=packet, settings={"reversed": True}))["lobby"]
        state = await service.lobby_delivery(owner)
        assert state["presentation"]["id"] == 123
        assert state["lobby"]["packet_name"] == "Private <training>"
        assert state["lobby"]["settings"]["reversed"] is True
        await service.lobby(owner, "quit", expected_version=lobby["version"])
        closed = await TrainingService(database).lobby_delivery(owner)
        assert closed["lobby"] is None and closed["presentation"]["id"] == 123
        async with database.sessions() as session:
            events = (await session.scalars(select(OutboxEventRecord).where(
                OutboxEventRecord.topic == "training.lobby.changed",
                OutboxEventRecord.aggregate_id == owner,
            ))).all()
            assert len(events) == 3
            player = await session.get(PlayerRecord, owner)
            assert player.training_lobby is None
    finally:
        await database.close()
