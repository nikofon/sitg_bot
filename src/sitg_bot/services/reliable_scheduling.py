from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from sitg_bot.services.reliable_delivery import DurableJobQueue
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import AppealRecord, GameRecord, PregameLobbyRecord


class AutomaticJobScheduler:
    """Reconciles authoritative deadlines into idempotent durable jobs."""

    def __init__(self, database: Database, queue: DurableJobQueue) -> None:
        self.database = database
        self.queue = queue

    async def reconcile(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        scheduled = 0
        async with self.database.sessions() as session:
            games = tuple(
                (
                    await session.execute(
                        select(GameRecord).where(GameRecord.status.in_(("lobby", "active")))
                    )
                ).scalars()
            )
            lobbies = tuple(
                (
                    await session.execute(
                        select(PregameLobbyRecord).where(PregameLobbyRecord.status == "assembling")
                    )
                ).scalars()
            )
            appeals = tuple(
                (
                    await session.execute(
                        select(AppealRecord).where(
                            AppealRecord.status.in_(
                                (
                                    "voting",
                                    "awaiting_escalation",
                                    "awaiting_commentary",
                                    "escalated",
                                )
                            )
                        )
                    )
                ).scalars()
            )

        for game in games:
            deadline = self._game_deadline(game)
            if deadline is None:
                continue
            await self.queue.schedule(
                "game.deadline",
                self._key("game", game.id, deadline),
                {"game_id": str(game.id), "expected_deadline": deadline.isoformat()},
                scheduled_at=deadline,
                priority=10,
            )
            scheduled += 1
        for lobby in lobbies:
            await self.queue.schedule(
                "lobby.expire",
                self._key("lobby", lobby.id, lobby.expires_at),
                {"lobby_id": str(lobby.id), "expected_deadline": lobby.expires_at.isoformat()},
                scheduled_at=lobby.expires_at,
                priority=20,
            )
            scheduled += 1
        for appeal in appeals:
            deadline = self._appeal_deadline(appeal)
            if deadline is None:
                continue
            await self.queue.schedule(
                "appeal.deadline",
                self._key("appeal", appeal.id, deadline),
                {"appeal_id": str(appeal.id), "game_id": str(appeal.game_id)},
                scheduled_at=deadline,
                priority=15,
            )
            scheduled += 1

        scheduled += await self._periodic(
            "rating.settlement", now, every=timedelta(seconds=1), priority=30
        )
        scheduled += await self._periodic(
            "matchmaking.scan", now, every=timedelta(seconds=1), priority=40
        )
        scheduled += await self._periodic(
            "classic.reconcile", now, every=timedelta(seconds=1), priority=35
        )
        scheduled += await self._periodic(
            "suspicion.tick", now, every=timedelta(seconds=30), priority=80
        )
        scheduled += await self._periodic(
            "tournament.start_reminder", now, every=timedelta(seconds=30), priority=50
        )
        scheduled += await self._periodic(
            "classic.chat.reminder", now, every=timedelta(seconds=30), priority=55
        )
        return scheduled

    async def _periodic(
        self,
        kind: str,
        now: datetime,
        *,
        every: timedelta,
        priority: int,
    ) -> int:
        interval_seconds = int(every.total_seconds())
        bucket = int(now.timestamp()) // interval_seconds
        await self.queue.schedule(
            kind,
            f"periodic:{kind}:{bucket}",
            {"scheduled_bucket": bucket},
            scheduled_at=now,
            priority=priority,
        )
        return 1

    @staticmethod
    def _game_deadline(game: GameRecord) -> datetime | None:
        candidates = (
            game.join_deadline,
            game.pause_abandonment_deadline,
            game.progression_deadline,
            game.answer_deadline,
            game.buzz_deadline,
        )
        return min((value for value in candidates if value is not None), default=None)

    @staticmethod
    def _appeal_deadline(appeal: AppealRecord) -> datetime | None:
        return {
            "voting": appeal.vote_deadline,
            "awaiting_escalation": appeal.escalation_deadline,
            "awaiting_commentary": appeal.commentary_deadline,
            "escalated": appeal.ticket_expires_at,
        }.get(appeal.status)

    @staticmethod
    def _key(prefix: str, identifier: object, deadline: datetime) -> str:
        return f"deadline:{prefix}:{identifier}:{deadline.isoformat()}"
