"""Bounded maintenance of technical records without expiring replay protection."""

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, tuple_, update

from sitg_bot.services.reliable_scheduling import PERIODIC_JOBS
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ApplicationLaunchReferenceRecord,
    ApplicationRequestAuditRecord,
    DurableJobRecord,
    MiniAppSessionRecord,
    OutboxEventRecord,
)

# A session lock spans the independently committed batches, including CLI runs.
MAINTENANCE_LOCK = 0x534954475052554E
PERIODIC_JOB_KINDS = frozenset(kind for kind, _, _ in PERIODIC_JOBS)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    success: timedelta = timedelta(days=7)
    failure: timedelta = timedelta(days=30)
    expiry_grace: timedelta = timedelta(days=1)

    def __post_init__(self) -> None:
        if self.success <= timedelta(0) or self.failure <= timedelta(0):
            raise ValueError("Success and failure retention must be positive")
        if self.expiry_grace < timedelta(0):
            raise ValueError("Expiry grace cannot be negative")


@dataclass(slots=True)
class MaintenanceCounts:
    eligible: int = 0
    deleted: int = 0
    compacted: int = 0


@dataclass(slots=True)
class MaintenanceReport:
    dry_run: bool
    categories: dict[str, MaintenanceCounts] = field(default_factory=dict)
    unfinished: list[dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0
    work_limit_reached: bool = False
    lock_acquired: bool = False


@dataclass(slots=True)
class _Category:
    name: str
    model: Any
    timestamp: Any
    conditions: tuple[Any, ...]
    compact: bool = False
    cursor: tuple[datetime, Any] | None = None


def _periodic_job(kind: str, key: str) -> bool:
    prefix = f"periodic:{kind}:"
    bucket = key.removeprefix(prefix)
    return (
        kind in PERIODIC_JOB_KINDS
        and key.startswith(prefix)
        and bucket.isascii()
        and bucket.isdecimal()
    )


class DatabaseMaintenanceService:
    def __init__(
        self,
        database: Database,
        *,
        policy: RetentionPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch_size: int = 1000,
        max_batches: int = 100,
        max_runtime: float = 10,
    ) -> None:
        if (
            not 1 <= batch_size <= 1000 or max_batches < 1
            or not math.isfinite(max_runtime) or max_runtime <= 0
        ):
            raise ValueError("Maintenance limits must be positive; batch size cannot exceed 1000")
        self.database = database
        self.policy = policy or RetentionPolicy()
        self.clock = clock
        self.batch_size = batch_size
        self.max_batches = max_batches
        self.max_runtime = max_runtime

    def _categories(self, now: datetime) -> deque[_Category]:
        success = now - self.policy.success
        failure = now - self.policy.failure
        categories = []
        for status, timestamp, cutoff in (
            ("succeeded", DurableJobRecord.completed_at, success),
            ("failed", DurableJobRecord.failed_at, failure),
        ):
            categories.append(_Category(
                "jobs", DurableJobRecord, timestamp,
                (DurableJobRecord.status == status, timestamp < cutoff,
                 DurableJobRecord.payload_pruned_at.is_(None)),
                compact=True,
            ))
        for status, timestamp, cutoff in (
            ("delivered", OutboxEventRecord.delivered_at, success),
            ("failed", OutboxEventRecord.failed_at, failure),
        ):
            categories.append(_Category(
                "outbox_events", OutboxEventRecord, timestamp,
                (OutboxEventRecord.status == status, timestamp < cutoff,
                 OutboxEventRecord.payload_pruned_at.is_(None),
                 OutboxEventRecord.topic != "telegram.chat.message"),
                compact=True,
            ))
        for outcomes, cutoff in ((["succeeded"], success), (["rejected", "failed"], failure)):
            categories.append(_Category(
                "request_audits", ApplicationRequestAuditRecord,
                ApplicationRequestAuditRecord.completed_at,
                (ApplicationRequestAuditRecord.outcome.in_(outcomes),
                 ApplicationRequestAuditRecord.completed_at < cutoff),
            ))
        for name, model in (
            ("sessions", MiniAppSessionRecord),
            ("launch_references", ApplicationLaunchReferenceRecord),
        ):
            categories.append(_Category(
                name, model, model.expires_at,
                (model.expires_at < now - self.policy.expiry_grace,),
            ))
        return deque(categories)

    async def run_once(self, *, dry_run: bool = True) -> MaintenanceReport:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Maintenance clock must include a timezone")
        started = time.monotonic()
        report = MaintenanceReport(dry_run=dry_run, categories={
            name: MaintenanceCounts() for name in (
                "periodic_jobs", "jobs", "outbox_events", "request_audits",
                "sessions", "launch_references",
            )
        })
        try:
            async with asyncio.timeout(self.max_runtime):
                async with self.database.engine.connect() as guard:
                    try:
                        # Serialize previews with apply runs, without taking row locks.
                        report.lock_acquired = bool(await guard.scalar(
                            select(func.pg_try_advisory_lock(MAINTENANCE_LOCK))
                        ))
                        await guard.commit()
                        if report.lock_acquired:
                            await self._run_batches(report, now)
                            await self._report_unfinished(report, now)
                            await guard.execute(select(func.pg_advisory_unlock(MAINTENANCE_LOCK)))
                            await guard.commit()
                    except BaseException:
                        # Also handles cancellation while acquisition is still in flight.
                        # Closing the connection releases any acquired session lock.
                        await guard.invalidate()
                        raise
        except TimeoutError:
            report.work_limit_reached = True
        finally:
            report.elapsed_seconds = time.monotonic() - started
        return report

    async def _run_batches(self, report: MaintenanceReport, now: datetime) -> None:
        categories = self._categories(now)
        for _ in range(self.max_batches):
            category = categories.popleft()
            rows, actions = await self._batch(category, now, dry_run=report.dry_run)
            # Count only after the batch transaction commits successfully.
            for name, operation, count in actions:
                counts = report.categories[name]
                counts.eligible += count
                if not report.dry_run:
                    setattr(counts, operation, getattr(counts, operation) + count)
            if len(rows) == self.batch_size:
                if report.dry_run:
                    category.cursor = (rows[-1].terminal_at, rows[-1].id)
                categories.append(category)
            if not categories:
                return
        report.work_limit_reached = True

    async def _batch(self, category: _Category, now: datetime, *, dry_run: bool):
        model = category.model
        columns = [model.id, category.timestamp.label("terminal_at")]
        if model is DurableJobRecord:
            columns.extend((model.kind, model.deduplication_key))
        statement = select(*columns).where(*category.conditions)
        if category.cursor is not None:
            statement = statement.where(
                tuple_(category.timestamp, model.id) > category.cursor
            )
        statement = statement.order_by(category.timestamp, model.id).limit(self.batch_size)
        if not dry_run:
            statement = statement.with_for_update(skip_locked=True)
        async with self.database.transaction() as session:
            rows = (await session.execute(statement)).all()
            deletes, compacts = [], []
            for row in rows:
                if category.compact and not (
                    model is DurableJobRecord and _periodic_job(row.kind, row.deduplication_key)
                ):
                    compacts.append(row.id)
                else:
                    deletes.append(row.id)
            if not dry_run:
                if deletes:
                    await session.execute(delete(model).where(
                        model.id.in_(deletes), *category.conditions,
                    ))
                if compacts:
                    await session.execute(update(model).where(
                        model.id.in_(compacts), *category.conditions,
                    ).values(payload={}, last_error=None, payload_pruned_at=now))
            delete_category = "periodic_jobs" if model is DurableJobRecord else category.name
            return rows, (
                (delete_category, "deleted", len(deletes)),
                (category.name, "compacted", len(compacts)),
            )

    async def _report_unfinished(self, report: MaintenanceReport, now: datetime) -> None:
        for model, status, label, states in (
            (DurableJobRecord, DurableJobRecord.status, DurableJobRecord.kind,
             ["queued", "running"]),
            (OutboxEventRecord, OutboxEventRecord.status, OutboxEventRecord.topic,
             ["pending", "processing"]),
            (ApplicationRequestAuditRecord, ApplicationRequestAuditRecord.outcome,
             ApplicationRequestAuditRecord.action, ["started"]),
        ):
            async with self.database.sessions() as session:
                rows = await session.execute(select(
                    status.label("status"), label.label("kind_or_topic"),
                    func.count().label("count"),
                    func.min(model.created_at).label("oldest_created_at"),
                ).where(
                    status.in_(states), model.created_at < now - self.policy.success,
                ).group_by(status, label).order_by(status, label))
                report.unfinished.extend(
                    {"table": model.__tablename__, **dict(row)} for row in rows.mappings()
                )
