from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sitg_bot.domain.appeals import AppealPolicy
from sitg_bot.domain.game_deadlines import (
    INITIAL_PLAYER_JOIN_TIMEOUT,
    PAUSED_GAME_ABANDONMENT_TIMEOUT,
    REMAINING_PLAYERS_JOIN_TIMEOUT,
)
from sitg_bot.domain.game_rulesets import (
    DEFAULT_RULESETS,
    ExposureClaim,
    GameRulesetRegistry,
    PlayUnit,
)
from sitg_bot.domain.game_settings import GameSettings, announcement_tokens
from sitg_bot.domain.packet import Question
from sitg_bot.domain.rating import (
    DEFAULT_CONFIDENCE_MODEL_KEY,
    STARTING_RATING,
    PairwiseRatingInput,
    RatingHistoryEntry,
    confidence_model,
    pairwise_rating_deltas,
)
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
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
    GameResultRecord,
    GameRulesetVersionRecord,
    GameThemeRecord,
    LogicalPacketRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    PlayerQuestionStateRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
    QuestionRevisionRecord,
    QuestionRoundRecord,
    RatingLedgerRecord,
    RulesetRatingLedgerRecord,
    RulesetRatingRecord,
    ScoreLedgerRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentTypeVersionRecord,
)


@dataclass(frozen=True, slots=True)
class ParticipantInput:
    telegram_user_id: int
    public_nickname: str


@dataclass(frozen=True, slots=True)
class ParticipantSnapshot:
    telegram_user_id: int | None
    display_name: str
    joined: bool
    active: bool
    abandoned_at: datetime | None
    can_reconnect: bool
    score: Decimal | int
    place: Decimal | None
    rating: Decimal


@dataclass(frozen=True, slots=True)
class ObserverSnapshot:
    telegram_user_id: int | None
    display_name: str
    active: bool
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class AnswerAttemptSnapshot:
    id: UUID
    attempt_number: int
    player_id: int
    submitted_answer: str | None
    timed_out: bool
    original_correct: bool
    final_correct: bool


@dataclass(frozen=True, slots=True)
class QuestionSnapshot:
    round_id: UUID
    sequence: int
    value: int
    text: str
    form: str
    attempted_player_ids: tuple[int, ...]
    eligible_player_ids: tuple[int, ...]
    accepted_buzzer_id: int | None
    revealed_answer: str | None
    commentary: str | None
    revealed_token_count: int
    total_token_count: int
    fully_announced: bool
    attempts: tuple[AnswerAttemptSnapshot, ...]


@dataclass(frozen=True, slots=True)
class AppealSnapshot:
    id: UUID
    round_id: UUID
    appellant_participant_id: UUID
    target_attempt_id: UUID
    target_player_id: int
    submitted_answer: str
    kind: str
    status: str
    voting_rule: str
    electorate_size: int
    approvals: int
    rejections: int
    vote_deadline: datetime
    escalation_enabled: bool
    escalation_deadline: datetime | None
    commentary_deadline: datetime | None
    ticket_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ManagerAppealTicket:
    id: UUID
    question_sequence: int
    question_text: str
    official_answer: str
    kind: str
    submitted_answer: str
    commentary: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    id: UUID
    version: int
    status: str
    phase: str
    paused: bool
    packet_version_ids: tuple[UUID, ...]
    tournament_id: UUID
    game_ruleset: str
    game_ruleset_version: int
    rating_confidence_model: str
    rating_pending: bool
    participants: tuple[ParticipantSnapshot, ...]
    observers: tuple[ObserverSnapshot, ...]
    question: QuestionSnapshot | None
    appeal: AppealSnapshot | None
    buzz_deadline: datetime | None
    answer_deadline: datetime | None
    progression_deadline: datetime | None
    progression_stage: str | None
    join_deadline: datetime | None
    pause_abandonment_deadline: datetime | None
    settings: GameSettings


@dataclass(frozen=True, slots=True)
class Transition:
    accepted: bool
    events: tuple[dict[str, Any], ...]
    snapshot: GameSnapshot


@dataclass(frozen=True, slots=True)
class ObservationResult:
    joined: bool
    confirmation_required: bool
    fresh_content_count: int
    events: tuple[dict[str, Any], ...]
    snapshot: GameSnapshot | None


