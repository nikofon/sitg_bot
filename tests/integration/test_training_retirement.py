from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import create_player
from sqlalchemy import select
from test_training import open_question, upload

from sitg_bot.services.training import TrainingService
from sitg_bot.storage.models import TrainingAttemptRecord

pytestmark = pytest.mark.integration


async def test_private_packet_retirement_rejects_pending_ballot_without_changing_verdict(database):
    owner = await create_player(database, 9491, "retire-ballot-owner")
    packet, _ = await upload(database, owner)
    service = TrainingService(database)
    now = datetime.now(UTC)
    state = (await service.start(owner, packet, confirm=True, now=now))["session"]
    state, now = await open_question(service, owner, state, now)
    run, question = UUID(state["id"]), UUID(state["question_revision_id"])
    for command, values in (("buzz", {}), ("answer", {"answer": "London"}), ("appeal", {})):
        state = (await service.act(
            owner, run, command, expected_version=state["version"],
            question_revision_id=question, now=now, **values,
        ))["session"]
    assert state["appeal_status"] == "voting"
    await service.packets.retire(owner, packet)
    async with database.sessions() as session:
        attempt = await session.scalar(select(TrainingAttemptRecord).where(
            TrainingAttemptRecord.session_id == run
        ))
        assert attempt.appeal_status == "rejected" and attempt.appeal_resolved_at is not None
        assert attempt.correct is False and attempt.original_correct is False
    assert (await service.current(owner, score_only=True))["session"]["score"] == -10
