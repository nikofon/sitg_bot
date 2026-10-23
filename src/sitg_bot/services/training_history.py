"""Owner-only, batched read projection of past training runs.

The projection reuses TrainingSession/TrainingQuestion/TrainingAttempt and the
pinned packet revisions; it never writes, never aggregates competitive data and
never reconstructs timings that were not recorded. Every page uses a bounded
number of queries independent of row counts (keyset pagination plus grouped
projections), mirroring the training packet catalogue contract.

Timing and verdict alignment with the gameplay component contract
(sitg-training-gameflow fields.md): question playback/disclosure evidence and
buzz timings come from the question row (``opened_at``, ``buzzed_at``,
``buzz_delay_seconds``, ``buzz_token_cursor``), never from attempt timestamps;
the effective score uses ``attempt.correct`` (+/- value) while
``original_correct`` preserves the initial verdict; a buzz may exist without an
answer attempt. Those columns land in the integration owner's reserved
migration 0027, so every optional column is resolved defensively and simply
stays null/absent until the combined schema is merged.
"""

import base64
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.training_access import require_trainee
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
    TrainingAttemptRecord,
    TrainingQuestionRecord,
    TrainingSessionRecord,
)

LIST_SIGNATURE = "training-history-list-v1"
TIMING_SAMPLE_LIMIT = 25

# Optional columns supplied by the integration owner's reserved 0027 migration.
QUESTION_TIMING_FIELDS = ("opened_at", "buzzed_at", "buzz_delay_seconds", "buzz_token_cursor")
ATTEMPT_VERDICT_FIELDS = ("original_correct", "appeal_status", "appealed_at", "appeal_resolved_at")


def _optional(model, name):
    column = getattr(model, name, None)
    return column if column is not None and hasattr(column, "key") else None


def _modifiers(settings: dict) -> list[str]:
    """Card modifier names derived from persisted settings, in display order."""
    modifiers = []
    if settings.get("reversed") is True:
        modifiers.append("reversed")
    if settings.get("hidden_after") is not None:
        modifiers.append("hidden")
    if settings.get("best_buzz") is True:
        modifiers.append("best_buzz")
    return modifiers


def _encode_cursor(payload: dict) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), default=str).encode()
        )
        .decode()
        .rstrip("=")
    )


def _decode_cursor(cursor: str, signature: str) -> dict:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(payload, dict) or payload.get("sig") != signature:
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ValueError("Invalid training history cursor") from error


