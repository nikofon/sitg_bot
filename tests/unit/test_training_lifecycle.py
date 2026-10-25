"""Behavioral SI training progression with real transitions and persisted-event contracts."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from test_training_gameflow import NOW, harness

from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.storage.models import TrainingAttemptRecord, TrainingEventRecord


def lifecycle_case():
    service, record, question, revision, _, session = harness()
    question.theme_revision_id = UUID(int=10)
    revision.author_ids = ()
    questions = [(question, revision, "First")]
    for index, theme_id in ((1, 10), (2, 11)):
        questions.append((
            SimpleNamespace(value=20, theme_revision_id=UUID(int=theme_id), best_buzz=None,
                            opened_at=None, paused_seconds=0),
            SimpleNamespace(id=UUID(int=20 + index), text=f"Question {index}", answer="Answer",
                            commentary="Commentary", form="Word", author_ids=()),
            "First" if theme_id == 10 else "Second",
        ))
    service._score = AsyncMock(return_value=0)
    service._question = AsyncMock(side_effect=lambda *_: questions[record.question_index])
    service._attempt = AsyncMock(side_effect=lambda *_: next((row for row in session.events
        if isinstance(row, TrainingAttemptRecord) and row.question_index == record.question_index),
        None))
    themes = {UUID(int=10): SimpleNamespace(name="First", commentary="Theme notes", author_ids=()),
              UUID(int=11): SimpleNamespace(name="Second", commentary="", author_ids=())}
    session.get = AsyncMock(side_effect=lambda model, key: themes.get(
        key, SimpleNamespace(name="Packet", public_nickname="Player"),
    ))

    async def scalar(statement):
        sql = str(statement)
        params = statement.compile().params
        if "count(distinct" in sql:
            return len({q[0].theme_revision_id for q in questions[:record.question_index + 1]})
        if "count(" in sql:
            return len(questions)
        position = params["position_1"]
        return questions[position][0].theme_revision_id if position < len(questions) else None

    session.scalar = AsyncMock(side_effect=scalar)
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [
        SimpleNamespace(id=UUID(int=10), name="First"),
        SimpleNamespace(id=UUID(int=10), name="First"),
        SimpleNamespace(id=UUID(int=11), name="Second"),
    ]))
    record.phase, record.progression_stage, record.deadline = "intermission", "ready_countdown", NOW
    return service, record, questions, session


def events(session):
    return [row for row in session.events if isinstance(row, TrainingEventRecord)]


async def due(service, record, session):
    now = record.deadline
    await service._advance(session, record, now)
    return now


async def test_automatic_run_announces_themes_cost_answer_scores_and_finishes_without_next():
    service, record, questions, session = lifecycle_case()
    await due(service, record, session)
    assert events(session)[0].kind == "themes_announced"
    assert events(session)[0].parameters["themes"] == [{"name": "First"}, {"name": "Second"}]
    await due(service, record, session)
    assert record.progression_stage == "theme_commentary"
    assert events(session)[-1].kind == "theme_started"
    await due(service, record, session)
    assert events(session)[-1].parameters["commentary"] == "Theme notes"
    await due(service, record, session)
    assert events(session)[-1].kind == "question_cost_announced"
    assert questions[0][0].opened_at == NOW  # harness evidence, not changed by announcement
    opened = await due(service, record, session)
    assert record.token_cursor == 1 and record.phase == "reading"
    assert questions[0][0].opened_at == opened
    # Let each question expire. No Next or manual reveal is issued anywhere.
    for index in range(3):
        while record.phase != "revealed":
            await due(service, record, session)
        assert record.question_index == index
        assert events(session)[-1].kind == "question_completed"
        attempt = [row for row in session.events if isinstance(row, TrainingAttemptRecord)][-1]
        assert attempt.outcome == "timeout" and attempt.correct is None
        if index == 0:
            await due(service, record, session)
            assert record.question_index == 1
            assert events(session)[-1].kind == "question_cost_announced"
            await due(service, record, session)
        else:
            await due(service, record, session)
            assert events(session)[-1].kind == "theme_completed"
            await due(service, record, session)
            assert events(session)[-1].kind == "scoreboard"
            service._score = AsyncMock(return_value=0)
            await due(service, record, session)
            if index == 1:
                assert events(session)[-1].parameters["name"] == "Second"
                await due(service, record, session)
                await due(service, record, session)
    assert record.status == "completed" and record.deadline is None
    assert events(session)[-1].kind == "game_completed"
    assert [row.sequence for row in events(session)] == list(range(1, len(events(session)) + 1))
    before = len(events(session))
    await service._advance(session, record, record.ended_at + timedelta(hours=1))
    assert len(events(session)) == before


async def test_late_worker_does_not_consume_unseen_reading_or_buzz_window():
    service, record, _, session = lifecycle_case()
    record.phase, record.progression_stage = "reading", None
    record.deadline = NOW + timedelta(seconds=1)
    record.token_cursor = 0
    await service._advance(session, record, NOW + timedelta(hours=1))
    assert record.token_cursor == 1 and record.phase == "reading"
    assert record.deadline == NOW + timedelta(hours=1, seconds=1)
    assert not any(isinstance(row, TrainingAttemptRecord) for row in session.events)


async def test_legacy_presentation_recovers_existing_chunks_without_inventing_playback():
    service, record, questions, session = lifecycle_case()
    record.phase, record.progression_stage = "reading", None
    record.token_cursor = 1
    questions[0][0].opened_at = None
    member = SimpleNamespace(presentation={"ids": [55, 56, 57]})
    await service._recover_presentation(session, record, member)
    assert questions[0][0].opened_at is None
    chunks = list(member.presentation["messages"].values())
    assert [entry["ids"] for entry in chunks] == [[55], [56], [57]]
    assert all(entry["round_id"] == str(questions[0][1].id) for entry in chunks)
    assert [row.kind for row in events(session)] == [
        "question_cost_announced", "question_token_revealed",
    ]


async def test_pause_and_solo_appeal_restore_remaining_auto_progression_time():
    service, record, _, session = lifecycle_case()
    service._score = AsyncMock(return_value=0)
    record.phase, record.progression_stage = "revealed", "question_complete"
    record.deadline = NOW + GameSettings().between_questions_delta
    attempt = TrainingAttemptRecord(answer="Wrong", correct=False, original_correct=False,
                                    appeal_status=None)
    service._attempt = AsyncMock(return_value=attempt)
    await service.act(record.owner_id, record.id, "appeal", expected_version=record.version,
                      question_revision_id=(await service._question(session, record))[1].id,
                      now=NOW + timedelta(seconds=1))
    assert record.status == "paused"
    await service._advance(session, record, NOW + timedelta(seconds=20))
    assert record.question_index == 0
    await service.act(record.owner_id, record.id, "vote", approve=True,
                      expected_version=record.version,
                      question_revision_id=(await service._question(session, record))[1].id,
                      now=NOW + timedelta(seconds=30))
    assert record.deadline == NOW + timedelta(seconds=32)
    assert attempt.correct is True and attempt.original_correct is False
    await service._advance(session, record, NOW + timedelta(seconds=31))
    assert record.question_index == 0
    await due(service, record, session)
    assert record.question_index == 1
