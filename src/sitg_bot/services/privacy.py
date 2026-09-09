from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    GameParticipantRecord,
    GameRecord,
    PlayerRecord,
    QuestionRoundRecord,
)


class PrivacyService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def anonymize_player(self, player_id: UUID) -> None:
        async with self.database.transaction() as session:
            player = await session.get(PlayerRecord, player_id, with_for_update=True)
            if player is None:
                raise LookupError("Player not found")
            active_games = await session.scalar(
                select(func.count())
                .select_from(GameParticipantRecord)
                .where(
                    GameParticipantRecord.player_id == player.id,
                    GameParticipantRecord.active.is_(True),
                )
            )
            if active_games:
                raise ValueError("A player in an active game cannot be anonymized")
            player.telegram_user_id = None
            player.real_name = None
            player.public_nickname = f"Deleted player {str(player.id)[:8]}"
            player.telegram_username = None
            player.telegram_public = False
            player.status = "anonymized"
            player.profile_version += 1
            player.profile_updated_at = datetime.now(UTC)

    async def purge_expired_answers(
        self, *, now: datetime | None = None, retention: timedelta = timedelta(days=180)
    ) -> int:
        cutoff = (now or datetime.now(UTC)) - retention
        expired_rounds = (
            select(QuestionRoundRecord.id)
            .join(GameRecord, GameRecord.id == QuestionRoundRecord.game_id)
            .where(
                GameRecord.status.in_({"finalized", "cancelled", "abandoned", "invalidated"}),
                func.coalesce(
                    GameRecord.finalized_at,
                    GameRecord.cancelled_at,
                    GameRecord.abandoned_at,
                    GameRecord.completed_at,
                )
                < cutoff,
            )
        )
        async with self.database.transaction() as session:
            result = await session.execute(
                update(AnswerAttemptRecord)
                .where(
                    AnswerAttemptRecord.round_id.in_(expired_rounds),
                    AnswerAttemptRecord.submitted_answer.is_not(None),
                )
                .values(submitted_answer=None)
            )
            return int(getattr(result, "rowcount", 0))
