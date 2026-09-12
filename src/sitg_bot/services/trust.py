from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from math import floor
from uuid import UUID, uuid4

from sqlalchemy import Numeric, and_, case, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sitg_bot.domain.trust import (
    CHEATING_REPORT_SUSPICION_DELTA,
    MINIMUM_RATED_GAMES,
    REPORT_TYPES,
    REPUTATION_VOTE_DELTA,
    TOXICITY_REPORT_REPUTATION_DELTA,
    ContinuousSignal,
    SignalCount,
    SISuspicionCounts,
    apply_reputation_delta,
    evaluate_si_suspicion_counts,
)
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    AppealRecord,
    GameParticipantRecord,
    GameRecord,
    GameRulesetVersionRecord,
    GameThemeRecord,
    PacketQuestionRecord,
    PlatformAdministratorRecord,
    PlayerBanRecord,
    PlayerQuestionStateRecord,
    PlayerRecord,
    PlayerReportRecord,
    QuestionRevisionRecord,
    QuestionRoundRecord,
    ReputationLedgerRecord,
    ReputationVoteRecord,
    RulesetRatingRecord,
    SIPlayerGameSuspicionMetricRecord,
    SIPlayerQuestionSuspicionMetricRecord,
    SIPlayerSuspicionAggregateRecord,
    SIQuestionSuspicionAggregateRecord,
    SISuspicionBaselineRecord,
    SISuspicionMaterializedGameRecord,
    SuspicionEvaluationRecord,
    SuspicionEvaluationScheduleRecord,
    SuspicionEvidenceActionRecord,
    SuspicionEvidenceRecord,
    SuspicionLedgerRecord,
)

SI_SUSPICION_ALGORITHM_VERSION = "si-v2-relative-buzz-evidence"


@dataclass(frozen=True, slots=True)
class ReputationVoteReceipt:
    vote_id: UUID
    game_id: UUID
    target_player_id: UUID
    value: int
    reputation_before: int
    applied_delta: int
    reputation_after: int
    next_vote_at: datetime


@dataclass(frozen=True, slots=True)
class PlayerReportReceipt:
    report_id: UUID
    game_id: UUID
    reported_player_id: UUID
    kind: str
    reputation_after: int
    suspicion_after: int


@dataclass(frozen=True, slots=True)
class SuspicionTickSummary:
    materialized_games: int
    enqueued_players: int
    claimed_jobs: int
    evaluated_players: int
    postponed_players: int
    failed_players: int
    raised_points: int


@dataclass(frozen=True, slots=True)
class SuspicionClearanceReceipt:
    player_id: UUID
    suspicion_before: int
    suspicion_after: int
    administrator_id: UUID


