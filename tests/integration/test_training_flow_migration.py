import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_initial_schema import baseline_database as _baseline_database
from test_training import open_question, upload

from sitg_bot.services.training import TrainingService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import PlayerRecord, TrainingAttemptRecord, TrainingQuestionRecord

pytestmark = pytest.mark.integration
baseline_database = _baseline_database


def test_training_upgrade_preserves_verdicts_without_inventing_playback(baseline_database):
    from conftest import create_player

    url, config = baseline_database
    command.upgrade(config, "head")

    async def seed():
        database = Database(url)
        try:
            owner = await create_player(database, 9481, "migration-trainee")
            packet, _ = await upload(database, owner)
            service = TrainingService(database)
            now = datetime.now(UTC)
            state = (await service.start(owner, packet, confirm=True, now=now))["session"]
            state, now = await open_question(service, owner, state, now)
            session_id, question_id = UUID(state["id"]), UUID(state["question_revision_id"])
            state = (await service.act(
                owner, session_id, "buzz", expected_version=state["version"],
                question_revision_id=question_id, now=now,
            ))["session"]
            await service.act(
                owner, session_id, "answer", expected_version=state["version"],
                question_revision_id=question_id, answer="Paris", now=now,
            )
            async with database.sessions() as session:
                attempt = await session.scalar(select(TrainingAttemptRecord).where(
                    TrainingAttemptRecord.session_id == session_id
                ))
                return owner, session_id, attempt.id, attempt.correct
        finally:
            await database.close()

    owner, session_id, attempt_id, verdict = asyncio.run(seed())
    # Old applications had no playback/timing/appeal columns. A roundtrip must
    # preserve their facts without reconstructing timestamps from attempt times.
    command.downgrade(config, "0026_knowledge_images")
    command.upgrade(config, "head")

    async def check():
        database = Database(url)
        try:
            async with database.sessions() as session:
                questions = list(await session.scalars(select(TrainingQuestionRecord).where(
                    TrainingQuestionRecord.session_id == session_id
                )))
                assert questions
                for question in questions:
                    assert question.opened_at is None and question.buzzed_at is None
                    assert question.buzz_delay_seconds is None
                    assert question.buzz_token_cursor is None
                    assert question.paused_seconds == 0
                attempt = await session.get(TrainingAttemptRecord, attempt_id)
                assert attempt.correct == attempt.original_correct == verdict
                assert attempt.appeal_status is None and attempt.appealed_at is None
                assert attempt.appeal_resolved_at is None and attempt.appeal_was_paused is False
                player = await session.get(PlayerRecord, owner)
                assert player.training_lobby_presentation == {}
            with pytest.raises(IntegrityError):
                async with database.transaction() as session:
                    await session.execute(text(
                        "UPDATE training_attempts SET appeal_status = 'unrecognized' WHERE id = :id"
                    ), {"id": attempt_id})
        finally:
            await database.close()

    asyncio.run(check())
