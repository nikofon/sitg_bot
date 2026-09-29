from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.players import AccountSnapshot, PlayerAccountService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ClassicChatRecord,
    ClassicMatchRecord,
    ClassicRoundRecord,
    ClassicStageRecord,
    GameObserverRecord,
    GameParticipantRecord,
    GameRecord,
    PlatformAdministratorRecord,
    PlayerBanRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
    PregameLobbyMemberRecord,
    PregameLobbyRecord,
    TelegramGameViewRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentRecord,
    TournamentTypeVersionRecord,
)

INTERACTION_MODES = ("player", "manager", "admin")


@dataclass(frozen=True, slots=True)
class NavigationTournamentAction:
    key: str
    descriptor_version: int


@dataclass(frozen=True, slots=True)
class NavigationTournament:
    id: UUID
    name: str
    slug: str
    status: str
    capability_actions: tuple[NavigationTournamentAction, ...] = ()


@dataclass(frozen=True, slots=True)
class NavigationLobby:
    id: UUID
    tournament_id: UUID
    status: str
    version: int


@dataclass(frozen=True, slots=True)
class NavigationGame:
    id: UUID
    tournament_id: UUID
    status: str
    phase: str
    version: int
    joined: bool
    active: bool
    can_reconnect: bool


@dataclass(frozen=True, slots=True)
class NavigationChat:
    id: UUID
    tournament_id: UUID
    tournament_name: str
    round_number: int
    match_number: int
    multiple_matches: bool


@dataclass(frozen=True, slots=True)
class NavigationSnapshot:
    account: AccountSnapshot
    available_modes: tuple[str, ...]
    active_mode: str
    context: str
    navigation_version: int
    selected_player_tournament: NavigationTournament | None
    selected_manager_tournament: NavigationTournament | None
    active_lobby: NavigationLobby | None
    active_game: NavigationGame | None
    allowed_actions: tuple[str, ...]
    ban_reason: str | None = None
    active_chat: NavigationChat | None = None


