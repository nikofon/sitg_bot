from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.game_deadlines import INITIAL_PLAYER_JOIN_TIMEOUT
from sitg_bot.domain.game_rulesets import (
    DEFAULT_RULESETS,
    GameRulesetRegistry,
    PlayUnit,
    RulesetParameters,
    ValidationViolation,
)
from sitg_bot.domain.rating import DEFAULT_CONFIDENCE_MODEL_KEY, confidence_model
from sitg_bot.services.classic import ClassicService, is_chair
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.persistent_game import GameSnapshot, ParticipantInput, PersistentGameService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.ruleset_content import (
    DEFAULT_CONTENT_ADAPTERS,
    PacketSelection,
    RulesetContentRegistry,
)
from sitg_bot.services.tournaments import TournamentContext, TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameObserverRecord,
    GamePacketVersionRecord,
    GameParticipantRecord,
    GameRecord,
    GameThemeRecord,
    LogicalPacketRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerBlacklistRecord,
    PlayerExposureClaimRecord,
    PlayerRecord,
    PregameLobbyEventRecord,
    PregameLobbyMemberRecord,
    PregameLobbyPacketRecord,
    PregameLobbyRecord,
    QuestionRevisionRecord,
    QuestionRoundRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
)


@dataclass(frozen=True, slots=True)
class LobbyMemberSnapshot:
    telegram_user_id: int | None
    display_name: str
    join_order: int
    ready: bool
    validation_violations: tuple[dict[str, object], ...]
    role: str = "player"
    fresh_content_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class LobbySnapshot:
    id: UUID
    version: int
    tournament_id: UUID
    invitation_code: str
    status: str
    max_players: int
    expires_at: datetime
    game_id: UUID | None
    searching: bool
    hybrid_matchmaking_available: bool
    search_started_at: datetime | None
    merged_into_lobby_id: UUID | None
    other_searching_lobby_count: int
    other_searching_player_count: int
    settings: RulesetParameters
    selected_packets: tuple[PacketSuggestion, ...]
    validation_violations: tuple[dict[str, object], ...]
    available_play_unit_count: int
    members: tuple[LobbyMemberSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PacketSuggestion:
    packet_id: UUID
    packet_version_id: UUID
    name: str
    total_play_unit_count: int = 0
    fresh_play_unit_count: int = 0
    validation_violations: tuple[dict[str, object], ...] = ()
    year: int | None = None
    published_at: datetime | None = None
    lead_author: str | None = None
    authors: tuple[str, ...] = ()
    playable_for_all: bool = False


@dataclass(frozen=True, slots=True)
class LobbyStartResult:
    started: bool
    lobby: LobbySnapshot
    game: GameSnapshot | None


@dataclass(frozen=True, slots=True)
class MatchmakingMerge:
    surviving_lobby_id: UUID
    discarded_lobby_id: UUID


class LobbyReadinessError(ValueError):
    """A stable, player-facing reason for rejecting readiness."""

    def __init__(self, reason: str, details: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class InvitationMatchmakingService:
    """Persistent invitation and hybrid lobby assembly and packet selection."""

    def __init__(
        self,
        database: Database,
        *,
        lobby_lifetime: timedelta = timedelta(hours=24),
        initial_rating_tolerance: int = 100,
        rating_tolerance_step: int = 50,
        initial_reputation_tolerance: int = 5,
        reputation_tolerance_step: int = 5,
        tolerance_step_interval: timedelta = timedelta(seconds=30),
        maximum_tolerance: int | None = None,
        rulesets: GameRulesetRegistry = DEFAULT_RULESETS,
        content_adapters: RulesetContentRegistry = DEFAULT_CONTENT_ADAPTERS,
        rating_confidence_model: str = DEFAULT_CONFIDENCE_MODEL_KEY,
    ) -> None:
        tolerances = (
            initial_rating_tolerance,
            rating_tolerance_step,
            initial_reputation_tolerance,
            reputation_tolerance_step,
        )
        if min(tolerances) < 0 or (maximum_tolerance is not None and maximum_tolerance < 0):
            raise ValueError("Matchmaking tolerances cannot be negative")
        if tolerance_step_interval <= timedelta(0):
            raise ValueError("The matchmaking tolerance interval must be positive")
        self.database = database
        self.lobby_lifetime = lobby_lifetime
        self.initial_rating_tolerance = initial_rating_tolerance
        self.rating_tolerance_step = rating_tolerance_step
        self.initial_reputation_tolerance = initial_reputation_tolerance
        self.reputation_tolerance_step = reputation_tolerance_step
        self.tolerance_step_interval = tolerance_step_interval
        self.maximum_tolerance = maximum_tolerance
        self.rulesets = rulesets
        self.tournaments = TournamentService(database, rulesets=rulesets)
        self.content_adapters = content_adapters
        self.rating_confidence_model = confidence_model(rating_confidence_model).key

    async def create_lobby(
        self,
        creator: ParticipantInput,
        *,
        max_players: int | None = None,
        tournament_id: UUID,
    ) -> LobbySnapshot:
        if max_players is not None and (
            isinstance(max_players, bool) or not isinstance(max_players, int)
            or not 1 <= max_players <= 12
        ):
            raise ValueError("A lobby supports between one and twelve players")
        async with self.database.transaction() as session:
            context = await self.tournaments.context(session, tournament_id)
            if not context.assembly_open:
                raise ValueError("tournament_stage_closed")
            type_maximum = int(context.type_rules.get("maximum_players", 12))
            type_minimum = int(context.type_rules.get("minimum_players", 1))
            settings = context.settings
            if context.type_key == "classic":
                max_players = max_players or min(12, type_maximum)
            else:
                default_maximum = int(settings.to_dict()["maximum_players"])
                if max_players is not None and max_players != default_maximum:
                    if "maximum_players" not in context.mutable_parameters:
                        raise PermissionError("Tournament locks parameter: maximum_players")
                    settings = settings.updated({"maximum_players": max_players})
                max_players = int(settings.to_dict()["maximum_players"])
            if not type_minimum <= max_players <= min(12, type_maximum):
                raise ValueError(
                    f"Lobby capacity must be between {type_minimum} and {min(12, type_maximum)}"
                )
            player = await self._registered_player(session, creator)
            membership = await session.get(
                TournamentMembershipRecord, (context.tournament_id, player.id)
            )
            if membership is None or membership.status != "active":
                raise PermissionError("Active tournament membership is required")
            await self._require_not_manager(session, context.tournament_id, player.id)
            await self._require_available(session, player.id)
            lobby = PregameLobbyRecord(
                tournament_id=context.tournament_id,
                tournament_type_version_id=context.type_version_id,
                game_ruleset_version_id=context.game_ruleset_version_id,
                tournament_policy_version_id=context.policy_version_id,
                creator_player_id=player.id,
                invitation_code=secrets.token_urlsafe(24),
                max_players=max_players,
                expires_at=datetime.now(UTC) + self.lobby_lifetime,
                settings=settings.to_dict(),
            )
            session.add(lobby)
            await session.flush()
            session.add(
                PregameLobbyMemberRecord(lobby_id=lobby.id, player_id=player.id, join_order=1)
            )
            await self._event(
                session,
                lobby.id,
                "lobby_created",
                {"creator_player_id": str(player.id)},
            )
            await session.flush()
            return await self._snapshot(session, lobby)

    async def join(
        self,
        invitation_code: str,
        player_input: ParticipantInput,
        *,
        role: str = "player",
        confirm_fresh: bool = False,
    ) -> LobbySnapshot:
        if role not in {"player", "observer"}:
            raise ValueError("Lobby role must be player or observer")
        async with self.database.transaction() as session:
            lobby = await session.scalar(
                select(PregameLobbyRecord)
                .where(PregameLobbyRecord.invitation_code == invitation_code)
                .with_for_update()
            )
            if lobby is None:
                raise LookupError("Invitation not found")
            await self._require_open(session, lobby)
            player = await self._registered_player(session, player_input)
            membership = await session.get(
                TournamentMembershipRecord, (lobby.tournament_id, player.id)
            )
            if membership is None or membership.status != "active":
                raise PermissionError("Active tournament membership is required")
            if role == "player":
                await self._require_not_manager(session, lobby.tournament_id, player.id)
            context = await self.tournaments.context(session, lobby.tournament_id)
            if role == "observer" and context.observing_policy == "forbidden":
                raise PermissionError("Observing is forbidden by tournament policy")
            existing = await session.scalar(
                select(PregameLobbyMemberRecord).where(
                    PregameLobbyMemberRecord.lobby_id == lobby.id,
                    PregameLobbyMemberRecord.player_id == player.id,
                )
            )
            if existing is not None and existing.active:
                return await self._snapshot(session, lobby)
            await self._require_available(session, player.id)
            member_count = await self._active_member_count(session, lobby.id)
            if role == "player" and member_count >= lobby.max_players:
                raise ValueError("Lobby is full")
            if existing is None:
                member = PregameLobbyMemberRecord(
                    lobby_id=lobby.id,
                    player_id=player.id,
                    join_order=(
                        await session.scalar(
                            select(func.max(PregameLobbyMemberRecord.join_order)).where(
                                PregameLobbyMemberRecord.lobby_id == lobby.id
                            )
                        )
                        or 0
                    )
                    + 1,
                    role=role,
                    fresh_content_confirmed=confirm_fresh if role == "observer" else False,
                )
                session.add(member)
            else:
                existing.active = True
                existing.ready = False
                existing.validation_violations = []
                existing.role = role
                existing.fresh_content_confirmed = confirm_fresh if role == "observer" else False
            await session.flush()
            await self._refresh_validation(session, lobby)
            await self._event(
                session,
                lobby.id,
                "player_joined",
                {"player_id": str(player.id), "role": role},
            )
            if role == "player" and member_count + 1 >= lobby.max_players and lobby.searching:
                lobby.searching = False
                lobby.search_started_at = None
                await self._event(session, lobby.id, "matchmaking_search_completed", {})
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def set_role(
        self,
        lobby_id: UUID,
        telegram_user_id: int,
        role: str,
        *,
        confirm_fresh: bool = False,
        expected_version: int | None = None,
    ) -> LobbySnapshot:
        if role not in {"player", "observer"}:
            raise ValueError("Lobby role must be player or observer")
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            member = await self._member(session, lobby.id, telegram_user_id)
            context = await self.tournaments.context(session, lobby.tournament_id)
            if role == "observer" and context.observing_policy == "forbidden":
                raise PermissionError("Observing is forbidden by tournament policy")
            if role == "player" and member.role != "player":
                await self._require_not_manager(session, lobby.tournament_id, member.player_id)
                if await self._active_member_count(session, lobby.id) >= lobby.max_players:
                    raise ValueError("Lobby is full")
                await self._require_no_active_game(session, member.player_id)
            member.role = role
            member.ready = False
            member.fresh_content_confirmed = confirm_fresh if role == "observer" else False
            await self._refresh_validation(session, lobby)
            await self._event(
                session,
                lobby.id,
                "role_changed",
                {"player_id": str(member.player_id), "role": role},
            )
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    @staticmethod
    async def _require_not_manager(
        session: AsyncSession, tournament_id: UUID, player_id: UUID
    ) -> None:
        manager = await session.get(TournamentManagerRecord, (tournament_id, player_id))
        if manager is not None and manager.revoked_at is None:
            raise PermissionError("Tournament managers cannot play in their tournament")

    async def invite(
        self, lobby_id: UUID, telegram_user_id: int, username: str, *, expected_version: int
    ) -> dict[str, bool]:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._member(session, lobby_id, telegram_user_id)
            recipient = await session.scalar(
                select(PlayerRecord).where(
                    func.lower(PlayerRecord.telegram_username) == username[1:].lower(),
                    PlayerRecord.status == "active",
                    PlayerRecord.registration_completed_at.is_not(None),
                    PlayerRecord.telegram_user_id.is_not(None),
                )
            )
            if recipient is None:
                raise LookupError("Registered Telegram user not found")
            sender = await session.scalar(
                select(PlayerRecord).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            context = await self.tournaments.context(session, lobby.tournament_id)
            tournament = await session.get(TournamentRecord, lobby.tournament_id)
            await TransactionalOutbox.enqueue(
                session,
                topic="telegram.lobby.notice",
                deduplication_key=f"lobby:{lobby_id}:invite:{sender.id}:{recipient.id}:{lobby.version}",
                partition_key=f"telegram:chat:{recipient.telegram_user_id}",
                aggregate_type="lobby",
                aggregate_id=lobby_id,
                aggregate_sequence=lobby.version,
                payload={
                    "recipient_telegram_user_id": recipient.telegram_user_id,
                    "locale": recipient.preferred_locale,
                    "kind": "invitation",
                    "sender": sender.public_nickname,
                    "tournament": tournament.name,
                    "invitation_code": lobby.invitation_code,
                    "allow_observer": context.observing_policy != "forbidden",
                },
            )
            return {"sent": True}

    async def select_packet(
        self,
        lobby_id: UUID,
        telegram_user_id: int,
        packet_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> LobbySnapshot:
        try:
            return await self._select_packet(
                lobby_id, telegram_user_id, packet_id, expected_version=expected_version
            )
        except (PermissionError, LookupError, ValueError) as error:
            # Only an authorized selection of a discoverable tournament packet is broadcast.
            async with self.database.transaction() as session:
                lobby = await self._locked_lobby(session, lobby_id)
                await self._require_open(session, lobby)
                await self._require_creator(session, lobby, telegram_user_id)
                assignment = await session.scalar(
                    select(TournamentPacketAssignmentRecord).where(
                        TournamentPacketAssignmentRecord.tournament_id == lobby.tournament_id,
                        TournamentPacketAssignmentRecord.packet_id == packet_id,
                        TournamentPacketAssignmentRecord.status == "active",
                    )
                )
                if assignment is not None and await self.tournaments.has_assignment_access(
                    session, assignment, lobby.creator_player_id, "discoverable"
                ):
                    version = await session.scalar(
                        select(PacketVersionRecord)
                        .where(
                            PacketVersionRecord.packet_id == packet_id,
                            PacketVersionRecord.state == "published",
                        )
                        .order_by(PacketVersionRecord.version_number.desc())
                        .limit(1)
                    )
                    if version is not None:
                        reason = (
                            "stale_write"
                            if isinstance(error, StaleWriteError)
                            else "packet_not_playable"
                            if isinstance(error, PermissionError)
                            else "packet_unavailable"
                        )
                        await self._event(
                            session,
                            lobby_id,
                            "packet_rejected",
                            {"packet_name": version.name, "reason": reason},
                        )
            raise

    async def _select_packet(
        self,
        lobby_id: UUID,
        telegram_user_id: int,
        packet_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            packet = await session.get(LogicalPacketRecord, packet_id)
            if packet is None or packet.retired_at is not None:
                raise LookupError("Packet not found")
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == lobby.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == packet.id,
                    TournamentPacketAssignmentRecord.status == "active",
                )
            )
            if assignment is None or not await self.tournaments.has_assignment_access(
                session, assignment, lobby.creator_player_id, "playable"
            ):
                raise PermissionError("Packet is not game-eligible in this tournament")
            version = (
                await session.get(PacketVersionRecord, assignment.adopted_version_id)
                if assignment.adopted_version_id is not None
                else await session.scalar(
                    select(PacketVersionRecord)
                    .where(
                        PacketVersionRecord.packet_id == packet.id,
                        PacketVersionRecord.state == "published",
                    )
                    .order_by(PacketVersionRecord.version_number.desc())
                    .limit(1)
                )
            )
            if version is None:
                raise LookupError("Packet has no published version")
            selected = await self._selected_packets(session, lobby.id)
            context = await self.tournaments.context(session, lobby.tournament_id)
            classic_notice = {}
            if context.type_key == "classic":
                if any(item.packet_id != packet.id for item in selected):
                    raise ValueError("Classic games use exactly one round packet")
                prescribed = await ClassicService.prescribed_match(
                    session,
                    lobby.tournament_id,
                    assignment.id,
                    [lobby.creator_player_id],
                    exact=False,
                )
                names = []
                for seat in prescribed.seats:
                    player = None if is_chair(seat) else await session.get(PlayerRecord, UUID(seat))
                    names.append("Chair" if player is None else player.public_nickname)
                classic_notice = {
                    "classic_players": names,
                    "classic_solo": len(prescribed.seats) == 1,
                }
            if not any(item.packet_id == packet.id for item in selected):
                session.add(
                    PregameLobbyPacketRecord(
                        lobby_id=lobby.id,
                        packet_id=packet.id,
                        packet_version_id=version.id,
                        assignment_id=assignment.id,
                        selection_order=(
                            max((item.selection_order for item in selected), default=0) + 1
                        ),
                    )
                )
            members = await self._all_active_members(session, lobby.id)
            for member in members:
                member.ready = False
                if member.role == "observer":
                    member.fresh_content_confirmed = False
            await session.flush()
            await self._refresh_validation(session, lobby)
            await self._event(
                session,
                lobby.id,
                "packet_selected",
                {
                    "packet_id": str(packet.id),
                    "packet_version_id": str(version.id),
                    "packet_name": version.name,
                    **classic_notice,
                },
            )
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def remove_packet(
        self,
        lobby_id: UUID,
        telegram_user_id: int,
        packet_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            packet_name = await session.scalar(
                select(PacketVersionRecord.name)
                .join(
                    PregameLobbyPacketRecord,
                    PregameLobbyPacketRecord.packet_version_id == PacketVersionRecord.id,
                )
                .where(
                    PregameLobbyPacketRecord.lobby_id == lobby.id,
                    PregameLobbyPacketRecord.packet_id == packet_id,
                )
            )
            result = await session.execute(
                delete(PregameLobbyPacketRecord).where(
                    PregameLobbyPacketRecord.lobby_id == lobby.id,
                    PregameLobbyPacketRecord.packet_id == packet_id,
                )
            )
            if not result.rowcount:
                raise LookupError("Packet is not selected")
            await self._clear_readiness(session, lobby.id)
            await self._refresh_validation(session, lobby)
            await self._event(
                session,
                lobby.id,
                "packet_removed",
                {"packet_id": str(packet_id), "packet_name": packet_name},
            )
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def set_settings(
        self,
        lobby_id: UUID,
        telegram_user_id: int,
        changes: dict[str, object],
        *,
        expected_version: int | None = None,
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            policy = await session.get(
                TournamentPolicyVersionRecord, lobby.tournament_policy_version_id
            )
            assert policy is not None
            locked = set(changes) - set(policy.player_mutable_parameters)
            if locked:
                raise PermissionError(f"Tournament locks parameter: {sorted(locked)[0]}")
            context = await self.tournaments.context(session, lobby.tournament_id)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            settings = ruleset.parameters(lobby.settings).updated(changes)
            if settings.to_dict() == lobby.settings:
                return await self._snapshot(session, lobby)
            changed = {
                key: value
                for key, value in settings.to_dict().items()
                if lobby.settings.get(key) != value
            }
            lobby.settings = settings.to_dict()
            if context.type_key != "classic":
                maximum = int(lobby.settings["maximum_players"])
                if await self._active_member_count(session, lobby.id) > maximum:
                    raise ValueError("Maximum players cannot be below the current lobby size")
                lobby.max_players = maximum
                if await self._active_member_count(session, lobby.id) >= maximum:
                    lobby.searching = False
                    lobby.search_started_at = None
            members = await self._all_active_members(session, lobby.id)
            for member in members:
                member.ready = False
                if member.role == "observer":
                    member.fresh_content_confirmed = False
            await self._event(session, lobby.id, "settings_changed", changed)
            await self._refresh_validation(session, lobby)
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def set_ready(
        self,
        lobby_id: UUID,
        telegram_user_id: int,
        *,
        ready: bool = True,
        expected_version: int | None = None,
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            if lobby.status != "assembling":
                raise LobbyReadinessError("closed")
            if datetime.now(UTC) >= lobby.expires_at:
                raise LobbyReadinessError("expired")
            member = await self._member(session, lobby.id, telegram_user_id)
            if member.role != "player":
                raise LobbyReadinessError("observer")
            if ready:
                if not await self._selected_packets(session, lobby.id):
                    raise LobbyReadinessError("packet_required")
                await self._refresh_validation(session, lobby)
                violations = member.validation_violations or lobby.validation
                if violations:
                    violation = violations[0]
                    raise LobbyReadinessError(str(violation["code"]), violation.get("details", {}))
            if member.ready == ready:
                return await self._snapshot(session, lobby)
            member.ready = ready
            members = await self._active_members(session, lobby.id)
            player = await session.get(PlayerRecord, member.player_id)
            await self._event(
                session,
                lobby.id,
                "readiness_changed",
                {
                    "player_id": str(member.player_id),
                    "player_name": player.public_nickname,
                    "ready": ready,
                    "ready_count": sum(item.ready for item in members),
                    "player_count": len(members),
                },
            )
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def leave(
        self, lobby_id: UUID, telegram_user_id: int, *, expected_version: int | None = None
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            member = await self._member(session, lobby.id, telegram_user_id)
            if member.player_id == lobby.creator_player_id:
                raise ValueError("The creator must cancel the lobby instead of leaving")
            member.active = False
            member.ready = False
            await self._refresh_validation(session, lobby)
            await self._event(
                session,
                lobby.id,
                "player_left",
                {"player_id": str(member.player_id), "role": member.role},
            )
            self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def cancel(
        self, lobby_id: UUID, telegram_user_id: int, *, expected_version: int | None = None
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            await self._event(session, lobby.id, "lobby_cancelled", {})
            await self._close_lobby(session, lobby, "cancelled")
            await session.flush()
            return await self._snapshot(session, lobby)

    async def find_players(
        self, lobby_id: UUID, telegram_user_id: int, *, expected_version: int | None = None
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            context = await self.tournaments.context(session, lobby.tournament_id)
            if not context.type_supports_hybrid_matchmaking:
                raise ValueError("Tournament type does not support hybrid matchmaking")
            if not context.hybrid_matchmaking_enabled:
                raise ValueError("Hybrid matchmaking is disabled by tournament policy")
            members = await self._active_members(session, lobby.id)
            if len(members) >= lobby.max_players:
                raise ValueError("Lobby is already full")
            selected = await self._selected_packets(session, lobby.id)
            if not selected:
                raise ValueError("Select at least one packet before finding players")
            if await self._viable_packet_plan(session, lobby, lobby, members) is None:
                raise ValueError("The lobby has no viable packet selection")
            if not lobby.searching:
                lobby.searching = True
                lobby.search_started_at = datetime.now(UTC)
                await self._event(session, lobby.id, "matchmaking_search_started", {})
                self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def cancel_search(
        self, lobby_id: UUID, telegram_user_id: int, *, expected_version: int | None = None
    ) -> LobbySnapshot:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            if lobby.searching:
                lobby.searching = False
                lobby.search_started_at = None
                await self._event(session, lobby.id, "matchmaking_search_cancelled", {})
                self._bump(lobby)
            await session.flush()
            return await self._snapshot(session, lobby)

    async def blacklist_player(
        self, blocker_player_id: UUID, blocked_telegram_user_id: int
    ) -> tuple[dict[str, object], ...]:
        async with self.database.transaction() as session:
            await self._lock_active_lobby_for_player(session, blocker_player_id)
            blocked = await session.scalar(
                select(PlayerRecord).where(
                    PlayerRecord.telegram_user_id == blocked_telegram_user_id
                )
            )
            if blocked is None or blocked.status != "active":
                raise LookupError("Player not found")
            if blocked.id == blocker_player_id:
                raise ValueError("A player cannot blacklist themselves")
            existing = await session.get(PlayerBlacklistRecord, (blocker_player_id, blocked.id))
            if existing is None:
                session.add(
                    PlayerBlacklistRecord(
                        blocker_player_id=blocker_player_id,
                        blocked_player_id=blocked.id,
                    )
                )
                await session.flush()
            return await self._blacklist_snapshot(session, blocker_player_id)

    async def unblacklist_player(
        self, blocker_player_id: UUID, blocked_telegram_user_id: int
    ) -> tuple[dict[str, object], ...]:
        async with self.database.transaction() as session:
            await self._lock_active_lobby_for_player(session, blocker_player_id)
            blocked_id = await session.scalar(
                select(PlayerRecord.id).where(
                    PlayerRecord.telegram_user_id == blocked_telegram_user_id
                )
            )
            if blocked_id is None:
                raise LookupError("Player not found")
            await session.execute(
                delete(PlayerBlacklistRecord).where(
                    PlayerBlacklistRecord.blocker_player_id == blocker_player_id,
                    PlayerBlacklistRecord.blocked_player_id == blocked_id,
                )
            )
            return await self._blacklist_snapshot(session, blocker_player_id)

    async def blacklist(self, blocker_player_id: UUID) -> tuple[dict[str, object], ...]:
        async with self.database.sessions() as session:
            return await self._blacklist_snapshot(session, blocker_player_id)

    async def match_searching(self, *, limit: int = 100) -> tuple[MatchmakingMerge, ...]:
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            lobbies = list(
                (
                    await session.execute(
                        select(PregameLobbyRecord)
                        .where(
                            PregameLobbyRecord.status == "assembling",
                            PregameLobbyRecord.searching.is_(True),
                        )
                        .order_by(
                            PregameLobbyRecord.search_started_at,
                            PregameLobbyRecord.id,
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            eligible_lobbies: list[PregameLobbyRecord] = []
            for lobby in lobbies:
                context = await self.tournaments.context(session, lobby.tournament_id)
                if context.hybrid_matchmaking_enabled:
                    eligible_lobbies.append(lobby)
                else:
                    lobby.searching = False
                    lobby.search_started_at = None
            lobbies = eligible_lobbies
            merges: list[MatchmakingMerge] = []
            while True:
                best: (
                    tuple[
                        tuple[float, float, datetime, str],
                        PregameLobbyRecord,
                        PregameLobbyRecord,
                        list[PregameLobbyPacketRecord],
                    ]
                    | None
                ) = None
                for index, first in enumerate(lobbies):
                    if not first.searching or first.status != "assembling":
                        continue
                    first_members = await self._active_members(session, first.id)
                    first_players = await self._players_for_members(session, first_members)
                    for second in lobbies[index + 1 :]:
                        if not second.searching or second.status != "assembling":
                            continue
                        if first.tournament_id != second.tournament_id:
                            continue
                        if (
                            first.tournament_type_version_id != second.tournament_type_version_id
                            or first.game_ruleset_version_id != second.game_ruleset_version_id
                            or first.tournament_policy_version_id
                            != second.tournament_policy_version_id
                        ):
                            continue
                        second_members = await self._active_members(session, second.id)
                        combined_count = len(first_members) + len(second_members)
                        survivor, discarded = self._merge_direction(first, second, combined_count)
                        if survivor is None or discarded is None:
                            continue
                        second_players = await self._players_for_members(session, second_members)
                        if await self._has_blacklist_conflict(
                            session, first_members, second_members
                        ):
                            continue
                        rating_difference = abs(
                            await self._average_tournament_rating(
                                session, first.tournament_id, first_members
                            )
                            - await self._average_tournament_rating(
                                session, second.tournament_id, second_members
                            )
                        )
                        reputation_difference = abs(
                            self._average(first_players, "reputation")
                            - self._average(second_players, "reputation")
                        )
                        if not self._within_tolerance(
                            first,
                            second,
                            now,
                            rating_difference,
                            reputation_difference,
                        ):
                            continue
                        survivor_members = (
                            first_members if survivor.id == first.id else second_members
                        )
                        discarded_members = (
                            second_members if survivor.id == first.id else first_members
                        )
                        plan = await self._viable_packet_plan(
                            session,
                            survivor,
                            discarded,
                            survivor_members + discarded_members,
                        )
                        if plan is None:
                            continue
                        score = (
                            rating_difference,
                            reputation_difference,
                            second.search_started_at or now,
                            str(second.id),
                        )
                        if best is None or score < best[0]:
                            best = (score, survivor, discarded, plan)
                if best is None:
                    break
                _, survivor, discarded, plan = best
                await self._merge_lobbies(session, survivor, discarded, plan)
                merges.append(MatchmakingMerge(survivor.id, discarded.id))
            await session.flush()
            return tuple(merges)

    async def active_lobby_id(self, player_id: UUID) -> UUID | None:
        async with self.database.sessions() as session:
            return await session.scalar(
                select(PregameLobbyMemberRecord.lobby_id)
                .where(
                    PregameLobbyMemberRecord.player_id == player_id,
                    PregameLobbyMemberRecord.active.is_(True),
                )
                .limit(1)
            )

    async def searching_lobby_ids(self) -> tuple[UUID, ...]:
        async with self.database.sessions() as session:
            return tuple(
                (
                    await session.execute(
                        select(PregameLobbyRecord.id)
                        .where(
                            PregameLobbyRecord.status == "assembling",
                            PregameLobbyRecord.searching.is_(True),
                        )
                        .order_by(PregameLobbyRecord.search_started_at)
                    )
                ).scalars()
            )

    @staticmethod
    def _merge_direction(
        first: PregameLobbyRecord,
        second: PregameLobbyRecord,
        combined_count: int,
    ) -> tuple[PregameLobbyRecord | None, PregameLobbyRecord | None]:
        ordered = sorted(
            (first, second),
            key=lambda lobby: (lobby.search_started_at or lobby.created_at, str(lobby.id)),
        )
        if combined_count <= ordered[0].max_players:
            return ordered[0], ordered[1]
        if combined_count <= ordered[1].max_players:
            return ordered[1], ordered[0]
        return None, None

    @staticmethod
    async def _players_for_members(
        session: AsyncSession, members: list[PregameLobbyMemberRecord]
    ) -> list[PlayerRecord]:
        if not members:
            return []
        return list(
            (
                await session.execute(
                    select(PlayerRecord).where(
                        PlayerRecord.id.in_([member.player_id for member in members])
                    )
                )
            ).scalars()
        )

    @staticmethod
    def _average(players: list[PlayerRecord], attribute: str) -> float:
        return sum(float(getattr(player, attribute)) for player in players) / len(players)

    @staticmethod
    async def _average_tournament_rating(
        session: AsyncSession,
        tournament_id: UUID,
        members: list[PregameLobbyMemberRecord],
    ) -> float:
        ratings = tuple(
            (
                await session.execute(
                    select(TournamentMembershipRecord.rating).where(
                        TournamentMembershipRecord.tournament_id == tournament_id,
                        TournamentMembershipRecord.player_id.in_(
                            member.player_id for member in members
                        ),
                    )
                )
            ).scalars()
        )
        return sum(ratings) / len(ratings)

    def _within_tolerance(
        self,
        first: PregameLobbyRecord,
        second: PregameLobbyRecord,
        now: datetime,
        rating_difference: float,
        reputation_difference: float,
    ) -> bool:
        rating_tolerance = min(
            self._tolerance(
                first.search_started_at,
                now,
                self.initial_rating_tolerance,
                self.rating_tolerance_step,
            ),
            self._tolerance(
                second.search_started_at,
                now,
                self.initial_rating_tolerance,
                self.rating_tolerance_step,
            ),
        )
        reputation_tolerance = min(
            self._tolerance(
                first.search_started_at,
                now,
                self.initial_reputation_tolerance,
                self.reputation_tolerance_step,
            ),
            self._tolerance(
                second.search_started_at,
                now,
                self.initial_reputation_tolerance,
                self.reputation_tolerance_step,
            ),
        )
        return (
            rating_difference <= rating_tolerance and reputation_difference <= reputation_tolerance
        )

    def _tolerance(
        self,
        started_at: datetime | None,
        now: datetime,
        initial: int,
        step: int,
    ) -> int:
        if started_at is None:
            return 0
        elapsed_steps = max(
            0,
            int((now - started_at).total_seconds() // self.tolerance_step_interval.total_seconds()),
        )
        tolerance = initial + elapsed_steps * step
        if self.maximum_tolerance is None:
            return tolerance
        return min(self.maximum_tolerance, tolerance)

    @staticmethod
    async def _has_blacklist_conflict(
        session: AsyncSession,
        first_members: list[PregameLobbyMemberRecord],
        second_members: list[PregameLobbyMemberRecord],
    ) -> bool:
        first_ids = [member.player_id for member in first_members]
        second_ids = [member.player_id for member in second_members]
        conflict = await session.scalar(
            select(PlayerBlacklistRecord.blocker_player_id)
            .where(
                or_(
                    PlayerBlacklistRecord.blocker_player_id.in_(first_ids)
                    & PlayerBlacklistRecord.blocked_player_id.in_(second_ids),
                    PlayerBlacklistRecord.blocker_player_id.in_(second_ids)
                    & PlayerBlacklistRecord.blocked_player_id.in_(first_ids),
                )
            )
            .limit(1)
        )
        return conflict is not None

    async def _viable_packet_plan(
        self,
        session: AsyncSession,
        survivor: PregameLobbyRecord,
        other: PregameLobbyRecord,
        members: list[PregameLobbyMemberRecord],
    ) -> list[PregameLobbyPacketRecord] | None:
        lobby_ids = (survivor.id,) if survivor.id == other.id else (survivor.id, other.id)
        selected: list[PregameLobbyPacketRecord] = []
        seen_packet_ids: set[UUID] = set()
        for lobby_id in lobby_ids:
            for item in await self._selected_packets(session, lobby_id):
                if item.packet_id not in seen_packet_ids:
                    selected.append(item)
                    seen_packet_ids.add(item.packet_id)
        if not selected:
            return None
        context = await self.tournaments.context(session, survivor.tournament_id)
        if not context.assembly_open:
            return None
        ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
        if len(members) > min(12, int(context.type_rules.get("maximum_players", 12))):
            return None
        minimum_packets = int(context.type_rules.get("minimum_packets", 1))
        maximum_packets = context.type_rules.get("maximum_packets")
        if len(selected) < minimum_packets or (
            maximum_packets is not None and len(selected) > int(maximum_packets)
        ):
            return None
        for item in selected:
            if (
                await self._packet_selection_violation(session, survivor.tournament_id, item)
                is not None
            ):
                return None
            assignment = await session.get(TournamentPacketAssignmentRecord, item.assignment_id)
            assert assignment is not None
            for member in members:
                if not await self.tournaments.has_assignment_access(
                    session, assignment, member.player_id, "playable"
                ):
                    return None
        available = await self._available_play_units(session, selected, members, context)
        violations = ruleset.validate_lobby(
            player_count=len(members),
            packet_count=len(selected),
            available_play_units=available,
            parameters=ruleset.parameters(survivor.settings),
        )
        return None if violations else selected

    async def _merge_lobbies(
        self,
        session: AsyncSession,
        survivor: PregameLobbyRecord,
        discarded: PregameLobbyRecord,
        packet_plan: list[PregameLobbyPacketRecord],
    ) -> None:
        discarded_members = await self._all_active_members(session, discarded.id)
        transferred_player_ids = [member.player_id for member in discarded_members]
        for member in discarded_members:
            member.active = False
            member.ready = False
        await session.flush()

        next_order = (
            await session.scalar(
                select(func.max(PregameLobbyMemberRecord.join_order)).where(
                    PregameLobbyMemberRecord.lobby_id == survivor.id
                )
            )
            or 0
        )
        for source_member in discarded_members:
            existing = await session.scalar(
                select(PregameLobbyMemberRecord).where(
                    PregameLobbyMemberRecord.lobby_id == survivor.id,
                    PregameLobbyMemberRecord.player_id == source_member.player_id,
                )
            )
            if existing is None:
                next_order += 1
                session.add(
                    PregameLobbyMemberRecord(
                        lobby_id=survivor.id,
                        player_id=source_member.player_id,
                        join_order=next_order,
                        role=source_member.role,
                        fresh_content_confirmed=source_member.fresh_content_confirmed,
                    )
                )
            else:
                existing.active = True
                existing.ready = False
                existing.validation_violations = []
                existing.role = source_member.role
                existing.fresh_content_confirmed = source_member.fresh_content_confirmed

        packet_values = [
            (item.packet_id, item.packet_version_id, item.assignment_id) for item in packet_plan
        ]
        current_packets = await self._selected_packets(session, survivor.id)
        for item in current_packets:
            session.expunge(item)
        await session.execute(
            delete(PregameLobbyPacketRecord)
            .where(PregameLobbyPacketRecord.lobby_id == survivor.id)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            insert(PregameLobbyPacketRecord),
            [
                {
                    "lobby_id": survivor.id,
                    "packet_id": packet_id,
                    "packet_version_id": packet_version_id,
                    "assignment_id": assignment_id,
                    "selection_order": selection_order,
                }
                for selection_order, (packet_id, packet_version_id, assignment_id) in enumerate(
                    packet_values, 1
                )
            ],
        )
        await session.flush()
        await self._clear_readiness(session, survivor.id)
        await self._refresh_validation(session, survivor)

        discarded.status = "merged"
        discarded.searching = False
        discarded.search_started_at = None
        discarded.merged_into_lobby_id = survivor.id
        self._bump(discarded)
        member_count = await self._active_member_count(session, survivor.id)
        if member_count >= survivor.max_players:
            survivor.searching = False
            survivor.search_started_at = None
            await self._event(session, survivor.id, "matchmaking_search_completed", {})
        await self._event(
            session,
            survivor.id,
            "lobby_merged",
            {
                "discarded_lobby_id": str(discarded.id),
                "transferred_player_ids": [str(player_id) for player_id in transferred_player_ids],
            },
        )
        await self._event(
            session,
            discarded.id,
            "lobby_merged",
            {"surviving_lobby_id": str(survivor.id)},
        )
        self._bump(survivor)

    @staticmethod
    async def _blacklist_snapshot(
        session: AsyncSession, blocker_player_id: UUID
    ) -> tuple[dict[str, object], ...]:
        rows = (
            await session.execute(
                select(PlayerBlacklistRecord, PlayerRecord)
                .join(
                    PlayerRecord,
                    PlayerRecord.id == PlayerBlacklistRecord.blocked_player_id,
                )
                .where(PlayerBlacklistRecord.blocker_player_id == blocker_player_id)
                .order_by(PlayerRecord.public_nickname, PlayerRecord.telegram_user_id)
            )
        ).all()
        return tuple(
            {
                "telegram_user_id": player.telegram_user_id,
                "display_name": player.public_nickname,
                "created_at": record.created_at,
            }
            for record, player in rows
        )

    @staticmethod
    async def _lock_active_lobby_for_player(session: AsyncSession, player_id: UUID) -> None:
        await session.scalar(
            select(PregameLobbyRecord)
            .join(
                PregameLobbyMemberRecord,
                PregameLobbyMemberRecord.lobby_id == PregameLobbyRecord.id,
            )
            .where(
                PregameLobbyMemberRecord.player_id == player_id,
                PregameLobbyMemberRecord.active.is_(True),
            )
            .with_for_update()
        )

    async def expire_due(self, *, limit: int = 100) -> tuple[UUID, ...]:
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            lobbies = list(
                (
                    await session.execute(
                        select(PregameLobbyRecord)
                        .where(
                            PregameLobbyRecord.status == "assembling",
                            PregameLobbyRecord.expires_at <= now,
                        )
                        .order_by(PregameLobbyRecord.expires_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for lobby in lobbies:
                await self._close_lobby(session, lobby, "expired")
                await self._event(session, lobby.id, "lobby_expired", {})
            return tuple(lobby.id for lobby in lobbies)

    async def suggest_packets(
        self, lobby_id: UUID, *, telegram_user_id: int | None = None
    ) -> tuple[PacketSuggestion, ...]:
        async with self.database.sessions() as session:
            lobby = await session.get(PregameLobbyRecord, lobby_id)
            if lobby is None:
                raise LookupError("Lobby not found")
            members = await self._active_members(session, lobby.id)
            context = await self.tournaments.context(session, lobby.tournament_id)
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            packet_rows = list(
                (
                    await session.execute(
                        select(LogicalPacketRecord, TournamentPacketAssignmentRecord)
                        .join(
                            TournamentPacketAssignmentRecord,
                            TournamentPacketAssignmentRecord.packet_id == LogicalPacketRecord.id,
                        )
                        .where(
                            TournamentPacketAssignmentRecord.tournament_id == lobby.tournament_id,
                            TournamentPacketAssignmentRecord.status == "active",
                            LogicalPacketRecord.retired_at.is_(None),
                        )
                    )
                ).all()
            )
            viewer_id = lobby.creator_player_id
            if telegram_user_id is not None:
                viewer = await self._member(session, lobby.id, telegram_user_id)
                viewer_id = viewer.player_id
            suggestions: list[PacketSuggestion] = []
            for packet, assignment in packet_rows:
                if not await self.tournaments.has_assignment_access(
                    session, assignment, viewer_id, "discoverable"
                ):
                    continue
                version = (
                    await session.get(PacketVersionRecord, assignment.adopted_version_id)
                    if assignment.adopted_version_id is not None
                    else await session.scalar(
                        select(PacketVersionRecord)
                        .where(
                            PacketVersionRecord.packet_id == packet.id,
                            PacketVersionRecord.state == "published",
                        )
                        .order_by(PacketVersionRecord.version_number.desc())
                        .limit(1)
                    )
                )
                if version is not None:
                    candidate = PregameLobbyPacketRecord(
                        lobby_id=lobby.id,
                        packet_id=packet.id,
                        packet_version_id=version.id,
                        assignment_id=assignment.id,
                        selection_order=1,
                    )
                    available = await self._available_play_units(
                        session, [candidate], members, context
                    )
                    total = await self._packet_play_unit_count(session, version.id, context)
                    settings = ruleset.parameters(lobby.settings)
                    if context.type_key == "classic" and context.ruleset_key == "si":
                        settings = settings.updated({"theme_count": max(1, total)})
                    violations = tuple(
                        {
                            "code": violation.code,
                            "details": dict(violation.details),
                        }
                        for violation in ruleset.validate_lobby(
                            player_count=len(members),
                            packet_count=1,
                            available_play_units=available,
                            parameters=settings,
                        )
                    )
                    suggestions.append(
                        await self._describe_packet(
                            session, candidate, version, members,
                            total=total, fresh=len(available), violations=violations,
                        )
                    )
            return tuple(sorted(suggestions, key=lambda item: item.name.casefold()))

    async def start(
        self, lobby_id: UUID, telegram_user_id: int, *, expected_version: int | None = None
    ) -> LobbyStartResult:
        async with self.database.transaction() as session:
            lobby = await self._locked_lobby(session, lobby_id)
            self._require_version(lobby, expected_version)
            await self._require_open(session, lobby)
            await self._require_creator(session, lobby, telegram_user_id)
            if lobby.searching:
                raise ValueError("Cancel Find players before starting the lobby")
            selected_packets = await self._selected_packets(session, lobby.id)
            if not selected_packets:
                raise ValueError("Select at least one packet before starting")
            await self._refresh_validation(session, lobby)
            members = await self._active_members(session, lobby.id)
            if lobby.validation:
                raise ValueError(str(lobby.validation[0]["code"]))
            if any(not member.ready for member in members):
                raise ValueError("Every lobby member must be ready")
            for member in members:
                await self._require_no_active_game(session, member.player_id)
                for selected_packet in selected_packets:
                    assignment = await session.get(
                        TournamentPacketAssignmentRecord,
                        selected_packet.assignment_id,
                    )
                    if assignment is None or not await self.tournaments.has_assignment_access(
                        session, assignment, member.player_id, "playable"
                    ):
                        raise PermissionError("Every player needs packet game eligibility")
            context = await self.tournaments.context(session, lobby.tournament_id, lock=True)
            if not context.assembly_open:
                raise ValueError("tournament_stage_closed")
            classic_match = None
            if context.type_key == "classic":
                if len(selected_packets) != 1:
                    raise ValueError("Classic games use exactly one round packet")
                classic_match = await ClassicService.prescribed_match(
                    session,
                    lobby.tournament_id,
                    selected_packets[0].assignment_id,
                    [m.player_id for m in members],
                    exact=True,
                )
            ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
            available_play_units = await self._available_play_units(
                session, selected_packets, members, context
            )
            plan = ruleset.prepare_assignment(
                player_count=len(members),
                packet_version_ids=[item.packet_version_id for item in selected_packets],
                available_play_units=available_play_units,
                parameters=ruleset.parameters(lobby.settings),
            )
            observers = await self._active_observers(session, lobby.id)
            if observers and context.observing_policy == "forbidden":
                raise PermissionError("Observing is forbidden by tournament policy")
            observer_fresh_claims: dict[UUID, list[tuple[str, UUID, UUID]]] = {}
            for observer in observers:
                fresh = await self._fresh_plan_claims(session, observer.player_id, plan.to_dict())
                observer_fresh_claims[observer.player_id] = fresh
                if fresh and context.observing_policy == "burnt-only":
                    raise PermissionError(
                        "Burnt-only observing does not allow this observer to see fresh content"
                    )
                if fresh and not observer.fresh_content_confirmed:
                    raise PermissionError("Observer must explicitly confirm burning fresh content")

            now = datetime.now(UTC)
            game = GameRecord(
                tournament_id=lobby.tournament_id,
                tournament_type_version_id=lobby.tournament_type_version_id,
                game_ruleset_version_id=lobby.game_ruleset_version_id,
                tournament_policy_version_id=lobby.tournament_policy_version_id,
                rating_confidence_model=self.rating_confidence_model,
                host_player_id=members[0].player_id,
                source_lobby_id=lobby.id,
                assignment_plan={
                    **plan.to_dict(),
                    **({"classic_match_id": str(classic_match.id)} if classic_match else {}),
                },
                join_deadline=now + INITIAL_PLAYER_JOIN_TIMEOUT,
            )
            session.add(game)
            await session.flush()
            for selected_packet in selected_packets:
                session.add(
                    GamePacketVersionRecord(
                        game_id=game.id,
                        packet_version_id=selected_packet.packet_version_id,
                        assignment_id=selected_packet.assignment_id,
                        selection_order=selected_packet.selection_order,
                    )
                )
            for seat, member in enumerate(members, 1):
                if classic_match is not None:
                    seat = classic_match.seats.index(str(member.player_id)) + 1
                membership = await session.get(
                    TournamentMembershipRecord,
                    (lobby.tournament_id, member.player_id),
                    with_for_update=True,
                )
                player = await session.get(PlayerRecord, member.player_id, with_for_update=True)
                if membership is None or membership.status != "active" or player is None:
                    raise PermissionError("Active tournament membership is required")
                membership.rating_sequence += 1
                player.game_sequence += 1
                participant = GameParticipantRecord(
                    tournament_id=lobby.tournament_id,
                    game_id=game.id,
                    player_id=member.player_id,
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
                                player_id=member.player_id,
                                packet_version_id=play_unit.packet_version_id,
                                claim_namespace=claim.namespace,
                                claim_id=claim.identity,
                            )
                        )
                member.active = False
            if classic_match is not None:
                await ClassicService.attach_chairs(session, classic_match, game)
            for observer in observers:
                session.add(
                    GameObserverRecord(
                        game_id=game.id,
                        player_id=observer.player_id,
                    )
                )
                for namespace, claim_id, packet_version_id in observer_fresh_claims[
                    observer.player_id
                ]:
                    live_claim = await session.scalar(
                        select(PlayerExposureClaimRecord.id).where(
                            PlayerExposureClaimRecord.player_id == observer.player_id,
                            PlayerExposureClaimRecord.claim_namespace == namespace,
                            PlayerExposureClaimRecord.claim_id == claim_id,
                            PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
                        )
                    )
                    if live_claim is None:
                        session.add(
                            PlayerExposureClaimRecord(
                                game_id=game.id,
                                player_id=observer.player_id,
                                packet_version_id=packet_version_id,
                                claim_namespace=namespace,
                                claim_id=claim_id,
                            )
                        )
                observer.active = False
            sequence = 0
            for game_theme_position, play_unit in enumerate(plan.play_units, 1):
                session.add(
                    GameThemeRecord(
                        game_id=game.id,
                        theme_revision_id=play_unit.revision_id,
                        source_packet_version_id=play_unit.packet_version_id,
                        position=game_theme_position,
                    )
                )
                for question_revision_id in play_unit.question_revision_ids:
                    sequence += 1
                    session.add(
                        QuestionRoundRecord(
                            game_id=game.id,
                            question_revision_id=question_revision_id,
                            sequence=sequence,
                        )
                    )
            await PersistentGameService._event(
                session,
                game.id,
                "game_created",
                {
                    "source_lobby_id": str(lobby.id),
                    "packet_version_ids": [
                        str(item.packet_version_id) for item in selected_packets
                    ],
                    "play_unit_count": len(plan.play_units),
                    "assignment_seed": plan.seed,
                    "players": [str(member.player_id) for member in members],
                    "join_deadline": game.join_deadline.isoformat(),
                    "join_deadline_stage": "first_player",
                },
            )
            await PersistentGameService._event(
                session,
                game.id,
                "players_assigned",
                {
                    "join_required": True,
                    "join_deadline": game.join_deadline.isoformat(),
                },
            )
            lobby.status = "started"
            lobby.game_id = game.id
            self._bump(lobby)
            await self._event(session, lobby.id, "game_started", {"game_id": str(game.id)})
            await session.flush()
            game_snapshot = await PersistentGameService(
                self.database,
                rulesets=self.rulesets,
                rating_confidence_model=self.rating_confidence_model,
            )._snapshot(session, game)
            return LobbyStartResult(True, await self._snapshot(session, lobby), game_snapshot)

    async def get(self, lobby_id: UUID) -> LobbySnapshot:
        async with self.database.sessions() as session:
            lobby = await session.get(PregameLobbyRecord, lobby_id)
            if lobby is None:
                raise LookupError("Lobby not found")
            return await self._snapshot(session, lobby)

    async def ongoing_lobbies(self, player_id: UUID) -> tuple[dict[str, object], ...]:
        """Project open lobbies from tournaments where the player is a member or manager."""
        async with self.database.sessions() as session:
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active":
                raise PermissionError("Active player registration is required")
            tournament_names, managed_ids = await self.tournaments.player_tournament_map(
                session, player_id
            )
            if not tournament_names:
                return ()
            cards: list[dict[str, object]] = []
            lobbies = (
                await session.execute(
                    select(PregameLobbyRecord)
                    .where(
                        PregameLobbyRecord.tournament_id.in_(tournament_names),
                        PregameLobbyRecord.status == "assembling",
                    )
                    .order_by(PregameLobbyRecord.created_at)
                )
            ).scalars()
            for lobby in lobbies:
                snapshot = await self._snapshot(session, lobby)
                viewer = next(
                    (
                        member
                        for member in snapshot.members
                        if member.telegram_user_id == player.telegram_user_id
                    ),
                    None,
                )
                cards.append(
                    {
                        "id": str(lobby.id),
                        "version": lobby.version,
                        "tournament_id": str(lobby.tournament_id),
                        "tournament_name": tournament_names[lobby.tournament_id],
                        "invitation_code": lobby.invitation_code,
                        "max_players": lobby.max_players,
                        "searching": lobby.searching,
                        "expires_at": lobby.expires_at,
                        "members": [
                            {
                                "display_name": member.display_name,
                                "role": member.role,
                                "ready": member.ready,
                            }
                            for member in snapshot.members
                        ],
                        "selected_packets": [
                            {
                                "packet_id": str(packet.packet_id),
                                "name": packet.name,
                                "lead_author": packet.lead_author,
                                "year": packet.year,
                                "fresh_play_unit_count": packet.fresh_play_unit_count,
                                "total_play_unit_count": packet.total_play_unit_count,
                                "playable_for_all": packet.playable_for_all,
                            }
                            for packet in snapshot.selected_packets
                        ],
                        "is_member": viewer is not None,
                        "viewer_role": viewer.role if viewer is not None else None,
                        "viewer_manages": lobby.tournament_id in managed_ids,
                    }
                )
            return tuple(cards)

    async def _refresh_validation(
        self,
        session: AsyncSession,
        lobby: PregameLobbyRecord,
    ) -> bool:
        previous = list(lobby.validation)
        selected = await self._selected_packets(session, lobby.id)
        members = await self._active_members(session, lobby.id)
        context = await self.tournaments.context(session, lobby.tournament_id)
        ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
        if context.type_key == "classic" and context.ruleset_key == "si":
            # Count the full packet, not only fresh themes: exposure must block
            # the game rather than silently shorten a prescribed Classic round.
            theme_count = sum([
                await self._packet_play_unit_count(session, item.packet_version_id, context)
                for item in selected
            ])
            lobby.settings = {**lobby.settings, "theme_count": max(1, theme_count)}
        available = await self._available_play_units(session, selected, members, context)
        violations = list(
            ruleset.validate_lobby(
                player_count=len(members),
                packet_count=len(selected),
                available_play_units=available,
                parameters=ruleset.parameters(lobby.settings),
            )
        )
        if not context.assembly_open:
            violations.append(ValidationViolation("tournament_stage_closed", {}))
        if context.type_key != "classic":
            settings = ruleset.parameters(lobby.settings).to_dict()
            minimum = max(
                int(settings["minimum_players"]), int(context.type_rules.get("minimum_players", 1))
            )
            maximum = min(
                int(settings["maximum_players"]), int(context.type_rules.get("maximum_players", 12))
            )
            if not minimum <= len(members) <= maximum:
                violations.append(ValidationViolation("ruleset_player_limit_exceeded", {
                    "minimum": minimum, "maximum": maximum, "actual": len(members),
                }))
        if context.type_key == "classic" and selected:
            try:
                if len(selected) != 1:
                    raise ValueError("classic_participants_required")
                await ClassicService.prescribed_match(
                    session,
                    lobby.tournament_id,
                    selected[0].assignment_id,
                    [m.player_id for m in members],
                    exact=True,
                )
            except ValueError:
                violations.append(ValidationViolation("classic_participants_required", {}))
        minimum = int(context.type_rules.get("minimum_players", 1))
        maximum = min(12, int(context.type_rules.get("maximum_players", 12)))
        if not minimum <= len(members) <= maximum:
            violations.append(
                ValidationViolation(
                    "tournament_capacity_restriction",
                    {"minimum": minimum, "maximum": maximum, "actual": len(members)},
                )
            )
        maximum_packets = context.type_rules.get("maximum_packets")
        minimum_packets = int(context.type_rules.get("minimum_packets", 1))
        if len(selected) < minimum_packets:
            violations.append(
                ValidationViolation(
                    "tournament_packet_limit_exceeded",
                    {"minimum": minimum_packets, "actual": len(selected)},
                )
            )
        if maximum_packets is not None and len(selected) > int(maximum_packets):
            violations.append(
                ValidationViolation(
                    "tournament_packet_limit_exceeded",
                    {"maximum": int(maximum_packets), "actual": len(selected)},
                )
            )
        for item in selected:
            violation = await self._packet_selection_violation(session, lobby.tournament_id, item)
            if violation is not None:
                violations.append(violation)
        lobby.validation = [
            {"code": violation.code, "details": dict(violation.details)} for violation in violations
        ]
        for member in members:
            member_violations: list[dict[str, object]] = []
            membership = await session.get(
                TournamentMembershipRecord, (lobby.tournament_id, member.player_id)
            )
            if membership is None or membership.status != "active":
                member_violations.append({"code": "tournament_membership_required", "details": {}})
            for item in selected:
                assignment = await session.get(TournamentPacketAssignmentRecord, item.assignment_id)
                if assignment is None or not await self.tournaments.has_assignment_access(
                    session, assignment, member.player_id, "playable"
                ):
                    member_violations.append(
                        {
                            "code": "packet_not_playable",
                            "details": {"packet_id": str(item.packet_id)},
                        }
                    )
            member.validation_violations = member_violations
        return previous != lobby.validation

    async def _snapshot(self, session: AsyncSession, lobby: PregameLobbyRecord) -> LobbySnapshot:
        rows = (
            await session.execute(
                select(PregameLobbyMemberRecord, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == PregameLobbyMemberRecord.player_id)
                .where(
                    PregameLobbyMemberRecord.lobby_id == lobby.id,
                    PregameLobbyMemberRecord.active.is_(True),
                )
                .order_by(PregameLobbyMemberRecord.join_order)
            )
        ).all()
        selected = await self._selected_packets(session, lobby.id)
        context = await self.tournaments.context(session, lobby.tournament_id)
        ruleset = self.tournaments.rulesets.get(context.ruleset_key, context.ruleset_version)
        selected_snapshots: list[PacketSuggestion] = []
        members = [member for member, _ in rows if member.role == "player"]
        for item in selected:
            version = await session.get(PacketVersionRecord, item.packet_version_id)
            assert version is not None
            total = await self._packet_play_unit_count(session, version.id, context)
            available = await self._available_play_units(session, [item], members, context)
            selected_snapshots.append(
                await self._describe_packet(
                    session, item, version, members, total=total, fresh=len(available),
                )
            )
        all_available = await self._available_play_units(session, selected, members, context)
        other_searching_lobby_count = int(
            await session.scalar(
                select(func.count())
                .select_from(PregameLobbyRecord)
                .where(
                    PregameLobbyRecord.status == "assembling",
                    PregameLobbyRecord.searching.is_(True),
                    PregameLobbyRecord.tournament_id == lobby.tournament_id,
                    PregameLobbyRecord.id != lobby.id,
                )
            )
            or 0
        )
        other_searching_player_count = int(
            await session.scalar(
                select(func.count())
                .select_from(PregameLobbyMemberRecord)
                .join(
                    PregameLobbyRecord,
                    PregameLobbyRecord.id == PregameLobbyMemberRecord.lobby_id,
                )
                .where(
                    PregameLobbyRecord.status == "assembling",
                    PregameLobbyRecord.searching.is_(True),
                    PregameLobbyRecord.tournament_id == lobby.tournament_id,
                    PregameLobbyRecord.id != lobby.id,
                    PregameLobbyMemberRecord.active.is_(True),
                    PregameLobbyMemberRecord.role == "player",
                )
            )
            or 0
        )
        return LobbySnapshot(
            id=lobby.id,
            version=lobby.version,
            tournament_id=lobby.tournament_id,
            invitation_code=lobby.invitation_code,
            status=lobby.status,
            max_players=lobby.max_players,
            expires_at=lobby.expires_at,
            game_id=lobby.game_id,
            searching=lobby.searching,
            hybrid_matchmaking_available=context.hybrid_matchmaking_enabled,
            search_started_at=lobby.search_started_at,
            merged_into_lobby_id=lobby.merged_into_lobby_id,
            other_searching_lobby_count=other_searching_lobby_count,
            other_searching_player_count=other_searching_player_count,
            settings=ruleset.parameters(lobby.settings),
            selected_packets=tuple(selected_snapshots),
            validation_violations=tuple(lobby.validation),
            available_play_unit_count=len(all_available),
            members=tuple(
                LobbyMemberSnapshot(
                    player.telegram_user_id,
                    player.public_nickname,
                    member.join_order,
                    member.ready,
                    tuple(member.validation_violations),
                    member.role,
                    member.fresh_content_confirmed,
                )
                for member, player in rows
            ),
        )

    @staticmethod
    async def _selected_packets(
        session: AsyncSession, lobby_id: UUID
    ) -> list[PregameLobbyPacketRecord]:
        return list(
            (
                await session.execute(
                    select(PregameLobbyPacketRecord)
                    .where(PregameLobbyPacketRecord.lobby_id == lobby_id)
                    .order_by(PregameLobbyPacketRecord.selection_order)
                )
            ).scalars()
        )

    async def _describe_packet(
        self,
        session: AsyncSession,
        selection: PregameLobbyPacketRecord,
        version: PacketVersionRecord,
        members: list[PregameLobbyMemberRecord],
        *,
        total: int,
        fresh: int,
        violations: tuple[dict[str, object], ...] = (),
    ) -> PacketSuggestion:
        author_ids = select(ThemeRevisionRecord.author_id).where(
            ThemeRevisionRecord.packet_version_id == version.id
        ).union(
            select(QuestionRevisionRecord.author_id)
            .join(
                PacketQuestionRecord,
                PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
            )
            .where(PacketQuestionRecord.packet_version_id == version.id)
        )
        authors = list(await session.scalars(
            select(AuthorRecord).where(or_(
                AuthorRecord.id.in_(author_ids),
                AuthorRecord.id == version.lead_author_id,
            )).order_by(AuthorRecord.display_name, AuthorRecord.id)
        ))
        assignment = await session.get(TournamentPacketAssignmentRecord, selection.assignment_id)
        playable_for_all = assignment is not None
        if assignment is not None:
            for member in members:
                if not await self.tournaments.has_assignment_access(
                    session, assignment, member.player_id, "playable"
                ):
                    playable_for_all = False
                    break
        return PacketSuggestion(
            packet_id=selection.packet_id,
            packet_version_id=version.id,
            name=version.name,
            total_play_unit_count=total,
            fresh_play_unit_count=fresh,
            validation_violations=violations,
            year=version.year,
            published_at=version.published_at,
            lead_author=next(
                (author.display_name for author in authors if author.id == version.lead_author_id),
                None,
            ),
            authors=tuple(author.display_name for author in authors),
            playable_for_all=playable_for_all,
        )

    async def _packet_play_unit_count(
        self,
        session: AsyncSession,
        version_id: UUID,
        context: TournamentContext,
    ) -> int:
        adapter = self.content_adapters.get(context.ruleset_key, context.ruleset_version)
        return await adapter.play_unit_count(session, version_id)

    async def _available_play_units(
        self,
        session: AsyncSession,
        selected: list[PregameLobbyPacketRecord],
        members: list[PregameLobbyMemberRecord],
        context: TournamentContext,
    ) -> list[PlayUnit]:
        adapter = self.content_adapters.get(context.ruleset_key, context.ruleset_version)
        return await adapter.available_play_units(
            session,
            [PacketSelection(item.packet_version_id, item.selection_order) for item in selected],
            [member.player_id for member in members],
        )

    @staticmethod
    async def _packet_selection_violation(
        session: AsyncSession,
        tournament_id: UUID,
        selection: PregameLobbyPacketRecord,
    ) -> ValidationViolation | None:
        assignment = await session.get(TournamentPacketAssignmentRecord, selection.assignment_id)
        if (
            assignment is None
            or assignment.status != "active"
            or assignment.tournament_id != tournament_id
            or assignment.packet_id != selection.packet_id
        ):
            return ValidationViolation(
                "packet_not_playable",
                {"packet_id": str(selection.packet_id), "reason": "assignment_inactive"},
            )
        version = await session.get(PacketVersionRecord, selection.packet_version_id)
        if (
            version is None
            or version.state != "published"
            or version.packet_id != selection.packet_id
        ):
            return ValidationViolation(
                "packet_not_playable",
                {"packet_id": str(selection.packet_id), "reason": "version_unavailable"},
            )
        if assignment.adopted_version_id is not None:
            current_version_id = assignment.adopted_version_id
        else:
            current_version_id = await session.scalar(
                select(PacketVersionRecord.id)
                .where(
                    PacketVersionRecord.packet_id == selection.packet_id,
                    PacketVersionRecord.state == "published",
                )
                .order_by(PacketVersionRecord.version_number.desc())
                .limit(1)
            )
        if current_version_id != selection.packet_version_id:
            return ValidationViolation(
                "packet_not_playable",
                {"packet_id": str(selection.packet_id), "reason": "version_not_adopted"},
            )
        return None

    @staticmethod
    async def _clear_readiness(session: AsyncSession, lobby_id: UUID) -> None:
        members = await InvitationMatchmakingService._all_active_members(session, lobby_id)
        for member in members:
            member.ready = False
            if member.role == "observer":
                member.fresh_content_confirmed = False

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
    async def _locked_lobby(session: AsyncSession, lobby_id: UUID) -> PregameLobbyRecord:
        lobby = await session.scalar(
            select(PregameLobbyRecord).where(PregameLobbyRecord.id == lobby_id).with_for_update()
        )
        if lobby is None:
            raise LookupError("Lobby not found")
        return lobby

    @staticmethod
    async def _active_members(
        session: AsyncSession, lobby_id: UUID
    ) -> list[PregameLobbyMemberRecord]:
        return list(
            (
                await session.execute(
                    select(PregameLobbyMemberRecord)
                    .where(
                        PregameLobbyMemberRecord.lobby_id == lobby_id,
                        PregameLobbyMemberRecord.active.is_(True),
                        PregameLobbyMemberRecord.role == "player",
                    )
                    .order_by(PregameLobbyMemberRecord.join_order)
                    .with_for_update()
                )
            ).scalars()
        )

    @staticmethod
    async def _active_member_count(session: AsyncSession, lobby_id: UUID) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(PregameLobbyMemberRecord)
                .where(
                    PregameLobbyMemberRecord.lobby_id == lobby_id,
                    PregameLobbyMemberRecord.active.is_(True),
                    PregameLobbyMemberRecord.role == "player",
                )
            )
            or 0
        )

    @staticmethod
    async def _all_active_members(
        session: AsyncSession, lobby_id: UUID
    ) -> list[PregameLobbyMemberRecord]:
        return list(
            (
                await session.execute(
                    select(PregameLobbyMemberRecord)
                    .where(
                        PregameLobbyMemberRecord.lobby_id == lobby_id,
                        PregameLobbyMemberRecord.active.is_(True),
                    )
                    .order_by(PregameLobbyMemberRecord.join_order)
                    .with_for_update()
                )
            ).scalars()
        )

    @staticmethod
    async def _active_observers(
        session: AsyncSession, lobby_id: UUID
    ) -> list[PregameLobbyMemberRecord]:
        return [
            member
            for member in await InvitationMatchmakingService._all_active_members(session, lobby_id)
            if member.role == "observer"
        ]

    @staticmethod
    async def _fresh_plan_claims(
        session: AsyncSession, player_id: UUID, plan: dict[str, object]
    ) -> list[tuple[str, UUID, UUID]]:
        units = plan.get("play_units", [])
        claims = [
            (
                str(claim["namespace"]),
                UUID(str(claim["identity"])),
                UUID(str(unit["packet_version_id"])),
            )
            for unit in units
            if isinstance(unit, dict)
            for claim in unit.get("claims", [])
            if isinstance(claim, dict)
        ]
        if not claims:
            return []
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
    async def _member(
        session: AsyncSession, lobby_id: UUID, telegram_user_id: int
    ) -> PregameLobbyMemberRecord:
        member = await session.scalar(
            select(PregameLobbyMemberRecord)
            .join(PlayerRecord, PlayerRecord.id == PregameLobbyMemberRecord.player_id)
            .where(
                PregameLobbyMemberRecord.lobby_id == lobby_id,
                PregameLobbyMemberRecord.active.is_(True),
                PlayerRecord.telegram_user_id == telegram_user_id,
            )
            .with_for_update()
        )
        if member is None:
            raise PermissionError("Player is not a lobby member")
        return member

    async def _require_open(self, session: AsyncSession, lobby: PregameLobbyRecord) -> None:
        if lobby.status != "assembling":
            raise ValueError("Lobby is closed")
        if datetime.now(UTC) >= lobby.expires_at:
            raise ValueError("Lobby has expired")

    @staticmethod
    async def _require_creator(
        session: AsyncSession, lobby: PregameLobbyRecord, telegram_user_id: int
    ) -> None:
        creator_id = await session.scalar(
            select(PlayerRecord.telegram_user_id).where(PlayerRecord.id == lobby.creator_player_id)
        )
        if creator_id != telegram_user_id:
            raise PermissionError("Only the lobby creator may do this")

    @staticmethod
    async def _require_no_active_game(session: AsyncSession, player_id: UUID) -> None:
        active_game = await session.scalar(
            select(GameParticipantRecord.id)
            .where(
                GameParticipantRecord.player_id == player_id,
                GameParticipantRecord.active.is_(True),
            )
            .limit(1)
        )
        if active_game is not None:
            raise ValueError("Player already belongs to an active game")

    async def _require_available(self, session: AsyncSession, player_id: UUID) -> None:
        await self._require_no_active_game(session, player_id)
        active_lobby = await session.scalar(
            select(PregameLobbyMemberRecord.id)
            .where(
                PregameLobbyMemberRecord.player_id == player_id,
                PregameLobbyMemberRecord.active.is_(True),
            )
            .limit(1)
        )
        if active_lobby is not None:
            raise ValueError("Player already belongs to an active lobby")

    @staticmethod
    async def _close_lobby(session: AsyncSession, lobby: PregameLobbyRecord, status: str) -> None:
        lobby.status = status
        lobby.searching = False
        lobby.search_started_at = None
        lobby.version += 1
        lobby.updated_at = datetime.now(UTC)
        members = await InvitationMatchmakingService._all_active_members(session, lobby.id)
        for member in members:
            member.active = False

    @staticmethod
    async def _event(
        session: AsyncSession, lobby_id: UUID, kind: str, payload: dict[str, object]
    ) -> None:
        sequence = (
            await session.scalar(
                select(func.max(PregameLobbyEventRecord.sequence)).where(
                    PregameLobbyEventRecord.lobby_id == lobby_id
                )
            )
            or 0
        )
        event_sequence = sequence + 1
        session.add(
            PregameLobbyEventRecord(
                lobby_id=lobby_id, sequence=event_sequence, kind=kind, payload=payload
            )
        )
        await TransactionalOutbox.enqueue_lobby_event(
            session,
            lobby_id=lobby_id,
            sequence=event_sequence,
            kind=kind,
            parameters=payload,
        )

        if kind in {
            "settings_changed",
            "packet_selected",
            "packet_removed",
            "packet_rejected",
            "player_joined",
            "player_left",
            "lobby_cancelled",
            "readiness_changed",
        }:
            notice = {"changes": payload} if kind == "settings_changed" else dict(payload)
            if kind in {"player_joined", "player_left"}:
                player = await session.get(PlayerRecord, UUID(str(payload["player_id"])))
                notice["player_name"] = player.public_nickname
            recipients = (
                await session.execute(
                    select(PlayerRecord)
                    .join(
                        PregameLobbyMemberRecord,
                        PregameLobbyMemberRecord.player_id == PlayerRecord.id,
                    )
                    .where(
                        PregameLobbyMemberRecord.lobby_id == lobby_id,
                        PregameLobbyMemberRecord.active.is_(True),
                        PlayerRecord.telegram_user_id.is_not(None),
                    )
                )
            ).scalars()
            for recipient in recipients:
                if kind == "readiness_changed" and str(recipient.id) == payload["player_id"]:
                    continue
                await TransactionalOutbox.enqueue(
                    session,
                    topic="telegram.lobby.notice",
                    deduplication_key=f"lobby:{lobby_id}:{event_sequence}:{recipient.id}",
                    partition_key=f"telegram:chat:{recipient.telegram_user_id}",
                    aggregate_type="lobby",
                    aggregate_id=lobby_id,
                    aggregate_sequence=event_sequence,
                    payload={
                        "recipient_telegram_user_id": recipient.telegram_user_id,
                        "locale": recipient.preferred_locale,
                        "kind": kind,
                        **notice,
                    },
                )

    @staticmethod
    def _bump(lobby: PregameLobbyRecord) -> None:
        lobby.version += 1
        lobby.updated_at = datetime.now(UTC)

    @staticmethod
    def _require_version(lobby: PregameLobbyRecord, expected_version: int | None) -> None:
        if expected_version is not None and lobby.version != expected_version:
            raise StaleWriteError("Lobby has changed")
