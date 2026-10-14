from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Uuid, create_engine

from sitg_bot.domain.game_rulesets import SIGameRuleset
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.packet import Question
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.storage.models import PacketQuestionRecord


@pytest.mark.parametrize("token_index,fully_announced", [(1, False), (2, True)])
async def test_answer_event_preserves_question_reading_progress(
    monkeypatch, token_index, fully_announced,
) -> None:
    game = SimpleNamespace(
        current_round_id=uuid4(), question_token_index=token_index,
        assignment_plan={"parameters": {"question_token_target_chars": 5}},
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[
            game, SimpleNamespace(question_revision_id=uuid4()),
            SimpleNamespace(text="First last"),
        ]),
        scalar=AsyncMock(return_value=0), add=Mock(),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(TransactionalOutbox, "enqueue_game_event", enqueue)

    event = await PersistentGameService._event(session, uuid4(), "answer_judged", {})

    assert event["payload"]["fully_announced"] is fully_announced
    assert event["payload"]["revealed_text"] == ("First last" if fully_announced else "First")
    assert enqueue.await_args.kwargs["parameters"] == event["payload"]


@pytest.mark.parametrize(("answer", "submitted", "rejected", "correct"), [
    ("Museum", "Newtn", (), True),
    ("Museum", "Newton", ("(Sir) Newton",), False),
    ("Museum", None, (), False),
    ("[Ян] Вермеер.", "вермеер", (), True),
    ("[Ян] Вермеер.", "Верме\u0301ер,", ("[Ян] Вермеер.",), False),
])
async def test_attempt_judges_patterns_without_expanding_answers(
    monkeypatch, answer, submitted, rejected, correct,
) -> None:
    def expansion_forbidden(_):
        raise AssertionError("Gameplay must not enumerate answer combinations")

    monkeypatch.setattr(Question, "all_answers", property(expansion_forbidden))
    monkeypatch.setattr(Question, "all_rejected_answers", property(expansion_forbidden))
    round_record = SimpleNamespace(id=uuid4(), question_revision_id=uuid4())
    revision = SimpleNamespace(
        text="Question", answer=answer, commentary="", form="", source="",
        accepted_answers=("(Sir) " * 80 + "Newton",), rejected_answers=rejected,
    )
    state = SimpleNamespace(attempted=False, eligible=True)
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[round_record, revision]),
        scalar=AsyncMock(side_effect=[0, state, 0]), add=Mock(), flush=AsyncMock(),
    )
    game = SimpleNamespace(id=uuid4(), current_round_id=round_record.id)
    participant = SimpleNamespace(id=uuid4(), score=Decimal(0))
    service = SimpleNamespace(
        _placement=AsyncMock(return_value=SimpleNamespace(value=10)),
        _ruleset=AsyncMock(return_value=SIGameRuleset()),
        _settings=lambda _: GameSettings(),
        _event=AsyncMock(return_value={}), _close_round=AsyncMock(return_value={}),
    )

    await PersistentGameService._record_attempt(
        service, session, game, participant,
        submitted_answer=submitted, timed_out=submitted is None,
    )

    attempt = session.add.call_args_list[0].args[0]
    assert attempt.original_correct is correct
    assert attempt.final_correct is correct
    assert participant.score == (10 if correct else -10)


@pytest.mark.parametrize("values", [
    (10, 20, 30, 40, 50),
    (0, 10, 20, 30, 40, 50),
    (0,),
    (10, 30, 50),
    (10, 20, 30, 40, 50, 60),
])
async def test_theme_completes_only_after_its_last_question(values) -> None:
    metadata = MetaData()
    placements = Table(
        PacketQuestionRecord.__tablename__, metadata,
        Column("packet_version_id", Uuid(native_uuid=False)),
        Column("theme_revision_id", Uuid(native_uuid=False)),
        Column("question_revision_id", Uuid(native_uuid=False)),
        Column("position", Integer),
        Column("value", Integer),
    )
    version_id, theme_id = uuid4(), uuid4()
    rows = [{
        "packet_version_id": version_id, "theme_revision_id": theme_id,
        "question_revision_id": uuid4(), "position": position, "value": value,
    } for position, value in enumerate(values, 1)]
    settings = GameSettings(
        between_questions_delay=2, last_question_to_theme_complete_delay=7,
    )
    service = PersistentGameService(None)
    service._event = AsyncMock(return_value={"kind": "question_completed"})
    service._settings = Mock(return_value=settings)
    service._placement = AsyncMock()
    engine = create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.connect() as connection:
            connection.execute(placements.insert(), [
                *rows,
                {**rows[0], "theme_revision_id": uuid4(), "position": 99},
                {**rows[0], "packet_version_id": uuid4(), "position": 99},
            ])

            async def scalar(query):
                return connection.execute(query).scalar()

            for row in rows:
                round_record = SimpleNamespace(id=uuid4(), status="active")
                game = SimpleNamespace(id=uuid4(), current_round_id=round_record.id)
                session = SimpleNamespace(get=AsyncMock(return_value=round_record), scalar=scalar)
                service._placement.return_value = SimpleNamespace(**row)
                before = datetime.now(UTC)

                event = await service._close_round(session, game, "buzz_timeout")

                last_question = row["position"] == len(values)
                assert game.progression_stage == (
                    "theme_complete" if last_question else "next_question"
                )
                delay = (settings.last_question_to_theme_complete_delta if last_question
                         else settings.between_questions_delta)
                assert before + delay <= game.progression_deadline <= datetime.now(UTC) + delay
                assert round_record.status == "completed"
                assert game.phase == "intermission"
                assert event["kind"] == "question_completed"
    finally:
        engine.dispose()
