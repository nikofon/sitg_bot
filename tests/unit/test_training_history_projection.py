"""History security/disclosure and recorded-field projection (no live storage)."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import column
from sqlalchemy.dialects import postgresql

from sitg_bot.services.training_history import (
    TrainingHistoryService,
    _decode_cursor,
    _encode_cursor,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
QUESTION = UUID(int=1)
THEME = UUID(int=2)
REVISION = UUID(int=3)
CLAIMS = {("question", QUESTION), ("theme", THEME)}


def question(**changes):
    return SimpleNamespace(**{
        "question_id": QUESTION, "theme_id": THEME, "question_revision_id": REVISION,
        "position": 0, "value": 10, "theme": "Private theme", "text": "Private text",
        "answer": "Private answer", "accepted_answers": ["Accepted"],
        "rejected_answers": ["Rejected"], "commentary": "Comment", "source": "Source",
        "opened_at": None, "buzzed_at": None, "buzz_delay_seconds": None,
        "buzz_token_cursor": None, **changes,
    })


def attempt(**changes):
    return SimpleNamespace(**{
        "answer": "My answer", "correct": True, "outcome": "answered", "token_cursor": 7,
        "created_at": NOW, "original_correct": False, "appeal_status": "accepted",
        "appealed_at": NOW, "appeal_resolved_at": NOW, **changes,
    })


def project(row, own=None, *, claims=CLAIMS, denied=False, active=False):
    return TrainingHistoryService._question_row(row, own, {}, claims, denied, active)


@pytest.mark.parametrize("active", [True, False])
def test_pin_and_packet_burn_alone_never_disclose_future_question(active):
    result = project(question(), active=active)
    assert result["protected"] == "pending"
    assert result["theme"] is None and result["text"] is None and result["answer"] is None


def test_opened_question_after_stop_and_legacy_finished_attempt_are_readable():
    assert project(question(opened_at=NOW))["text"] == "Private text"
    result = project(question(), attempt())
    assert result["text"] == "Private text"
    assert result["buzz"] is None  # never invented from attempt.created_at/token_cursor
    assert result["own"]["correct"] is True
    assert result["own"]["original_correct"] is False
    assert result["own"]["appeal_status"] == "accepted"


def test_active_question_cannot_bypass_incremental_or_hidden_playback():
    result = project(question(opened_at=NOW, buzzed_at=NOW), active=True)
    assert result["protected"] == "pending" and result["text"] is None


@pytest.mark.parametrize("claims", [set(), {("theme", THEME)}, {("question", QUESTION)}])
def test_missing_disclosure_claims_deny_content_even_with_finished_attempt(claims):
    result = project(question(), attempt(), claims=claims)
    assert result["protected"] == "undisclosed"
    assert result["theme"] is None and result["source"] is None


def test_revocation_denies_source_but_keeps_owners_answer_and_score_evidence():
    result = project(question(opened_at=NOW), attempt(), denied=True)
    assert result["protected"] == "revoked"
    assert result["text"] is None and result["accepted_answers"] is None
    assert result["own"]["answer"] == "My answer"


def test_accepted_buzz_without_answer_attempt_survives_stop():
    result = project(question(opened_at=NOW, buzzed_at=NOW,
                              buzz_delay_seconds=Decimal("1.25"), buzz_token_cursor=3))
    assert result["own"] is None
    assert result["buzz"] == {
        "buzzed_at": NOW, "buzz_delay_seconds": 1.25, "buzz_token_cursor": 3,
    }
    assert result["text"] == "Private text"


@pytest.mark.parametrize("cursor", ["garbage", "e30", "W10", "bnVsbA", "////"])
def test_cursor_rejects_invalid_shapes(cursor):
    with pytest.raises(ValueError):
        _decode_cursor(cursor, "owner:session")


def test_cursor_preserves_owner_and_session_filter_identity():
    encoded = _encode_cursor({"sig": "owner:session", "position": 0})
    assert _decode_cursor(encoded, "owner:session")["position"] == 0
    with pytest.raises(ValueError):
        _decode_cursor(encoded, "another-owner:session")
    with pytest.raises(ValueError):
        _decode_cursor(encoded, "owner:another-session")


async def test_timing_statistics_sql_has_bounded_samples_and_no_competitive_tables():
    # Compile against supplied 0027 column expressions without inventing storage
    # or modifying ORM metadata. Persistence of these fields needs combined schema.
    timing = {name: column(f"training_questions.{name}") for name in (
        "opened_at", "buzzed_at", "buzz_delay_seconds",
    )}
    session = SimpleNamespace(execute=AsyncMock(side_effect=[
        [(REVISION, 2, 1, 1, 0)], [(REVISION, 2, 1)], [(REVISION, Decimal("1.5"))],
    ]))
    stats = await TrainingHistoryService._question_statistics(
        session, [REVISION], timing
    )
    assert stats[REVISION]["buzzes"] == 1
    assert stats[REVISION]["buzz_timings"] == [1.5]
    statements = [str(call.args[0].compile(dialect=postgresql.dialect(),
                                         compile_kwargs={"literal_binds": True}))
                  for call in session.execute.await_args_list]
    assert len(statements) == 3
    assert "row_number() OVER" in statements[-1] and "rank <= 25" in statements[-1]
    assert "games" not in " ".join(statements) and "rating" not in " ".join(statements)
    assert "array_agg" not in " ".join(statements)