class TrustService:
    """Global trust changes and bounded, incremental SI suspicion processing."""

    def __init__(
        self,
        database: Database,
        *,
        vote_cooldown: timedelta = timedelta(days=7),
        materialization_batch_size: int = 5,
        evaluation_batch_size: int = 10,
        enqueue_interval: timedelta = timedelta(hours=1),
        distribution_window: timedelta = timedelta(days=7),
        claim_timeout: timedelta = timedelta(minutes=15),
        postponed_retry_delay: timedelta = timedelta(days=7),
    ) -> None:
        if vote_cooldown <= timedelta(0):
            raise ValueError("vote_cooldown must be positive")
        if materialization_batch_size < 1 or evaluation_batch_size < 1:
            raise ValueError("Suspicion batch sizes must be positive")
        intervals = (
            enqueue_interval,
            distribution_window,
            claim_timeout,
            postponed_retry_delay,
        )
        if min(intervals) <= timedelta(0):
            raise ValueError("Suspicion processing intervals must be positive")
        self.database = database
        self.vote_cooldown = vote_cooldown
        self.materialization_batch_size = materialization_batch_size
        self.evaluation_batch_size = evaluation_batch_size
        self.enqueue_interval = enqueue_interval
        self.distribution_window = distribution_window
        self.claim_timeout = claim_timeout
        self.postponed_retry_delay = postponed_retry_delay

    async def vote_reputation(
        self,
        game_id: UUID,
        voter_player_id: UUID,
        target_telegram_user_id: int,
        value: int,
        *,
        expected_game_version: int | None = None,
    ) -> ReputationVoteReceipt:
        if isinstance(value, bool) or value not in {-1, 1}:
            raise ValueError("Reputation vote must be an upvote or downvote")
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            target_id = await session.scalar(
                select(PlayerRecord.id).where(
                    PlayerRecord.telegram_user_id == target_telegram_user_id
                )
            )
            if target_id is None:
                raise LookupError("Player not found")
            _, target = await self._lock_players(session, voter_player_id, target_id)
            await self._require_finished_game_participants(
                session,
                game_id,
                voter_player_id,
                target.id,
                expected_game_version=expected_game_version,
            )
            previous = await session.scalar(
                select(ReputationVoteRecord)
                .where(
                    ReputationVoteRecord.voter_player_id == voter_player_id,
                    ReputationVoteRecord.target_player_id == target.id,
                )
                .order_by(ReputationVoteRecord.created_at.desc())
                .limit(1)
            )
            if previous is not None and previous.created_at + self.vote_cooldown > now:
                raise ValueError(
                    "This player can be rated again after "
                    f"{(previous.created_at + self.vote_cooldown).isoformat()}"
                )
            duplicate = await session.scalar(
                select(ReputationVoteRecord.id).where(
                    ReputationVoteRecord.game_id == game_id,
                    ReputationVoteRecord.voter_player_id == voter_player_id,
                    ReputationVoteRecord.target_player_id == target.id,
                )
            )
            if duplicate is not None:
                raise ValueError("This player was already rated for this game")
            before = target.reputation
            requested_delta = value * REPUTATION_VOTE_DELTA
            after, applied_delta = apply_reputation_delta(before, requested_delta)
            vote = ReputationVoteRecord(
                game_id=game_id,
                voter_player_id=voter_player_id,
                target_player_id=target.id,
                value=value,
            )
            session.add(vote)
            await session.flush()
            target.reputation = after
            session.add(
                ReputationLedgerRecord(
                    player_id=target.id,
                    game_id=game_id,
                    vote_id=vote.id,
                    reputation_before=before,
                    requested_delta=requested_delta,
                    applied_delta=applied_delta,
                    reputation_after=after,
                    reason="player_vote",
                )
            )
            return ReputationVoteReceipt(
                vote.id,
                game_id,
                target.id,
                value,
                before,
                applied_delta,
                after,
                now + self.vote_cooldown,
            )

    async def report_player(
        self,
        game_id: UUID,
        reporter_player_id: UUID,
        target_telegram_user_id: int,
        kind: str,
        *,
        details: str | None = None,
        expected_game_version: int | None = None,
    ) -> PlayerReportReceipt:
        normalized_kind = kind.casefold()
        if normalized_kind not in REPORT_TYPES:
            raise ValueError("Report type must be cheating or toxicity")
        if details is not None:
            details = details.strip() or None
            if details is not None and len(details) > 2000:
                raise ValueError("Report details cannot exceed 2000 characters")
        async with self.database.transaction() as session:
            target_id = await session.scalar(
                select(PlayerRecord.id).where(
                    PlayerRecord.telegram_user_id == target_telegram_user_id
                )
            )
            if target_id is None:
                raise LookupError("Player not found")
            _, target = await self._lock_players(session, reporter_player_id, target_id)
            await self._require_finished_game_participants(
                session,
                game_id,
                reporter_player_id,
                target.id,
                expected_game_version=expected_game_version,
            )
            duplicate = await session.scalar(
                select(PlayerReportRecord.id).where(
                    PlayerReportRecord.game_id == game_id,
                    PlayerReportRecord.reporter_player_id == reporter_player_id,
                    PlayerReportRecord.reported_player_id == target.id,
                )
            )
            if duplicate is not None:
                raise ValueError("This player was already reported for this game")
            report = PlayerReportRecord(
                game_id=game_id,
                reporter_player_id=reporter_player_id,
                reported_player_id=target.id,
                kind=normalized_kind,
                details=details,
            )
            session.add(report)
            await session.flush()
            if normalized_kind == "toxicity":
                before = target.reputation
                after, applied = apply_reputation_delta(before, TOXICITY_REPORT_REPUTATION_DELTA)
                target.reputation = after
                session.add(
                    ReputationLedgerRecord(
                        player_id=target.id,
                        game_id=game_id,
                        report_id=report.id,
                        reputation_before=before,
                        requested_delta=TOXICITY_REPORT_REPUTATION_DELTA,
                        applied_delta=applied,
                        reputation_after=after,
                        reason="toxicity_report",
                    )
                )
            else:
                before = target.suspicion
                target.suspicion += CHEATING_REPORT_SUSPICION_DELTA
                session.add(
                    SuspicionLedgerRecord(
                        player_id=target.id,
                        report_id=report.id,
                        suspicion_before=before,
                        delta=CHEATING_REPORT_SUSPICION_DELTA,
                        suspicion_after=target.suspicion,
                        reason="cheating_report",
                    )
                )
            return PlayerReportReceipt(
                report.id,
                game_id,
                target.id,
                normalized_kind,
                target.reputation,
                target.suspicion,
            )

    async def review_player_suspicion(
        self,
        administrator_id: UUID,
        player_id: UUID,
        *,
        limit: int = 20,
    ) -> dict[str, object]:
        if limit < 1 or limit > 100:
            raise ValueError("Suspicion review limit must be between 1 and 100")
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            player = await session.get(PlayerRecord, player_id)
            if player is None:
                raise LookupError("Player not found")
            evaluations = list(
                (
                    await session.execute(
                        select(SuspicionEvaluationRecord)
                        .where(SuspicionEvaluationRecord.player_id == player_id)
                        .order_by(
                            SuspicionEvaluationRecord.period_end.desc(),
                            SuspicionEvaluationRecord.created_at.desc(),
                        )
                        .limit(limit)
                    )
                ).scalars()
            )
            evaluation_ids = [item.id for item in evaluations]
            evidence_rows = (
                list(
                    (
                        await session.execute(
                            select(SuspicionEvidenceRecord)
                            .where(SuspicionEvidenceRecord.evaluation_id.in_(evaluation_ids))
                            .order_by(SuspicionEvidenceRecord.created_at)
                        )
                    ).scalars()
                )
                if evaluation_ids
                else []
            )
            evidence_ids = [item.id for item in evidence_rows]
            action_rows = (
                list(
                    (
                        await session.execute(
                            select(SuspicionEvidenceActionRecord)
                            .where(SuspicionEvidenceActionRecord.evidence_id.in_(evidence_ids))
                            .order_by(
                                SuspicionEvidenceActionRecord.evidence_id,
                                SuspicionEvidenceActionRecord.id,
                            )
                        )
                    ).scalars()
                )
                if evidence_ids
                else []
            )
            ledgers = list(
                (
                    await session.execute(
                        select(SuspicionLedgerRecord)
                        .where(SuspicionLedgerRecord.player_id == player_id)
                        .order_by(SuspicionLedgerRecord.created_at.desc())
                        .limit(limit * 10)
                    )
                ).scalars()
            )
            reports = list(
                (
                    await session.execute(
                        select(PlayerReportRecord)
                        .where(
                            PlayerReportRecord.reported_player_id == player_id,
                            PlayerReportRecord.kind == "cheating",
                        )
                        .order_by(PlayerReportRecord.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )
            actions_by_evidence: dict[UUID, list[dict[str, object]]] = {}
            for action in action_rows:
                actions_by_evidence.setdefault(action.evidence_id, []).append(
                    {
                        "id": action.id,
                        "game_id": action.game_id,
                        "round_id": action.round_id,
                        "question_id": action.question_id,
                        "observed_at": action.observed_at,
                        "snapshot": action.snapshot,
                    }
                )
            evidence_by_evaluation: dict[UUID, list[dict[str, object]]] = {}
            for evidence in evidence_rows:
                evidence_by_evaluation.setdefault(evidence.evaluation_id, []).append(
                    {
                        "id": evidence.id,
                        "signal": evidence.signal,
                        "ruleset_key": evidence.ruleset_key,
                        "algorithm_version": evidence.algorithm_version,
                        "baseline_id": evidence.baseline_id,
                        "created_at": evidence.created_at,
                        "summary": evidence.summary,
                        "actions": actions_by_evidence.get(evidence.id, []),
                    }
                )
            return {
                "player": {
                    "id": player.id,
                    "telegram_user_id": player.telegram_user_id,
                    "display_name": player.public_nickname,
                    "suspicion": player.suspicion,
                },
                "evaluations": [
                    {
                        "id": item.id,
                        "ruleset_key": item.ruleset_key,
                        "period_start": item.period_start,
                        "period_end": item.period_end,
                        "status": item.status,
                        "evaluated_at": item.evaluated_at,
                        "details": item.details,
                        "evidence": evidence_by_evaluation.get(item.id, []),
                    }
                    for item in evaluations
                ],
                "ledger": [
                    {
                        "id": item.id,
                        "report_id": item.report_id,
                        "evaluation_id": item.evaluation_id,
                        "administrator_id": item.administrator_id,
                        "ruleset_key": item.ruleset_key,
                        "before": item.suspicion_before,
                        "delta": item.delta,
                        "after": item.suspicion_after,
                        "reason": item.reason,
                        "note": item.note,
                        "created_at": item.created_at,
                    }
                    for item in ledgers
                ],
                "cheating_reports": [
                    {
                        "id": item.id,
                        "game_id": item.game_id,
                        "reporter_player_id": item.reporter_player_id,
                        "details": item.details,
                        "created_at": item.created_at,
                    }
                    for item in reports
                ],
            }

    async def clear_player_suspicion(
        self,
        administrator_id: UUID,
        player_id: UUID,
        *,
        note: str,
    ) -> SuspicionClearanceReceipt:
        normalized_note = note.strip()
        if not normalized_note:
            raise ValueError("A suspicion clearance note is required")
        if len(normalized_note) > 2000:
            raise ValueError("Suspicion clearance note is too long")
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            player = await session.scalar(
                select(PlayerRecord).where(PlayerRecord.id == player_id).with_for_update()
            )
            if player is None:
                raise LookupError("Player not found")
            before = player.suspicion
            player.suspicion = 0
            session.add(
                SuspicionLedgerRecord(
                    player_id=player.id,
                    administrator_id=administrator_id,
                    suspicion_before=before,
                    delta=-before,
                    suspicion_after=0,
                    reason="admin_clearance",
                    note=normalized_note,
                )
            )
            return SuspicionClearanceReceipt(player.id, before, 0, administrator_id)

    async def suspicion_ledger(
        self,
        administrator_id: UUID,
        *,
        limit: int = 50,
    ) -> dict[str, object]:
        """Project admin-reviewable player cards ordered by suspicion, bans excluded."""
        if limit < 1 or limit > 100:
            raise ValueError("Suspicion ledger limit must be between 1 and 100")
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            active_bans = (
                select(PlayerBanRecord.player_id)
                .where(PlayerBanRecord.lifted_at.is_(None))
                .subquery()
            )
            players = list(
                (
                    await session.execute(
                        select(PlayerRecord)
                        .outerjoin(active_bans, active_bans.c.player_id == PlayerRecord.id)
                        .where(
                            PlayerRecord.suspicion > 0,
                            active_bans.c.player_id.is_(None),
                        )
                        .order_by(PlayerRecord.suspicion.desc(), PlayerRecord.id)
                        .limit(limit)
                    )
                ).scalars()
            )
            player_ids = [item.id for item in players]
            if not player_ids:
                return {"items": []}
            ratings = {
                (row[0], row[1]): float(row[2])
                for row in (
                    await session.execute(
                        select(
                            RulesetRatingRecord.player_id,
                            RulesetRatingRecord.ruleset_key,
                            RulesetRatingRecord.rating,
                        ).where(RulesetRatingRecord.player_id.in_(player_ids))
                    )
                ).all()
            }
            games = (
                await session.execute(
                    select(
                        GameParticipantRecord.player_id,
                        GameRulesetVersionRecord.key,
                        func.count(GameRecord.id),
                    )
                    .select_from(GameParticipantRecord)
                    .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                    .join(
                        GameRulesetVersionRecord,
                        GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
                    )
                    .where(
                        GameParticipantRecord.player_id.in_(player_ids),
                        GameRecord.status.in_(("completed", "finalized")),
                    )
                    .group_by(GameParticipantRecord.player_id, GameRulesetVersionRecord.key)
                )
            ).all()
            reports = (
                await session.execute(
                    select(
                        PlayerReportRecord.reported_player_id,
                        PlayerReportRecord.kind,
                        func.count(),
                    )
                    .where(PlayerReportRecord.reported_player_id.in_(player_ids))
                    .group_by(PlayerReportRecord.reported_player_id, PlayerReportRecord.kind)
                )
            ).all()
            rulesets: dict[UUID, dict[str, dict[str, object]]] = {
                player_id: {} for player_id in player_ids
            }
            for player_id, ruleset_key, games_played in games:
                rulesets[player_id][ruleset_key] = {
                    "rating": ratings.get((player_id, ruleset_key)),
                    "games_played": int(games_played),
                }
            for (player_id, ruleset_key), rating in ratings.items():
                rulesets[player_id].setdefault(
                    ruleset_key, {"rating": rating, "games_played": 0}
                )
            report_counts: dict[UUID, dict[str, int]] = {player_id: {} for player_id in player_ids}
            for player_id, kind, count in reports:
                report_counts[player_id][kind] = int(count)
            return {
                "items": [
                    {
                        "player_id": player.id,
                        "display_name": player.public_nickname,
                        "telegram_username": player.telegram_username,
                        "suspicion": player.suspicion,
                        "rulesets": [
                            {
                                "ruleset_key": key,
                                "rating": value["rating"],
                                "games_played": value["games_played"],
                            }
                            for key, value in sorted(rulesets[player.id].items())
                        ],
                        "reports": [
                            {"kind": kind, "count": count}
                            for kind, count in sorted(report_counts[player.id].items())
                        ],
                    }
                    for player in players
                ]
            }

    async def suspicion_inspection(
        self,
        administrator_id: UUID,
        player_id: UUID,
        *,
        limit: int = 100,
    ) -> dict[str, object]:
        """Show every event that increased the player's suspicion level."""
        if limit < 1 or limit > 100:
            raise ValueError("Suspicion inspection limit must be between 1 and 100")
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            player = await session.get(PlayerRecord, player_id)
            if player is None:
                raise LookupError("Player not found")
            ledgers = list(
                (
                    await session.execute(
                        select(SuspicionLedgerRecord)
                        .where(
                            SuspicionLedgerRecord.player_id == player_id,
                            SuspicionLedgerRecord.delta > 0,
                        )
                        .order_by(SuspicionLedgerRecord.created_at.desc(), SuspicionLedgerRecord.id)
                        .limit(limit)
                    )
                ).scalars()
            )
            evaluation_ids = [item.evaluation_id for item in ledgers if item.evaluation_id]
            evidence_rows = list(
                (
                    await session.execute(
                        select(SuspicionEvidenceRecord)
                        .where(
                            SuspicionEvidenceRecord.player_id == player_id,
                            SuspicionEvidenceRecord.evaluation_id.in_(evaluation_ids),
                        )
                        .order_by(SuspicionEvidenceRecord.created_at.desc())
                    )
                ).scalars()
            ) if evaluation_ids else []
            evidence_by_evaluation: dict[UUID, list[dict[str, object]]] = {}
            for evidence in evidence_rows:
                assert evidence.evaluation_id is not None
                evidence_by_evaluation.setdefault(evidence.evaluation_id, []).append(
                    {
                        "signal": evidence.signal,
                        "ruleset_key": evidence.ruleset_key,
                        "summary": evidence.summary,
                        "created_at": evidence.created_at,
                    }
                )
            return {
                "player": {
                    "id": player.id,
                    "display_name": player.public_nickname,
                    "telegram_username": player.telegram_username,
                    "suspicion": player.suspicion,
                },
                "events": [
                    {
                        "id": item.id,
                        "reason": item.reason,
                        "ruleset_key": item.ruleset_key,
                        "delta": item.delta,
                        "before": item.suspicion_before,
                        "after": item.suspicion_after,
                        "note": item.note,
                        "created_at": item.created_at,
                        "evidence": (
                            evidence_by_evaluation.get(item.evaluation_id, [])
                            if item.evaluation_id
                            else []
                        ),
                    }
                    for item in ledgers
                ],
            }

    async def process_suspicion_tick(self, *, now: datetime | None = None) -> SuspicionTickSummary:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        materialized = await self.materialize_si_games(now=current)
        enqueued = await self.enqueue_si_evaluations(now=current)
        job_ids = await self.claim_si_evaluations(now=current)
        evaluated = postponed = failed = raised = 0
        for job_id in job_ids:
            try:
                evaluation_now = datetime.now(UTC)
                status, points = await self._evaluate_si_job(job_id, now=evaluation_now)
                raised += points
                if status == "evaluated":
                    evaluated += 1
                else:
                    postponed += 1
            except Exception as error:
                failed += 1
                await self._fail_si_job(job_id, error, now=datetime.now(UTC))
        return SuspicionTickSummary(
            materialized,
            enqueued,
            len(job_ids),
            evaluated,
            postponed,
            failed,
            raised,
        )

    async def materialize_si_games(self, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        counted_participant = aliased(GameParticipantRecord)
        participant_count = (
            select(func.count())
            .select_from(counted_participant)
            .where(
                counted_participant.game_id == GameRecord.id,
                counted_participant.is_chair.is_(False),
            )
            .scalar_subquery()
        )
        unresolved_appeal = (
            select(AppealRecord.id)
            .where(
                AppealRecord.game_id == GameRecord.id,
                AppealRecord.status.in_(
                    ("voting", "awaiting_escalation", "awaiting_commentary", "escalated")
                ),
            )
            .exists()
        )
        already_materialized = (
            select(SISuspicionMaterializedGameRecord.game_id)
            .where(SISuspicionMaterializedGameRecord.game_id == GameRecord.id)
            .exists()
        )
        async with self.database.transaction() as session:
            games = list(
                (
                    await session.execute(
                        select(GameRecord)
                        .join(
                            GameRulesetVersionRecord,
                            GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
                        )
                        .where(
                            GameRulesetVersionRecord.key == "si",
                            GameRecord.status.in_(("completed", "finalized")),
                            GameRecord.completed_at.is_not(None),
                            GameRecord.completed_at <= current,
                            participant_count >= 2,
                            ~unresolved_appeal,
                            ~already_materialized,
                        )
                        .order_by(GameRecord.completed_at.desc(), GameRecord.id)
                        .limit(self.materialization_batch_size)
                        .with_for_update(skip_locked=True, of=GameRecord)
                    )
                ).scalars()
            )
            for game in games:
                await self._materialize_si_game(session, game)
            return len(games)

    async def _materialize_si_game(self, session: AsyncSession, game: GameRecord) -> None:
        assert game.completed_at is not None
        participants = list(
            (
                await session.execute(
                    select(GameParticipantRecord).where(
                        GameParticipantRecord.game_id == game.id,
                        GameParticipantRecord.is_chair.is_(False),
                    )
                )
            ).scalars()
        )
        counters = {
            item.player_id: {
                "accepted_buzzes": 0,
                "buzz_revealed_fraction_total": Decimal(0),
                "buzz_revealed_fraction_squared_total": Decimal(0),
                "buzz_revealed_fraction_observations": 0,
                "late_buzzes": 0,
                "high_value_exposures": 0,
                "high_value_correct": 0,
            }
            for item in participants
        }
        rows = (
            await session.execute(
                select(
                    GameParticipantRecord.player_id,
                    QuestionRoundRecord.id,
                    QuestionRevisionRecord.question_id,
                    PacketQuestionRecord.value,
                    AnswerAttemptRecord.final_correct,
                    AnswerAttemptRecord.judged_at,
                    PlayerQuestionStateRecord.buzzed_at,
                    PlayerQuestionStateRecord.buzz_revealed_fraction,
                    PlayerQuestionStateRecord.buzz_time_remaining_fraction,
                )
                .select_from(PlayerQuestionStateRecord)
                .join(
                    GameParticipantRecord,
                    GameParticipantRecord.id == PlayerQuestionStateRecord.participant_id,
                )
                .join(
                    QuestionRoundRecord,
                    QuestionRoundRecord.id == PlayerQuestionStateRecord.round_id,
                )
                .join(
                    QuestionRevisionRecord,
                    QuestionRevisionRecord.id == QuestionRoundRecord.question_revision_id,
                )
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                )
                .join(
                    GameThemeRecord,
                    and_(
                        GameThemeRecord.game_id == game.id,
                        GameThemeRecord.theme_revision_id == PacketQuestionRecord.theme_revision_id,
                        GameThemeRecord.source_packet_version_id
                        == PacketQuestionRecord.packet_version_id,
                    ),
                )
                .outerjoin(
                    AnswerAttemptRecord,
                    and_(
                        AnswerAttemptRecord.round_id == QuestionRoundRecord.id,
                        AnswerAttemptRecord.participant_id == GameParticipantRecord.id,
                    ),
                )
                .where(GameParticipantRecord.game_id == game.id)
            )
        ).all()
        question_totals: dict[UUID, list[int]] = {}
        seen: set[tuple[UUID, UUID]] = set()
        for (
            player_id,
            round_id,
            question_id,
            value,
            correct,
            answered_at,
            buzzed_at,
            revealed,
            remaining,
        ) in rows:
            key = (player_id, question_id)
            if key in seen:
                continue
            seen.add(key)
            is_correct = bool(correct)
            counter = counters[player_id]
            if buzzed_at is not None:
                counter["accepted_buzzes"] += 1
                if revealed is not None:
                    revealed_value = Decimal(revealed)
                    counter["buzz_revealed_fraction_total"] += revealed_value
                    counter["buzz_revealed_fraction_squared_total"] += (
                        revealed_value * revealed_value
                    )
                    counter["buzz_revealed_fraction_observations"] += 1
                if remaining is not None and Decimal(remaining) <= Decimal("0.1"):
                    counter["late_buzzes"] += 1
            if int(value) in {40, 50}:
                counter["high_value_exposures"] += 1
                counter["high_value_correct"] += int(is_correct)
            session.add(
                SIPlayerQuestionSuspicionMetricRecord(
                    game_id=game.id,
                    player_id=player_id,
                    question_id=question_id,
                    round_id=round_id,
                    question_value=int(value),
                    correct=is_correct,
                    buzzed_at=buzzed_at,
                    answered_at=answered_at,
                    buzz_revealed_fraction=revealed,
                    buzz_time_remaining_fraction=remaining,
                )
            )
            totals = question_totals.setdefault(question_id, [0, 0])
            totals[0] += 1
            totals[1] += int(is_correct)
        player_aggregate_values: list[dict[str, object]] = []
        for participant in participants:
            values = counters[participant.player_id]
            session.add(
                SIPlayerGameSuspicionMetricRecord(
                    game_id=game.id,
                    player_id=participant.player_id,
                    completed_at=game.completed_at,
                    **values,
                )
            )
            player_aggregate_values.append(
                {
                    "player_id": participant.player_id,
                    "games_played": 1,
                    **values,
                }
            )
        if player_aggregate_values:
            statement = pg_insert(SIPlayerSuspicionAggregateRecord).values(player_aggregate_values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[SIPlayerSuspicionAggregateRecord.player_id],
                    set_={
                        "games_played": SIPlayerSuspicionAggregateRecord.games_played + 1,
                        "accepted_buzzes": (
                            SIPlayerSuspicionAggregateRecord.accepted_buzzes
                            + statement.excluded.accepted_buzzes
                        ),
                        "buzz_revealed_fraction_total": (
                            SIPlayerSuspicionAggregateRecord.buzz_revealed_fraction_total
                            + statement.excluded.buzz_revealed_fraction_total
                        ),
                        "buzz_revealed_fraction_squared_total": (
                            SIPlayerSuspicionAggregateRecord.buzz_revealed_fraction_squared_total
                            + statement.excluded.buzz_revealed_fraction_squared_total
                        ),
                        "buzz_revealed_fraction_observations": (
                            SIPlayerSuspicionAggregateRecord.buzz_revealed_fraction_observations
                            + statement.excluded.buzz_revealed_fraction_observations
                        ),
                        "late_buzzes": (
                            SIPlayerSuspicionAggregateRecord.late_buzzes
                            + statement.excluded.late_buzzes
                        ),
                        "high_value_exposures": (
                            SIPlayerSuspicionAggregateRecord.high_value_exposures
                            + statement.excluded.high_value_exposures
                        ),
                        "high_value_correct": (
                            SIPlayerSuspicionAggregateRecord.high_value_correct
                            + statement.excluded.high_value_correct
                        ),
                        "updated_at": func.now(),
                    },
                )
            )
        question_aggregate_values = [
            {"question_id": question_id, "exposures": exposures, "correct": correct}
            for question_id, (exposures, correct) in question_totals.items()
        ]
        if question_aggregate_values:
            statement = pg_insert(SIQuestionSuspicionAggregateRecord).values(
                question_aggregate_values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[SIQuestionSuspicionAggregateRecord.question_id],
                    set_={
                        "exposures": (
                            SIQuestionSuspicionAggregateRecord.exposures
                            + statement.excluded.exposures
                        ),
                        "correct": (
                            SIQuestionSuspicionAggregateRecord.correct + statement.excluded.correct
                        ),
                        "updated_at": func.now(),
                    },
                )
            )
        session.add(
            SISuspicionMaterializedGameRecord(
                game_id=game.id,
                completed_at=game.completed_at,
            )
        )

    async def enqueue_si_evaluations(self, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        period_start, period_end = self.completed_week(current)
        async with self.database.transaction() as session:
            inserted_schedule = await session.scalar(
                pg_insert(SuspicionEvaluationScheduleRecord)
                .values(
                    ruleset_key="si",
                    period_start=period_start,
                    period_end=period_end,
                    enqueued_at=current,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        SuspicionEvaluationScheduleRecord.ruleset_key,
                        SuspicionEvaluationScheduleRecord.period_start,
                        SuspicionEvaluationScheduleRecord.period_end,
                    ]
                )
                .returning(SuspicionEvaluationScheduleRecord.period_start)
            )
            schedule = await session.scalar(
                select(SuspicionEvaluationScheduleRecord)
                .where(
                    SuspicionEvaluationScheduleRecord.ruleset_key == "si",
                    SuspicionEvaluationScheduleRecord.period_start == period_start,
                    SuspicionEvaluationScheduleRecord.period_end == period_end,
                )
                .with_for_update()
            )
            assert schedule is not None
            if inserted_schedule is None and schedule.enqueued_at + self.enqueue_interval > current:
                return 0
            schedule.enqueued_at = current
            established = (
                select(SIPlayerSuspicionAggregateRecord.player_id)
                .where(SIPlayerSuspicionAggregateRecord.games_played >= MINIMUM_RATED_GAMES)
                .subquery()
            )
            player_ids = list(
                (
                    await session.execute(
                        select(SIPlayerGameSuspicionMetricRecord.player_id)
                        .join(
                            established,
                            established.c.player_id == SIPlayerGameSuspicionMetricRecord.player_id,
                        )
                        .where(
                            SIPlayerGameSuspicionMetricRecord.completed_at >= period_start,
                            SIPlayerGameSuspicionMetricRecord.completed_at < period_end,
                        )
                        .distinct()
                    )
                ).scalars()
            )
            distribution_seconds = max(1, int(self.distribution_window.total_seconds()))
            evaluation_values: list[dict[str, object]] = []
            for player_id in player_ids:
                scheduled_at = period_end + timedelta(seconds=player_id.int % distribution_seconds)
                evaluation_values.append(
                    {
                        "id": uuid4(),
                        "player_id": player_id,
                        "ruleset_key": "si",
                        "period_start": period_start,
                        "period_end": period_end,
                        "status": "queued",
                        "scheduled_at": scheduled_at,
                        "attempts": 0,
                        "details": {},
                    }
                )
            inserted = 0
            for offset in range(0, len(evaluation_values), 500):
                statement = (
                    pg_insert(SuspicionEvaluationRecord)
                    .values(evaluation_values[offset : offset + 500])
                    .on_conflict_do_nothing(
                        index_elements=[
                            SuspicionEvaluationRecord.player_id,
                            SuspicionEvaluationRecord.ruleset_key,
                            SuspicionEvaluationRecord.period_start,
                            SuspicionEvaluationRecord.period_end,
                        ]
                    )
                    .returning(SuspicionEvaluationRecord.id)
                )
                inserted += len((await session.execute(statement)).scalars().all())
            return inserted

    async def claim_si_evaluations(self, *, now: datetime | None = None) -> tuple[UUID, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        async with self.database.transaction() as session:
            await session.execute(
                update(SuspicionEvaluationRecord)
                .where(
                    SuspicionEvaluationRecord.status == "processing",
                    SuspicionEvaluationRecord.claimed_at < current - self.claim_timeout,
                )
                .values(status="queued", claimed_at=None, scheduled_at=current)
            )
            jobs = list(
                (
                    await session.execute(
                        select(SuspicionEvaluationRecord)
                        .where(
                            SuspicionEvaluationRecord.ruleset_key == "si",
                            SuspicionEvaluationRecord.status.in_(("queued", "postponed")),
                            SuspicionEvaluationRecord.scheduled_at <= current,
                        )
                        .order_by(
                            SuspicionEvaluationRecord.scheduled_at,
                            SuspicionEvaluationRecord.created_at,
                        )
                        .limit(self.evaluation_batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for job in jobs:
                job.status = "processing"
                job.claimed_at = current
                job.attempts += 1
                job.last_error = None
            return tuple(job.id for job in jobs)

    async def _evaluate_si_job(self, evaluation_id: UUID, *, now: datetime) -> tuple[str, int]:
        async with self.database.transaction() as session:
            evaluation = await session.scalar(
                select(SuspicionEvaluationRecord)
                .where(SuspicionEvaluationRecord.id == evaluation_id)
                .with_for_update()
            )
            if evaluation is None or evaluation.status != "processing":
                return "evaluated", 0
            aggregate = await session.get(
                SIPlayerSuspicionAggregateRecord,
                evaluation.player_id,
            )
            game_rows = list(
                (
                    await session.execute(
                        select(SIPlayerGameSuspicionMetricRecord)
                        .where(
                            SIPlayerGameSuspicionMetricRecord.player_id == evaluation.player_id,
                            SIPlayerGameSuspicionMetricRecord.completed_at < evaluation.period_end,
                        )
                        .order_by(
                            SIPlayerGameSuspicionMetricRecord.completed_at,
                            SIPlayerGameSuspicionMetricRecord.game_id,
                        )
                    )
                ).scalars()
            )
            if aggregate is None or len(game_rows) < MINIMUM_RATED_GAMES:
                evaluation.status = "evaluated"
                evaluation.claimed_at = None
                evaluation.evaluated_at = now
                evaluation.details = {
                    "reason": "rating_not_established",
                    "games_played": len(game_rows),
                    "required_games": MINIMUM_RATED_GAMES,
                }
                return "evaluated", 0
            rating = await session.scalar(
                select(RulesetRatingRecord.rating).where(
                    RulesetRatingRecord.ruleset_key == "si",
                    RulesetRatingRecord.player_id == evaluation.player_id,
                )
            )
            rating_value = float(rating if rating is not None else 1000)
            rating_bucket = floor(rating_value / 50) * 50
            baseline = await self._baseline(
                session,
                evaluation.period_start,
                evaluation.period_end,
                rating_bucket,
                now,
            )
            eligible_game_ids = {
                item.game_id
                for item in game_rows[MINIMUM_RATED_GAMES - 1 :]
                if evaluation.period_start <= item.completed_at < evaluation.period_end
            }
            period_rows = [item for item in game_rows if item.game_id in eligible_game_ids]
            player_counts = await self._counts_for_rows(
                session,
                evaluation.player_id,
                period_rows,
                baseline.rare_correct_rate_cutoff,
            )
            player_early_timing = self._timing_for_rows(period_rows)
            baseline_counts = self._counts_from_details(baseline.details)
            baseline_early_timing = self._timing_from_details(baseline.details)
            target_baseline, target_early_timing = await self._target_baseline_statistics(
                session,
                evaluation.player_id,
                baseline.built_at,
                baseline.rare_correct_rate_cutoff,
            )
            cohort_counts = self._subtract_counts(baseline_counts, target_baseline)
            cohort_early_timing = self._subtract_timing(baseline_early_timing, target_early_timing)
            cohort_players = max(0, int(baseline.details.get("players", 0)) - 1)
            result = evaluate_si_suspicion_counts(
                player_counts,
                cohort_counts,
                cohort_players=cohort_players,
                player_early_timing=player_early_timing,
                cohort_early_timing=cohort_early_timing,
            )
            evaluation.details = {
                **result.details,
                "target_rating": rating_value,
                "rating_bucket": rating_bucket,
                "baseline_id": str(baseline.id),
                "baseline_built_at": baseline.built_at.isoformat(),
                "baseline_average_buzz_revealed_fraction": (
                    float(baseline.average_buzz_revealed_fraction)
                    if baseline.average_buzz_revealed_fraction is not None
                    else None
                ),
                "rare_question_correct_rate_cutoff": (
                    float(baseline.rare_correct_rate_cutoff)
                    if baseline.rare_correct_rate_cutoff is not None
                    else None
                ),
            }
            player = await session.scalar(
                select(PlayerRecord)
                .where(PlayerRecord.id == evaluation.player_id)
                .with_for_update()
            )
            if player is None:
                raise RuntimeError("Suspicion evaluation player is missing")
            existing = set(
                (
                    await session.execute(
                        select(SuspicionLedgerRecord.reason).where(
                            SuspicionLedgerRecord.evaluation_id == evaluation.id
                        )
                    )
                ).scalars()
            )
            raised = 0
            for signal in result.raised_signals:
                if signal in existing:
                    continue
                before = player.suspicion
                player.suspicion += 1
                raised += 1
                session.add(
                    SuspicionLedgerRecord(
                        player_id=player.id,
                        evaluation_id=evaluation.id,
                        ruleset_key="si",
                        suspicion_before=before,
                        delta=1,
                        suspicion_after=player.suspicion,
                        reason=signal,
                    )
                )
                await self._snapshot_si_evidence(
                    session,
                    evaluation,
                    baseline,
                    signal,
                    period_rows,
                    result.details,
                )
            evaluation.claimed_at = None
            if result.status == "postponed":
                evaluation.status = "postponed"
                evaluation.scheduled_at = now + self.postponed_retry_delay
            else:
                evaluation.status = "evaluated"
                evaluation.evaluated_at = now
            return result.status, raised

    async def _snapshot_si_evidence(
        self,
        session: AsyncSession,
        evaluation: SuspicionEvaluationRecord,
        baseline: SISuspicionBaselineRecord,
        signal: str,
        period_rows: list[SIPlayerGameSuspicionMetricRecord],
        result_details: dict[str, object],
    ) -> None:
        signal_details = result_details.get("signals", {})
        assert isinstance(signal_details, dict)
        metric_details = signal_details.get(signal, {})
        assert isinstance(metric_details, dict)
        evidence = SuspicionEvidenceRecord(
            evaluation_id=evaluation.id,
            player_id=evaluation.player_id,
            baseline_id=baseline.id,
            ruleset_key="si",
            signal=signal,
            algorithm_version=SI_SUSPICION_ALGORITHM_VERSION,
            summary={
                "period_start": evaluation.period_start.isoformat(),
                "period_end": evaluation.period_end.isoformat(),
                "rating_bucket": baseline.rating_bucket,
                "baseline_built_at": baseline.built_at.isoformat(),
                "statistics": metric_details,
                "minimum_player_observations": 10,
                "minimum_cohort_observations": 100,
                "minimum_cohort_players": 10,
                "required_margin": 0.15,
                "required_z_score": 2.5,
                "late_buzz_remaining_fraction_threshold": 0.1,
                "rare_question_correct_rate_cutoff": (
                    float(baseline.rare_correct_rate_cutoff)
                    if baseline.rare_correct_rate_cutoff is not None
                    else None
                ),
            },
        )
        session.add(evidence)
        await session.flush()
        game_ids = [item.game_id for item in period_rows]
        if not game_ids:
            return
        facts = list(
            (
                await session.execute(
                    select(
                        SIPlayerQuestionSuspicionMetricRecord,
                        SIQuestionSuspicionAggregateRecord.exposures,
                        SIQuestionSuspicionAggregateRecord.correct,
                    )
                    .join(
                        SIQuestionSuspicionAggregateRecord,
                        SIQuestionSuspicionAggregateRecord.question_id
                        == SIPlayerQuestionSuspicionMetricRecord.question_id,
                    )
                    .where(
                        SIPlayerQuestionSuspicionMetricRecord.player_id == evaluation.player_id,
                        SIPlayerQuestionSuspicionMetricRecord.game_id.in_(game_ids),
                    )
                    .order_by(
                        SIPlayerQuestionSuspicionMetricRecord.game_id,
                        SIPlayerQuestionSuspicionMetricRecord.round_id,
                    )
                )
            ).all()
        )
        for fact, question_exposures, question_correct in facts:
            question_rate = int(question_correct) / int(question_exposures)
            qualifies = False
            qualification: dict[str, object] = {}
            observed_at = fact.answered_at or fact.buzzed_at
            if signal == "very_early_buzz":
                qualifies = fact.buzz_revealed_fraction is not None
                qualification = {
                    "buzz_revealed_fraction": (
                        float(fact.buzz_revealed_fraction)
                        if fact.buzz_revealed_fraction is not None
                        else None
                    ),
                    "player_average_revealed_fraction": metric_details.get(
                        "player_average_revealed_fraction"
                    ),
                    "cohort_average_revealed_fraction": metric_details.get(
                        "cohort_average_revealed_fraction"
                    ),
                }
            elif signal == "very_late_buzz":
                qualifies = fact.buzz_time_remaining_fraction is not None and Decimal(
                    fact.buzz_time_remaining_fraction
                ) <= Decimal("0.1")
                qualification = {
                    "buzz_time_remaining_fraction": (
                        float(fact.buzz_time_remaining_fraction)
                        if fact.buzz_time_remaining_fraction is not None
                        else None
                    ),
                    "threshold": 0.1,
                }
            elif signal == "rare_question_accuracy":
                cutoff = baseline.rare_correct_rate_cutoff
                qualifies = (
                    fact.correct
                    and int(question_exposures) >= 10
                    and cutoff is not None
                    and Decimal(str(question_rate)) <= cutoff
                )
                qualification = {
                    "correct": fact.correct,
                    "question_correct_rate": question_rate,
                    "question_exposures": int(question_exposures),
                    "cutoff": float(cutoff) if cutoff is not None else None,
                }
            if not qualifies:
                continue
            session.add(
                SuspicionEvidenceActionRecord(
                    evidence_id=evidence.id,
                    game_id=fact.game_id,
                    round_id=fact.round_id,
                    question_id=fact.question_id,
                    observed_at=observed_at,
                    snapshot={
                        **qualification,
                        "buzzed_at": (
                            fact.buzzed_at.isoformat() if fact.buzzed_at is not None else None
                        ),
                        "answered_at": (
                            fact.answered_at.isoformat() if fact.answered_at is not None else None
                        ),
                        "question_value": fact.question_value,
                        "correct": fact.correct,
                        "question_correct_rate_at_evaluation": question_rate,
                        "question_exposures_at_evaluation": int(question_exposures),
                    },
                )
            )

    async def _baseline(
        self,
        session: AsyncSession,
        period_start: datetime,
        period_end: datetime,
        rating_bucket: int,
        now: datetime,
    ) -> SISuspicionBaselineRecord:
        await session.execute(
            pg_insert(SISuspicionBaselineRecord)
            .values(
                id=uuid4(),
                period_start=period_start,
                period_end=period_end,
                rating_bucket=rating_bucket,
                built_at=period_end - timedelta(days=3650),
                details={},
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SISuspicionBaselineRecord.period_start,
                    SISuspicionBaselineRecord.period_end,
                    SISuspicionBaselineRecord.rating_bucket,
                ]
            )
        )
        baseline = await session.scalar(
            select(SISuspicionBaselineRecord)
            .where(
                SISuspicionBaselineRecord.period_start == period_start,
                SISuspicionBaselineRecord.period_end == period_end,
                SISuspicionBaselineRecord.rating_bucket == rating_bucket,
            )
            .with_for_update()
        )
        assert baseline is not None
        if baseline.details and baseline.built_at + self.postponed_retry_delay > now:
            return baseline
        rates = sorted(
            float(correct) / int(exposures)
            for correct, exposures in (
                await session.execute(
                    select(
                        SIQuestionSuspicionAggregateRecord.correct,
                        SIQuestionSuspicionAggregateRecord.exposures,
                    ).where(SIQuestionSuspicionAggregateRecord.exposures >= 10)
                )
            ).all()
        )
        rare_cutoff = rates[max(0, int(len(rates) * 0.2) - 1)] if rates else None
        lower = rating_bucket - 200
        upper = rating_bucket + 249.9999
        aggregate_rows = list(
            (
                await session.execute(
                    select(SIPlayerSuspicionAggregateRecord)
                    .outerjoin(
                        RulesetRatingRecord,
                        and_(
                            RulesetRatingRecord.player_id
                            == SIPlayerSuspicionAggregateRecord.player_id,
                            RulesetRatingRecord.ruleset_key == "si",
                        ),
                    )
                    .where(
                        SIPlayerSuspicionAggregateRecord.games_played >= MINIMUM_RATED_GAMES,
                        func.coalesce(RulesetRatingRecord.rating, 1000) >= lower,
                        func.coalesce(RulesetRatingRecord.rating, 1000) <= upper,
                    )
                )
            ).scalars()
        )
        revealed_observations = sum(
            item.buzz_revealed_fraction_observations for item in aggregate_rows
        )
        revealed_total = sum(
            (Decimal(item.buzz_revealed_fraction_total) for item in aggregate_rows),
            Decimal(0),
        )
        revealed_squared_total = sum(
            (Decimal(item.buzz_revealed_fraction_squared_total) for item in aggregate_rows),
            Decimal(0),
        )
        average_revealed_fraction = (
            revealed_total / revealed_observations if revealed_observations else None
        )
        rare_eligible = rare_positive = 0
        if rare_cutoff is not None:
            rare_eligible, rare_positive = (
                await session.execute(
                    select(
                        func.count(),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        SIPlayerQuestionSuspicionMetricRecord.correct.is_(True),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                    )
                    .join(
                        SIQuestionSuspicionAggregateRecord,
                        SIQuestionSuspicionAggregateRecord.question_id
                        == SIPlayerQuestionSuspicionMetricRecord.question_id,
                    )
                    .join(
                        SIPlayerSuspicionAggregateRecord,
                        SIPlayerSuspicionAggregateRecord.player_id
                        == SIPlayerQuestionSuspicionMetricRecord.player_id,
                    )
                    .outerjoin(
                        RulesetRatingRecord,
                        and_(
                            RulesetRatingRecord.player_id
                            == SIPlayerQuestionSuspicionMetricRecord.player_id,
                            RulesetRatingRecord.ruleset_key == "si",
                        ),
                    )
                    .where(
                        SIPlayerSuspicionAggregateRecord.games_played >= MINIMUM_RATED_GAMES,
                        func.coalesce(RulesetRatingRecord.rating, 1000) >= lower,
                        func.coalesce(RulesetRatingRecord.rating, 1000) <= upper,
                        SIQuestionSuspicionAggregateRecord.exposures >= 10,
                        (
                            cast(
                                SIQuestionSuspicionAggregateRecord.correct,
                                Numeric(18, 8),
                            )
                            / SIQuestionSuspicionAggregateRecord.exposures
                        )
                        <= Decimal(str(rare_cutoff)),
                    )
                )
            ).one()
        details = {
            "players": len(aggregate_rows),
            "very_early_buzz": {
                "positive": 0,
                "eligible": revealed_observations,
                "total_revealed_fraction": float(revealed_total),
                "total_squared_revealed_fraction": float(revealed_squared_total),
                "average_revealed_fraction": (
                    float(average_revealed_fraction)
                    if average_revealed_fraction is not None
                    else None
                ),
            },
            "very_late_buzz": {
                "positive": sum(item.late_buzzes for item in aggregate_rows),
                "eligible": sum(item.accepted_buzzes for item in aggregate_rows),
            },
            "rare_question_accuracy": {
                "positive": int(rare_positive or 0),
                "eligible": int(rare_eligible or 0),
            },
        }
        baseline.average_buzz_revealed_fraction = average_revealed_fraction
        baseline.rare_correct_rate_cutoff = (
            Decimal(str(rare_cutoff)) if rare_cutoff is not None else None
        )
        baseline.details = details
        baseline.built_at = now
        return baseline

    async def _counts_for_rows(
        self,
        session: AsyncSession,
        player_id: UUID,
        rows: list[SIPlayerGameSuspicionMetricRecord],
        rare_cutoff: Decimal | None,
    ) -> SISuspicionCounts:
        game_ids = [item.game_id for item in rows]
        rare_eligible = rare_positive = 0
        if game_ids and rare_cutoff is not None:
            rare_eligible, rare_positive = (
                await session.execute(
                    select(
                        func.count(),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        SIPlayerQuestionSuspicionMetricRecord.correct.is_(True),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                    )
                    .join(
                        SIQuestionSuspicionAggregateRecord,
                        SIQuestionSuspicionAggregateRecord.question_id
                        == SIPlayerQuestionSuspicionMetricRecord.question_id,
                    )
                    .where(
                        SIPlayerQuestionSuspicionMetricRecord.player_id == player_id,
                        SIPlayerQuestionSuspicionMetricRecord.game_id.in_(game_ids),
                        SIQuestionSuspicionAggregateRecord.exposures >= 10,
                        (
                            cast(
                                SIQuestionSuspicionAggregateRecord.correct,
                                Numeric(18, 8),
                            )
                            / SIQuestionSuspicionAggregateRecord.exposures
                        )
                        <= rare_cutoff,
                    )
                )
            ).one()
        return SISuspicionCounts(
            SignalCount(
                0,
                sum(item.buzz_revealed_fraction_observations for item in rows),
            ),
            SignalCount(
                sum(item.late_buzzes for item in rows),
                sum(item.accepted_buzzes for item in rows),
            ),
            SignalCount(int(rare_positive or 0), int(rare_eligible or 0)),
        )

    async def _target_baseline_statistics(
        self,
        session: AsyncSession,
        player_id: UUID,
        before: datetime,
        rare_cutoff: Decimal | None,
    ) -> tuple[SISuspicionCounts, ContinuousSignal]:
        rows = list(
            (
                await session.execute(
                    select(SIPlayerGameSuspicionMetricRecord)
                    .join(
                        SISuspicionMaterializedGameRecord,
                        SISuspicionMaterializedGameRecord.game_id
                        == SIPlayerGameSuspicionMetricRecord.game_id,
                    )
                    .where(
                        SIPlayerGameSuspicionMetricRecord.player_id == player_id,
                        SISuspicionMaterializedGameRecord.materialized_at <= before,
                    )
                )
            ).scalars()
        )
        return (
            await self._counts_for_rows(session, player_id, rows, rare_cutoff),
            self._timing_for_rows(rows),
        )

    @staticmethod
    def _counts_from_details(details: dict[str, object]) -> SISuspicionCounts:
        def count(name: str) -> SignalCount:
            raw = details.get(name, {})
            assert isinstance(raw, dict)
            return SignalCount(
                int(raw.get("positive", 0)),
                int(raw.get("eligible", 0)),
            )

        return SISuspicionCounts(
            count("very_early_buzz"),
            count("very_late_buzz"),
            count("rare_question_accuracy"),
        )

    @staticmethod
    def _timing_for_rows(
        rows: list[SIPlayerGameSuspicionMetricRecord],
    ) -> ContinuousSignal:
        return ContinuousSignal(
            total=float(
                sum(
                    (Decimal(item.buzz_revealed_fraction_total) for item in rows),
                    Decimal(0),
                )
            ),
            total_squares=float(
                sum(
                    (Decimal(item.buzz_revealed_fraction_squared_total) for item in rows),
                    Decimal(0),
                )
            ),
            observations=sum(item.buzz_revealed_fraction_observations for item in rows),
        )

    @staticmethod
    def _timing_from_details(details: dict[str, object]) -> ContinuousSignal:
        raw = details.get("very_early_buzz", {})
        assert isinstance(raw, dict)
        return ContinuousSignal(
            total=float(raw.get("total_revealed_fraction", 0)),
            total_squares=float(raw.get("total_squared_revealed_fraction", 0)),
            observations=int(raw.get("eligible", 0)),
        )

    @staticmethod
    def _subtract_timing(
        total: ContinuousSignal,
        contribution: ContinuousSignal,
    ) -> ContinuousSignal:
        return ContinuousSignal(
            total=max(0.0, total.total - contribution.total),
            total_squares=max(0.0, total.total_squares - contribution.total_squares),
            observations=max(0, total.observations - contribution.observations),
        )

    @staticmethod
    def _subtract_counts(
        total: SISuspicionCounts,
        contribution: SISuspicionCounts,
    ) -> SISuspicionCounts:
        def subtract(first: SignalCount, second: SignalCount) -> SignalCount:
            return SignalCount(
                max(0, first.positive - second.positive),
                max(0, first.eligible - second.eligible),
            )

        return SISuspicionCounts(
            subtract(total.very_early_buzz, contribution.very_early_buzz),
            subtract(total.very_late_buzz, contribution.very_late_buzz),
            subtract(total.rare_question_accuracy, contribution.rare_question_accuracy),
        )

    async def _fail_si_job(
        self,
        evaluation_id: UUID,
        error: Exception,
        *,
        now: datetime,
    ) -> None:
        async with self.database.transaction() as session:
            evaluation = await session.scalar(
                select(SuspicionEvaluationRecord)
                .where(SuspicionEvaluationRecord.id == evaluation_id)
                .with_for_update()
            )
            if evaluation is None or evaluation.status != "processing":
                return
            delay_seconds = min(3600, 60 * (2 ** min(evaluation.attempts, 6)))
            evaluation.status = "queued"
            evaluation.claimed_at = None
            evaluation.scheduled_at = now + timedelta(seconds=delay_seconds)
            evaluation.last_error = str(error)[:2000]

    @staticmethod
    def completed_week(now: datetime | None = None) -> tuple[datetime, datetime]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        period_end = datetime.combine(
            current.date() - timedelta(days=current.weekday()),
            time.min,
            tzinfo=UTC,
        )
        return period_end - timedelta(days=7), period_end

    @staticmethod
    async def _require_administrator(session: AsyncSession, administrator_id: UUID) -> None:
        administrator = await session.get(PlatformAdministratorRecord, administrator_id)
        if administrator is None or administrator.revoked_at is not None:
            raise PermissionError("Platform administrator role is required")

    @staticmethod
    async def _lock_players(
        session: AsyncSession,
        first_id: UUID,
        second_id: UUID,
    ) -> tuple[PlayerRecord, PlayerRecord]:
        if first_id == second_id:
            raise ValueError("Players cannot rate or report themselves")
        players = list(
            (
                await session.execute(
                    select(PlayerRecord)
                    .where(PlayerRecord.id.in_((first_id, second_id)))
                    .order_by(PlayerRecord.id)
                    .with_for_update()
                )
            ).scalars()
        )
        by_id = {player.id: player for player in players}
        if first_id not in by_id or second_id not in by_id:
            raise LookupError("Player not found")
        return by_id[first_id], by_id[second_id]

    @staticmethod
    async def _require_finished_game_participants(
        session: AsyncSession,
        game_id: UUID,
        first_id: UUID,
        second_id: UUID,
        *,
        expected_game_version: int | None = None,
    ) -> None:
        game = await session.get(GameRecord, game_id, with_for_update=True)
        if game is None:
            raise LookupError("Game not found")
        if expected_game_version is not None and game.version != expected_game_version:
            raise StaleWriteError("Game has changed")
        if game.status not in {"completed", "finalized"}:
            raise ValueError("Players can only be rated or reported after the game")
        count = await session.scalar(
            select(func.count())
            .select_from(GameParticipantRecord)
            .where(
                GameParticipantRecord.game_id == game_id,
                GameParticipantRecord.player_id.in_((first_id, second_id)),
            )
        )
        if count != 2:
            raise PermissionError("Both players must have played in this game")
