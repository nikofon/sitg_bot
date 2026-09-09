import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ApplicationLaunchReferenceRecord,
    GameParticipantRecord,
    PacketDraftRecord,
    PlatformAdministratorRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
)


@dataclass(frozen=True, slots=True)
class LaunchReference:
    value: str
    expires_at: datetime

    @property
    def telegram_payload(self) -> str:
        return f"lr_{self.value}"


@dataclass(frozen=True, slots=True)
class ResolvedLaunchRoute:
    route: str
    target_id: UUID


class LaunchReferenceService:
    """Maps compact opaque Telegram/URL values to authorized application routes."""

    def __init__(
        self,
        database: Database,
        *,
        signing_key: str,
        bot_id: int,
        environment: str = "production",
        default_lifetime: timedelta = timedelta(minutes=10),
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("Launch-reference signing key must contain at least 32 characters")
        if bot_id <= 0:
            raise ValueError("Telegram bot ID must be positive")
        if environment not in {"production", "test"}:
            raise ValueError("Telegram environment must be production or test")
        if default_lifetime <= timedelta(0):
            raise ValueError("Launch-reference lifetime must be positive")
        self.database = database
        self.signing_key = signing_key.encode()
        self.bot_id = bot_id
        self.environment = environment
        self.default_lifetime = default_lifetime

    async def create(
        self,
        *,
        route: str,
        target_id: UUID,
        created_by_player_id: UUID,
        intended_player_id: UUID | None = None,
        lifetime: timedelta | None = None,
        one_time: bool = True,
    ) -> LaunchReference:
        if route not in {
            "lobby",
            "report",
            "manager_settings",
            "manager_management",
            "packet_draft",
        }:
            raise ValueError("Unknown launch route")
        ttl = lifetime or self.default_lifetime
        if ttl <= timedelta(0) or ttl > timedelta(hours=24):
            raise ValueError("Launch-reference lifetime must be within 24 hours")
        raw_reference = secrets.token_urlsafe(18)
        now = datetime.now(UTC)
        expires_at = now + ttl
        async with self.database.transaction() as session:
            creator = await session.get(PlayerRecord, created_by_player_id)
            if creator is None or creator.status != "active":
                raise PermissionError("Active launch-reference creator is required")
            await self._require_route_access(
                session, route=route, target_id=target_id, player_id=created_by_player_id
            )
            if intended_player_id is not None:
                intended = await session.get(PlayerRecord, intended_player_id)
                if intended is None or intended.status != "active":
                    raise LookupError("Intended launch-reference player not found")
            session.add(
                ApplicationLaunchReferenceRecord(
                    token_digest=self._digest(raw_reference),
                    route=route,
                    target_id=target_id,
                    created_by_player_id=created_by_player_id,
                    intended_player_id=intended_player_id,
                    bot_id=self.bot_id,
                    environment=self.environment,
                    expires_at=expires_at,
                    one_time=one_time,
                )
            )
        reference = LaunchReference(raw_reference, expires_at)
        if len(reference.telegram_payload.encode()) > 64:
            raise RuntimeError("Generated launch reference exceeds Telegram payload limit")
        return reference

    async def resolve(
        self,
        raw_reference: str,
        *,
        player_id: UUID,
    ) -> ResolvedLaunchRoute:
        if not raw_reference or len(raw_reference) > 64:
            raise LookupError("Launch reference not found")
        async with self.database.transaction() as session:
            record = await session.scalar(
                select(ApplicationLaunchReferenceRecord)
                .where(ApplicationLaunchReferenceRecord.token_digest == self._digest(raw_reference))
                .with_for_update()
            )
            now = datetime.now(UTC)
            if (
                record is None
                or record.bot_id != self.bot_id
                or record.environment != self.environment
                or record.expires_at <= now
                or (record.one_time and record.redeemed_at is not None)
            ):
                raise LookupError("Launch reference not found")
            if record.intended_player_id is not None and record.intended_player_id != player_id:
                raise PermissionError("Launch reference belongs to another player")
            await self._require_route_access(
                session,
                route=record.route,
                target_id=record.target_id,
                player_id=player_id,
            )
            if record.one_time:
                record.redeemed_by_player_id = player_id
                record.redeemed_at = now
            return ResolvedLaunchRoute(record.route, record.target_id)

    @staticmethod
    async def _require_route_access(
        session: AsyncSession,
        *,
        route: str,
        target_id: UUID,
        player_id: UUID,
    ) -> None:
        if route == "lobby":
            membership = await session.scalar(
                select(PregameLobbyMemberRecord.id).where(
                    PregameLobbyMemberRecord.lobby_id == target_id,
                    PregameLobbyMemberRecord.player_id == player_id,
                    PregameLobbyMemberRecord.active.is_(True),
                )
            )
        elif route == "report":
            membership = await session.scalar(
                select(GameParticipantRecord.id).where(
                    GameParticipantRecord.game_id == target_id,
                    GameParticipantRecord.player_id == player_id,
                )
            )
        elif route in {"manager_settings", "manager_management"}:
            membership = await session.scalar(
                select(TournamentManagerRecord.player_id).where(
                    TournamentManagerRecord.tournament_id == target_id,
                    TournamentManagerRecord.player_id == player_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
            )
        else:
            draft = await session.get(PacketDraftRecord, target_id)
            if draft is None:
                membership = None
            else:
                administrator = await session.scalar(
                    select(PlatformAdministratorRecord.player_id).where(
                        PlatformAdministratorRecord.player_id == player_id,
                        PlatformAdministratorRecord.revoked_at.is_(None),
                    )
                )
                manager = await session.scalar(
                    select(TournamentManagerRecord.player_id).where(
                        TournamentManagerRecord.tournament_id
                        == draft.creation_tournament_id,
                        TournamentManagerRecord.player_id == player_id,
                        TournamentManagerRecord.revoked_at.is_(None),
                    )
                )
                member = await session.scalar(
                    select(TournamentMembershipRecord.player_id).where(
                        TournamentMembershipRecord.tournament_id
                        == draft.creation_tournament_id,
                        TournamentMembershipRecord.player_id == player_id,
                        TournamentMembershipRecord.status == "active",
                    )
                )
                membership = administrator or manager or (
                    member if draft.uploader_id == player_id else None
                )
        if membership is None:
            raise PermissionError("Player cannot access the launch route")

    def _digest(self, value: str) -> str:
        return hmac.new(self.signing_key, value.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def parse_telegram_payload(payload: str) -> str:
        if not payload.startswith("lr_") or len(payload.encode()) > 64:
            raise ValueError("Invalid launch-reference payload")
        value = payload[3:]
        if not value or not all(character.isalnum() or character in "-_" for character in value):
            raise ValueError("Invalid launch-reference payload")
        return value
