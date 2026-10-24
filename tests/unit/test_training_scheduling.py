from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, Uuid, create_engine

from sitg_bot.services.reliable_scheduling import PERIODIC_JOBS, AutomaticJobScheduler
from sitg_bot.storage.models import GameRecord, TrainingSessionRecord


async def test_training_deadlines_are_selected_in_bounded_batches_after_normal_scheduling():
    now = datetime(2026, 1, 1)
    engine = create_engine("sqlite://")
    table = Table(
        "training_sessions", MetaData(), Column("id", Uuid, primary_key=True),
        Column("status", String), Column("deadline", DateTime),
        Column("hidden_deadline", DateTime), Column("text_hidden", Boolean),
    )
    table.create(engine)
    expected = {}
    values = []
    for index in range(505):
        identifier = UUID(int=index + 1)
        deadline = now + timedelta(seconds=index + 10)
        hidden = now + timedelta(seconds=index + 2)
        row = {
            "id": identifier, "status": "active", "deadline": None,
            "hidden_deadline": None, "text_hidden": False,
        }
        if index < 205:
            row["status"] = "paused" if index % 4 == 0 else "active"
            row["deadline"] = deadline if index % 4 != 1 else None
            row["hidden_deadline"] = hidden if index % 4 in {1, 2, 3} else None
            row["text_hidden"] = index % 4 == 3
            expected[str(identifier)] = hidden if index % 4 in {1, 2} else deadline
        elif index % 3 == 0:
            row["hidden_deadline"], row["text_hidden"] = hidden, True
        elif index % 3 == 1:
            row["status"] = "paused"
        else:
            row["status"], row["deadline"] = "completed", deadline
        values.append(row)
    with engine.begin() as connection:
        connection.execute(table.insert(), values)
    batches, calls, jobs = [], [], {}
    game = SimpleNamespace(
        id=UUID(int=900), join_deadline=now, pause_abandonment_deadline=None,
        progression_deadline=None, answer_deadline=None, buzz_deadline=None,
    )

    class Session:
        async def execute(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            if entity is TrainingSessionRecord:
                with engine.connect() as connection:
                    frozen = connection.execute(statement).freeze()
                batches.append(len(frozen().all()))
                return frozen()
            return SimpleNamespace(scalars=lambda: [game] if entity is GameRecord else [])

        async def scalars(self, statement):
            return (await self.execute(statement)).scalars()

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def schedule(kind, key, payload, **kwargs):
        calls.append(kind)
        jobs[key] = (kind, payload, kwargs)

    database = SimpleNamespace(sessions=sessions)
    queue = SimpleNamespace(schedule=schedule)
    try:
        for _ in range(2):
            batches.clear()
            calls.clear()
            scheduled = await AutomaticJobScheduler(database, queue).reconcile(now=now)
            assert scheduled == len(expected) + len(PERIODIC_JOBS) + 1
            assert batches == [100, 100, 5]
            assert calls[0] == "game.deadline"
            assert calls.index("training.deadline") == len(PERIODIC_JOBS) + 1
            training = {
                payload["session_id"]: options["scheduled_at"]
                for kind, payload, options in jobs.values() if kind == "training.deadline"
            }
            assert training == expected
            assert len(jobs) == len(expected) + len(PERIODIC_JOBS) + 1
    finally:
        engine.dispose()
