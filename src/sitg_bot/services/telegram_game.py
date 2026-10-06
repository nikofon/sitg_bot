"""Authorized, privacy-safe gameplay projection shared by requests and delivery."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select

from sitg_bot.application.contracts import GameActOperation
from sitg_bot.domain.game_action_reasons import game_action_reason
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.trust import TrustService
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    AppealRecord,
    AppealVoteRecord,
    AuthorRecord,
    GameEventRecord,
    GameObserverRecord,
    GamePacketVersionRecord,
    GameParticipantRecord,
    GameRecord,
    PacketVersionRecord,
    PlayerRecord,
    PlayerReportRecord,
    QuestionRevisionRecord,
    QuestionRoundRecord,
    ReputationVoteRecord,
    ScoreLedgerRecord,
    TelegramGameViewRecord,
    ThemeRevisionRecord,
    TournamentRecord,
)


class _BoundDatabase:
    """Reuse the outer locked transaction for existing domain service operations."""

    def __init__(self, session):
        self.session = session

    @asynccontextmanager
    async def transaction(self):
        yield self.session

    sessions = transaction


class TelegramGameService:
    def __init__(self, database):
        self.database = database

    async def view(
        self, telegram_user_id: int, game_id: UUID | None = None, *, reconnect: bool = False
    ) -> dict:
        async with self.database.transaction() as session:
            if reconnect and game_id is None:
                game_id = await session.scalar(
                    select(GameParticipantRecord.game_id)
                    .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                    .where(PlayerRecord.telegram_user_id == telegram_user_id)
                    .order_by(GameParticipantRecord.global_game_sequence.desc())
                    .limit(1)
                )
            player, game, member, observer = await self._context(session, telegram_user_id, game_id)
            if reconnect and (
                game.status != "active"
                or member is None
                or member.active
                or member.abandoned_at is None
                or await PersistentGameService._has_later_game(session, member)
            ):
                raise LookupError("No game available for reconnection")
            return await self._view(session, player, game, member, observer)

    async def observe(
        self, telegram_user_id: int, game_id: UUID, *, confirm_fresh: bool = False
    ) -> dict:
        """Join an ongoing game as an observer and schedule a full Telegram replay."""
        async with self.database.transaction() as session:
            player = await session.scalar(
                select(PlayerRecord).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            if player is None or player.status != "active":
                raise PermissionError("Active player registration is required")
            game = await session.scalar(
                select(GameRecord).where(GameRecord.id == game_id).with_for_update()
            )
            if game is None:
                raise LookupError("Game not found")
            participant = await session.scalar(
                select(GameParticipantRecord.id).where(
                    GameParticipantRecord.game_id == game.id,
                    GameParticipantRecord.player_id == player.id,
                )
            )
            if participant is not None:
                raise PermissionError("Game participants cannot join their game as observers")
            observer = await session.scalar(
                select(GameObserverRecord)
                .where(
                    GameObserverRecord.game_id == game.id,
                    GameObserverRecord.player_id == player.id,
                )
                .with_for_update()
            )
            already_active = observer is not None and observer.active
            result = await PersistentGameService(_BoundDatabase(session)).observe(
                game.id, telegram_user_id, confirm_fresh=confirm_fresh
            )
            if not result.joined:
                return {
                    "game_id": str(game.id),
                    "joined": False,
                    "confirmation_required": True,
                    "fresh_content_count": result.fresh_content_count,
                }
            cursor = await self._cursor(session, game.id, player.id)
            if not already_active or cursor.dismissed_at is not None:
                cursor.dismissed_at = None
                cursor.connected = False
                cursor.messages = {}
                cursor.flow_sequence = 0
                await TransactionalOutbox.enqueue(
                    session,
                    topic="game.event",
                    deduplication_key=(
                        f"game:{game.id}:observe:{telegram_user_id}:{uuid4()}"
                    ),
                    partition_key=f"telegram:chat:{telegram_user_id}",
                    aggregate_type="game",
                    aggregate_id=game.id,
                    payload={
                        "recipient_telegram_user_id": telegram_user_id,
                        "game_id": str(game.id),
                    },
                )
            await session.flush()
            return {
                "game_id": str(game.id),
                "joined": True,
                "confirmation_required": False,
                "fresh_content_count": result.fresh_content_count,
            }

    async def act(self, telegram_user_id: int, operation: GameActOperation) -> dict:
        if operation.command in {"reputation", "report"}:
            return await self._trust_action(telegram_user_id, operation)
        async with self.database.transaction() as session:
            player, game, member, observer = await self._context(
                session, telegram_user_id, operation.game_id
            )
            view = await self._view(session, player, game, member, observer)
            command = operation.command
            cursor = await self._cursor(session, game.id, player.id)
            reconnecting = (
                command == "join"
                and game.status == "active"
                and member is not None
                and not member.active
            )
            if cursor.dismissed_at and not reconnecting:
                raise PermissionError("Game has been left")
            if command == "quit":
                if game.status in {"active", "lobby"}:
                    raise ValueError("An active game must be abandoned")
                cursor.dismissed_at = datetime.now(UTC)
                cursor.connected = False
                await self._cleanup(session, game.id, telegram_user_id)
                return {"accepted": True, "receipt": None}
            if command not in view["actions"]:
                return {"accepted": False, "reason": game_action_reason(view, command)}
            if (
                command in {"buzz", "answer", "appeal", "pause", "resume"}
                and operation.round_id != game.current_round_id
            ):
                return {"accepted": False, "reason": "question_changed"}
            if command in {"vote", "escalate", "commentary"} and (
                not view["appeal"] or str(operation.appeal_id) != str(view["appeal"]["id"])
            ):
                return {"accepted": False, "reason": "appeal_changed"}
            bound = _BoundDatabase(session)
            games = PersistentGameService(bound)
            accepted = True
            if command in {"join", "buzz", "pause", "resume"}:
                previous_sequence = view["last_sequence"]
                result = await getattr(games, command)(game.id, telegram_user_id)
                accepted = result.accepted
                if accepted and reconnecting:
                    cursor.dismissed_at = None
                    cursor.messages = {}
                    cursor.flow_sequence = previous_sequence
            elif command == "answer":
                if not operation.text or not operation.text.strip():
                    raise ValueError("Answer cannot be empty")
                accepted = (await games.answer(game.id, telegram_user_id, operation.text)).accepted
            elif command == "appeal":
                # Telegram has no persistent per-player socket. Joined, active players
                # are the electorate; domain vote deadlines handle non-responses.
                connected = set(
                    (
                        await session.execute(
                            select(PlayerRecord.telegram_user_id)
                            .join(
                                GameParticipantRecord,
                                GameParticipantRecord.player_id == PlayerRecord.id,
                            )
                            .where(
                                GameParticipantRecord.game_id == game.id,
                                GameParticipantRecord.active.is_(True),
                                GameParticipantRecord.joined.is_(True),
                            )
                        )
                    ).scalars()
                )
                accepted = (await games.submit_appeal(
                    game.id, telegram_user_id, connected, target_attempt_id=operation.target_id
                )).accepted
            elif command in {"vote", "escalate"}:
                if operation.approve is None:
                    raise ValueError("A decision is required")
                method = games.vote_appeal if command == "vote" else games.choose_appeal_escalation
                keyword = "approve" if command == "vote" else "escalate"
                accepted = (
                    await method(
                        game.id,
                        operation.appeal_id,
                        telegram_user_id,
                        **{keyword: operation.approve},
                    )
                ).accepted
            elif command == "commentary":
                accepted = (
                    await games.submit_appeal_commentary(
                        game.id, operation.appeal_id, telegram_user_id, operation.text or ""
                    )
                ).accepted
            elif command == "abandon":
                await games.abandon_player(game.id, telegram_user_id)
            elif command == "observe_leave":
                await games.stop_observing(game.id, telegram_user_id)
            cursor = await self._cursor(session, game.id, player.id)
            cursor.connected = bool(member and member.joined and member.active)
            if command in {"abandon", "observe_leave"}:
                cursor.dismissed_at = datetime.now(UTC)
                await self._cleanup(session, game.id, telegram_user_id)
            await session.flush()
            return {"accepted": accepted, "receipt": None}

    @staticmethod
    async def _cleanup(session, game_id, telegram_user_id):
        player_id = await session.scalar(
            select(PlayerRecord.id).where(PlayerRecord.telegram_user_id == telegram_user_id)
        )
        cursor = await session.get(TelegramGameViewRecord, (game_id, player_id))
        message_ids = {
            mid for entry in (cursor.messages or {}).values() for mid in entry.get("ids", [])
        }
        await TransactionalOutbox.enqueue(
            session,
            topic="telegram.game.cleanup",
            deduplication_key=f"game:{game_id}:cleanup:{telegram_user_id}:{cursor.dismissed_at.isoformat()}",
            partition_key=f"telegram:chat:{telegram_user_id}",
            aggregate_type="game",
            aggregate_id=game_id,
            payload={
                "game_id": str(game_id),
                "recipient_telegram_user_id": telegram_user_id,
                "message_ids": sorted(message_ids),
            },
        )

    async def _trust_action(self, telegram_user_id, operation):
        async with self.database.transaction() as session:
            player, game, member, _ = await self._context(
                session, telegram_user_id, operation.game_id
            )
            if member is None or game.status not in {"completed", "finalized"}:
                raise PermissionError("A completed shared game is required")
            cursor = await self._cursor(session, game.id, player.id)
            if cursor.dismissed_at:
                raise PermissionError("Game has been left")
            target = await session.scalar(
                select(PlayerRecord)
                .join(GameParticipantRecord, GameParticipantRecord.player_id == PlayerRecord.id)
                .where(
                    GameParticipantRecord.game_id == game.id, PlayerRecord.id == operation.target_id
                )
            )
            if target is None or target.id == player.id or target.telegram_user_id is None:
                raise PermissionError("Invalid game participant")
        # Trust owns its player-before-game lock order and revalidates membership,
        # completion, version, duplicate reports, and the global voting cooldown.
        trust = TrustService(self.database)
        if operation.command == "reputation":
            if operation.approve is None:
                raise ValueError("A vote is required")
            receipt = await trust.vote_reputation(
                game.id,
                player.id,
                target.telegram_user_id,
                1 if operation.approve else -1,
                expected_game_version=game.version,
            )
            return {"accepted": True, "receipt": {"vote_id": receipt.vote_id}}
        if operation.report_kind is None:
            raise ValueError("A report kind is required")
        receipt = await trust.report_player(
            game.id,
            player.id,
            target.telegram_user_id,
            operation.report_kind,
            details=operation.text,
            expected_game_version=game.version,
        )
        return {"accepted": True, "receipt": {"report_id": receipt.report_id}}

    async def delivery(self, telegram_user_id: int, game_id: UUID) -> dict:
        async with self.database.transaction() as session:
            player, game, member, observer = await self._context(session, telegram_user_id, game_id)
            cursor = await self._cursor(session, game.id, player.id)
            if cursor.dismissed_at or (
                member and not member.active and game.status in {"lobby", "active"}
            ):
                return {"skip": True}
            events = (
                (
                    await session.execute(
                        select(GameEventRecord)
                        .where(
                            GameEventRecord.game_id == game.id,
                            GameEventRecord.sequence > cursor.flow_sequence,
                        )
                        .order_by(GameEventRecord.sequence)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            frames = []
            for event in events:
                params = dict(event.payload)
                round_id = params.get("round_id")
                if round_id and event.kind in {
                    "question_cost_announced",
                    "question_completed",
                    "player_buzzed",
                }:
                    current = await session.get(QuestionRoundRecord, UUID(str(round_id)))
                    revision = await session.get(
                        QuestionRevisionRecord, current.question_revision_id
                    )
                    placement = await PersistentGameService(_BoundDatabase(session))._placement(
                        session, game, current
                    )
                    theme = await session.get(ThemeRevisionRecord, placement.theme_revision_id)
                    params["theme"] = theme.name
                    if event.kind == "question_completed":
                        params.update(
                            text=revision.text,
                            answer=revision.answer,
                            commentary=revision.commentary or "",
                            author=await session.scalar(
                                select(AuthorRecord.display_name).where(
                                    AuthorRecord.id == revision.author_id
                                )
                            )
                            or "—",
                        )
                    if event.kind == "player_buzzed":
                        participant = await session.get(
                            GameParticipantRecord, UUID(params["participant_id"])
                        )
                        params["mine"] = participant.player_id == player.id
                        public = await session.get(PlayerRecord, participant.player_id)
                        params["name"] = public.public_nickname
                        params["form"] = revision.form if params["mine"] else None
                if event.kind == "answer_judged":
                    attempt = await session.get(AnswerAttemptRecord, UUID(params["attempt_id"]))
                    participant = await session.get(GameParticipantRecord, attempt.participant_id)
                    public = await session.get(PlayerRecord, participant.player_id)
                    params.update(
                        name=public.public_nickname,
                        answer=attempt.submitted_answer,
                        mine=public.id == player.id,
                    )
                if event.kind in {"player_reconnected", "player_abandoned"} or (
                    event.kind == "game_cancelled"
                    and params.get("reason") == "player_abandoned_before_theme_reveal"
                ):
                    participant = await session.get(
                        GameParticipantRecord, UUID(params["participant_id"])
                    )
                    params["mine"] = participant.player_id == player.id
                    public = await session.get(PlayerRecord, participant.player_id)
                    params["name"] = public.public_nickname
                if event.kind in {
                    "appeal_voting_started",
                    "appeal_vote_cast",
                    "appeal_resolved",
                    "appeal_vote_rejected",
                    "appeal_commentary_requested",
                    "appeal_escalated",
                }:
                    appeal = await session.get(AppealRecord, UUID(params["appeal_id"]))
                    approvals, rejections = await PersistentGameService._vote_tally(
                        session, appeal.id
                    )
                    vote = (
                        (await session.get(AppealVoteRecord, (appeal.id, member.id)))
                        if member
                        else None
                    )
                    attempt = await session.get(AnswerAttemptRecord, appeal.target_attempt_id)
                    params["ballot"] = {
                        "id": str(appeal.id),
                        "kind": appeal.kind,
                        "submitted_answer": attempt.submitted_answer,
                        "status": appeal.status,
                        "approvals": approvals,
                        "rejections": rejections,
                        "electorate_size": appeal.electorate_size,
                        "can_vote": bool(
                            vote is not None
                            and vote.approve is None
                            and member.active
                            and appeal.status == "voting"
                        ),
                    }
                if event.kind == "scoreboard":
                    identities = dict((await session.execute(
                        select(PlayerRecord.telegram_user_id, PlayerRecord.id).where(
                            PlayerRecord.telegram_user_id.in_([
                                p["telegram_user_id"] for p in params.get("players", ())
                                if p.get("telegram_user_id") is not None
                            ])
                        )
                    )).all())
                    params = {
                        "players": [
                            {
                                "name": p["display_name"], "score": p["score"],
                                "player_id": str(identities[p["telegram_user_id"]])
                                if p.get("telegram_user_id") in identities else None,
                            }
                            for p in params.get("players", ())
                        ]
                    }
                frames.append(
                    {"sequence": event.sequence, "kind": event.kind, "parameters": params}
                )
            return {
                "skip": False,
                "view": await self._view(session, player, game, member, observer),
                "events": frames,
                "sequence": cursor.flow_sequence,
                "messages": cursor.messages or {},
            }

    async def presentation(self, telegram_user_id, game_id):
        async with self.database.transaction() as session:
            player_id = await session.scalar(
                select(PlayerRecord.id).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            cursor = await session.get(TelegramGameViewRecord, (game_id, player_id))
            if cursor is None:
                return {"messages": {}, "dismissed": False}
            messages = dict(cursor.messages or {})
            return {"messages": messages, "dismissed": cursor.dismissed_at is not None}

    async def record_delivery(
        self, telegram_user_id, game_id, *, sequence=None, key=None, value=None
    ):
        async with self.database.transaction() as session:
            player_id = await session.scalar(
                select(PlayerRecord.id).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            game = await session.get(GameRecord, game_id, with_for_update=True)
            if game is None or player_id is None:
                raise LookupError("Game or player not found")
            member = await session.scalar(
                select(GameParticipantRecord.id).where(
                    GameParticipantRecord.game_id == game_id,
                    GameParticipantRecord.player_id == player_id,
                )
            )
            observer = await session.scalar(
                select(GameObserverRecord.game_id).where(
                    GameObserverRecord.game_id == game_id, GameObserverRecord.player_id == player_id
                )
            )
            if member is None and observer is None:
                raise PermissionError("Game membership required")
            cursor = await self._cursor(session, game_id, player_id)
            if key is not None:
                cursor.messages = {**(cursor.messages or {}), key: value}
            if sequence is not None:
                cursor.flow_sequence = max(cursor.flow_sequence, sequence)

    async def disconnect(self, telegram_user_id: int, game_id: UUID):
        async with self.database.transaction() as session:
            player_id = await session.scalar(
                select(PlayerRecord.id).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            cursor = await session.get(TelegramGameViewRecord, (game_id, player_id))
            if cursor is not None:
                cursor.connected = False

    @staticmethod
    async def _cursor(session, game_id, player_id):
        cursor = await session.get(TelegramGameViewRecord, (game_id, player_id))
        if cursor is None:
            cursor = TelegramGameViewRecord(
                game_id=game_id,
                player_id=player_id,
                flow_sequence=0,
                messages={},
            )
            session.add(cursor)
            await session.flush()
        return cursor

    @staticmethod
    async def _context(session, telegram_user_id, game_id):
        player = await session.scalar(
            select(PlayerRecord).where(
                PlayerRecord.telegram_user_id == telegram_user_id, PlayerRecord.status == "active"
            )
        )
        if player is None:
            raise PermissionError("Active player required")
        if game_id is None:
            game_id = await session.scalar(
                select(GameObserverRecord.game_id)
                .join(GameRecord, GameRecord.id == GameObserverRecord.game_id)
                .outerjoin(
                    TelegramGameViewRecord,
                    (TelegramGameViewRecord.game_id == GameRecord.id)
                    & (TelegramGameViewRecord.player_id == player.id),
                )
                .where(
                    GameObserverRecord.player_id == player.id,
                    GameObserverRecord.active.is_(True),
                    TelegramGameViewRecord.dismissed_at.is_(None),
                )
                .order_by(GameObserverRecord.joined_at.desc())
                .limit(1)
            )
        if game_id is None:
            game_id = await session.scalar(
                select(GameParticipantRecord.game_id)
                .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                .outerjoin(
                    TelegramGameViewRecord,
                    (TelegramGameViewRecord.game_id == GameRecord.id)
                    & (TelegramGameViewRecord.player_id == player.id),
                )
                .where(
                    GameParticipantRecord.player_id == player.id,
                    GameParticipantRecord.global_game_sequence
                    == select(func.max(GameParticipantRecord.global_game_sequence))
                    .where(GameParticipantRecord.player_id == player.id)
                    .correlate(None)
                    .scalar_subquery(),
                    TelegramGameViewRecord.dismissed_at.is_(None),
                    or_(
                        GameParticipantRecord.active.is_(True),
                        (
                            GameRecord.status.in_(
                                (
                                    "completed",
                                    "finalized",
                                    "cancelled",
                                    "failed_to_start",
                                    "abandoned",
                                )
                            )
                        )
                        & TelegramGameViewRecord.player_id.is_not(None),
                    ),
                )
                .order_by(GameParticipantRecord.global_game_sequence.desc())
                .limit(1)
            )
        game = await session.scalar(
            select(GameRecord).where(GameRecord.id == game_id).with_for_update()
        )
        if game is None:
            raise LookupError("Game not found")
        member = await session.scalar(
            select(GameParticipantRecord).where(
                GameParticipantRecord.game_id == game.id,
                GameParticipantRecord.player_id == player.id,
            )
        )
        observer = await session.scalar(
            select(GameObserverRecord).where(
                GameObserverRecord.game_id == game.id,
                GameObserverRecord.player_id == player.id,
                GameObserverRecord.active.is_(True),
            )
        )
        if member is None and observer is None:
            raise PermissionError("Game membership required")
        if (
            member
            and not member.active
            and game.status == "active"
            and await PersistentGameService._has_later_game(session, member)
        ):
            raise PermissionError("Access to this abandoned game was forfeited")
        return player, game, member, observer

    @staticmethod
    async def _correct_points(session, participant_id):
        # A corrected -10 +20 means +10, rather than twenty positive points.
        totals = (
            select(func.sum(ScoreLedgerRecord.delta).label("score"))
            .where(ScoreLedgerRecord.participant_id == participant_id)
            .group_by(ScoreLedgerRecord.round_id)
            .subquery()
        )
        return await session.scalar(
            select(func.coalesce(func.sum(totals.c.score), 0)).where(totals.c.score > 0)
        )

    async def _view(self, session, player, game, member, observer):
        games = PersistentGameService(_BoundDatabase(session))
        snapshot = await games._snapshot(session, game)
        rows = (
            await session.execute(
                select(GameParticipantRecord, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                .where(GameParticipantRecord.game_id == game.id)
                .order_by(GameParticipantRecord.seat)
            )
        ).all()
        identities = {
            p.telegram_user_id: str(p.id) for _, p in rows if p.telegram_user_id is not None
        }
        participants = []
        for (_participant, public), item in zip(rows, snapshot.participants, strict=True):
            vote = (
                await session.scalar(
                    select(ReputationVoteRecord)
                    .where(
                        ReputationVoteRecord.voter_player_id == player.id,
                        ReputationVoteRecord.target_player_id == public.id,
                    )
                    .order_by(ReputationVoteRecord.created_at.desc())
                    .limit(1)
                )
                if game.status in {"completed", "finalized"}
                else None
            )
            vote_reason = None
            if vote and vote.game_id == game.id:
                vote_reason = "voted"
            elif vote and vote.created_at + timedelta(days=7) > datetime.now(UTC):
                vote_reason = "cooldown"
            reported = (
                tuple(
                    (
                        await session.execute(
                            select(PlayerReportRecord.kind).where(
                                PlayerReportRecord.game_id == game.id,
                                PlayerReportRecord.reporter_player_id == player.id,
                                PlayerReportRecord.reported_player_id == public.id,
                            )
                        )
                    ).scalars()
                )
                if game.status in {"completed", "finalized"}
                else ()
            )
            participants.append(
                {
                    "id": str(public.id),
                    "name": public.public_nickname,
                    "joined": item.joined,
                    "active": item.active,
                    "is_chair": _participant.is_chair,
                    "score": item.score,
                    "correct_points": await self._correct_points(session, _participant.id),
                    "place": item.place,
                    "self": public.id == player.id,
                    "vote_reason": vote_reason,
                    "reported": reported,
                    "telegram_username": public.telegram_username
                    if (
                        game.status in {"completed", "finalized"}
                        and member is not None
                        and public.telegram_public
                    )
                    else None,
                }
            )
        question = asdict(snapshot.question) if snapshot.question else None
        if question:
            question["author"] = None
            if question["revealed_answer"] is not None:
                current = await session.get(QuestionRoundRecord, snapshot.question.round_id)
                revision = await session.get(QuestionRevisionRecord, current.question_revision_id)
                question["author"] = (
                    await session.scalar(
                        select(AuthorRecord.display_name).where(
                            AuthorRecord.id == revision.author_id
                        )
                    )
                    or "—"
                )
            for key in ("attempted_player_ids", "eligible_player_ids"):
                question[key] = [identities.get(value) for value in question[key]]
            question["accepted_buzzer_id"] = identities.get(question["accepted_buzzer_id"])
            if (
                question["accepted_buzzer_id"] != str(player.id)
                and question["revealed_answer"] is None
            ):
                question["form"] = None
            for attempt in question["attempts"]:
                attempt["player_id"] = identities.get(attempt["player_id"])
        appeal = asdict(snapshot.appeal) if snapshot.appeal else None
        if appeal:
            appeal["target_player_id"] = identities.get(appeal["target_player_id"])
            appeal.pop("appellant_participant_id")
        actions = ["view", "score", "players"]
        appeal_targets = []
        if member and (
            not member.joined
            and game.status == "lobby"
            or game.status == "active"
            and not member.active
            and snapshot.participants[
                next(i for i, (m, _) in enumerate(rows) if m.id == member.id)
            ].can_reconnect
        ):
            actions.append("join")
        if observer and not member:
            actions.append("observe_leave")
        if member and member.active and game.status in {"active", "lobby"}:
            actions.append("abandon")
        if (
            member
            and member.active
            and member.joined
            and game.status == "active"
            and snapshot.game_ruleset == "si"
            and snapshot.game_ruleset_version == 1
        ):
            if question and game.phase == "question" and not snapshot.paused:
                if question["accepted_buzzer_id"] == str(player.id):
                    actions.append("answer")
                elif (
                    question["accepted_buzzer_id"] is None
                    and question["revealed_token_count"] > 0
                    and str(player.id) in question["eligible_player_ids"]
                    and str(player.id) not in question["attempted_player_ids"]
                ):
                    actions.append("buzz")
            blocking = game.appeal_selection_player_id is not None or (
                appeal and appeal["status"] in {
                    "voting", "awaiting_escalation", "awaiting_commentary",
                }
            )
            if snapshot.paused and not blocking:
                actions.append("resume")
            elif (
                game.phase == "intermission" and snapshot.settings.pausing_allowed and not blocking
            ):
                actions.append("pause")
            if (
                question
                and game.phase == "intermission"
                and game.progression_stage in {"next_question", "theme_complete"}
            ):
                existing = await session.scalar(
                    select(AppealRecord.id).where(
                        AppealRecord.game_id == game.id,
                        AppealRecord.round_id == game.current_round_id,
                        AppealRecord.status != "rejected",
                    )
                )
                if existing is None and (
                    game.appeal_selection_deadline is None
                    or game.appeal_selection_player_id == player.id
                ):
                    appealed_attempts = {
                        str(attempt_id) for attempt_id in await session.scalars(
                            select(AppealRecord.target_attempt_id).where(
                                AppealRecord.game_id == game.id,
                                AppealRecord.round_id == game.current_round_id,
                            )
                        )
                    }
                    appeal_targets = [
                        a
                        for a in question["attempts"]
                        if str(a["id"]) not in appealed_attempts
                        and (
                            a["original_correct"]
                            or (a["player_id"] == str(player.id) and not a["timed_out"])
                        )
                    ]
                    if appeal_targets:
                        actions.append("appeal")
            if appeal:
                record = await session.get(AppealRecord, snapshot.appeal.id)
                vote = await session.get(AppealVoteRecord, (record.id, member.id))
                if record.status == "voting" and vote is not None and vote.approve is None:
                    actions.append("vote")
                if record.appellant_participant_id == member.id:
                    if record.status == "awaiting_escalation":
                        actions.append("escalate")
                    elif record.status == "awaiting_commentary":
                        actions.append("commentary")
        if member and game.status in {"completed", "finalized"}:
            actions.extend(("reputation", "report"))
        last_sequence = await session.scalar(
            select(GameEventRecord.sequence)
            .where(GameEventRecord.game_id == game.id)
            .order_by(GameEventRecord.sequence.desc())
            .limit(1)
        )
        theme = await session.scalar(
            select(GameEventRecord.payload)
            .where(GameEventRecord.game_id == game.id, GameEventRecord.kind == "theme_started")
            .order_by(GameEventRecord.sequence.desc())
            .limit(1)
        )
        announcement = await session.scalar(
            select(GameEventRecord.payload)
            .where(GameEventRecord.game_id == game.id, GameEventRecord.kind == "themes_announced")
            .order_by(GameEventRecord.sequence)
            .limit(1)
        )
        themes = announcement.get("themes", []) if announcement else []
        if themes and game.status == "active":
            actions.append("themes")
        settlement = None
        if game.status == "finalized":
            settlement = await session.scalar(
                select(GameEventRecord.payload)
                .where(
                    GameEventRecord.game_id == game.id,
                    GameEventRecord.kind == "game_finalized",
                )
                .order_by(GameEventRecord.sequence.desc())
                .limit(1)
            )
        cursor = await session.get(TelegramGameViewRecord, (game.id, player.id))
        messages = cursor.messages if cursor else {}
        tournament_name = await session.scalar(
            select(TournamentRecord.name).where(TournamentRecord.id == game.tournament_id)
        )
        packet_names = list(
            await session.scalars(
                select(PacketVersionRecord.name)
                .join(
                    GamePacketVersionRecord,
                    GamePacketVersionRecord.packet_version_id == PacketVersionRecord.id,
                )
                .where(GamePacketVersionRecord.game_id == game.id)
                .order_by(GamePacketVersionRecord.selection_order)
            )
        )
        return {
            "messages": messages or {},
            "dismissed": bool(cursor and cursor.dismissed_at),
            "id": str(game.id),
            "version": game.version,
            "status": game.status,
            "phase": game.phase,
            "paused": game.paused,
            "appeal_selecting": game.appeal_selection_player_id is not None,
            "pausing_allowed": snapshot.settings.pausing_allowed,
            "ruleset": snapshot.game_ruleset,
            "ruleset_version": snapshot.game_ruleset_version,
            "locale": player.preferred_locale,
            "viewer_id": str(player.id),
            "tournament_name": tournament_name,
            "packet_names": packet_names,
            "participants": participants,
            "question": question,
            "appeal": appeal,
            "observers": [{"name": o.display_name, "active": o.active} for o in snapshot.observers],
            "appeal_targets": appeal_targets,
            "actions": actions,
            "theme": theme,
            "themes": themes,
            "join_deadline": snapshot.join_deadline,
            "answer_deadline": snapshot.answer_deadline,
            "buzz_deadline": snapshot.buzz_deadline,
            "progression_deadline": snapshot.progression_deadline,
            "message_delay": snapshot.settings.message_delay,
            "rating_pending": snapshot.rating_pending,
            "rating_changes": [
                {key: change[key] for key in ("player_id", "scope", "before", "after", "delta")}
                for change in (settlement or {}).get("rating_changes", [])
            ],
            "last_sequence": last_sequence or 0,
        }
