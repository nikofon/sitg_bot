from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from sitg_bot.services.reliable_delivery import DurableJobQueue
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AppealRecord,
    GameRecord,
    PregameLobbyRecord,
    TrainingSessionRecord,
)

PERIODIC_JOBS = (
    ("rating.settlement", timedelta(seconds=1), 30),
    ("matchmaking.scan", timedelta(seconds=1), 40),
    ("classic.reconcile", timedelta(seconds=1), 35),
    ("suspicion.tick", timedelta(seconds=30), 80),
    ("tournament.start_reminder", timedelta(seconds=30), 50),
    ("classic.chat.reminder", timedelta(seconds=30), 55),
    ("packet.availability", timedelta(seconds=30), 55),
    ("game.finished_closure", timedelta(seconds=30), 60),
)


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

        for kind, every, priority in PERIODIC_JOBS:
            scheduled += await self._periodic(kind, now, every=every, priority=priority)

        async with self.database.sessions() as session:
            cursor = None
            model = TrainingSessionRecord
            while True:
                statement = select(
                    model.id, model.deadline, model.hidden_deadline, model.text_hidden,
                ).where(
                    model.status.in_(("active", "paused")),
                    model.deadline.is_not(None)
                    | (model.hidden_deadline.is_not(None) & model.text_hidden.is_(False)),
                )
                if cursor is not None:
                    statement = statement.where(model.id > cursor)
                training = (await session.execute(statement.order_by(model.id).limit(100))).all()
                for record in training:
                    deadline = min(value for value in (
                        record.deadline,
                        record.hidden_deadline if not record.text_hidden else None,
                    ) if value is not None)
                    await self.queue.schedule(
                        "training.deadline", self._key("training", record.id, deadline),
                        {"session_id": str(record.id)}, scheduled_at=deadline, priority=12,
                    )
                    scheduled += 1
                if len(training) < 100:
                    break
                cursor = training[-1].id
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
