from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from sitg_bot.domain.game_rulesets import SIGameRuleset
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.packet import Question
from sitg_bot.services.persistent_game import PersistentGameService


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