class PersistentGameService:
    """Applies one locked, durable state transition per gameplay command."""

    def __init__(
        self,
        database: Database,
        *,
        rating_confidence_model: str = DEFAULT_CONFIDENCE_MODEL_KEY,
        rulesets: GameRulesetRegistry = DEFAULT_RULESETS,
    ) -> None:
        self.database = database
        self.rating_confidence = confidence_model(rating_confidence_model)
        self.rulesets = rulesets
        self.tournaments = TournamentService(database, rulesets=rulesets)

    async def create_game(
        self,
        packet_version_id: UUID,
        participants: list[ParticipantInput],
        *,
        tournament_id: UUID,
        parameter_overrides: dict[str, object] | None = None,
    ) -> GameSnapshot:
        if not 1 <= len(participants) <= 12:
            raise ValueError("A game supports between one and twelve participants")
        telegram_ids = [participant.telegram_user_id for participant in participants]
        if len(telegram_ids) != len(set(telegram_ids)):
            raise ValueError("Participant IDs must be unique")
        async with self.database.transaction() as session:
            context = await self.tournaments.context(session, tournament_id)
            if not context.assembly_open:
                raise ValueError("tournament_stage_closed")
            minimum = int(context.type_rules.get("minimum_players", 1))
            maximum = min(12, int(context.type_rules.get("maximum_players", 12)))
            if not minimum <= len(participants) <= maximum:
                raise ValueError("tournament_capacity_restriction")
            minimum_packets = int(context.type_rules.get("minimum_packets", 1))
            maximum_packets = context.type_rules.get("maximum_packets")
            if minimum_packets > 1 or (maximum_packets is not None and int(maximum_packets) < 1):
                raise ValueError("tournament_packet_limit_exceeded")
            ruleset = self.rulesets.get(context.ruleset_key, context.ruleset_version)
            locked = set(parameter_overrides or ()) - context.mutable_parameters
            if locked:
                raise PermissionError(f"Tournament locks parameter: {sorted(locked)[0]}")
            settings = context.settings.updated(parameter_overrides or {})
            version = await session.get(PacketVersionRecord, packet_version_id)
            if version is None or version.state != "published":
                raise LookupError("Published packet version not found")
            packet = await session.get(LogicalPacketRecord, version.packet_id)
            assert packet is not None
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == context.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == packet.id,
                    TournamentPacketAssignmentRecord.status == "active",
                )
            )
            if assignment is None:
                raise ValueError("Packet is not assigned to this tournament")
            if assignment.adopted_version_id not in (None, version.id):
                raise ValueError("Packet version is not adopted by this tournament")
            players = [await self._registered_player(session, item) for item in participants]
            for player in players:
                membership = await session.get(
                    TournamentMembershipRecord, (context.tournament_id, player.id)
                )
                if membership is None or membership.status != "active":
                    raise PermissionError("Active tournament membership is required")
                if not await self.tournaments.has_assignment_access(
                    session, assignment, player.id, "playable"
                ):
                    raise PermissionError("Packet game eligibility is required")
                active_game = await session.scalar(
                    select(GameParticipantRecord.id)
                    .where(
                        GameParticipantRecord.player_id == player.id,
                        GameParticipantRecord.active.is_(True),
                    )
                    .limit(1)
                )
                if active_game is not None:
                    raise ValueError("Player already belongs to an active game")
                active_lobby = await session.scalar(
                    select(PregameLobbyMemberRecord.id)
                    .where(
                        PregameLobbyMemberRecord.player_id == player.id,
                        PregameLobbyMemberRecord.active.is_(True),
                    )
                    .limit(1)
                )
                if active_lobby is not None:
                    raise ValueError("Player already belongs to an active lobby")
            themes = list(
                (
                    await session.execute(
                        select(ThemeRevisionRecord)
                        .where(ThemeRevisionRecord.packet_version_id == version.id)
                        .order_by(ThemeRevisionRecord.position)
                    )
                ).scalars()
            )
            play_units: list[PlayUnit] = []
            for theme in themes:
                question_rows = list(
                    (
                        await session.execute(
                            select(PacketQuestionRecord, QuestionRevisionRecord.question_id)
                            .join(
                                QuestionRevisionRecord,
                                QuestionRevisionRecord.id
                                == PacketQuestionRecord.question_revision_id,
                            )
                            .where(
                                PacketQuestionRecord.packet_version_id == version.id,
                                PacketQuestionRecord.theme_revision_id == theme.id,
                            )
                            .order_by(PacketQuestionRecord.position)
                        )
                    ).all()
                )
                values = tuple(placement.value for placement, _ in question_rows)
                if values != settings.question_values:
                    raise ValueError(
                        "Packet content does not match the tournament SI question_values"
                    )
                claims = (ExposureClaim("theme", theme.theme_id),) + tuple(
                    ExposureClaim("question", question_id) for _, question_id in question_rows
                )
                conflict = await session.scalar(
                    select(PlayerExposureClaimRecord.id)
                    .where(
                        PlayerExposureClaimRecord.player_id.in_([player.id for player in players]),
                        PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
                        or_(
                            *[
                                (PlayerExposureClaimRecord.claim_namespace == claim.namespace)
                                & (PlayerExposureClaimRecord.claim_id == claim.identity)
                                for claim in claims
                            ]
                        ),
                    )
                    .limit(1)
                )
                if conflict is None:
                    play_units.append(
                        PlayUnit(
                            kind="si_theme",
                            logical_id=theme.theme_id,
                            revision_id=theme.id,
                            packet_version_id=version.id,
                            packet_order=1,
                            position=theme.position,
                            question_revision_ids=tuple(
                                placement.question_revision_id for placement, _ in question_rows
                            ),
                            claims=claims,
                            metadata={"question_values": list(values)},
                        )
                    )
            plan = ruleset.prepare_assignment(
                player_count=len(players),
                packet_version_ids=(version.id,),
                available_play_units=play_units,
                parameters=settings,
            )
            now = datetime.now(UTC)
            game = GameRecord(
                tournament_id=context.tournament_id,
                tournament_type_version_id=context.type_version_id,
                game_ruleset_version_id=context.game_ruleset_version_id,
                tournament_policy_version_id=context.policy_version_id,
                rating_confidence_model=self.rating_confidence.key,
                host_player_id=players[0].id,
                assignment_plan=plan.to_dict(),
                join_deadline=now + INITIAL_PLAYER_JOIN_TIMEOUT,
            )
            session.add(game)
            await session.flush()
            session.add(
                GamePacketVersionRecord(
                    game_id=game.id,
                    packet_version_id=version.id,
                    assignment_id=assignment.id,
                    selection_order=1,
                )
            )
            for position, play_unit in enumerate(plan.play_units, 1):
                session.add(
                    GameThemeRecord(
                        game_id=game.id,
                        theme_revision_id=play_unit.revision_id,
                        source_packet_version_id=version.id,
                        position=position,
                    )
                )
            for seat, player in enumerate(players, 1):
                membership = await session.get(
                    TournamentMembershipRecord, (context.tournament_id, player.id)
                )
                assert membership is not None
                membership.rating_sequence += 1
                player.game_sequence += 1
                participant = GameParticipantRecord(
                    tournament_id=context.tournament_id,
                    game_id=game.id,
                    player_id=player.id,
                    seat=seat,
                    rating_sequence=membership.rating_sequence,
                    global_game_sequence=player.game_sequence,
                )
                session.add(participant)
                seen_claims: set[tuple[str, UUID]] = set()
                for play_unit in plan.play_units:
                    for claim in play_unit.claims:
                        identity = (claim.namespace, claim.identity)
                        if identity in seen_claims:
                            continue
                        seen_claims.add(identity)
                        session.add(
                            PlayerExposureClaimRecord(
                                game_id=game.id,
                                player_id=player.id,
                                packet_version_id=play_unit.packet_version_id,
                                claim_namespace=claim.namespace,
                                claim_id=claim.identity,
                            )
                        )
            sequence = 0
            for play_unit in plan.play_units:
                for question_revision_id in play_unit.question_revision_ids:
                    sequence += 1
                    session.add(
                        QuestionRoundRecord(
                            game_id=game.id,
                            question_revision_id=question_revision_id,
                            sequence=sequence,
                        )
                    )
            await self._event(
                session,
                game.id,
                "game_created",
                {
                    "packet_version_ids": [str(version.id)],
                    "play_unit_count": len(plan.play_units),
                    "assignment_seed": plan.seed,
                    "tournament_id": str(context.tournament_id),
                    "game_ruleset": ruleset.key,
                    "game_ruleset_version": ruleset.version,
                    "players": [str(player.id) for player in players],
                    "join_deadline": game.join_deadline,
                    "join_deadline_stage": "first_player",
                },
            )
            await session.flush()
            return await self._snapshot(session, game)

    async def observable_games(
        self, tournament_id: UUID, player_id: UUID
    ) -> tuple[dict[str, Any], ...]:
        async with self.database.sessions() as session:
            manages = await session.scalar(
                select(TournamentManagerRecord.player_id).where(
                    TournamentManagerRecord.tournament_id == tournament_id,
                    TournamentManagerRecord.player_id == player_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
            )
            membership = await session.get(
                TournamentMembershipRecord, (tournament_id, player_id)
            )
            if manages is None and (membership is None or membership.status != "active"):
                raise PermissionError("Active tournament membership is required")
            games = tuple(
                (
                    await session.execute(
                        select(GameRecord)
                        .where(
                            GameRecord.tournament_id == tournament_id,
                            GameRecord.status.in_(("lobby", "active")),
                        )
                        .order_by(GameRecord.created_at)
                    )
                ).scalars()
            )
            result: list[dict[str, Any]] = []
            for game in games:
                card = await self._observable_card(session, game, player_id)
                if card is not None:
                    result.append(card)
            return tuple(result)

    async def ongoing_games(self, player_id: UUID) -> tuple[dict[str, Any], ...]:
        """Project observable games from tournaments the player joins or manages."""
        async with self.database.sessions() as session:
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise PermissionError("Active player registration is required")
            tournament_names, _ = await self.tournaments.player_tournament_map(
                session, player_id
            )
            if not tournament_names:
                return ()
            games = tuple(
                (
                    await session.execute(
                        select(GameRecord)
                        .where(
                            GameRecord.tournament_id.in_(tournament_names),
                            GameRecord.status.in_(("lobby", "active")),
                        )
                        .order_by(GameRecord.created_at)
                    )
                ).scalars()
            )
            result: list[dict[str, Any]] = []
            for game in games:
                card = await self._observable_card(session, game, player_id)
                if card is None:
                    continue
                card["tournament_id"] = str(game.tournament_id)
                card["tournament_name"] = tournament_names[game.tournament_id]
                result.append(card)
            return tuple(result)

    async def _observable_card(
        self, session: AsyncSession, game: GameRecord, player_id: UUID
    ) -> dict[str, Any] | None:
        """Project one observable game for a player, or None when it must be skipped."""
        participant = await session.scalar(
            select(GameParticipantRecord.id).where(
                GameParticipantRecord.game_id == game.id,
                GameParticipantRecord.player_id == player_id,
            )
        )
        if participant is not None:
            return None
        policy = await session.get(
            TournamentPolicyVersionRecord, game.tournament_policy_version_id
        )
        if policy is None:
            return None
        manages = await session.scalar(
            select(TournamentManagerRecord.player_id).where(
                TournamentManagerRecord.tournament_id == game.tournament_id,
                TournamentManagerRecord.player_id == player_id,
                TournamentManagerRecord.revoked_at.is_(None),
            )
        )
        mode = str(policy.policies.get("observing", "forbidden"))
        fresh_count = len(
            await self._fresh_plan_claims(session, player_id, game.assignment_plan)
        )
        observing = await session.scalar(
            select(GameObserverRecord.active).where(
                GameObserverRecord.game_id == game.id,
                GameObserverRecord.player_id == player_id,
            )
        )
        participant_rows = (
            await session.execute(
                select(PlayerRecord.public_nickname)
                .join(
                    GameParticipantRecord,
                    GameParticipantRecord.player_id == PlayerRecord.id,
                )
                .where(GameParticipantRecord.game_id == game.id)
                .order_by(PlayerRecord.public_nickname)
            )
        ).scalars()
        participants = tuple(participant_rows)
        can_observe = (
            manages is not None
            or mode == "unlimited"
            or (mode == "burnt-only" and fresh_count == 0)
        )
        return {
            "id": game.id,
            "status": game.status,
            "phase": game.phase,
            "participant_count": len(participants),
            "participants": participants,
            "observing": bool(observing),
            "observing_policy": mode,
            "managed": manages is not None,
            "fresh_content_count": fresh_count,
            "confirmation_required": can_observe and fresh_count > 0,
            "can_observe": can_observe,
        }

    async def join(self, game_id: UUID, telegram_user_id: int) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            participant = await self._participant(
                session, game.id, telegram_user_id, require_active=False
            )
            if game.status == "active" and not participant.active:
                await self._stop_other_observations(session, participant.player_id, game.id)
                return await self._reconnect_participant(session, game, participant)
            self._require(game.status == "lobby", "The lobby is closed")
            now = datetime.now(UTC)
            if game.join_deadline is not None and now >= game.join_deadline:
                events = await self._fail_to_start(session, game)
                self._bump(game)
                await session.flush()
                return Transition(False, tuple(events), await self._snapshot(session, game))
            await self._stop_other_observations(session, participant.player_id, game.id)
            events: list[dict[str, Any]] = []
            if participant.joined:
                return Transition(False, (), await self._snapshot(session, game))
            participant.joined = True
            participant.ready = True
            events.append(
                await self._event(
                    session,
                    game.id,
                    "player_joined",
                    {"participant_id": str(participant.id)},
                )
            )
            remaining = await session.scalar(
                select(func.count())
                .select_from(GameParticipantRecord)
                .where(
                    GameParticipantRecord.game_id == game.id,
                    GameParticipantRecord.joined.is_(False),
                )
            )
            if remaining == 0:
                settings = self._settings(game)
                game.status = "active"
                game.phase = "countdown"
                game.join_deadline = None
                game.progression_stage = "ready_countdown"
                game.progression_deadline = now + settings.ready_delta
                events.append(
                    await self._event(
                        session,
                        game.id,
                        "ready_countdown",
                        {"seconds": settings.ready_delay},
                    )
                )
            else:
                joined = await session.scalar(
                    select(func.count())
                    .select_from(GameParticipantRecord)
                    .where(
                        GameParticipantRecord.game_id == game.id,
                        GameParticipantRecord.joined.is_(True),
                    )
                )
                if joined == 1:
                    game.join_deadline = now + REMAINING_PLAYERS_JOIN_TIMEOUT
                    events.append(
                        await self._event(
                            session,
                            game.id,
                            "join_deadline_started",
                            {
                                "stage": "remaining_players",
                                "deadline": game.join_deadline,
                            },
                        )
                    )
            self._bump(game)
            await session.flush()
            return Transition(True, tuple(events), await self._snapshot(session, game))

    async def active_observed_game_ids(self, player_id: UUID) -> tuple[UUID, ...]:
        async with self.database.sessions() as session:
            return tuple(
                (
                    await session.execute(
                        select(GameObserverRecord.game_id).where(
                            GameObserverRecord.player_id == player_id,
                            GameObserverRecord.active.is_(True),
                        )
                    )
                ).scalars()
            )

    async def observe(
        self,
        game_id: UUID,
        telegram_user_id: int,
        *,
        confirm_fresh: bool = False,
    ) -> ObservationResult:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(game.status in {"lobby", "active"}, "Game is not ongoing")
            player = await session.scalar(
                select(PlayerRecord).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            if player is None or player.status != "active":
                raise LookupError("Active player not found")
            membership = await session.get(
                TournamentMembershipRecord, (game.tournament_id, player.id)
            )
            manages = await session.scalar(
                select(TournamentManagerRecord.player_id).where(
                    TournamentManagerRecord.tournament_id == game.tournament_id,
                    TournamentManagerRecord.player_id == player.id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
            )
            if manages is None and (membership is None or membership.status != "active"):
                raise PermissionError("Active tournament membership is required")
            participant = await session.scalar(
                select(GameParticipantRecord.id).where(
                    GameParticipantRecord.game_id == game.id,
                    GameParticipantRecord.player_id == player.id,
                )
            )
            if participant is not None:
                raise PermissionError("Game participants cannot join their game as observers")
            active_participant_game = await session.scalar(
                select(GameParticipantRecord.game_id)
                .where(
                    GameParticipantRecord.player_id == player.id,
                    GameParticipantRecord.active.is_(True),
                    GameParticipantRecord.joined.is_(True),
                )
                .limit(1)
            )
            if active_participant_game is not None:
                raise PermissionError("Players cannot observe while playing another game")
            policy = await session.get(
                TournamentPolicyVersionRecord, game.tournament_policy_version_id
            )
            if policy is None:
                raise RuntimeError("Tournament policy snapshot is missing")
            observing_policy = str(policy.policies.get("observing", "forbidden"))
            if observing_policy == "forbidden" and manages is None:
                raise PermissionError("Observing is forbidden by tournament policy")
            observer = await session.scalar(
                select(GameObserverRecord).where(
                    GameObserverRecord.game_id == game.id,
                    GameObserverRecord.player_id == player.id,
                )
            )
            if observer is not None and observer.active:
                return ObservationResult(
                    True,
                    False,
                    0,
                    await self._events(session, game.id),
                    await self._snapshot(session, game),
                )
            fresh = await self._fresh_plan_claims(session, player.id, game.assignment_plan)
            if fresh and observing_policy == "burnt-only" and manages is None:
                raise PermissionError(
                    "Burnt-only observing does not allow this player to see fresh content"
                )
            if fresh and not confirm_fresh:
                return ObservationResult(
                    False,
                    True,
                    len(fresh),
                    (),
                    None,
                )
            now = datetime.now(UTC)
            if observer is None:
                session.add(GameObserverRecord(game_id=game.id, player_id=player.id))
            else:
                observer.active = True
                observer.joined_at = now
                observer.left_at = None
            content_disclosed = await session.scalar(
                select(GameEventRecord.id)
                .where(
                    GameEventRecord.game_id == game.id,
                    GameEventRecord.kind == "themes_announced",
                )
                .limit(1)
            )
            for namespace, claim_id, packet_version_id in fresh:
                live_claim = await session.scalar(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == player.id,
                        PlayerExposureClaimRecord.claim_namespace == namespace,
                        PlayerExposureClaimRecord.claim_id == claim_id,
                        PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
                    )
                )
                if live_claim is not None:
                    if content_disclosed is not None:
                        live_claim.state = "burnt"
                        live_claim.burnt_at = now
                    continue
                existing_claim = await session.scalar(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.game_id == game.id,
                        PlayerExposureClaimRecord.player_id == player.id,
                        PlayerExposureClaimRecord.claim_namespace == namespace,
                        PlayerExposureClaimRecord.claim_id == claim_id,
                    )
                )
                if existing_claim is None:
                    session.add(
                        PlayerExposureClaimRecord(
                            game_id=game.id,
                            player_id=player.id,
                            packet_version_id=packet_version_id,
                            claim_namespace=namespace,
                            claim_id=claim_id,
                            state="burnt" if content_disclosed is not None else "reserved",
                            burnt_at=now if content_disclosed is not None else None,
                        )
                    )
                else:
                    existing_claim.state = "burnt" if content_disclosed is not None else "reserved"
                    existing_claim.burnt_at = now if content_disclosed is not None else None
                    existing_claim.released_at = None
            await session.flush()
            return ObservationResult(
                True,
                False,
                len(fresh),
                await self._events(session, game.id),
                await self._snapshot(session, game),
            )

    async def stop_observing(self, game_id: UUID, telegram_user_id: int) -> GameSnapshot:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            observer = await self._observer(session, game.id, telegram_user_id)
            observer.active = False
            observer.left_at = datetime.now(UTC)
            reservations = (
                await session.execute(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.game_id == game.id,
                        PlayerExposureClaimRecord.player_id == observer.player_id,
                        PlayerExposureClaimRecord.state == "reserved",
                    )
                )
            ).scalars()
            for claim in reservations:
                claim.state = "released"
                claim.released_at = observer.left_at
            await session.flush()
            return await self._snapshot(session, game)

    async def abandon_player(self, game_id: UUID, telegram_user_id: int) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(game.status in {"lobby", "active"}, "Game is not running")
            participant = await self._participant(session, game.id, telegram_user_id)
            revealed = await session.scalar(
                select(PlayerExposureClaimRecord.id)
                .where(
                    PlayerExposureClaimRecord.game_id == game.id,
                    PlayerExposureClaimRecord.state == "burnt",
                )
                .limit(1)
            )
            if revealed is None:
                events = await self._cancel_before_theme_reveal(session, game, participant)
                self._bump(game)
                await session.flush()
                return Transition(True, tuple(events), await self._snapshot(session, game))

            now = datetime.now(UTC)
            participant.active = False
            participant.abandoned_at = now
            events: list[dict[str, Any]] = []
            if game.phase == "question" and game.current_round_id is not None:
                if game.accepted_buzzer_id == participant.id:
                    events.extend(
                        await self._record_attempt(
                            session,
                            game,
                            participant,
                            submitted_answer=None,
                            timed_out=True,
                        )
                    )
                else:
                    state = await session.scalar(
                        select(PlayerQuestionStateRecord)
                        .where(
                            PlayerQuestionStateRecord.round_id == game.current_round_id,
                            PlayerQuestionStateRecord.participant_id == participant.id,
                        )
                        .with_for_update()
                    )
                    if state is not None:
                        state.eligible = False
                    remaining = await session.scalar(
                        select(func.count())
                        .select_from(PlayerQuestionStateRecord)
                        .where(
                            PlayerQuestionStateRecord.round_id == game.current_round_id,
                            PlayerQuestionStateRecord.eligible.is_(True),
                        )
                    )
                    if remaining == 0 and game.accepted_buzzer_id is None:
                        events.append(
                            await self._close_round(session, game, "all_active_players_exhausted")
                        )
            events.append(
                await self._event(
                    session,
                    game.id,
                    "player_abandoned",
                    {
                        "participant_id": str(participant.id),
                        "claims_remain_burnt": True,
                        "reconnect_allowed": True,
                    },
                )
            )
            self._bump(game)
            await session.flush()
            return Transition(True, tuple(events), await self._snapshot(session, game))

    async def _cancel_before_theme_reveal(
        self,
        session: AsyncSession,
        game: GameRecord,
        abandoning_participant: GameParticipantRecord,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        game.status = "cancelled"
        game.phase = "finished"
        game.cancelled_at = now
        game.join_deadline = None
        game.pause_abandonment_deadline = None
        game.accepted_buzzer_id = None
        game.buzz_deadline = None
        game.answer_deadline = None
        game.progression_deadline = None
        game.progression_stage = None
        participants = (
            await session.execute(
                select(GameParticipantRecord).where(GameParticipantRecord.game_id == game.id)
            )
        ).scalars()
        for participant in participants:
            participant.active = False
        exposure_claims = (
            await session.execute(
                select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.game_id == game.id,
                    PlayerExposureClaimRecord.state == "reserved",
                )
            )
        ).scalars()
        for claim in exposure_claims:
            claim.state = "released"
            claim.released_at = now
        return [
            await self._event(
                session,
                game.id,
                "game_cancelled",
                {
                    "reason": "player_abandoned_before_theme_reveal",
                    "participant_id": str(abandoning_participant.id),
                    "claims_burnt": False,
                    "rating_changes": False,
                },
            )
        ]

    async def _fail_to_start(
        self,
        session: AsyncSession,
        game: GameRecord,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        joined_count = int(
            await session.scalar(
                select(func.count())
                .select_from(GameParticipantRecord)
                .where(
                    GameParticipantRecord.game_id == game.id,
                    GameParticipantRecord.joined.is_(True),
                )
            )
            or 0
        )
        game.status = "failed_to_start"
        game.phase = "finished"
        game.cancelled_at = now
        game.join_deadline = None
        game.pause_abandonment_deadline = None
        game.accepted_buzzer_id = None
        game.buzz_deadline = None
        game.answer_deadline = None
        game.progression_deadline = None
        game.progression_stage = None
        participants = (
            await session.execute(
                select(GameParticipantRecord).where(GameParticipantRecord.game_id == game.id)
            )
        ).scalars()
        for participant in participants:
            participant.active = False
        observers = (
            await session.execute(
                select(GameObserverRecord).where(
                    GameObserverRecord.game_id == game.id,
                    GameObserverRecord.active.is_(True),
                )
            )
        ).scalars()
        for observer in observers:
            observer.active = False
            observer.left_at = now
        reservations = (
            await session.execute(
                select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.game_id == game.id,
                    PlayerExposureClaimRecord.state == "reserved",
                )
            )
        ).scalars()
        for reservation in reservations:
            reservation.state = "released"
            reservation.released_at = now
        return [
            await self._event(
                session,
                game.id,
                "game_cancelled",
                {
                    "reason": (
                        "no_players_joined" if joined_count == 0 else "not_all_players_joined"
                    ),
                    "joined_player_count": joined_count,
                    "claims_burnt": False,
                    "rating_changes": False,
                },
            )
        ]

    async def _reconnect_participant(
        self,
        session: AsyncSession,
        game: GameRecord,
        participant: GameParticipantRecord,
    ) -> Transition:
        self._require(
            participant.abandoned_at is not None,
            "Inactive participant cannot reconnect",
        )
        self._require(
            not await self._has_later_game(session, participant),
            "Reconnection was forfeited by entering another game",
        )
        active_lobby = await session.scalar(
            select(PregameLobbyMemberRecord.id)
            .where(
                PregameLobbyMemberRecord.player_id == participant.player_id,
                PregameLobbyMemberRecord.active.is_(True),
            )
            .limit(1)
        )
        self._require(
            active_lobby is None,
            "Leave the active pregame lobby before reconnecting",
        )
        other_active_game = await session.scalar(
            select(GameParticipantRecord.id)
            .where(
                GameParticipantRecord.player_id == participant.player_id,
                GameParticipantRecord.game_id != game.id,
                GameParticipantRecord.active.is_(True),
            )
            .limit(1)
        )
        self._require(other_active_game is None, "Player already belongs to another game")
        participant.active = True
        participant.reconnected_at = datetime.now(UTC)
        if game.phase == "question" and game.current_round_id is not None:
            state = await session.scalar(
                select(PlayerQuestionStateRecord)
                .where(
                    PlayerQuestionStateRecord.round_id == game.current_round_id,
                    PlayerQuestionStateRecord.participant_id == participant.id,
                )
                .with_for_update()
            )
            if state is None:
                session.add(
                    PlayerQuestionStateRecord(
                        round_id=game.current_round_id,
                        participant_id=participant.id,
                    )
                )
            elif not state.attempted:
                state.eligible = True
        event = await self._event(
            session,
            game.id,
            "player_reconnected",
            {"participant_id": str(participant.id)},
        )
        self._bump(game)
        await session.flush()
        return Transition(True, (event,), await self._snapshot(session, game))

    @staticmethod
    async def _has_later_game(
        session: AsyncSession,
        participant: GameParticipantRecord,
    ) -> bool:
        later_game = await session.scalar(
            select(GameParticipantRecord.id)
            .where(
                GameParticipantRecord.player_id == participant.player_id,
                GameParticipantRecord.global_game_sequence > participant.global_game_sequence,
            )
            .limit(1)
        )
        return later_game is not None

    async def advance(self, game_id: UUID) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(game.status == "active", "Game is not active")
            self._require(
                game.phase in {"countdown", "intermission"},
                "Game is not awaiting progression",
            )
            self._require(not game.paused, "Game is paused")
            events = await self._progress_game(session, game)
            self._bump(game)
            await session.flush()
            return Transition(True, tuple(events), await self._snapshot(session, game))

    async def buzz(self, game_id: UUID, telegram_user_id: int) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(game.status == "active" and game.phase == "question", "No question")
            self._require(game.question_token_index > 0, "Question text has not started")
            if game.accepted_buzzer_id is not None:
                return Transition(False, (), await self._snapshot(session, game))
            if game.buzz_deadline is not None and datetime.now(UTC) >= game.buzz_deadline:
                event = await self._close_round(session, game, "buzz_timeout")
                self._bump(game)
                await session.flush()
                return Transition(False, (event,), await self._snapshot(session, game))
            participant = await self._participant(session, game.id, telegram_user_id)
            state = await session.scalar(
                select(PlayerQuestionStateRecord)
                .where(
                    PlayerQuestionStateRecord.round_id == game.current_round_id,
                    PlayerQuestionStateRecord.participant_id == participant.id,
                )
                .with_for_update()
            )
            self._require(
                state is not None and state.eligible and not state.attempted,
                "Player is not eligible to buzz",
            )
            order = (
                await session.scalar(
                    select(func.count())
                    .select_from(PlayerQuestionStateRecord)
                    .where(
                        PlayerQuestionStateRecord.round_id == game.current_round_id,
                        PlayerQuestionStateRecord.buzzed_at.is_not(None),
                    )
                )
            ) or 0
            now = datetime.now(UTC)
            state.buzzed_at = now
            state.accepted_buzz_order = order + 1
            tokens = await self._question_tokens(session, game)
            state.buzz_revealed_fraction = (
                Decimal(game.question_token_index) / Decimal(len(tokens)) if tokens else Decimal(1)
            )
            if game.buzz_deadline is not None:
                timeout = Decimal(str(self._settings(game).buzz_timeout))
                remaining = Decimal(str(max(0.0, (game.buzz_deadline - now).total_seconds())))
                state.buzz_time_remaining_fraction = (
                    min(Decimal(1), remaining / timeout) if timeout > 0 else Decimal(0)
                )
            game.accepted_buzzer_id = participant.id
            game.buzz_deadline = None
            game.progression_deadline = None
            game.answer_deadline = now + self._settings(game).answer_delta
            event = await self._event(
                session,
                game.id,
                "player_buzzed",
                {"participant_id": str(participant.id)},
            )
            self._bump(game)
            await session.flush()
            return Transition(True, (event,), await self._snapshot(session, game))

    async def answer(self, game_id: UUID, telegram_user_id: int, answer: str) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(game.status == "active" and game.phase == "question", "No question")
            participant = await self._participant(session, game.id, telegram_user_id)
            self._require(
                game.accepted_buzzer_id == participant.id,
                "Only the accepted buzzer may answer",
            )
            timed_out = bool(game.answer_deadline and datetime.now(UTC) >= game.answer_deadline)
            events = await self._record_attempt(
                session,
                game,
                participant,
                submitted_answer=None if timed_out else answer,
                timed_out=timed_out,
            )
            self._bump(game)
            await session.flush()
            return Transition(not timed_out, tuple(events), await self._snapshot(session, game))

    async def submit_appeal(
        self,
        game_id: UUID,
        telegram_user_id: int,
        connected_player_ids: set[int] | frozenset[int],
        *,
        target_attempt_id: UUID | None = None,
    ) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(
                game.status == "active"
                and game.phase == "intermission"
                and game.current_round_id is not None,
                "Appeals can only be submitted immediately after a question",
            )
            self._require(
                game.progression_stage in {"next_question", "theme_complete"},
                "The appeal window for this question has closed",
            )
            round_record = await session.get(QuestionRoundRecord, game.current_round_id)
            self._require(
                round_record is not None and round_record.status == "completed",
                "The current question is not complete",
            )
            existing = await session.scalar(
                select(AppealRecord.id).where(
                    AppealRecord.game_id == game.id,
                    AppealRecord.round_id == game.current_round_id,
                )
            )
            self._require(existing is None, "This question has already been appealed")
            appellant = await self._participant(session, game.id, telegram_user_id)
            attempts = list(
                (
                    await session.execute(
                        select(AnswerAttemptRecord)
                        .where(AnswerAttemptRecord.round_id == game.current_round_id)
                        .order_by(AnswerAttemptRecord.attempt_number)
                    )
                ).scalars()
            )
            eligible = [
                attempt
                for attempt in attempts
                if attempt.original_correct
                or (
                    attempt.participant_id == appellant.id
                    and not attempt.original_correct
                    and not attempt.timed_out
                )
            ]
            self._require(eligible, "There is no answer this player may appeal")
            if target_attempt_id is None:
                self._require(
                    len(eligible) == 1,
                    "target_attempt_id is required when more than one answer can be appealed",
                )
                target = eligible[0]
            else:
                target = next((item for item in eligible if item.id == target_attempt_id), None)
                self._require(target is not None, "This player may not appeal that answer")
            assert target is not None and target.submitted_answer is not None

            electorate_rows = (
                await session.execute(
                    select(GameParticipantRecord, PlayerRecord.telegram_user_id)
                    .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                    .where(
                        GameParticipantRecord.game_id == game.id,
                        GameParticipantRecord.active.is_(True),
                        PlayerRecord.telegram_user_id.in_(connected_player_ids),
                    )
                )
            ).all()
            self._require(
                any(participant.id == appellant.id for participant, _ in electorate_rows),
                "The appellant must be connected when voting starts",
            )
            policy = await self._appeal_policy(session, game)
            escalation_enabled = policy.escalation_enabled
            now = datetime.now(UTC)
            appeal = AppealRecord(
                tournament_id=game.tournament_id,
                game_id=game.id,
                round_id=game.current_round_id,
                appellant_participant_id=appellant.id,
                target_attempt_id=target.id,
                kind="reject_correct" if target.original_correct else "accept_incorrect",
                voting_rule=policy.voting_rule,
                electorate_size=len(electorate_rows),
                vote_deadline=now + policy.vote_timeout,
                escalation_enabled=escalation_enabled,
                game_was_paused=game.paused,
            )
            session.add(appeal)
            await session.flush()
            for participant, _ in electorate_rows:
                session.add(AppealVoteRecord(appeal_id=appeal.id, participant_id=participant.id))
            if not game.paused:
                game.paused = True
                game.paused_at = now
            event = await self._event(
                session,
                game.id,
                "appeal_voting_started",
                {
                    "appeal_id": str(appeal.id),
                    "round_id": str(appeal.round_id),
                    "appellant_participant_id": str(appellant.id),
                    "target_attempt_id": str(target.id),
                    "kind": appeal.kind,
                    "submitted_answer": target.submitted_answer,
                    "voting_rule": appeal.voting_rule,
                    "electorate_size": appeal.electorate_size,
                    "vote_deadline": appeal.vote_deadline,
                    "escalation_enabled": appeal.escalation_enabled,
                },
            )
            self._bump(game)
            await session.flush()
            return Transition(True, (event,), await self._snapshot(session, game))

    async def vote_appeal(
        self, game_id: UUID, appeal_id: UUID, telegram_user_id: int, *, approve: bool
    ) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            appeal = await self._locked_appeal(session, appeal_id, game_id=game.id)
            if appeal.status != "voting":
                raise ValueError("Appeal voting is closed")
            if datetime.now(UTC) >= appeal.vote_deadline:
                events = await self._resolve_vote(session, game, appeal, timed_out=True)
                self._bump(game)
                await session.flush()
                return Transition(False, tuple(events), await self._snapshot(session, game))
            participant = await self._participant(session, game.id, telegram_user_id)
            vote = await session.get(AppealVoteRecord, (appeal.id, participant.id))
            if vote is None:
                raise PermissionError("Player is not in this appeal's electorate")
            self._require(vote.approve is None, "Player has already voted on this appeal")
            vote.approve = approve
            vote.voted_at = datetime.now(UTC)
            await session.flush()
            approvals, rejections = await self._vote_tally(session, appeal.id)
            events = [
                await self._event(
                    session,
                    game.id,
                    "appeal_vote_cast",
                    {
                        "appeal_id": str(appeal.id),
                        "votes_cast": approvals + rejections,
                        "electorate_size": appeal.electorate_size,
                    },
                )
            ]
            policy = AppealPolicy(voting_rule=appeal.voting_rule)
            if policy.vote_decided(approvals, rejections, appeal.electorate_size) is not None:
                events.extend(await self._resolve_vote(session, game, appeal, timed_out=False))
            self._bump(game)
            await session.flush()
            return Transition(True, tuple(events), await self._snapshot(session, game))

    async def choose_appeal_escalation(
        self, game_id: UUID, appeal_id: UUID, telegram_user_id: int, *, escalate: bool
    ) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            appeal = await self._locked_appeal(session, appeal_id, game_id=game.id)
            self._require(
                appeal.status == "awaiting_escalation", "Appeal is not awaiting escalation"
            )
            appellant = await self._participant(session, game.id, telegram_user_id)
            if appellant.id != appeal.appellant_participant_id:
                raise PermissionError("Only the appellant may choose escalation")
            if appeal.escalation_deadline and datetime.now(UTC) >= appeal.escalation_deadline:
                events = await self._reject_appeal(
                    session, game, appeal, source="timeout", resume_game=True
                )
                accepted = False
            elif escalate:
                policy = await self._appeal_policy(session, game)
                appeal.status = "awaiting_commentary"
                appeal.commentary_deadline = datetime.now(UTC) + policy.commentary_timeout
                events = [
                    await self._event(
                        session,
                        game.id,
                        "appeal_commentary_requested",
                        {
                            "appeal_id": str(appeal.id),
                            "deadline": appeal.commentary_deadline,
                        },
                    )
                ]
                accepted = True
            else:
                events = await self._reject_appeal(
                    session, game, appeal, source="player_declined", resume_game=True
                )
                accepted = True
            self._bump(game)
            await session.flush()
            return Transition(accepted, tuple(events), await self._snapshot(session, game))

    async def submit_appeal_commentary(
        self, game_id: UUID, appeal_id: UUID, telegram_user_id: int, commentary: str
    ) -> Transition:
        normalized = commentary.strip()
        if not normalized:
            raise ValueError("Appeal commentary cannot be empty")
        if len(normalized) > 10_000:
            raise ValueError("Appeal commentary cannot exceed 10000 characters")
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            appeal = await self._locked_appeal(session, appeal_id, game_id=game.id)
            self._require(
                appeal.status == "awaiting_commentary", "Appeal is not awaiting commentary"
            )
            appellant = await self._participant(session, game.id, telegram_user_id)
            if appellant.id != appeal.appellant_participant_id:
                raise PermissionError("Only the appellant may submit appeal commentary")
            if appeal.commentary_deadline and datetime.now(UTC) >= appeal.commentary_deadline:
                events = await self._escalate_appeal(session, game, appeal, commentary="")
                accepted = False
            else:
                events = await self._escalate_appeal(session, game, appeal, commentary=normalized)
                accepted = True
            self._bump(game)
            await session.flush()
            return Transition(accepted, tuple(events), await self._snapshot(session, game))

    async def manager_appeal_tickets(
        self, manager_id: UUID, *, tournament_id: UUID | None = None
    ) -> tuple[ManagerAppealTicket, ...]:
        async with self.database.sessions() as session:
            manager_tournaments = select(TournamentManagerRecord.tournament_id).where(
                TournamentManagerRecord.player_id == manager_id,
                TournamentManagerRecord.revoked_at.is_(None),
            )
            query = (
                select(
                    AppealRecord,
                    QuestionRoundRecord,
                    QuestionRevisionRecord,
                    AnswerAttemptRecord,
                )
                .join(QuestionRoundRecord, QuestionRoundRecord.id == AppealRecord.round_id)
                .join(
                    QuestionRevisionRecord,
                    QuestionRevisionRecord.id == QuestionRoundRecord.question_revision_id,
                )
                .join(AnswerAttemptRecord, AnswerAttemptRecord.id == AppealRecord.target_attempt_id)
                .where(
                    AppealRecord.status == "escalated",
                    AppealRecord.ticket_expires_at > datetime.now(UTC),
                    AppealRecord.tournament_id.in_(manager_tournaments),
                )
                .order_by(AppealRecord.ticket_expires_at, AppealRecord.created_at)
            )
            if tournament_id is not None:
                query = query.where(AppealRecord.tournament_id == tournament_id)
            rows = (await session.execute(query)).all()
            return tuple(
                ManagerAppealTicket(
                    id=appeal.id,
                    question_sequence=round_record.sequence,
                    question_text=question.text,
                    official_answer=question.answer,
                    kind=appeal.kind,
                    submitted_answer=attempt.submitted_answer or "",
                    commentary=appeal.commentary or "",
                    created_at=appeal.created_at,
                    expires_at=appeal.ticket_expires_at,
                )
                for appeal, round_record, question, attempt in rows
                if appeal.ticket_expires_at is not None
            )

    async def decide_escalated_appeal(
        self, appeal_id: UUID, manager_id: UUID, *, approve: bool
    ) -> Transition:
        async with self.database.transaction() as session:
            appeal = await session.get(AppealRecord, appeal_id)
            if appeal is None:
                raise LookupError("Appeal not found")
            manager = await session.get(TournamentManagerRecord, (appeal.tournament_id, manager_id))
            if manager is None or manager.revoked_at is not None:
                raise PermissionError("Tournament manager role is required")
            game = await self._locked_game(session, appeal.game_id)
            appeal = await self._locked_appeal(session, appeal_id, game_id=game.id)
            self._require(appeal.status == "escalated", "Appeal ticket is closed")
            if appeal.ticket_expires_at and datetime.now(UTC) >= appeal.ticket_expires_at:
                events = await self._reject_appeal(
                    session, game, appeal, source="timeout", resume_game=False
                )
                accepted = False
            elif approve:
                appeal.status = "accepted"
                appeal.decision_source = "manager"
                appeal.decided_by_manager_id = manager_id
                appeal.resolved_at = datetime.now(UTC)
                correction = await self._apply_appeal_correction(session, game, appeal)
                events = [
                    await self._event(
                        session,
                        game.id,
                        "appeal_resolved",
                        {
                            "appeal_id": str(appeal.id),
                            "approved": True,
                            "source": "manager",
                        },
                    ),
                    correction,
                ]
                events.extend(await self._complete_after_appeal(session, game))
                accepted = True
            else:
                events = await self._reject_appeal(
                    session, game, appeal, source="manager", resume_game=False
                )
                appeal.decided_by_manager_id = manager_id
                accepted = True
            self._bump(game)
            await session.flush()
            return Transition(accepted, tuple(events), await self._snapshot(session, game))

    async def progress_due_appeals(self, *, limit: int = 100) -> tuple[UUID, ...]:
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            appeal_ids = tuple(
                (
                    await session.execute(
                        select(AppealRecord.id)
                        .where(
                            or_(
                                and_(
                                    AppealRecord.status == "voting",
                                    AppealRecord.vote_deadline <= now,
                                ),
                                and_(
                                    AppealRecord.status == "awaiting_escalation",
                                    AppealRecord.escalation_deadline <= now,
                                ),
                                and_(
                                    AppealRecord.status == "awaiting_commentary",
                                    AppealRecord.commentary_deadline <= now,
                                ),
                                and_(
                                    AppealRecord.status == "escalated",
                                    AppealRecord.ticket_expires_at <= now,
                                ),
                            )
                        )
                        .order_by(AppealRecord.created_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        progressed: list[UUID] = []
        for appeal_id in appeal_ids:
            async with self.database.transaction() as session:
                appeal = await session.get(AppealRecord, appeal_id)
                if appeal is None:
                    continue
                game = await self._locked_game(session, appeal.game_id)
                appeal = await self._locked_appeal(session, appeal_id, game_id=game.id)
                now = datetime.now(UTC)
                if appeal.status == "voting" and appeal.vote_deadline <= now:
                    await self._resolve_vote(session, game, appeal, timed_out=True)
                elif (
                    appeal.status == "awaiting_escalation"
                    and appeal.escalation_deadline is not None
                    and appeal.escalation_deadline <= now
                ):
                    await self._reject_appeal(
                        session, game, appeal, source="timeout", resume_game=True
                    )
                elif (
                    appeal.status == "awaiting_commentary"
                    and appeal.commentary_deadline is not None
                    and appeal.commentary_deadline <= now
                ):
                    await self._escalate_appeal(session, game, appeal, commentary="")
                elif (
                    appeal.status == "escalated"
                    and appeal.ticket_expires_at is not None
                    and appeal.ticket_expires_at <= now
                ):
                    await self._reject_appeal(
                        session, game, appeal, source="timeout", resume_game=False
                    )
                else:
                    continue
                self._bump(game)
                await session.flush()
                progressed.append(game.id)
        return tuple(progressed)

    async def expire(self, game_id: UUID) -> Transition:
        """Apply a stored deadline after a restart or from the future job worker."""
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(game.status == "active" and game.phase == "question", "No question")
            now = datetime.now(UTC)
            if game.accepted_buzzer_id and game.answer_deadline and now >= game.answer_deadline:
                participant = await session.get(GameParticipantRecord, game.accepted_buzzer_id)
                assert participant is not None
                events = await self._record_attempt(
                    session, game, participant, submitted_answer=None, timed_out=True
                )
            elif (
                game.accepted_buzzer_id is None and game.buzz_deadline and now >= game.buzz_deadline
            ):
                events = [await self._close_round(session, game, "buzz_timeout")]
            else:
                return Transition(False, (), await self._snapshot(session, game))
            self._bump(game)
            await session.flush()
            return Transition(True, tuple(events), await self._snapshot(session, game))

    async def progress_due(self, game_id: UUID) -> Transition:
        """Apply one due automatic transition without a persistent step-6 job."""
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            now = datetime.now(UTC)
            if (
                game.status == "lobby"
                and game.join_deadline is not None
                and now >= game.join_deadline
            ):
                events = await self._fail_to_start(session, game)
            elif game.status == "active" and await session.scalar(
                select(self._unattended_condition(game.id))
            ):
                events = await self._finish_abandoned_game(
                    session, game, reason="no_reconnectable_players"
                )
            elif (
                game.status == "active"
                and game.paused
                and game.pause_abandonment_deadline is not None
                and now >= game.pause_abandonment_deadline
            ):
                events = await self._finish_abandoned_game(
                    session, game, reason="pause_inactivity_timeout"
                )
            elif game.status != "active" or game.paused:
                return Transition(False, (), await self._snapshot(session, game))
            elif (
                game.phase in {"countdown", "intermission"}
                and game.progression_deadline is not None
                and now >= game.progression_deadline
            ):
                events = await self._progress_game(session, game)
            elif (
                game.phase == "question"
                and game.progression_stage
                in {"question_reveal_start", "question_reveal", "buzz_timer_start"}
                and game.accepted_buzzer_id is None
                and game.progression_deadline is not None
                and now >= game.progression_deadline
            ):
                events = await self._progress_question_announcement(session, game)
            elif (
                game.phase == "question"
                and game.accepted_buzzer_id is not None
                and game.answer_deadline is not None
                and now >= game.answer_deadline
            ):
                participant = await session.get(GameParticipantRecord, game.accepted_buzzer_id)
                assert participant is not None
                events = await self._record_attempt(
                    session, game, participant, submitted_answer=None, timed_out=True
                )
            elif (
                game.phase == "question"
                and game.accepted_buzzer_id is None
                and game.buzz_deadline is not None
                and now >= game.buzz_deadline
            ):
                events = [await self._close_round(session, game, "buzz_timeout")]
            else:
                return Transition(False, (), await self._snapshot(session, game))
            self._bump(game)
            await session.flush()
            return Transition(True, tuple(events), await self._snapshot(session, game))

    async def progress_all_due(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Progress currently due games; row locks make concurrent runners safe."""
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            game_ids = tuple(
                (
                    await session.execute(
                        select(GameRecord.id)
                        .where(
                            or_(
                                and_(
                                    GameRecord.status == "lobby",
                                    GameRecord.join_deadline <= now,
                                ),
                                and_(
                                    GameRecord.status == "active",
                                    GameRecord.paused.is_(True),
                                    GameRecord.pause_abandonment_deadline <= now,
                                ),
                                and_(
                                    GameRecord.status == "active",
                                    GameRecord.paused.is_(False),
                                    (
                                        (GameRecord.progression_deadline <= now)
                                        | (GameRecord.buzz_deadline <= now)
                                        | (GameRecord.answer_deadline <= now)
                                    ),
                                ),
                            ),
                        )
                        .order_by(GameRecord.last_activity_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        progressed: list[UUID] = []
        for game_id in game_ids:
            transition = await self.progress_due(game_id)
            if transition.accepted:
                progressed.append(game_id)
        return tuple(progressed)

    async def sync_pause_abandonment_deadlines(
        self,
        connected_player_ids_by_game: dict[UUID, set[UUID]],
        *,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        """Start or cancel inactivity deadlines from the server's live connection view."""
        async with self.database.sessions() as session:
            game_ids = tuple(
                (
                    await session.execute(
                        select(GameRecord.id)
                        .where(GameRecord.status == "active", GameRecord.paused.is_(True))
                        .order_by(GameRecord.last_activity_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        changed: list[UUID] = []
        for game_id in game_ids:
            async with self.database.transaction() as session:
                game = await self._locked_game(session, game_id)
                if game.status != "active" or not game.paused:
                    continue
                connected_player_ids = connected_player_ids_by_game.get(game.id, set())
                connected_participant = None
                if connected_player_ids:
                    connected_participant = await session.scalar(
                        select(GameParticipantRecord.id)
                        .where(
                            GameParticipantRecord.game_id == game.id,
                            GameParticipantRecord.player_id.in_(connected_player_ids),
                            GameParticipantRecord.active.is_(True),
                        )
                        .limit(1)
                    )
                if connected_participant is not None:
                    if game.pause_abandonment_deadline is None:
                        continue
                    game.pause_abandonment_deadline = None
                    await self._event(
                        session,
                        game.id,
                        "pause_abandonment_deadline_cancelled",
                        {},
                    )
                else:
                    if game.pause_abandonment_deadline is not None:
                        continue
                    game.pause_abandonment_deadline = (
                        datetime.now(UTC) + PAUSED_GAME_ABANDONMENT_TIMEOUT
                    )
                    await self._event(
                        session,
                        game.id,
                        "pause_abandonment_deadline_started",
                        {"deadline": game.pause_abandonment_deadline},
                    )
                self._bump(game)
                await session.flush()
                changed.append(game.id)
        return tuple(changed)

    @staticmethod
    def _unattended_condition(game_id):
        later = aliased(GameParticipantRecord)
        entered_later_game = (
            select(later.id)
            .where(
                later.player_id == GameParticipantRecord.player_id,
                later.global_game_sequence > GameParticipantRecord.global_game_sequence,
            )
            .correlate(GameParticipantRecord)
            .exists()
        )
        remaining_player = (
            select(GameParticipantRecord.id)
            .where(
                GameParticipantRecord.game_id == game_id,
                or_(
                    GameParticipantRecord.active.is_(True),
                    and_(GameParticipantRecord.abandoned_at.is_not(None), ~entered_later_game),
                ),
            )
            .correlate(GameRecord)
            .exists()
        )
        observer = (
            select(GameObserverRecord.id)
            .where(
                GameObserverRecord.game_id == game_id,
                GameObserverRecord.active.is_(True),
            )
            .correlate(GameRecord)
            .exists()
        )
        return ~remaining_player & ~observer

    async def terminate_unattended_games(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Settle games whose players all forfeited reconnect and which nobody observes."""
        async with self.database.sessions() as session:
            game_ids = tuple(
                (
                    await session.execute(
                        select(GameRecord.id)
                        .where(
                            GameRecord.status == "active",
                            self._unattended_condition(GameRecord.id),
                        )
                        .order_by(GameRecord.last_activity_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        terminated = []
        for game_id in game_ids:
            async with self.database.transaction() as session:
                game = await self._locked_game(session, game_id)
                if game.status != "active" or not await session.scalar(
                    select(self._unattended_condition(game.id))
                ):
                    continue
                await self._finish_abandoned_game(session, game, reason="no_reconnectable_players")
                self._bump(game)
                await session.flush()
                terminated.append(game_id)
        return tuple(terminated)

    async def _finish_abandoned_game(
        self,
        session: AsyncSession,
        game: GameRecord,
        *,
        reason: str,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        events: list[dict[str, Any]] = []
        unresolved_appeals = (
            await session.execute(
                select(AppealRecord).where(
                    AppealRecord.game_id == game.id,
                    AppealRecord.status.in_(
                        ("voting", "awaiting_escalation", "awaiting_commentary", "escalated")
                    ),
                )
            )
        ).scalars()
        for appeal in unresolved_appeals:
            appeal.status = "rejected"
            appeal.decision_source = "game_abandonment"
            appeal.resolved_at = now
            events.append(
                await self._event(
                    session,
                    game.id,
                    "appeal_resolved",
                    {
                        "appeal_id": str(appeal.id),
                        "approved": False,
                        "source": "game_abandonment",
                    },
                )
            )
        game.abandoned_at = now
        game.paused = False
        game.paused_at = None
        game.pause_abandonment_deadline = None
        game.join_deadline = None
        game.accepted_buzzer_id = None
        game.buzz_deadline = None
        game.answer_deadline = None
        game.progression_deadline = None
        game.progression_stage = None
        reservations = (
            await session.execute(
                select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.game_id == game.id,
                    PlayerExposureClaimRecord.state == "reserved",
                )
            )
        ).scalars()
        for reservation in reservations:
            reservation.state = "released"
            reservation.released_at = now
        events.append(
            await self._event(
                session,
                game.id,
                "game_abandoned",
                {
                    "reason": reason,
                    "claims_remain_burnt": True,
                    "results_use_actual_scores": True,
                },
            )
        )
        events.extend(await self._finalize(session, game))
        return events

    async def abandon(self, game_id: UUID, *, reason: str) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(
                game.status in {"lobby", "active"},
                "Game is already terminal",
            )
            now = datetime.now(UTC)
            game.status = "abandoned"
            game.phase = "finished"
            game.abandoned_at = now
            game.accepted_buzzer_id = None
            game.buzz_deadline = None
            game.answer_deadline = None
            game.progression_deadline = None
            game.progression_stage = None
            game.join_deadline = None
            game.pause_abandonment_deadline = None
            participants = (
                await session.execute(
                    select(GameParticipantRecord).where(GameParticipantRecord.game_id == game.id)
                )
            ).scalars()
            for participant in participants:
                participant.active = False
            exposure_claims = (
                await session.execute(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.game_id == game.id,
                        PlayerExposureClaimRecord.state == "reserved",
                    )
                )
            ).scalars()
            for claim in exposure_claims:
                claim.state = "released"
                claim.released_at = now
            event = await self._event(session, game.id, "game_abandoned", {"reason": reason})
            self._bump(game)
            await session.flush()
            return Transition(True, (event,), await self._snapshot(session, game))

    async def pause(self, game_id: UUID, telegram_user_id: int) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(
                game.status == "active" and game.phase == "intermission",
                "Game can only pause between questions",
            )
            self._require(self._settings(game).pausing_allowed, "Pausing is disabled for this game")
            participant = await self._participant(session, game.id, telegram_user_id)
            if game.paused:
                return Transition(False, (), await self._snapshot(session, game))
            game.paused = True
            game.paused_at = datetime.now(UTC)
            game.pause_abandonment_deadline = None
            game.progression_deadline = None
            event = await self._event(
                session,
                game.id,
                "game_paused",
                {"participant_id": str(participant.id)},
            )
            self._bump(game)
            await session.flush()
            return Transition(True, (event,), await self._snapshot(session, game))

    async def resume(self, game_id: UUID, telegram_user_id: int) -> Transition:
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            participant = await self._participant(session, game.id, telegram_user_id)
            self._require(game.paused, "Game is not paused")
            blocking_appeal = await session.scalar(
                select(AppealRecord.id)
                .where(
                    AppealRecord.game_id == game.id,
                    AppealRecord.status.in_(
                        ("voting", "awaiting_escalation", "awaiting_commentary")
                    ),
                )
                .limit(1)
            )
            self._require(blocking_appeal is None, "Game cannot resume during an appeal decision")
            game.paused = False
            game.paused_at = None
            game.pause_abandonment_deadline = None
            game.progression_deadline = datetime.now(UTC) + self._settings(game).message_delta
            event = await self._event(
                session,
                game.id,
                "game_resumed",
                {"participant_id": str(participant.id)},
            )
            self._bump(game)
            await session.flush()
            return Transition(True, (event,), await self._snapshot(session, game))

    async def request_score(self, game_id: UUID, telegram_user_id: int) -> Transition:
        """Announce scores between questions and preserve message spacing."""
        async with self.database.transaction() as session:
            game = await self._locked_game(session, game_id)
            self._require(
                game.status == "active"
                and game.phase == "intermission"
                and game.current_round_id is not None,
                "Scores can only be requested between questions",
            )
            participant = await self._participant(session, game.id, telegram_user_id)
            event = await self._scoreboard_event(
                session,
                game,
                reason="player_requested",
                requested_by=participant.id,
            )
            if not game.paused:
                earliest_next_message = datetime.now(UTC) + self._settings(game).message_delta
                game.progression_deadline = max(
                    game.progression_deadline or earliest_next_message,
                    earliest_next_message,
                )
            self._bump(game)
            await session.flush()
            return Transition(True, (event,), await self._snapshot(session, game))

    async def request_observer_score(self, game_id: UUID, telegram_user_id: int) -> Transition:
        """Return a private score response without writing or delaying the game."""
        async with self.database.sessions() as session:
            game = await session.get(GameRecord, game_id)
            if game is None:
                raise LookupError("Game not found")
            await self._observer(session, game.id, telegram_user_id)
            payload = await self._scoreboard_payload(session, game)
            event = {
                "sequence": 0,
                "kind": "scoreboard",
                "payload": {"reason": "observer_requested", "private": True, **payload},
            }
            return Transition(True, (event,), await self._snapshot(session, game))

    async def recover(self, game_id: UUID) -> GameSnapshot:
        async with self.database.sessions() as session:
            game = await session.get(GameRecord, game_id)
            if game is None:
                raise LookupError("Game not found")
            return await self._snapshot(session, game)

    async def events(self, game_id: UUID) -> tuple[dict[str, Any], ...]:
        async with self.database.sessions() as session:
            return await self._events(session, game_id)

    async def _progress_game(self, session: AsyncSession, game: GameRecord) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        if game.phase == "countdown" and game.progression_stage == "ready_countdown":
            theme_rows = (
                await session.execute(
                    select(
                        GameThemeRecord,
                        ThemeRevisionRecord,
                        AuthorRecord.display_name,
                        PacketVersionRecord,
                    )
                    .join(
                        ThemeRevisionRecord,
                        ThemeRevisionRecord.id == GameThemeRecord.theme_revision_id,
                    )
                    .outerjoin(AuthorRecord, AuthorRecord.id == ThemeRevisionRecord.author_id)
                    .join(
                        PacketVersionRecord,
                        PacketVersionRecord.id == GameThemeRecord.source_packet_version_id,
                    )
                    .where(GameThemeRecord.game_id == game.id)
                    .order_by(GameThemeRecord.position)
                )
            ).all()
            reservations = (
                await session.execute(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.game_id == game.id,
                        PlayerExposureClaimRecord.state == "reserved",
                    )
                )
            ).scalars()
            for reservation in reservations:
                reservation.state = "burnt"
                reservation.burnt_at = now
            observers = tuple(
                (
                    await session.execute(
                        select(GameObserverRecord).where(
                            GameObserverRecord.game_id == game.id,
                            GameObserverRecord.active.is_(True),
                        )
                    )
                ).scalars()
            )
            for observer in observers:
                for namespace, claim_id, _ in await self._fresh_plan_claims(
                    session, observer.player_id, game.assignment_plan
                ):
                    observer_reservation = await session.scalar(
                        select(PlayerExposureClaimRecord).where(
                            PlayerExposureClaimRecord.player_id == observer.player_id,
                            PlayerExposureClaimRecord.claim_namespace == namespace,
                            PlayerExposureClaimRecord.claim_id == claim_id,
                            PlayerExposureClaimRecord.state == "reserved",
                        )
                    )
                    if observer_reservation is not None:
                        observer_reservation.state = "burnt"
                        observer_reservation.burnt_at = now
            game.phase = "intermission"
            game.progression_stage = "theme_start"
            game.progression_deadline = now + self._settings(game).game_start_to_first_theme_delta
            return [
                await self._event(session, game.id, "game_started", {}),
                await self._event(
                    session,
                    game.id,
                    "themes_announced",
                    {"themes": self._theme_announcement_payload(theme_rows)},
                ),
            ]

        if game.phase != "intermission":
            raise ValueError("Game is not awaiting progression")
        if game.progression_stage == "theme_start":
            next_round = await self._next_pending_round(session, game.id)
            if next_round is None:
                game.progression_stage = "finish"
                game.progression_deadline = now
                return []
            placement = await self._placement(session, game, next_round)
            theme = await session.get(ThemeRevisionRecord, placement.theme_revision_id)
            assert theme is not None
            game_theme, packet_version, packet_changed = await self._game_theme_context(
                session, game.id, theme.id
            )
            author = None
            if theme.author_id is not None:
                author = await session.scalar(
                    select(AuthorRecord.display_name).where(AuthorRecord.id == theme.author_id)
                )
            game.progression_stage = "question_start"
            game.progression_deadline = now + self._settings(game).theme_to_first_question_delta
            return [
                await self._event(
                    session,
                    game.id,
                    "theme_started",
                    {
                        "theme_revision_id": str(theme.id),
                        "position": game_theme.position,
                        "name": theme.name,
                        "author": author,
                        "packet_name": packet_version.name,
                        "packet_version_id": str(packet_version.id),
                        "packet_changed": packet_changed,
                    },
                )
            ]
        if game.progression_stage in {"question_start", "next_question"}:
            return await self._start_next_round(session, game)
        if game.progression_stage == "theme_complete":
            assert game.current_round_id is not None
            round_record = await session.get(QuestionRoundRecord, game.current_round_id)
            assert round_record is not None
            placement = await self._placement(session, game, round_record)
            theme = await session.get(ThemeRevisionRecord, placement.theme_revision_id)
            assert theme is not None
            game_theme, packet_version, _ = await self._game_theme_context(
                session, game.id, theme.id
            )
            game.progression_stage = "theme_scoreboard"
            game.progression_deadline = (
                now + self._settings(game).theme_complete_to_scoreboard_delta
            )
            return [
                await self._event(
                    session,
                    game.id,
                    "theme_completed",
                    {
                        "theme_revision_id": str(theme.id),
                        "position": game_theme.position,
                        "name": theme.name,
                        "packet_name": packet_version.name,
                    },
                )
            ]
        if game.progression_stage == "theme_scoreboard":
            next_round = await self._next_pending_round(session, game.id)
            game.progression_stage = "theme_start" if next_round is not None else "finish"
            game.progression_deadline = now + (
                self._settings(game).between_themes_delta
                if next_round is not None
                else self._settings(game).message_delta
            )
            return [await self._scoreboard_event(session, game, reason="theme_completed")]
        if game.progression_stage == "finish":
            return await self._finalize(session, game)
        raise ValueError("Game progression stage is missing")

    async def _start_next_round(
        self, session: AsyncSession, game: GameRecord
    ) -> list[dict[str, Any]]:
        next_round = await session.scalar(
            select(QuestionRoundRecord)
            .where(
                QuestionRoundRecord.game_id == game.id,
                QuestionRoundRecord.status == "pending",
            )
            .order_by(QuestionRoundRecord.sequence)
            .limit(1)
            .with_for_update()
        )
        if next_round is None:
            return await self._finalize(session, game)

        now = datetime.now(UTC)
        settings = self._settings(game)
        next_round.status = "active"
        next_round.started_at = now
        game.phase = "question"
        game.current_round_id = next_round.id
        game.question_token_index = 0
        game.buzz_deadline = None
        game.answer_deadline = None
        game.progression_deadline = None
        game.progression_stage = "question_reveal_start"
        participants = (
            await session.execute(
                select(GameParticipantRecord).where(
                    GameParticipantRecord.game_id == game.id,
                    GameParticipantRecord.active.is_(True),
                )
            )
        ).scalars()
        for participant in participants:
            session.add(
                PlayerQuestionStateRecord(round_id=next_round.id, participant_id=participant.id)
            )
        placement = await self._placement(session, game, next_round)
        events = [
            await self._event(
                session,
                game.id,
                "question_cost_announced",
                {
                    "round_id": str(next_round.id),
                    "sequence": next_round.sequence,
                    "value": placement.value,
                },
            )
        ]
        if settings.question_cost_announcement_delay == 0:
            events.extend(await self._begin_question_reveal(session, game))
        else:
            game.progression_deadline = now + settings.question_cost_announcement_delta
        return events

    async def _progress_question_announcement(
        self, session: AsyncSession, game: GameRecord
    ) -> list[dict[str, Any]]:
        if game.progression_stage == "question_reveal_start":
            return await self._begin_question_reveal(session, game)
        if game.progression_stage == "question_reveal":
            return await self._reveal_next_question_token(session, game)
        if game.progression_stage == "buzz_timer_start":
            return [await self._start_buzz_timer(session, game)]
        raise ValueError("Question announcement is not awaiting progression")

    async def _begin_question_reveal(
        self, session: AsyncSession, game: GameRecord
    ) -> list[dict[str, Any]]:
        settings = self._settings(game)
        tokens = await self._question_tokens(session, game)
        if settings.question_token_delay == 0:
            game.question_token_index = len(tokens)
        elif tokens:
            game.question_token_index = 1
        events = [await self._question_token_event(session, game, tokens)]
        if game.question_token_index >= len(tokens):
            events.extend(await self._complete_question_announcement(session, game))
        else:
            game.progression_stage = "question_reveal"
            game.progression_deadline = datetime.now(UTC) + settings.token_delta
        return events

    async def _reveal_next_question_token(
        self, session: AsyncSession, game: GameRecord
    ) -> list[dict[str, Any]]:
        tokens = await self._question_tokens(session, game)
        if game.question_token_index < len(tokens):
            game.question_token_index += 1
        events = [await self._question_token_event(session, game, tokens)]
        settings = self._settings(game)
        if game.question_token_index >= len(tokens):
            events.extend(await self._complete_question_announcement(session, game))
        else:
            game.progression_deadline = datetime.now(UTC) + settings.token_delta
        return events

    async def _complete_question_announcement(
        self, session: AsyncSession, game: GameRecord
    ) -> list[dict[str, Any]]:
        settings = self._settings(game)
        events = [await self._question_fully_announced_event(session, game)]
        game.buzz_deadline = None
        if settings.buzz_timer_countdown_delay == 0:
            events.append(await self._start_buzz_timer(session, game))
        else:
            game.progression_stage = "buzz_timer_start"
            game.progression_deadline = datetime.now(UTC) + settings.buzz_timer_countdown_delta
        return events

    async def _start_buzz_timer(self, session: AsyncSession, game: GameRecord) -> dict[str, Any]:
        settings = self._settings(game)
        game.progression_stage = None
        game.progression_deadline = None
        game.buzz_deadline = datetime.now(UTC) + settings.buzz_delta
        return await self._event(
            session,
            game.id,
            "buzz_timer_started",
            {"seconds": settings.buzz_timeout},
        )

    async def _question_token_event(
        self, session: AsyncSession, game: GameRecord, tokens: tuple[str, ...]
    ) -> dict[str, Any]:
        visible = " ".join(tokens[: game.question_token_index])
        return await self._event(
            session,
            game.id,
            "question_token_revealed",
            {
                "text": visible,
                "revealed_token_count": game.question_token_index,
                "total_token_count": len(tokens),
            },
        )

    async def _question_fully_announced_event(
        self, session: AsyncSession, game: GameRecord
    ) -> dict[str, Any]:
        return await self._event(
            session,
            game.id,
            "question_fully_announced",
            {"buzz_timer_countdown_delay": self._settings(game).buzz_timer_countdown_delay},
        )

    async def _question_tokens(self, session: AsyncSession, game: GameRecord) -> tuple[str, ...]:
        assert game.current_round_id is not None
        round_record = await session.get(QuestionRoundRecord, game.current_round_id)
        assert round_record is not None
        revision = await session.get(QuestionRevisionRecord, round_record.question_revision_id)
        assert revision is not None
        return announcement_tokens(revision.text, self._settings(game).question_token_target_chars)

    @staticmethod
    async def _next_pending_round(
        session: AsyncSession, game_id: UUID
    ) -> QuestionRoundRecord | None:
        return await session.scalar(
            select(QuestionRoundRecord)
            .where(
                QuestionRoundRecord.game_id == game_id,
                QuestionRoundRecord.status == "pending",
            )
            .order_by(QuestionRoundRecord.sequence)
            .limit(1)
            .with_for_update()
        )

    @staticmethod
    async def _placement(
        session: AsyncSession,
        game: GameRecord,
        round_record: QuestionRoundRecord,
    ) -> PacketQuestionRecord:
        placement = await session.scalar(
            select(PacketQuestionRecord)
            .join(
                GameThemeRecord,
                and_(
                    GameThemeRecord.game_id == game.id,
                    GameThemeRecord.theme_revision_id == PacketQuestionRecord.theme_revision_id,
                    GameThemeRecord.source_packet_version_id
                    == PacketQuestionRecord.packet_version_id,
                ),
            )
            .where(PacketQuestionRecord.question_revision_id == round_record.question_revision_id)
        )
        assert placement is not None
        return placement

    @staticmethod
    async def _game_theme_context(
        session: AsyncSession, game_id: UUID, theme_revision_id: UUID
    ) -> tuple[GameThemeRecord, PacketVersionRecord, bool]:
        row = (
            await session.execute(
                select(GameThemeRecord, PacketVersionRecord)
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == GameThemeRecord.source_packet_version_id,
                )
                .where(
                    GameThemeRecord.game_id == game_id,
                    GameThemeRecord.theme_revision_id == theme_revision_id,
                )
            )
        ).one()
        game_theme, packet_version = row
        previous_packet_version_id = await session.scalar(
            select(GameThemeRecord.source_packet_version_id).where(
                GameThemeRecord.game_id == game_id,
                GameThemeRecord.position == game_theme.position - 1,
            )
        )
        return (
            game_theme,
            packet_version,
            previous_packet_version_id is not None
            and previous_packet_version_id != game_theme.source_packet_version_id,
        )

    @staticmethod
    def _theme_announcement_payload(
        rows: list[tuple[GameThemeRecord, ThemeRevisionRecord, str | None, PacketVersionRecord]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        previous_packet_version_id: UUID | None = None
        for game_theme, theme, author, packet_version in rows:
            result.append(
                {
                    "position": game_theme.position,
                    "name": theme.name,
                    "author": author,
                    "packet_name": packet_version.name,
                    "packet_version_id": str(packet_version.id),
                    "packet_changed": (
                        previous_packet_version_id is not None
                        and previous_packet_version_id != packet_version.id
                    ),
                }
            )
            previous_packet_version_id = packet_version.id
        return result

    async def _scoreboard_event(
        self,
        session: AsyncSession,
        game: GameRecord,
        *,
        reason: str,
        requested_by: UUID | None = None,
    ) -> dict[str, Any]:
        payload = await self._scoreboard_payload(session, game)
        return await self._event(
            session,
            game.id,
            "scoreboard",
            {
                "reason": reason,
                "requested_by": str(requested_by) if requested_by else None,
                **payload,
            },
        )

    @staticmethod
    async def _scoreboard_payload(session: AsyncSession, game: GameRecord) -> dict[str, Any]:
        rows = (
            await session.execute(
                select(GameParticipantRecord, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                .where(GameParticipantRecord.game_id == game.id)
                .order_by(GameParticipantRecord.seat)
            )
        ).all()
        return {
            "players": [
                {
                    "telegram_user_id": player.telegram_user_id,
                    "display_name": player.public_nickname,
                    "score": participant.score,
                    "active": participant.active,
                    "abandoned": participant.abandoned_at is not None and not participant.active,
                }
                for participant, player in rows
            ]
        }

    async def _record_attempt(
        self,
        session: AsyncSession,
        game: GameRecord,
        participant: GameParticipantRecord,
        *,
        submitted_answer: str | None,
        timed_out: bool,
    ) -> list[dict[str, Any]]:
        assert game.current_round_id is not None
        round_record = await session.get(QuestionRoundRecord, game.current_round_id)
        assert round_record is not None
        revision = await session.get(QuestionRevisionRecord, round_record.question_revision_id)
        assert revision is not None
        placement = await self._placement(session, game, round_record)
        question = Question(
            text=revision.text,
            answer=revision.answer,
            commentary=revision.commentary,
            value=placement.value,
            accepted_answers=tuple(revision.accepted_answers),
            form=revision.form,
            source=revision.source,
        )
        ruleset = await self._ruleset(session, game)
        correct = submitted_answer is not None and ruleset.judge_answer(
            submitted_answer, question.all_answers
        )
        attempt_number = (
            await session.scalar(
                select(func.count())
                .select_from(AnswerAttemptRecord)
                .where(AnswerAttemptRecord.round_id == round_record.id)
            )
        ) or 0
        attempt = AnswerAttemptRecord(
            round_id=round_record.id,
            participant_id=participant.id,
            attempt_number=attempt_number + 1,
            submitted_answer=submitted_answer,
            timed_out=timed_out,
            original_correct=correct,
            final_correct=correct,
        )
        session.add(attempt)
        await session.flush()
        delta = ruleset.score_answer(placement.value, correct, self._settings(game))
        session.add(
            ScoreLedgerRecord(
                game_id=game.id,
                participant_id=participant.id,
                round_id=round_record.id,
                attempt_id=attempt.id,
                delta=delta,
                reason=(
                    "correct_answer"
                    if correct
                    else "answer_timeout"
                    if timed_out
                    else "incorrect_answer"
                ),
            )
        )
        participant.score += delta
        state = await session.scalar(
            select(PlayerQuestionStateRecord)
            .where(
                PlayerQuestionStateRecord.round_id == round_record.id,
                PlayerQuestionStateRecord.participant_id == participant.id,
            )
            .with_for_update()
        )
        assert state is not None
        state.attempted = True
        state.eligible = False
        game.accepted_buzzer_id = None
        game.answer_deadline = None
        remaining = await session.scalar(
            select(func.count())
            .select_from(PlayerQuestionStateRecord)
            .where(
                PlayerQuestionStateRecord.round_id == round_record.id,
                PlayerQuestionStateRecord.eligible.is_(True),
            )
        )
        events = [
            await self._event(
                session,
                game.id,
                "answer_judged",
                {
                    "participant_id": str(participant.id),
                    "attempt_id": str(attempt.id),
                    "timed_out": timed_out,
                    "original_correct": correct,
                    "final_correct": correct,
                    "score_delta": delta,
                },
            )
        ]
        if correct or remaining == 0:
            events.append(await self._close_round(session, game, "answered"))
        else:
            settings = self._settings(game)
            tokens = await self._question_tokens(session, game)
            if game.question_token_index < len(tokens):
                game.progression_stage = "question_reveal"
                game.progression_deadline = datetime.now(UTC) + settings.token_delta
                game.buzz_deadline = None
            elif game.progression_stage == "buzz_timer_start":
                game.progression_deadline = datetime.now(UTC) + settings.buzz_timer_countdown_delta
                game.buzz_deadline = None
            else:
                game.progression_stage = None
                game.progression_deadline = None
                game.buzz_deadline = datetime.now(UTC) + settings.buzz_delta
        return events

    async def _close_round(
        self, session: AsyncSession, game: GameRecord, reason: str
    ) -> dict[str, Any]:
        assert game.current_round_id is not None
        round_record = await session.get(QuestionRoundRecord, game.current_round_id)
        assert round_record is not None
        round_record.status = "completed"
        round_record.completed_at = datetime.now(UTC)
        game.phase = "intermission"
        game.accepted_buzzer_id = None
        game.buzz_deadline = None
        game.answer_deadline = None
        placement = await self._placement(session, game, round_record)
        settings = self._settings(game)
        if placement.position == 5:
            game.progression_stage = "theme_complete"
            delay = settings.last_question_to_theme_complete_delta
        else:
            game.progression_stage = "next_question"
            delay = settings.between_questions_delta
        game.progression_deadline = datetime.now(UTC) + delay
        return await self._event(
            session,
            game.id,
            "question_completed",
            {"round_id": str(round_record.id), "reason": reason},
        )

    async def _resolve_vote(
        self,
        session: AsyncSession,
        game: GameRecord,
        appeal: AppealRecord,
        *,
        timed_out: bool,
    ) -> list[dict[str, Any]]:
        approvals, rejections = await self._vote_tally(session, appeal.id)
        policy = AppealPolicy(voting_rule=appeal.voting_rule)
        approved = policy.vote_approved(approvals, appeal.electorate_size)
        if not timed_out:
            decision = policy.vote_decided(approvals, rejections, appeal.electorate_size)
            if decision is None:
                return []
            approved = decision
        if approved:
            appeal.status = "accepted"
            appeal.decision_source = "player_vote"
            appeal.resolved_at = datetime.now(UTC)
            resolution = await self._event(
                session,
                game.id,
                "appeal_resolved",
                {
                    "appeal_id": str(appeal.id),
                    "approved": True,
                    "source": "player_vote",
                    "approvals": approvals,
                    "rejections": rejections,
                    "missing_votes": appeal.electorate_size - approvals - rejections,
                },
            )
            correction = await self._apply_appeal_correction(session, game, appeal)
            self._resume_game_after_appeal(game, appeal)
            return [resolution, correction]
        if appeal.escalation_enabled:
            appeal.status = "awaiting_escalation"
            appeal.escalation_deadline = (
                datetime.now(UTC)
                + (await self._appeal_policy(session, game)).escalation_decision_timeout
            )
            return [
                await self._event(
                    session,
                    game.id,
                    "appeal_vote_rejected",
                    {
                        "appeal_id": str(appeal.id),
                        "approvals": approvals,
                        "rejections": rejections,
                        "missing_votes": appeal.electorate_size - approvals - rejections,
                        "escalation_deadline": appeal.escalation_deadline,
                    },
                )
            ]
        return await self._reject_appeal(
            session, game, appeal, source="player_vote", resume_game=True
        )

    async def _reject_appeal(
        self,
        session: AsyncSession,
        game: GameRecord,
        appeal: AppealRecord,
        *,
        source: str,
        resume_game: bool,
    ) -> list[dict[str, Any]]:
        appeal.status = "rejected"
        appeal.decision_source = source
        appeal.resolved_at = datetime.now(UTC)
        if resume_game:
            self._resume_game_after_appeal(game, appeal)
        events = [
            await self._event(
                session,
                game.id,
                "appeal_resolved",
                {
                    "appeal_id": str(appeal.id),
                    "approved": False,
                    "source": source,
                },
            )
        ]
        events.extend(await self._complete_after_appeal(session, game))
        return events

    async def _escalate_appeal(
        self,
        session: AsyncSession,
        game: GameRecord,
        appeal: AppealRecord,
        *,
        commentary: str,
    ) -> list[dict[str, Any]]:
        policy = await self._appeal_policy(session, game)
        now = datetime.now(UTC)
        appeal.status = "escalated"
        appeal.commentary = commentary
        appeal.escalated_at = now
        appeal.ticket_expires_at = now + policy.ticket_expiry
        self._resume_game_after_appeal(game, appeal)
        return [
            await self._event(
                session,
                game.id,
                "appeal_escalated",
                {
                    "appeal_id": str(appeal.id),
                    "ticket_expires_at": appeal.ticket_expires_at,
                },
            )
        ]

    async def _apply_appeal_correction(
        self, session: AsyncSession, game: GameRecord, appeal: AppealRecord
    ) -> dict[str, Any]:
        target = await session.get(AnswerAttemptRecord, appeal.target_attempt_id)
        assert target is not None
        round_record = await session.get(QuestionRoundRecord, appeal.round_id)
        assert round_record is not None
        placement = await self._placement(session, game, round_record)
        ruleset = await self._ruleset(session, game)
        settings = self._settings(game)
        attempts = list(
            (
                await session.execute(
                    select(AnswerAttemptRecord)
                    .where(AnswerAttemptRecord.round_id == appeal.round_id)
                    .order_by(AnswerAttemptRecord.attempt_number)
                )
            ).scalars()
        )
        affected = (
            [target]
            if appeal.kind == "reject_correct"
            else [item for item in attempts if item.attempt_number >= target.attempt_number]
        )
        corrections: list[dict[str, Any]] = []
        for attempt in affected:
            original = await session.scalar(
                select(ScoreLedgerRecord)
                .where(
                    ScoreLedgerRecord.attempt_id == attempt.id,
                    ScoreLedgerRecord.correction_of_id.is_(None),
                )
                .order_by(ScoreLedgerRecord.id)
                .limit(1)
            )
            assert original is not None
            if attempt.id == target.id:
                final_correct = appeal.kind == "accept_incorrect"
                desired = ruleset.score_answer(placement.value, final_correct, settings)
            else:
                final_correct = False
                desired = Decimal(0)
            delta = Decimal(desired) - Decimal(original.delta)
            attempt.final_correct = final_correct
            if not delta:
                continue
            correction = ScoreLedgerRecord(
                game_id=game.id,
                participant_id=attempt.participant_id,
                round_id=appeal.round_id,
                attempt_id=attempt.id,
                appeal_id=appeal.id,
                delta=delta,
                reason="appeal_correction",
                correction_of_id=original.id,
            )
            session.add(correction)
            participant = await session.get(GameParticipantRecord, attempt.participant_id)
            assert participant is not None
            participant.score += delta
            corrections.append(
                {
                    "participant_id": str(participant.id),
                    "attempt_id": str(attempt.id),
                    "delta": delta,
                }
            )
        if game.status == "completed":
            await self._refresh_completed_results(session, game)
        return await self._event(
            session,
            game.id,
            "appeal_score_corrected",
            {"appeal_id": str(appeal.id), "corrections": corrections},
        )

    @staticmethod
    def _resume_game_after_appeal(game: GameRecord, appeal: AppealRecord) -> None:
        if appeal.game_was_paused or game.status != "active":
            return
        game.paused = False
        game.paused_at = None
        game.pause_abandonment_deadline = None
        game.progression_deadline = (
            datetime.now(UTC) + PersistentGameService._settings(game).message_delta
        )

    @staticmethod
    async def _vote_tally(session: AsyncSession, appeal_id: UUID) -> tuple[int, int]:
        approvals = (
            await session.scalar(
                select(func.count())
                .select_from(AppealVoteRecord)
                .where(AppealVoteRecord.appeal_id == appeal_id, AppealVoteRecord.approve.is_(True))
            )
        ) or 0
        rejections = (
            await session.scalar(
                select(func.count())
                .select_from(AppealVoteRecord)
                .where(
                    AppealVoteRecord.appeal_id == appeal_id,
                    AppealVoteRecord.approve.is_(False),
                )
            )
        ) or 0
        return approvals, rejections

    @staticmethod
    async def _appeal_policy(session: AsyncSession, game: GameRecord) -> AppealPolicy:
        policy = await session.get(TournamentPolicyVersionRecord, game.tournament_policy_version_id)
        if policy is None:
            raise RuntimeError("Tournament policy snapshot is missing")
        return AppealPolicy.from_mapping(policy.policies)

    @staticmethod
    async def _locked_appeal(
        session: AsyncSession, appeal_id: UUID, *, game_id: UUID | None = None
    ) -> AppealRecord:
        query = select(AppealRecord).where(AppealRecord.id == appeal_id)
        if game_id is not None:
            query = query.where(AppealRecord.game_id == game_id)
        appeal = await session.scalar(query.with_for_update())
        if appeal is None:
            raise LookupError("Appeal not found")
        return appeal

    async def _finalize(self, session: AsyncSession, game: GameRecord) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        game.status = "completed"
        game.phase = "finished"
        game.completed_at = now
        game.join_deadline = None
        game.pause_abandonment_deadline = None
        participants = list(
            (
                await session.execute(
                    select(GameParticipantRecord).where(GameParticipantRecord.game_id == game.id)
                )
            ).scalars()
        )
        ruleset = await self._ruleset(session, game)
        settings = self._settings(game)
        metrics: dict[UUID, tuple[Any, ...]] = {}
        for participant in participants:
            ledger_total = (
                await session.scalar(
                    select(func.coalesce(func.sum(ScoreLedgerRecord.delta), 0)).where(
                        ScoreLedgerRecord.participant_id == participant.id
                    )
                )
            ) or 0
            participant.score = ledger_total
            correct_rows = (
                await session.execute(
                    select(PacketQuestionRecord.value)
                    .join(
                        QuestionRoundRecord,
                        QuestionRoundRecord.question_revision_id
                        == PacketQuestionRecord.question_revision_id,
                    )
                    .join(
                        GameThemeRecord,
                        and_(
                            GameThemeRecord.game_id == game.id,
                            GameThemeRecord.theme_revision_id
                            == PacketQuestionRecord.theme_revision_id,
                            GameThemeRecord.source_packet_version_id
                            == PacketQuestionRecord.packet_version_id,
                        ),
                    )
                    .join(
                        AnswerAttemptRecord,
                        AnswerAttemptRecord.round_id == QuestionRoundRecord.id,
                    )
                    .where(
                        QuestionRoundRecord.game_id == game.id,
                        AnswerAttemptRecord.participant_id == participant.id,
                        AnswerAttemptRecord.final_correct.is_(True),
                    )
                )
            ).scalars()
            values = list(correct_rows)
            metrics[participant.id] = ruleset.ranking_key(
                score=participant.score,
                correct_values=values,
                parameters=settings,
            )
        ranked = sorted(
            participants,
            key=lambda participant: (metrics[participant.id], -participant.seat),
            reverse=True,
        )
        self._assign_shared_places(ranked, metrics)
        for participant in ranked:
            participant.active = False
            session.add(
                GameResultRecord(
                    game_id=game.id,
                    player_id=participant.player_id,
                    tournament_id=game.tournament_id,
                    tournament_policy_version_id=game.tournament_policy_version_id,
                    place=participant.final_place,
                    score=participant.score,
                )
            )
        completed = await self._event(session, game.id, "game_completed", {})
        game.progression_deadline = None
        game.progression_stage = None
        if await self._has_unresolved_appeals(session, game.id):
            deferred = await self._event(
                session,
                game.id,
                "finalization_deferred",
                {"reason": "unresolved_appeal"},
            )
            return [completed, deferred]
        if len(ranked) < 2:
            game.status = "finalized"
            game.finalized_at = now
            finalized = await self._finalized_event(session, game, ranked, [])
            return [completed, finalized]
        if await self._rating_dependencies_resolved(session, game, ranked):
            finalized = await self._apply_rating_settlement(session, game, ranked)
            return [completed, finalized]
        deferred = await self._event(
            session,
            game.id,
            "rating_deferred",
            {"reason": "earlier_rating_result_unsettled"},
        )
        return [completed, deferred]

    async def _rating_dependencies_resolved(
        self,
        session: AsyncSession,
        game: GameRecord,
        participants: list[GameParticipantRecord],
    ) -> bool:
        local_rating_enabled = await self._rating_enabled(session, game)
        ruleset_version = await session.get(GameRulesetVersionRecord, game.game_ruleset_version_id)
        if ruleset_version is None:
            raise RuntimeError("Game ruleset version is missing")
        for participant in participants:
            if local_rating_enabled:
                unresolved = await session.scalar(
                    select(GameRecord.id)
                    .join(
                        GameParticipantRecord,
                        GameParticipantRecord.game_id == GameRecord.id,
                    )
                    .outerjoin(
                        RatingLedgerRecord,
                        and_(
                            RatingLedgerRecord.game_id == GameRecord.id,
                            RatingLedgerRecord.player_id == participant.player_id,
                        ),
                    )
                    .where(
                        GameParticipantRecord.player_id == participant.player_id,
                        GameParticipantRecord.tournament_id == game.tournament_id,
                        GameRecord.id != game.id,
                        GameRecord.tournament_id == game.tournament_id,
                        GameParticipantRecord.rating_sequence < participant.rating_sequence,
                        GameRecord.status.in_(("lobby", "active", "completed")),
                        RatingLedgerRecord.id.is_(None),
                    )
                    .limit(1)
                )
                if unresolved is not None:
                    return False
            unresolved_ruleset = await session.scalar(
                select(GameRecord.id)
                .join(GameParticipantRecord, GameParticipantRecord.game_id == GameRecord.id)
                .join(
                    GameRulesetVersionRecord,
                    GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
                )
                .outerjoin(
                    RulesetRatingLedgerRecord,
                    and_(
                        RulesetRatingLedgerRecord.game_id == GameRecord.id,
                        RulesetRatingLedgerRecord.player_id == participant.player_id,
                    ),
                )
                .where(
                    GameParticipantRecord.player_id == participant.player_id,
                    GameRecord.id != game.id,
                    GameRulesetVersionRecord.key == ruleset_version.key,
                    GameParticipantRecord.global_game_sequence < participant.global_game_sequence,
                    GameRecord.status.in_(("lobby", "active", "completed")),
                    RulesetRatingLedgerRecord.id.is_(None),
                )
                .limit(1)
            )
            if unresolved_ruleset is not None:
                return False
        return True

    async def _apply_rating_settlement(
        self,
        session: AsyncSession,
        game: GameRecord,
        participants: list[GameParticipantRecord],
    ) -> dict[str, Any]:
        rating_changes: list[dict[str, Any]] = []
        rating_confidence = confidence_model(game.rating_confidence_model)
        played_at = game.completed_at or datetime.now(UTC)
        ordered = sorted(participants, key=lambda item: str(item.player_id))
        ruleset_version = await session.get(GameRulesetVersionRecord, game.game_ruleset_version_id)
        policy = await session.get(TournamentPolicyVersionRecord, game.tournament_policy_version_id)
        if ruleset_version is None or policy is None:
            raise RuntimeError("Game rating configuration snapshot is missing")

        if await self._rating_enabled(session, game):
            memberships: dict[UUID, TournamentMembershipRecord] = {}
            histories: dict[UUID, list[RatingHistoryEntry]] = {}
            confidence_before = {}
            for participant in ordered:
                membership = await session.scalar(
                    select(TournamentMembershipRecord)
                    .where(
                        TournamentMembershipRecord.tournament_id == game.tournament_id,
                        TournamentMembershipRecord.player_id == participant.player_id,
                    )
                    .with_for_update()
                )
                assert membership is not None
                memberships[participant.player_id] = membership
                history = await self._tournament_rating_history(
                    session, game.tournament_id, participant.player_id
                )
                histories[participant.player_id] = history
                confidence_before[participant.player_id] = rating_confidence.calculate(
                    current_rating=Decimal(membership.rating),
                    history=history,
                    as_of=played_at,
                )
            deltas = pairwise_rating_deltas(
                [
                    PairwiseRatingInput(
                        participant.player_id,
                        Decimal(memberships[participant.player_id].rating),
                        Decimal(participant.final_place),
                        confidence_before[participant.player_id].k_factor,
                    )
                    for participant in ordered
                ]
            )
            for participant in ordered:
                player_id = participant.player_id
                membership = memberships[player_id]
                before = Decimal(membership.rating)
                delta = deltas[player_id]
                after = before + delta
                after_confidence = rating_confidence.calculate(
                    current_rating=after,
                    history=[*histories[player_id], RatingHistoryEntry(delta, played_at)],
                    as_of=played_at,
                )
                membership.rating = after
                session.add(
                    RatingLedgerRecord(
                        tournament_id=game.tournament_id,
                        game_id=game.id,
                        player_id=player_id,
                        rating_before=before,
                        delta=delta,
                        rating_after=after,
                        confidence_before=confidence_before[player_id].confidence,
                        confidence_after=after_confidence.confidence,
                        k_factor=confidence_before[player_id].k_factor,
                        rating_model=rating_confidence.key,
                        played_at=played_at,
                    )
                )
                rating_changes.append(
                    self._rating_change_payload(
                        "tournament",
                        player_id,
                        before,
                        delta,
                        after,
                        confidence_before[player_id].confidence,
                        after_confidence.confidence,
                        confidence_before[player_id].k_factor,
                        Decimal(1),
                        rating_model=rating_confidence.key,
                    )
                )

        ruleset_states: dict[UUID, RulesetRatingRecord] = {}
        ruleset_histories: dict[UUID, list[RatingHistoryEntry]] = {}
        ruleset_confidence_before = {}
        for participant in ordered:
            state = await session.get(
                RulesetRatingRecord,
                (ruleset_version.key, participant.player_id),
                with_for_update=True,
            )
            if state is None:
                state = RulesetRatingRecord(
                    ruleset_key=ruleset_version.key,
                    player_id=participant.player_id,
                    rating=STARTING_RATING,
                )
                session.add(state)
                await session.flush()
            ruleset_states[participant.player_id] = state
            history = await self._ruleset_rating_history(
                session, ruleset_version.key, participant.player_id
            )
            ruleset_histories[participant.player_id] = history
            ruleset_confidence_before[participant.player_id] = rating_confidence.calculate(
                current_rating=Decimal(state.rating), history=history, as_of=played_at
            )
        tournament_weight = Decimal(str(policy.policies.get("ruleset_rating_weight", 1)))
        ruleset_deltas = pairwise_rating_deltas(
            [
                PairwiseRatingInput(
                    participant.player_id,
                    Decimal(ruleset_states[participant.player_id].rating),
                    Decimal(participant.final_place),
                    ruleset_confidence_before[participant.player_id].k_factor,
                )
                for participant in ordered
            ],
            weight=tournament_weight,
        )
        for participant in ordered:
            player_id = participant.player_id
            state = ruleset_states[player_id]
            before = Decimal(state.rating)
            delta = ruleset_deltas[player_id]
            after = before + delta
            after_confidence = rating_confidence.calculate(
                current_rating=after,
                history=[
                    *ruleset_histories[player_id],
                    RatingHistoryEntry(delta, played_at),
                ],
                as_of=played_at,
            )
            state.rating = after
            session.add(
                RulesetRatingLedgerRecord(
                    ruleset_key=ruleset_version.key,
                    tournament_id=game.tournament_id,
                    game_id=game.id,
                    player_id=player_id,
                    rating_before=before,
                    delta=delta,
                    rating_after=after,
                    confidence_before=ruleset_confidence_before[player_id].confidence,
                    confidence_after=after_confidence.confidence,
                    k_factor=ruleset_confidence_before[player_id].k_factor,
                    tournament_weight=tournament_weight,
                    rating_model=rating_confidence.key,
                    played_at=played_at,
                )
            )
            rating_changes.append(
                self._rating_change_payload(
                    "ruleset",
                    player_id,
                    before,
                    delta,
                    after,
                    ruleset_confidence_before[player_id].confidence,
                    after_confidence.confidence,
                    ruleset_confidence_before[player_id].k_factor,
                    tournament_weight,
                    ruleset_key=ruleset_version.key,
                    rating_model=rating_confidence.key,
                )
            )
            result = await session.get(GameResultRecord, (game.id, participant.player_id))
            if result is not None:
                result.settled_at = datetime.now(UTC)
        game.status = "finalized"
        game.finalized_at = datetime.now(UTC)
        return await self._finalized_event(session, game, participants, rating_changes)

    async def _tournament_rating_history(
        self, session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> list[RatingHistoryEntry]:
        rows = (
            await session.execute(
                select(RatingLedgerRecord.delta, RatingLedgerRecord.played_at)
                .where(
                    RatingLedgerRecord.tournament_id == tournament_id,
                    RatingLedgerRecord.player_id == player_id,
                )
                .order_by(RatingLedgerRecord.played_at, RatingLedgerRecord.id)
            )
        ).all()
        return [RatingHistoryEntry(Decimal(delta), played_at) for delta, played_at in rows]

    async def _ruleset_rating_history(
        self, session: AsyncSession, ruleset_key: str, player_id: UUID
    ) -> list[RatingHistoryEntry]:
        rows = (
            await session.execute(
                select(RulesetRatingLedgerRecord.delta, RulesetRatingLedgerRecord.played_at)
                .where(
                    RulesetRatingLedgerRecord.ruleset_key == ruleset_key,
                    RulesetRatingLedgerRecord.player_id == player_id,
                )
                .order_by(
                    RulesetRatingLedgerRecord.played_at,
                    RulesetRatingLedgerRecord.id,
                )
            )
        ).all()
        return [RatingHistoryEntry(Decimal(delta), played_at) for delta, played_at in rows]

    def _rating_change_payload(
        self,
        scope: str,
        player_id: UUID,
        before: Decimal,
        delta: Decimal,
        after: Decimal,
        confidence_before: Decimal,
        confidence_after: Decimal,
        k_factor: Decimal,
        weight: Decimal,
        *,
        ruleset_key: str | None = None,
        rating_model: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": scope,
            "player_id": str(player_id),
            "before": before,
            "delta": delta,
            "after": after,
            "confidence_before": confidence_before,
            "confidence_after": confidence_after,
            "k_factor": k_factor,
            "weight": weight,
            "model": rating_model,
        }
        if ruleset_key is not None:
            payload["ruleset_key"] = ruleset_key
        return payload

    async def _finalized_event(
        self,
        session: AsyncSession,
        game: GameRecord,
        participants: list[GameParticipantRecord],
        rating_changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._event(
            session,
            game.id,
            "game_finalized",
            {
                "places": [
                    {
                        "participant_id": str(item.id),
                        "place": item.final_place,
                        "score": item.score,
                    }
                    for item in sorted(participants, key=lambda item: item.final_place or 99)
                ],
                "rating_changes": rating_changes,
            },
        )

    @staticmethod
    async def _has_unresolved_appeals(session: AsyncSession, game_id: UUID) -> bool:
        appeal_id = await session.scalar(
            select(AppealRecord.id)
            .where(
                AppealRecord.game_id == game_id,
                AppealRecord.status.in_(
                    ("voting", "awaiting_escalation", "awaiting_commentary", "escalated")
                ),
            )
            .limit(1)
        )
        return appeal_id is not None

    async def _refresh_completed_results(
        self, session: AsyncSession, game: GameRecord
    ) -> list[GameParticipantRecord]:
        participants = list(
            (
                await session.execute(
                    select(GameParticipantRecord).where(GameParticipantRecord.game_id == game.id)
                )
            ).scalars()
        )
        ruleset = await self._ruleset(session, game)
        settings = self._settings(game)
        metrics: dict[UUID, tuple[Any, ...]] = {}
        for participant in participants:
            participant.score = (
                await session.scalar(
                    select(func.coalesce(func.sum(ScoreLedgerRecord.delta), 0)).where(
                        ScoreLedgerRecord.participant_id == participant.id
                    )
                )
            ) or 0
            correct_values = list(
                (
                    await session.execute(
                        select(PacketQuestionRecord.value)
                        .join(
                            QuestionRoundRecord,
                            QuestionRoundRecord.question_revision_id
                            == PacketQuestionRecord.question_revision_id,
                        )
                        .join(
                            GameThemeRecord,
                            and_(
                                GameThemeRecord.game_id == game.id,
                                GameThemeRecord.theme_revision_id
                                == PacketQuestionRecord.theme_revision_id,
                                GameThemeRecord.source_packet_version_id
                                == PacketQuestionRecord.packet_version_id,
                            ),
                        )
                        .join(
                            AnswerAttemptRecord,
                            AnswerAttemptRecord.round_id == QuestionRoundRecord.id,
                        )
                        .where(
                            QuestionRoundRecord.game_id == game.id,
                            AnswerAttemptRecord.participant_id == participant.id,
                            AnswerAttemptRecord.final_correct.is_(True),
                        )
                    )
                ).scalars()
            )
            metrics[participant.id] = ruleset.ranking_key(
                score=participant.score,
                correct_values=correct_values,
                parameters=settings,
            )
        ranked = sorted(
            participants,
            key=lambda item: (metrics[item.id], -item.seat),
            reverse=True,
        )
        self._assign_shared_places(ranked, metrics)
        for participant in ranked:
            result = await session.get(GameResultRecord, (game.id, participant.player_id))
            if result is not None:
                result.place = participant.final_place
                result.score = participant.score
        return ranked

    @staticmethod
    def _assign_shared_places(
        ranked: list[GameParticipantRecord], metrics: dict[UUID, tuple[Any, ...]]
    ) -> None:
        start = 0
        while start < len(ranked):
            end = start + 1
            while end < len(ranked) and metrics[ranked[end].id] == metrics[ranked[start].id]:
                end += 1
            shared_place = (Decimal(start + 1) + Decimal(end)) / Decimal(2)
            for participant in ranked[start:end]:
                participant.final_place = shared_place
            start = end

    async def _complete_after_appeal(
        self, session: AsyncSession, game: GameRecord
    ) -> list[dict[str, Any]]:
        if game.status != "completed" or await self._has_unresolved_appeals(session, game.id):
            return []
        participants = await self._refresh_completed_results(session, game)
        if len(participants) < 2:
            game.status = "finalized"
            game.finalized_at = datetime.now(UTC)
            return [await self._finalized_event(session, game, participants, [])]
        if await self._rating_dependencies_resolved(session, game, participants):
            return [await self._apply_rating_settlement(session, game, participants)]
        return [
            await self._event(
                session,
                game.id,
                "rating_deferred",
                {"reason": "earlier_rating_result_unsettled"},
            )
        ]

    async def settle_pending_ratings(self, *, limit: int = 100) -> tuple[UUID, ...]:
        async with self.database.sessions() as session:
            game_ids = tuple(
                (
                    await session.execute(
                        select(GameRecord.id)
                        .where(
                            GameRecord.status == "completed",
                        )
                        .order_by(GameRecord.created_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        settled: list[UUID] = []
        for game_id in game_ids:
            async with self.database.transaction() as session:
                game = await session.scalar(
                    select(GameRecord).where(GameRecord.id == game_id).with_for_update()
                )
                if game is None or game.status != "completed":
                    continue
                if await self._has_unresolved_appeals(session, game.id):
                    continue
                participants = list(
                    (
                        await session.execute(
                            select(GameParticipantRecord).where(
                                GameParticipantRecord.game_id == game.id
                            )
                        )
                    ).scalars()
                )
                if len(participants) < 2:
                    game.status = "finalized"
                    game.finalized_at = datetime.now(UTC)
                    self._bump(game)
                    await session.flush()
                    settled.append(game.id)
                    continue
                if not await self._rating_dependencies_resolved(session, game, participants):
                    continue
                await self._apply_rating_settlement(session, game, participants)
                self._bump(game)
                await session.flush()
                settled.append(game.id)
        return tuple(settled)

    async def _snapshot(self, session: AsyncSession, game: GameRecord) -> GameSnapshot:
        participant_rows = (
            await session.execute(
                select(GameParticipantRecord, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                .where(GameParticipantRecord.game_id == game.id)
                .order_by(GameParticipantRecord.seat)
            )
        ).all()
        participant_snapshots = tuple(
            [
                ParticipantSnapshot(
                    telegram_user_id=player.telegram_user_id,
                    display_name=player.public_nickname,
                    joined=participant.joined,
                    active=participant.active,
                    abandoned_at=participant.abandoned_at,
                    can_reconnect=(
                        game.status == "active"
                        and not participant.active
                        and participant.abandoned_at is not None
                        and not await self._has_later_game(session, participant)
                    ),
                    score=participant.score,
                    place=participant.final_place,
                    rating=await self._tournament_rating(session, game.tournament_id, player.id),
                )
                for participant, player in participant_rows
            ]
        )
        observer_rows = (
            await session.execute(
                select(GameObserverRecord, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == GameObserverRecord.player_id)
                .where(GameObserverRecord.game_id == game.id)
                .order_by(GameObserverRecord.joined_at, GameObserverRecord.id)
            )
        ).all()
        observer_snapshots = tuple(
            ObserverSnapshot(
                telegram_user_id=player.telegram_user_id,
                display_name=player.public_nickname,
                active=observer.active,
                joined_at=observer.joined_at,
            )
            for observer, player in observer_rows
        )
        question: QuestionSnapshot | None = None
        if game.current_round_id is not None:
            round_record = await session.get(QuestionRoundRecord, game.current_round_id)
            if round_record is not None:
                revision = await session.get(
                    QuestionRevisionRecord, round_record.question_revision_id
                )
                placement = await self._placement(session, game, round_record)
                state_rows = (
                    await session.execute(
                        select(PlayerQuestionStateRecord, PlayerRecord.telegram_user_id)
                        .join(
                            GameParticipantRecord,
                            GameParticipantRecord.id == PlayerQuestionStateRecord.participant_id,
                        )
                        .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                        .where(PlayerQuestionStateRecord.round_id == round_record.id)
                    )
                ).all()
                attempt_rows = (
                    await session.execute(
                        select(AnswerAttemptRecord, PlayerRecord.telegram_user_id)
                        .join(
                            GameParticipantRecord,
                            GameParticipantRecord.id == AnswerAttemptRecord.participant_id,
                        )
                        .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                        .where(AnswerAttemptRecord.round_id == round_record.id)
                        .order_by(AnswerAttemptRecord.attempt_number)
                    )
                ).all()
                buzzer_id = None
                if game.accepted_buzzer_id:
                    buzzer = await session.get(GameParticipantRecord, game.accepted_buzzer_id)
                    if buzzer:
                        buzzer_id = await self._telegram_id(session, buzzer.player_id)
                assert revision is not None and placement is not None
                tokens = announcement_tokens(
                    revision.text, self._settings(game).question_token_target_chars
                )
                revealed_token_count = min(game.question_token_index, len(tokens))
                visible_text = " ".join(tokens[:revealed_token_count])
                if round_record.status == "completed":
                    visible_text = revision.text
                    revealed_token_count = len(tokens)
                question = QuestionSnapshot(
                    round_id=round_record.id,
                    sequence=round_record.sequence,
                    value=placement.value,
                    text=visible_text,
                    form=revision.form,
                    attempted_player_ids=tuple(
                        player_id for state, player_id in state_rows if state.attempted
                    ),
                    eligible_player_ids=tuple(
                        player_id for state, player_id in state_rows if state.eligible
                    ),
                    accepted_buzzer_id=buzzer_id,
                    revealed_answer=(
                        revision.answer if round_record.status == "completed" else None
                    ),
                    commentary=(
                        revision.commentary if round_record.status == "completed" else None
                    ),
                    revealed_token_count=revealed_token_count,
                    total_token_count=len(tokens),
                    fully_announced=revealed_token_count >= len(tokens),
                    attempts=tuple(
                        AnswerAttemptSnapshot(
                            id=attempt.id,
                            attempt_number=attempt.attempt_number,
                            player_id=player_id,
                            submitted_answer=attempt.submitted_answer,
                            timed_out=attempt.timed_out,
                            original_correct=attempt.original_correct,
                            final_correct=attempt.final_correct,
                        )
                        for attempt, player_id in attempt_rows
                    ),
                )
        ruleset_version = await session.get(GameRulesetVersionRecord, game.game_ruleset_version_id)
        assert ruleset_version is not None
        appeal_snapshot: AppealSnapshot | None = None
        appeal_row = (
            await session.execute(
                select(AppealRecord, AnswerAttemptRecord, PlayerRecord.telegram_user_id)
                .join(AnswerAttemptRecord, AnswerAttemptRecord.id == AppealRecord.target_attempt_id)
                .join(
                    GameParticipantRecord,
                    GameParticipantRecord.id == AnswerAttemptRecord.participant_id,
                )
                .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                .where(
                    AppealRecord.game_id == game.id,
                    AppealRecord.status.in_(
                        ("voting", "awaiting_escalation", "awaiting_commentary", "escalated")
                    ),
                )
                .order_by(AppealRecord.created_at.desc())
                .limit(1)
            )
        ).first()
        if appeal_row is not None:
            appeal, target_attempt, target_player_id = appeal_row
            approvals, rejections = await self._vote_tally(session, appeal.id)
            appeal_snapshot = AppealSnapshot(
                id=appeal.id,
                round_id=appeal.round_id,
                appellant_participant_id=appeal.appellant_participant_id,
                target_attempt_id=appeal.target_attempt_id,
                target_player_id=target_player_id,
                submitted_answer=target_attempt.submitted_answer or "",
                kind=appeal.kind,
                status=appeal.status,
                voting_rule=appeal.voting_rule,
                electorate_size=appeal.electorate_size,
                approvals=approvals,
                rejections=rejections,
                vote_deadline=appeal.vote_deadline,
                escalation_enabled=appeal.escalation_enabled,
                escalation_deadline=appeal.escalation_deadline,
                commentary_deadline=appeal.commentary_deadline,
                ticket_expires_at=appeal.ticket_expires_at,
            )
        return GameSnapshot(
            id=game.id,
            version=game.version,
            status=game.status,
            phase=game.phase,
            paused=game.paused,
            packet_version_ids=tuple(
                UUID(value) for value in game.assignment_plan["packet_version_ids"]
            ),
            tournament_id=game.tournament_id,
            game_ruleset=ruleset_version.key,
            game_ruleset_version=ruleset_version.version,
            rating_confidence_model=game.rating_confidence_model,
            rating_pending=game.status == "completed" and len(participant_rows) >= 2,
            participants=participant_snapshots,
            observers=observer_snapshots,
            question=question,
            appeal=appeal_snapshot,
            buzz_deadline=game.buzz_deadline,
            answer_deadline=game.answer_deadline,
            progression_deadline=game.progression_deadline,
            progression_stage=game.progression_stage,
            join_deadline=game.join_deadline,
            pause_abandonment_deadline=game.pause_abandonment_deadline,
            settings=self._settings(game),
        )

    @staticmethod
    def _settings(game: GameRecord) -> GameSettings:
        return GameSettings.from_mapping(game.assignment_plan["parameters"])

    async def _ruleset(self, session: AsyncSession, game: GameRecord):
        version = await session.get(GameRulesetVersionRecord, game.game_ruleset_version_id)
        if version is None:
            raise RuntimeError("Game ruleset version is missing")
        return self.rulesets.get(version.key, version.version)

    @staticmethod
    async def _rating_enabled(session: AsyncSession, game: GameRecord) -> bool:
        policy = await session.get(TournamentPolicyVersionRecord, game.tournament_policy_version_id)
        tournament_type = await session.get(
            TournamentTypeVersionRecord, game.tournament_type_version_id
        )
        if policy is None or tournament_type is None:
            raise RuntimeError("Tournament configuration snapshot is missing")
        return bool(
            tournament_type.rules.get("rated", False)
            and policy.policies.get("rating_enabled", False)
        )

    @staticmethod
    async def _tournament_rating(
        session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> Decimal:
        membership = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
        return Decimal(membership.rating) if membership is not None else STARTING_RATING

    @staticmethod
    async def _locked_game(session: AsyncSession, game_id: UUID) -> GameRecord:
        game = await session.scalar(
            select(GameRecord).where(GameRecord.id == game_id).with_for_update()
        )
        if game is None:
            raise LookupError("Game not found")
        return game

    @staticmethod
    async def _registered_player(
        session: AsyncSession, participant: ParticipantInput
    ) -> PlayerRecord:
        player = await session.scalar(
            select(PlayerRecord)
            .where(PlayerRecord.telegram_user_id == participant.telegram_user_id)
            .with_for_update()
        )
        if player is None or player.status != "active" or player.public_nickname is None:
            raise PermissionError("Completed player registration is required")
        return player

    @staticmethod
    async def _participant(
        session: AsyncSession,
        game_id: UUID,
        telegram_user_id: int,
        *,
        require_active: bool = True,
    ) -> GameParticipantRecord:
        participant = await session.scalar(
            select(GameParticipantRecord)
            .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
            .where(
                GameParticipantRecord.game_id == game_id,
                PlayerRecord.telegram_user_id == telegram_user_id,
            )
            .with_for_update()
        )
        if participant is None:
            raise PermissionError("Player is not a participant")
        if require_active and not participant.active:
            raise PermissionError("Player abandoned this game; reconnect first")
        return participant

    @staticmethod
    async def _observer(
        session: AsyncSession, game_id: UUID, telegram_user_id: int
    ) -> GameObserverRecord:
        observer = await session.scalar(
            select(GameObserverRecord)
            .join(PlayerRecord, PlayerRecord.id == GameObserverRecord.player_id)
            .where(
                GameObserverRecord.game_id == game_id,
                GameObserverRecord.active.is_(True),
                PlayerRecord.telegram_user_id == telegram_user_id,
            )
            .with_for_update()
        )
        if observer is None:
            raise PermissionError("Player is not an active observer")
        return observer

    @staticmethod
    async def _fresh_plan_claims(
        session: AsyncSession, player_id: UUID, plan: dict[str, Any]
    ) -> list[tuple[str, UUID, UUID]]:
        claims = [
            (
                str(claim["namespace"]),
                UUID(str(claim["identity"])),
                UUID(str(unit["packet_version_id"])),
            )
            for unit in plan.get("play_units", [])
            if isinstance(unit, dict)
            for claim in unit.get("claims", [])
            if isinstance(claim, dict)
        ]
        seen = set(
            (
                await session.execute(
                    select(
                        PlayerExposureClaimRecord.claim_namespace,
                        PlayerExposureClaimRecord.claim_id,
                    ).where(
                        PlayerExposureClaimRecord.player_id == player_id,
                        PlayerExposureClaimRecord.state == "burnt",
                    )
                )
            ).tuples()
        )
        return [claim for claim in claims if claim[:2] not in seen]

    @staticmethod
    async def _stop_other_observations(
        session: AsyncSession, player_id: UUID, participant_game_id: UUID
    ) -> None:
        observers = tuple(
            (
                await session.execute(
                    select(GameObserverRecord)
                    .where(
                        GameObserverRecord.player_id == player_id,
                        GameObserverRecord.game_id != participant_game_id,
                        GameObserverRecord.active.is_(True),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        if not observers:
            return
        now = datetime.now(UTC)
        observed_game_ids = [observer.game_id for observer in observers]
        for observer in observers:
            observer.active = False
            observer.left_at = now
        reservations = (
            await session.execute(
                select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.player_id == player_id,
                    PlayerExposureClaimRecord.game_id.in_(observed_game_ids),
                    PlayerExposureClaimRecord.state == "reserved",
                )
            )
        ).scalars()
        for claim in reservations:
            claim.state = "released"
            claim.released_at = now

    @staticmethod
    async def _events(session: AsyncSession, game_id: UUID) -> tuple[dict[str, Any], ...]:
        records = (
            await session.execute(
                select(GameEventRecord)
                .where(GameEventRecord.game_id == game_id)
                .order_by(GameEventRecord.sequence)
            )
        ).scalars()
        return tuple(
            {
                "sequence": record.sequence,
                "kind": record.kind,
                "payload": record.payload,
                "created_at": record.created_at,
            }
            for record in records
        )

    @staticmethod
    async def _telegram_id(session: AsyncSession, player_id: UUID) -> int:
        telegram_id = await session.scalar(
            select(PlayerRecord.telegram_user_id).where(PlayerRecord.id == player_id)
        )
        assert telegram_id is not None
        return telegram_id

    @staticmethod
    async def _event(
        session: AsyncSession, game_id: UUID, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if kind in {"question_token_revealed", "player_buzzed", "answer_judged"}:
            game = await session.get(GameRecord, game_id)
            if game is not None and game.current_round_id is not None:
                payload = {**payload, "round_id": str(game.current_round_id)}
                if kind in {"player_buzzed", "answer_judged"}:
                    current = await session.get(QuestionRoundRecord, game.current_round_id)
                    revision = await session.get(
                        QuestionRevisionRecord, current.question_revision_id
                    )
                    tokens = announcement_tokens(
                        revision.text,
                        PersistentGameService._settings(game).question_token_target_chars,
                    )
                    payload["revealed_text"] = " ".join(tokens[: game.question_token_index])
                    if kind == "player_buzzed":
                        payload["answer_deadline"] = game.answer_deadline
        payload = PersistentGameService._json_payload(payload)
        sequence = (
            await session.scalar(
                select(func.max(GameEventRecord.sequence)).where(GameEventRecord.game_id == game_id)
            )
        ) or 0
        record = GameEventRecord(game_id=game_id, sequence=sequence + 1, kind=kind, payload=payload)
        session.add(record)
        await TransactionalOutbox.enqueue_game_event(
            session,
            game_id=game_id,
            sequence=record.sequence,
            kind=kind,
            parameters=payload,
        )
        return {"sequence": record.sequence, "kind": kind, "payload": payload}

    @staticmethod
    def _json_payload(value: Any) -> Any:
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, (datetime, UUID)):
            return str(value) if isinstance(value, UUID) else value.isoformat()
        if isinstance(value, dict):
            return {key: PersistentGameService._json_payload(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [PersistentGameService._json_payload(item) for item in value]
        return value

    @staticmethod
    def _bump(game: GameRecord) -> None:
        game.version += 1
        game.last_activity_at = datetime.now(UTC)

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)