class TrainingHistoryService:
    def __init__(self, database: Database):
        self.database = database
        self.packets = TrainingPacketService(database)

    async def list_sessions(
        self, player_id: UUID, *, cursor: str | None = None, limit: int = 25
    ) -> dict:
        """Newest-first training run cards for the owner, keyset paginated."""
        if not 1 <= limit <= 25:
            raise ValueError("Training history page size must be between 1 and 25")
        signature = f"{LIST_SIGNATURE}:{player_id}"
        async with self.database.sessions() as session:
            await require_trainee(session, player_id)
            query = (
                select(
                    TrainingSessionRecord.id,
                    TrainingSessionRecord.created_at,
                    TrainingSessionRecord.ended_at,
                    TrainingSessionRecord.status,
                    TrainingSessionRecord.settings,
                    PacketVersionRecord.id.label("packet_version_id"),
                    PacketVersionRecord.packet_id,
                    PacketVersionRecord.name,
                )
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == TrainingSessionRecord.packet_version_id,
                )
                .where(TrainingSessionRecord.owner_id == player_id)
            )
            if cursor:
                payload = _decode_cursor(cursor, signature)
                try:
                    anchor = datetime.fromisoformat(payload["created_at"])
                    identity = UUID(payload["id"])
                    if anchor.tzinfo is None:
                        raise ValueError
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError("Invalid training history cursor") from error
                query = query.where(
                    tuple_(TrainingSessionRecord.created_at, TrainingSessionRecord.id)
                    < (anchor, identity)
                )
            rows = (
                await session.execute(
                    query.order_by(
                        TrainingSessionRecord.created_at.desc(),
                        TrainingSessionRecord.id.desc(),
                    ).limit(limit + 1)
                )
            ).all()
            page = rows[:limit]
            identifiers = [row.id for row in page]
            scores = await self._score_totals(session, identifiers)
            totals = await self._question_totals(session, identifiers)
            items = [
                {
                    "id": str(row.id),
                    "created_at": row.created_at,
                    "ended_at": row.ended_at,
                    "status": row.status,
                    "packets": [
                        {
                            "id": str(row.packet_version_id),
                            "packet_id": str(row.packet_id),
                            "name": row.name,
                        }
                    ],
                    "score": scores.get(row.id, 0),
                    "question_total": totals.get(row.id, 0),
                    "settings": row.settings,
                    "modifiers": _modifiers(row.settings or {}),
                }
                for row in page
            ]
            next_cursor = None
            if len(rows) > limit and page:
                last = page[-1]
                next_cursor = _encode_cursor(
                    {
                        "created_at": last.created_at.isoformat(),
                        "id": str(last.id),
                        "sig": signature,
                    }
                )
            return {"items": items, "next_cursor": next_cursor}

    async def session_detail(
        self, player_id: UUID, session_id: UUID, *, cursor: str | None = None, limit: int = 25
    ) -> dict:
        """Owner-only run summary plus one bounded page of ordered question rows.

        Protected question text is exposed only when the packet source is still
        readable through the live TrainingPacketService boundary, the player has
        a recorded burnt disclosure claim for the pinned content, and playback
        evidence exists for the question (opened_at / a buzz / an attempt).
        Unknown legacy timings stay null and are never reconstructed.
        """
        if not 1 <= limit <= 25:
            raise ValueError("Training history page size must be between 1 and 25")
        signature = hashlib.sha256(f"detail:{player_id}:{session_id}".encode()).hexdigest()[:16]
        after = -1
        if cursor:
            payload = _decode_cursor(cursor, signature)
            try:
                after = payload["position"]
                if type(after) is not int or after < 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Invalid training history cursor") from error
        async with self.database.sessions() as session:
            await require_trainee(session, player_id)
            record = await session.get(TrainingSessionRecord, session_id)
            if record is None or record.owner_id != player_id:
                raise LookupError("Training session not found")
            version = await session.get(PacketVersionRecord, record.packet_version_id)
            totals = await self._question_totals(session, [record.id])
            score = (await self._score_totals(session, [record.id])).get(record.id, 0)
            summary = {
                "id": str(record.id),
                "created_at": record.created_at,
                "ended_at": record.ended_at,
                "status": record.status,
                "packets": [
                    {
                        "id": str(record.packet_version_id),
                        "packet_id": str(version.packet_id) if version else None,
                        "name": version.name if version else None,
                    }
                ],
                "score": score,
                "question_total": totals.get(record.id, 0),
                "settings": record.settings,
                "modifiers": _modifiers(record.settings or {}),
            }
            # Retired, revoked or otherwise unreadable sources keep the metadata
            # summary but deny protected question text on every row.
            protected_source = False
            try:
                await self.packets.authorize(session, player_id, record.packet_version_id)
            except (LookupError, PermissionError):
                protected_source = True
            timing = {
                name: _optional(TrainingQuestionRecord, name)
                for name in QUESTION_TIMING_FIELDS
            }
            columns = [
                TrainingQuestionRecord.position,
                TrainingQuestionRecord.value,
                TrainingQuestionRecord.question_revision_id,
                ThemeRevisionRecord.name.label("theme"),
                QuestionRevisionRecord.question_id,
                ThemeRevisionRecord.theme_id,
                QuestionRevisionRecord.text,
                QuestionRevisionRecord.answer,
                QuestionRevisionRecord.accepted_answers,
                QuestionRevisionRecord.rejected_answers,
                QuestionRevisionRecord.commentary,
                QuestionRevisionRecord.source,
            ]
            for name, column in timing.items():
                if column is not None:
                    columns.append(column.label(name))
            rows = (
                await session.execute(
                    select(*columns)
                    .select_from(TrainingQuestionRecord)
                    .join(
                        QuestionRevisionRecord,
                        QuestionRevisionRecord.id
                        == TrainingQuestionRecord.question_revision_id,
                    )
                    .join(
                        ThemeRevisionRecord,
                        ThemeRevisionRecord.id == TrainingQuestionRecord.theme_revision_id,
                    )
                    .where(
                        TrainingQuestionRecord.session_id == record.id,
                        TrainingQuestionRecord.position > after,
                    )
                    .order_by(TrainingQuestionRecord.position)
                    .limit(limit + 1)
                )
            ).all()
            page = rows[:limit]
            next_cursor = None
            if len(rows) > limit and page:
                next_cursor = _encode_cursor(
                    {"position": page[-1].position, "sig": signature}
                )
            positions = [row.position for row in page]
            attempts = (
                {
                    attempt.question_index: attempt
                    for attempt in await session.scalars(
                        select(TrainingAttemptRecord).where(
                            TrainingAttemptRecord.session_id == record.id,
                            TrainingAttemptRecord.player_id == player_id,
                            TrainingAttemptRecord.question_index.in_(positions),
                        )
                    )
                }
                if positions
                else {}
            )
            revision_ids = [row.question_revision_id for row in page]
            statistics = (
                await self._question_statistics(session, revision_ids, timing)
                if revision_ids
                else {}
            )
            disclosed = await self._disclosed_claims(session, player_id, page)
            in_progress = record.status in {"active", "paused"}
            items = [
                self._question_row(
                    row, attempts.get(row.position), statistics, disclosed,
                    protected_source, in_progress,
                )
                for row in page
            ]
            return {
                **summary,
                "protected_source": protected_source,
                "rows": items,
                "next_cursor": next_cursor,
            }

    @staticmethod
    def _question_row(
        row, attempt, statistics, disclosed, protected_source, in_progress
    ):
        """One question row; protected content is dropped, never reconstructed."""
        empty_stats = {
            "presentations": 0,
            "buzzes": None,
            "attempts": 0,
            "answered": 0,
            "correct": 0,
            "incorrect": 0,
            # Legacy questions recorded no buzz timings; null means unavailable.
            "buzz_timings": None,
        }
        stats = statistics.get(row.question_revision_id, empty_stats)
        buzz = None
        buzzed_at = getattr(row, "buzzed_at", None)
        if buzzed_at is not None:
            delay = getattr(row, "buzz_delay_seconds", None)
            buzz = {
                "buzzed_at": buzzed_at,
                "buzz_delay_seconds": float(delay)
                if isinstance(delay, Decimal)
                else delay,
                "buzz_token_cursor": getattr(row, "buzz_token_cursor", None),
            }
        result = {
            "position": row.position,
            "value": row.value,
            "theme": row.theme,
            "text": None,
            "answer": None,
            "accepted_answers": None,
            "rejected_answers": None,
            "commentary": None,
            "source": None,
            "protected": None,
            "buzz": buzz,
            "stats": stats,
            "own": None,
        }
        if attempt is not None:
            own = {
                "answer": attempt.answer,
                "correct": attempt.correct,
                "outcome": attempt.outcome,
                "token_cursor": attempt.token_cursor,
                "created_at": attempt.created_at,
            }
            for name in ATTEMPT_VERDICT_FIELDS:
                own[name] = getattr(attempt, name, None)
            result["own"] = own
        if protected_source:
            result["theme"] = None
            result["stats"] = empty_stats
            result["protected"] = "revoked"
            return result
        pair = ("question", row.question_id)
        theme_pair = ("theme", row.theme_id)
        if pair not in disclosed or theme_pair not in disclosed:
            # A pinned revision alone is not proof that content was disclosed.
            result["theme"] = None
            result["stats"] = empty_stats
            result["protected"] = "undisclosed"
            return result
        opened_at = getattr(row, "opened_at", None)
        played = attempt is not None or opened_at is not None or buzzed_at is not None
        if not played or (in_progress and attempt is None):
            # Stopping a run does not disclose its unvisited tail. Do not bypass
            # hidden/incremental playback for a currently unanswered question.
            result["theme"] = None
            result["stats"] = empty_stats
            result["protected"] = "pending"
            return result
        result.update(
            text=row.text,
            answer=row.answer,
            accepted_answers=list(row.accepted_answers),
            rejected_answers=list(row.rejected_answers),
            commentary=row.commentary,
            source=row.source,
        )
        return result

    @staticmethod
    async def _score_totals(session: AsyncSession, session_ids: list[UUID]) -> dict[UUID, int]:
        """One grouped query: effective training score, independent of row count.

        Effective score per the gameplay contract: correct +value, incorrect
        (including post-buzz answer timeout) -value, everything else neutral.
        """
        if not session_ids:
            return {}
        rows = await session.execute(
            select(
                TrainingAttemptRecord.session_id,
                func.coalesce(
                    func.sum(
                        case(
                            (TrainingAttemptRecord.correct.is_(True), TrainingQuestionRecord.value),
                            (TrainingAttemptRecord.correct.is_(False),
                             -TrainingQuestionRecord.value),
                        )
                    ),
                    0,
                ),
            )
            .select_from(TrainingAttemptRecord)
            .join(
                TrainingQuestionRecord,
                and_(
                    TrainingQuestionRecord.session_id == TrainingAttemptRecord.session_id,
                    TrainingQuestionRecord.position == TrainingAttemptRecord.question_index,
                ),
            )
            .where(TrainingAttemptRecord.session_id.in_(session_ids))
            .group_by(TrainingAttemptRecord.session_id)
        )
        return {row[0]: int(row[1]) for row in rows}

    @staticmethod
    async def _question_totals(session: AsyncSession, session_ids: list[UUID]) -> dict[UUID, int]:
        if not session_ids:
            return {}
        rows = await session.execute(
            select(TrainingQuestionRecord.session_id, func.count())
            .where(TrainingQuestionRecord.session_id.in_(session_ids))
            .group_by(TrainingQuestionRecord.session_id)
        )
        return {row[0]: int(row[1]) for row in rows}

    @staticmethod
    async def _question_statistics(
        session: AsyncSession, question_ids: list[UUID], timing: dict
    ) -> dict[UUID, dict]:
        """Training-only counts and known buzz timing samples per pinned question.

        Anonymous revision-level counts aggregate training rows only (a buzz can
        exist without an attempt). Own answers/verdicts remain in the owner-only
        projection above. No competitive aggregates, identities or other owners'
        answers are returned, and protected rows withhold these statistics.
        """
        attempts = {
            row[0]: {
                "attempts": int(row[1]),
                "answered": int(row[2]),
                "correct": int(row[3]),
                "incorrect": int(row[4]),
            }
            for row in await session.execute(
                select(
                    TrainingAttemptRecord.question_revision_id,
                    func.count(),
                    func.count().filter(TrainingAttemptRecord.outcome == "answered"),
                    func.count().filter(TrainingAttemptRecord.correct.is_(True)),
                    func.count().filter(TrainingAttemptRecord.correct.is_(False)),
                )
                .where(
                    TrainingAttemptRecord.question_revision_id.in_(question_ids),
                )
                .group_by(TrainingAttemptRecord.question_revision_id)
            )
        }
        statistics = {
            identity: {
                **counts,
                "presentations": counts["attempts"],
                "buzzes": None,
                "buzz_timings": None,
            }
            for identity, counts in attempts.items()
        }
        buzzed_at = timing.get("buzzed_at")
        buzz_delay = timing.get("buzz_delay_seconds")
        opened_at = timing.get("opened_at")
        if buzzed_at is None or buzz_delay is None or opened_at is None:
            return statistics

        # Count recorded playback, not every pinned (future) question. Legacy
        # attempts prove play, but not an accepted buzz or its timing.
        played = or_(opened_at.is_not(None), buzzed_at.is_not(None),
                     TrainingAttemptRecord.id.is_not(None))
        scope = (TrainingQuestionRecord.question_revision_id.in_(question_ids),)
        rows = await session.execute(
            select(
                TrainingQuestionRecord.question_revision_id,
                func.count().filter(played),
                func.count().filter(buzzed_at.is_not(None)),
            )
            .join(TrainingSessionRecord,
                  TrainingSessionRecord.id == TrainingQuestionRecord.session_id)
            .outerjoin(TrainingAttemptRecord, and_(
                TrainingAttemptRecord.session_id == TrainingQuestionRecord.session_id,
                TrainingAttemptRecord.question_index == TrainingQuestionRecord.position,
            ))
            .where(*scope)
            .group_by(TrainingQuestionRecord.question_revision_id)
        )
        for identity, presentations, buzzes in rows:
            entry = statistics.setdefault(identity, {
                "attempts": 0, "answered": 0, "correct": 0, "incorrect": 0,
                "buzz_timings": None,
            })
            entry.update(presentations=int(presentations), buzzes=int(buzzes))

        # A SQL window bounds samples per question, rather than building an
        # unbounded array_agg of the player's lifetime history. Recent-first,
        # stable ties; at most 25 questions x 25 samples cross the DB boundary.
        samples = (
            select(
                TrainingQuestionRecord.question_revision_id.label("revision_id"),
                buzz_delay.label("delay"),
                func.row_number().over(
                    partition_by=TrainingQuestionRecord.question_revision_id,
                    order_by=(buzzed_at.desc(), TrainingQuestionRecord.session_id.desc(),
                              TrainingQuestionRecord.position.desc()),
                ).label("rank"),
            )
            .join(TrainingSessionRecord,
                  TrainingSessionRecord.id == TrainingQuestionRecord.session_id)
            .where(*scope, buzzed_at.is_not(None), buzz_delay.is_not(None))
            .subquery()
        )
        for identity, delay in await session.execute(
            select(samples.c.revision_id, samples.c.delay)
            .where(samples.c.rank <= TIMING_SAMPLE_LIMIT)
            .order_by(samples.c.revision_id, samples.c.rank)
        ):
            entry = statistics[identity]
            if entry["buzz_timings"] is None:
                entry["buzz_timings"] = []
            entry["buzz_timings"].append(float(delay))
        return statistics

    @staticmethod
    async def _disclosed_claims(session: AsyncSession, player_id: UUID, page) -> set[tuple]:
        """Burnt disclosure claims for the page's pinned theme/question identities."""
        pairs = sorted(
            {("theme", row.theme_id) for row in page}
            | {("question", row.question_id) for row in page}
        )
        if not pairs:
            return set()
        rows = await session.execute(
            select(
                PlayerExposureClaimRecord.claim_namespace,
                PlayerExposureClaimRecord.claim_id,
            ).where(
                PlayerExposureClaimRecord.player_id == player_id,
                tuple_(
                    PlayerExposureClaimRecord.claim_namespace,
                    PlayerExposureClaimRecord.claim_id,
                ).in_(pairs),
                PlayerExposureClaimRecord.state == "burnt",
            )
        )
        return {(row[0], row[1]) for row in rows}