class TelegramNavigationService:
    """Owns private-chat mode selection and derives authoritative recovery context."""

    def __init__(
        self,
        database: Database,
        *,
        player_accounts: PlayerAccountService | None = None,
    ) -> None:
        self.database = database
        self.player_accounts = player_accounts or PlayerAccountService(database)

    async def snapshot(self, telegram_user_id: int) -> NavigationSnapshot | None:
        async with self.database.transaction() as session:
            player = await session.scalar(
                select(PlayerRecord).where(PlayerRecord.telegram_user_id == telegram_user_id)
            )
            if player is None:
                return None
            return await self._snapshot(session, self.player_accounts._account_snapshot(player))

    async def set_mode(
        self,
        telegram_user_id: int,
        mode: str,
        *,
        expected_version: int,
    ) -> NavigationSnapshot:
        if mode not in INTERACTION_MODES:
            raise ValueError("Unknown interaction mode")
        async with self.database.transaction() as session:
            player = await self._active_player(session, telegram_user_id)
            navigation = await self._locked_navigation(session, player.id)
            self._require_version(navigation, expected_version)
            available_modes = await self._available_modes(session, player.id)
            if mode not in available_modes:
                raise PermissionError("Interaction mode is not available")
            if navigation is None:
                navigation = PlayerTelegramNavigationRecord(
                    player_id=player.id,
                    interaction_mode=mode,
                    version=1,
                )
                session.add(navigation)
            elif navigation.interaction_mode != mode:
                navigation.interaction_mode = mode
                navigation.version += 1
            await session.flush()
            return await self._snapshot(session, self.player_accounts._account_snapshot(player))

    async def set_context(
        self, telegram_user_id: int, context: str, *, expected_version: int | None = None
    ) -> NavigationSnapshot:
        if context not in {"tournament", "lobby", "lobby_other"}:
            raise ValueError("Unknown player context")
        async with self.database.transaction() as session:
            player = await self._active_player(session, telegram_user_id)
            navigation = await self._locked_navigation(session, player.id)
            if expected_version is not None:
                self._require_version(navigation, expected_version)
            if navigation is None:
                navigation = PlayerTelegramNavigationRecord(player_id=player.id, version=1)
                session.add(navigation)
            if context != "tournament":
                lobby = await self._active_lobby(session, player.id)
                if lobby is None:
                    raise LookupError("Active lobby not found")
                navigation.selected_player_tournament_id = lobby.tournament_id
                navigation.interaction_mode = "player"
            navigation.player_context = context
            navigation.version += 1
            await session.flush()
            return await self._snapshot(session, self.player_accounts._account_snapshot(player))

    async def select_tournament(
        self,
        telegram_user_id: int,
        *,
        mode: str,
        tournament_id: UUID | None,
        expected_version: int,
    ) -> NavigationSnapshot:
        if mode not in {"player", "manager"}:
            raise ValueError("A tournament can only be selected for player or manager mode")
        async with self.database.transaction() as session:
            player = await self._active_player(session, telegram_user_id)
            navigation = await self._locked_navigation(session, player.id)
            self._require_version(navigation, expected_version)
            if tournament_id is not None:
                await self._require_tournament_access(
                    session, player.id, mode=mode, tournament_id=tournament_id
                )
            if navigation is None:
                if tournament_id is None:
                    return await self._snapshot(
                        session, self.player_accounts._account_snapshot(player)
                    )
                navigation = PlayerTelegramNavigationRecord(
                    player_id=player.id,
                    version=1,
                    **{f"selected_{mode}_tournament_id": tournament_id},
                )
                session.add(navigation)
            else:
                field = f"selected_{mode}_tournament_id"
                if getattr(navigation, field) != tournament_id:
                    setattr(navigation, field, tournament_id)
                    navigation.version += 1
            if (
                mode == "player"
                and navigation is not None
                and navigation.player_context != "tournament"
            ):
                navigation.player_context = "tournament"
                navigation.version += 1
            await session.flush()
            return await self._snapshot(session, self.player_accounts._account_snapshot(player))

    async def _snapshot(
        self, session: AsyncSession, account: AccountSnapshot
    ) -> NavigationSnapshot:
        if account.registration_status != "active":
            context = (
                "registration" if account.registration_status == "registration" else "unavailable"
            )
            actions = (
                (
                    "start",
                    "menu",
                    "help",
                    "language",
                    "cancel",
                    "registration.continue",
                )
                if context == "registration"
                else ("start", "help")
            )
            return NavigationSnapshot(
                account=account,
                available_modes=("player",),
                active_mode="player",
                context=context,
                navigation_version=0,
                selected_player_tournament=None,
                selected_manager_tournament=None,
                active_lobby=None,
                active_game=None,
                allowed_actions=actions,
            )

        ban_reason = await session.scalar(
            select(PlayerBanRecord.reason).where(
                PlayerBanRecord.player_id == account.player_id,
                PlayerBanRecord.lifted_at.is_(None),
            )
        )
        if ban_reason is not None:
            # Banned players keep exactly one interaction: opening the packet library.
            return NavigationSnapshot(
                account=account,
                available_modes=("player",),
                active_mode="player",
                context="menu",
                navigation_version=0,
                selected_player_tournament=None,
                selected_manager_tournament=None,
                active_lobby=None,
                active_game=None,
                allowed_actions=("start", "menu", "help", "language", "player.library"),
                ban_reason=ban_reason,
            )

        navigation = await session.get(PlayerTelegramNavigationRecord, account.player_id)
        available_modes = await self._available_modes(session, account.player_id)
        saved_mode = navigation.interaction_mode if navigation is not None else "player"
        active_mode = saved_mode if saved_mode in available_modes else "player"
        version = navigation.version if navigation is not None else 0

        selected_player = await self._selected_tournament(
            session,
            account.player_id,
            mode="player",
            tournament_id=(
                navigation.selected_player_tournament_id if navigation is not None else None
            ),
        )
        selected_manager = await self._selected_tournament(
            session,
            account.player_id,
            mode="manager",
            tournament_id=(
                navigation.selected_manager_tournament_id if navigation is not None else None
            ),
        )
        active_game = await self._active_game(session, account.player_id)
        active_lobby = await self._active_lobby(session, account.player_id)

        active_chat = None
        if (
            active_mode == "player"
            and navigation is not None
            and navigation.player_context == "chat"
        ):
            active_chat = await self._active_chat(
                session, navigation.active_chat_id, account.player_id
            )
        if (
            navigation is not None
            and navigation.player_context == "chat"
            and active_chat is None
        ):
            # A played or lost chat cannot stay open; recover the tournament context.
            navigation.player_context = "tournament"
            navigation.active_chat_id = None
            navigation.version += 1
            version = navigation.version
            await session.flush()

        if active_game is not None and active_game.active:
            active_mode = "player"
            context = "game"
        elif (
            active_mode == "player"
            and active_chat is not None
        ):
            context = "chat"
        elif (
            active_mode == "player"
            and active_lobby is not None
            and navigation is not None
            and navigation.player_context in {"lobby", "lobby_other"}
        ):
            context = navigation.player_context
        elif (active_mode == "player" and selected_player is not None) or (
            active_mode == "manager" and selected_manager is not None
        ):
            context = "tournament"
        else:
            context = "menu"

        lobby_actions: tuple[str, ...] = ()
        if context in {"lobby", "lobby_other"} and active_lobby is not None:
            from sitg_bot.services.tournaments import TournamentService

            record = await session.get(PregameLobbyRecord, active_lobby.id)
            member = await session.scalar(
                select(PregameLobbyMemberRecord).where(
                    PregameLobbyMemberRecord.lobby_id == active_lobby.id,
                    PregameLobbyMemberRecord.player_id == account.player_id,
                    PregameLobbyMemberRecord.active.is_(True),
                )
            )
            actions = ["lobby.info", "lobby.leave", "back"]
            if member.role == "player":
                actions.append("lobby.unready" if member.ready else "lobby.ready")
            if record.creator_player_id == account.player_id:
                actions.append("lobby.start")
                policy = await TournamentService(self.database).context(
                    session, record.tournament_id
                )
                if policy.hybrid_matchmaking_enabled:
                    actions.append("lobby.search_cancel" if record.searching else "lobby.search")
            lobby_actions = tuple(actions)

        return NavigationSnapshot(
            account=account,
            available_modes=available_modes,
            active_mode=active_mode,
            context=context,
            navigation_version=version,
            selected_player_tournament=selected_player,
            selected_manager_tournament=selected_manager,
            active_lobby=active_lobby,
            active_game=active_game,
            active_chat=active_chat,
            allowed_actions=lobby_actions
            or self._allowed_actions(
                active_mode=active_mode,
                context=context,
                available_modes=available_modes,
                active_lobby=active_lobby,
                active_game=active_game,
                selected_player=selected_player,
            ),
        )

    @staticmethod
    async def _active_chat(
        session: AsyncSession, chat_id: UUID | None, player_id: UUID
    ) -> NavigationChat | None:
        if chat_id is None:
            return None
        chat = await session.get(ClassicChatRecord, chat_id)
        if chat is None:
            return None
        match = await session.get(ClassicMatchRecord, chat.match_id)
        if (
            match is None
            or match.game_id is not None
            or match.results is not None
            or str(player_id) not in (match.seats or ())
        ):
            return None
        round_record = await session.get(ClassicRoundRecord, match.round_id)
        stage = await session.get(ClassicStageRecord, round_record.stage_id)
        if (
            round_record.assignment_id is None
            or stage.started_at is None
            or stage.completed_at is not None
        ):
            return None
        tournament = await session.get(TournamentRecord, stage.tournament_id)
        if tournament is None:
            return None
        matches_in_round = (
            await session.scalar(
                select(func.count(ClassicMatchRecord.id)).where(
                    ClassicMatchRecord.round_id == round_record.id
                )
            )
            or 1
        )
        return NavigationChat(
            chat.id,
            tournament.id,
            tournament.name,
            round_record.number,
            match.number,
            matches_in_round > 1,
        )

    @staticmethod
    async def _available_modes(session: AsyncSession, player_id: UUID) -> tuple[str, ...]:
        administrator = await session.scalar(
            select(PlatformAdministratorRecord.player_id).where(
                PlatformAdministratorRecord.player_id == player_id,
                PlatformAdministratorRecord.revoked_at.is_(None),
            )
        )
        modes = ["player", "manager"]
        if administrator is not None:
            modes.append("admin")
        return tuple(modes)

    @staticmethod
    async def _selected_tournament(
        session: AsyncSession,
        player_id: UUID,
        *,
        mode: str,
        tournament_id: UUID | None,
    ) -> NavigationTournament | None:
        if tournament_id is None:
            return None
        query = select(TournamentRecord).where(TournamentRecord.id == tournament_id)
        if mode == "player":
            query = query.join(
                TournamentMembershipRecord,
                TournamentMembershipRecord.tournament_id == TournamentRecord.id,
            ).where(
                TournamentMembershipRecord.player_id == player_id,
                TournamentMembershipRecord.status == "active",
                TournamentRecord.finalized_at.is_not(None),
                ~select(TournamentManagerRecord.player_id)
                .where(
                    TournamentManagerRecord.tournament_id == TournamentRecord.id,
                    TournamentManagerRecord.player_id == player_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
                .exists(),
            )
        else:
            query = query.join(
                TournamentManagerRecord,
                TournamentManagerRecord.tournament_id == TournamentRecord.id,
            ).where(
                TournamentManagerRecord.player_id == player_id,
                TournamentManagerRecord.revoked_at.is_(None),
            )
        tournament = await session.scalar(query)
        if tournament is None:
            return None
        type_version = await session.get(TournamentTypeVersionRecord, tournament.type_version_id)
        return TelegramNavigationService._tournament(tournament, type_version=type_version)

    @staticmethod
    async def _active_lobby(session: AsyncSession, player_id: UUID) -> NavigationLobby | None:
        row = (
            await session.execute(
                select(PregameLobbyMemberRecord, PregameLobbyRecord)
                .join(
                    PregameLobbyRecord,
                    PregameLobbyRecord.id == PregameLobbyMemberRecord.lobby_id,
                )
                .where(
                    PregameLobbyMemberRecord.player_id == player_id,
                    PregameLobbyMemberRecord.active.is_(True),
                    PregameLobbyRecord.status == "assembling",
                )
                .order_by(PregameLobbyRecord.updated_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        _, lobby = row
        return NavigationLobby(lobby.id, lobby.tournament_id, lobby.status, lobby.version)

    @staticmethod
    async def _active_game(session: AsyncSession, player_id: UUID) -> NavigationGame | None:
        observed = await session.scalar(
            select(GameRecord)
            .join(GameObserverRecord, GameObserverRecord.game_id == GameRecord.id)
            .outerjoin(
                TelegramGameViewRecord,
                (TelegramGameViewRecord.game_id == GameRecord.id)
                & (TelegramGameViewRecord.player_id == player_id),
            )
            .where(
                GameObserverRecord.player_id == player_id,
                GameObserverRecord.active.is_(True),
                TelegramGameViewRecord.dismissed_at.is_(None),
            )
            .order_by(GameObserverRecord.joined_at.desc())
            .limit(1)
        )
        if observed is not None:
            return NavigationGame(
                observed.id,
                observed.tournament_id,
                observed.status,
                observed.phase,
                observed.version,
                True,
                True,
                False,
            )
        rows = (
            await session.execute(
                select(GameParticipantRecord, GameRecord)
                .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
                .outerjoin(
                    TelegramGameViewRecord,
                    (TelegramGameViewRecord.game_id == GameRecord.id)
                    & (TelegramGameViewRecord.player_id == player_id),
                )
                .where(
                    GameParticipantRecord.player_id == player_id,
                    GameParticipantRecord.global_game_sequence
                    == select(func.max(GameParticipantRecord.global_game_sequence))
                    .where(GameParticipantRecord.player_id == player_id)
                    .correlate(None)
                    .scalar_subquery(),
                    TelegramGameViewRecord.dismissed_at.is_(None),
                    or_(
                        GameParticipantRecord.active.is_(True),
                        GameRecord.status.in_(
                            ("completed", "finalized", "cancelled", "failed_to_start", "abandoned")
                        )
                        & TelegramGameViewRecord.player_id.is_not(None),
                    ),
                )
                .order_by(GameParticipantRecord.global_game_sequence.desc())
            )
        ).all()
        for participant, game in rows:
            can_reconnect = (
                game.status == "active"
                and not participant.active
                and participant.abandoned_at is not None
                and not await TelegramNavigationService._has_later_game(session, participant)
            )
            if participant.active or game.status not in {"active", "lobby"}:
                return NavigationGame(
                    game.id,
                    game.tournament_id,
                    game.status,
                    game.phase,
                    game.version,
                    participant.joined,
                    True,
                    can_reconnect,
                )
        return None

    @staticmethod
    async def _has_later_game(session: AsyncSession, participant: GameParticipantRecord) -> bool:
        return (
            await session.scalar(
                select(GameParticipantRecord.id)
                .where(
                    GameParticipantRecord.player_id == participant.player_id,
                    GameParticipantRecord.global_game_sequence > participant.global_game_sequence,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _allowed_actions(
        *,
        active_mode: str,
        context: str,
        available_modes: tuple[str, ...],
        active_lobby: NavigationLobby | None,
        active_game: NavigationGame | None,
        selected_player: NavigationTournament | None,
    ) -> tuple[str, ...]:
        actions = ["start", "menu", "help", "language"]
        if len(available_modes) > 1:
            actions.append("mode.switch")
        if active_lobby is not None:
            actions.append("lobby.reopen")
        if context == "game" and active_game is not None:
            actions.append("game.reconnect" if active_game.can_reconnect else "game.open")
        elif context == "chat":
            # An open tournament chat deliberately exposes no keyboard actions.
            pass
        elif active_mode == "player":
            if context == "menu":
                actions.append("notifications")
                actions.extend(
                    (
                        "player.menu",
                        "player.tournaments",
                        "player.settings",
                        "player.setting.set",
                        "player.other",
                        "player.profile",
                        "player.library",
                        "player.author_link",
                        "player.authors",
                        "player.ongoing",
                    )
                )
            else:
                actions.extend(
                    (
                        "player.tournament",
                        "player.tournament.create_lobby",
                        "player.tournament.info",
                        "player.tournament.chats",
                        "player.other",
                        "player.tournament.packet_upload",
                        "player.tournament.quit",
                    )
                )
                if selected_player is not None:
                    actions.extend(item.key for item in selected_player.capability_actions)
        elif active_mode == "manager":
            if context == "menu":
                actions.append("notifications")
                actions.extend(
                    (
                        "manager.menu",
                        "manager.tournament.select",
                        "manager.token.request",
                        "manager.appeals",
                        "manager.tokens",
                        "manager.tournament.create",
                    )
                )
            else:
                actions.extend(
                    (
                        "manager.tournament",
                        "manager.tournament.management",
                        "manager.tournament.packet_upload",
                        "manager.tournament.profile",
                        "tournament.registration_link",
                        "manager.tournament.quit",
                    )
                )
        else:
            if context == "menu":
                actions.append("notifications")
            actions.extend(
                (
                    "admin.menu",
                    "admin.token_requests.pending",
                    "admin.management",
                )
            )
        return tuple(actions)

    @staticmethod
    async def _active_player(session: AsyncSession, telegram_user_id: int) -> PlayerRecord:
        player = await session.scalar(
            select(PlayerRecord)
            .where(PlayerRecord.telegram_user_id == telegram_user_id)
            .with_for_update()
        )
        if player is None or player.status != "active":
            raise PermissionError("Completed player registration is required")
        return player

    @staticmethod
    async def _locked_navigation(
        session: AsyncSession, player_id: UUID
    ) -> PlayerTelegramNavigationRecord | None:
        return await session.scalar(
            select(PlayerTelegramNavigationRecord)
            .where(PlayerTelegramNavigationRecord.player_id == player_id)
            .with_for_update()
        )

    @staticmethod
    def _require_version(
        navigation: PlayerTelegramNavigationRecord | None, expected_version: int
    ) -> None:
        current = navigation.version if navigation is not None else 0
        if current != expected_version:
            raise StaleWriteError("Navigation state has changed")

    @staticmethod
    async def _require_tournament_access(
        session: AsyncSession,
        player_id: UUID,
        *,
        mode: str,
        tournament_id: UUID,
    ) -> None:
        if mode == "player":
            record = await session.get(TournamentMembershipRecord, (tournament_id, player_id))
            tournament = await session.get(TournamentRecord, tournament_id)
            manager = await session.get(TournamentManagerRecord, (tournament_id, player_id))
            allowed = (
                record is not None
                and record.status == "active"
                and tournament is not None
                and tournament.finalized_at is not None
                and (manager is None or manager.revoked_at is not None)
            )
        else:
            record = await session.get(TournamentManagerRecord, (tournament_id, player_id))
            allowed = record is not None and record.revoked_at is None
        if not allowed:
            raise PermissionError("Tournament is not available in this interaction mode")

    @staticmethod
    def _tournament(
        tournament: TournamentRecord,
        *,
        type_version: TournamentTypeVersionRecord | None = None,
    ) -> NavigationTournament:
        configured = (
            type_version.rules.get("telegram_player_actions", [])
            if type_version is not None
            else []
        )
        keys = [item for item in configured if isinstance(item, str)]
        if type_version is not None and type_version.key == "ladder" and not keys:
            keys = ["player.tournament.leaders"]
        if (
            type_version is not None
            and type_version.key == "classic"
            and "player.tournament.chats" not in keys
        ):
            keys = [*keys, "player.tournament.chats"]
        return NavigationTournament(
            tournament.id,
            tournament.name,
            tournament.slug,
            tournament.status,
            tuple(
                NavigationTournamentAction(item, type_version.version)
                for item in keys
                if type_version is not None
            ),
        )
