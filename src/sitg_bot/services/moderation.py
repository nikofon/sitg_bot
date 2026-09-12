import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    BugReportRecord,
    PlatformAdministratorRecord,
    PlayerBanRecord,
    PlayerRecord,
)

BAN_REASON_MAX_LENGTH = 2000
BUG_COMMENTARY_MAX_LENGTH = 4000
_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


@dataclass(frozen=True, slots=True)
class BanReceipt:
    player_id: UUID
    display_name: str | None
    telegram_username: str | None
    reason: str | None
    banned_at: datetime


@dataclass(frozen=True, slots=True)
class UnbanReceipt:
    player_id: UUID
    display_name: str | None
    telegram_username: str | None


@dataclass(frozen=True, slots=True)
class BugReportReceipt:
    report_id: UUID
    reporter_player_id: UUID
    created_at: datetime


class PlayerModerationService:
    """Admin-issued reversible player bans identified by player ID or Telegram username."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def ban_player(
        self,
        administrator_id: UUID,
        target: str,
        *,
        reason: str | None,
    ) -> BanReceipt:
        normalized_target = _normalize_target(target)
        normalized_reason = (reason or "").strip() or "Not specified"
        if len(normalized_reason) > BAN_REASON_MAX_LENGTH:
            raise ValueError("Ban reason is too long")
        async with self.database.transaction() as session:
            await _require_administrator(session, administrator_id)
            player = await _resolve_player(session, normalized_target)
            if player is None:
                raise LookupError("Player not found")
            administrator_role = await session.get(PlatformAdministratorRecord, player.id)
            if administrator_role is not None and administrator_role.revoked_at is None:
                raise PermissionError("Platform administrators cannot be banned")
            ban = await session.get(PlayerBanRecord, player.id, with_for_update=True)
            now = datetime.now(UTC)
            if ban is None:
                session.add(
                    PlayerBanRecord(
                        player_id=player.id,
                        reason=normalized_reason,
                        banned_by_id=administrator_id,
                        banned_at=now,
                    )
                )
            elif ban.lifted_at is not None:
                ban.reason = normalized_reason
                ban.banned_by_id = administrator_id
                ban.banned_at = now
                ban.lifted_by_id = None
                ban.lifted_at = None
            else:
                raise ValueError("Player is already banned")
            return BanReceipt(
                player.id,
                player.public_nickname,
                player.telegram_username,
                normalized_reason,
                now,
            )

    async def unban_player(
        self,
        administrator_id: UUID,
        target: str,
    ) -> UnbanReceipt:
        normalized_target = _normalize_target(target)
        async with self.database.transaction() as session:
            await _require_administrator(session, administrator_id)
            player = await _resolve_player(session, normalized_target)
            if player is None:
                raise LookupError("Player not found")
            ban = await session.get(PlayerBanRecord, player.id, with_for_update=True)
            if ban is None or ban.lifted_at is not None:
                raise ValueError("Player is not banned")
            now = datetime.now(UTC)
            ban.lifted_by_id = administrator_id
            ban.lifted_at = now
            return UnbanReceipt(player.id, player.public_nickname, player.telegram_username)

    @staticmethod
    async def active_ban_reason(session: AsyncSession, player_id: UUID) -> str | None:
        ban = await session.scalar(
            select(PlayerBanRecord.reason).where(
                PlayerBanRecord.player_id == player_id,
                PlayerBanRecord.lifted_at.is_(None),
            )
        )
        return None if ban is None else str(ban)


class BugReportService:
    """Stores player bug reports and notifies platform administrators."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def submit(self, player_id: UUID, commentary: str) -> BugReportReceipt:
        normalized = commentary.strip()
        if not normalized:
            raise ValueError("Bug commentary is required")
        if len(normalized) > BUG_COMMENTARY_MAX_LENGTH:
            raise ValueError("Bug commentary is too long")
        async with self.database.transaction() as session:
            reporter = await session.get(PlayerRecord, player_id)
            if reporter is None or reporter.status != "active":
                raise PermissionError("Completed player registration is required")
            report = BugReportRecord(
                reporter_player_id=reporter.id,
                commentary=normalized,
            )
            session.add(report)
            await session.flush()
            created_at = report.created_at or datetime.now(UTC)
            await NotificationWriter.create_for_administrators(
                session,
                kind="bug_report",
                deduplication_key=f"bug-report:{report.id}",
                payload={
                    "report_id": str(report.id),
                    "reporter_player_id": str(reporter.id),
                    "reporter_nickname": reporter.public_nickname,
                    "reporter_telegram_username": reporter.telegram_username,
                    "commentary": normalized,
                    "created_at": created_at.isoformat(),
                },
            )
            return BugReportReceipt(report.id, reporter.id, created_at)


def _normalize_target(target: str) -> str:
    normalized = target.strip().removeprefix("@")
    if not normalized:
        raise ValueError("Ban target is required")
    return normalized


async def _resolve_player(session: AsyncSession, target: str) -> PlayerRecord | None:
    try:
        player_id = UUID(target)
    except ValueError:
        player_id = None
    if player_id is not None:
        return await session.get(PlayerRecord, player_id)
    if _TELEGRAM_USERNAME_RE.fullmatch(target) is None:
        raise ValueError("Ban target is not a player ID or Telegram username")
    return await session.scalar(
        select(PlayerRecord).where(func.lower(PlayerRecord.telegram_username) == target.lower())
    )


async def _require_administrator(session: AsyncSession, administrator_id: UUID) -> None:
    administrator = await session.get(PlatformAdministratorRecord, administrator_id)
    if administrator is None or administrator.revoked_at is not None:
        raise PermissionError("Platform administrator rights are required")
