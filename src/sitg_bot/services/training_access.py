"""Account guards shared by training uploads and normal-game entry."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.storage.models import PlayerBanRecord, PlayerRecord, TrainingSessionRecord


async def require_trainee(
    session: AsyncSession,
    player_id: UUID,
    *,
    lock: bool = False,
) -> PlayerRecord:
    player = await session.get(PlayerRecord, player_id, with_for_update=lock)
    if player is None or player.status != "active":
        raise PermissionError("Completed registration is required")
    banned = await session.scalar(
        select(PlayerBanRecord.player_id).where(
            PlayerBanRecord.player_id == player_id,
            PlayerBanRecord.lifted_at.is_(None),
        )
    )
    if banned is not None:
        raise PermissionError("Training is unavailable while banned")
    return player


async def require_no_training(session: AsyncSession, player_id: UUID) -> None:
    if await session.scalar(
        select(TrainingSessionRecord.id)
        .where(
            TrainingSessionRecord.owner_id == player_id,
            TrainingSessionRecord.status.in_(("active", "paused")),
        )
        .limit(1)
    ):
        raise ValueError("Stop training before entering a normal game")
