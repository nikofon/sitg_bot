import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    DurableJobRecord,
    GameObserverRecord,
    GameParticipantRecord,
    OutboxEventRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
)


def retry_delay(
    attempts: int,
    *,
    base: timedelta = timedelta(seconds=1),
    maximum: timedelta = timedelta(hours=1),
) -> timedelta:
    """Return a capped exponential delay after a failed attempt."""
    if attempts < 1:
        raise ValueError("Retry attempts must be positive")
    if base <= timedelta(0) or maximum < base:
        raise ValueError("Retry delay bounds are invalid")
    return min(base * (2 ** min(attempts - 1, 20)), maximum)


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    event_id: UUID
    lease_token: UUID
    topic: str
    partition_key: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: UUID
    lease_token: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int


class TransactionalOutbox:
    """Transactional event storage with ordered, leased delivery and durable retries."""

    def __init__(
        self,
        database: Database,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Outbox lease duration must be positive")
        self.database = database
        self.lease_duration = lease_duration

    @staticmethod
    async def enqueue(
        session: AsyncSession,
        *,
        topic: str,
        deduplication_key: str,
        partition_key: str,
        payload: Mapping[str, Any],
        aggregate_type: str | None = None,
        aggregate_id: UUID | None = None,
        aggregate_sequence: int | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 12,
    ) -> UUID:
        if not topic or not deduplication_key or not partition_key:
            raise ValueError("Outbox topic, deduplication key, and partition key are required")
        if (aggregate_type is None) != (aggregate_id is None):
            raise ValueError("Outbox aggregate type and ID must be supplied together")
        if aggregate_sequence is not None and aggregate_sequence < 0:
            raise ValueError("Outbox aggregate sequence cannot be negative")
        if max_attempts < 1:
            raise ValueError("Outbox max attempts must be positive")
        event_id = uuid4()
        inserted_id = await session.scalar(
            pg_insert(OutboxEventRecord)
            .values(
                id=event_id,
                topic=topic,
                deduplication_key=deduplication_key,
                partition_key=partition_key,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                aggregate_sequence=aggregate_sequence,
                payload=dict(payload),
                available_at=available_at or datetime.now(UTC),
                max_attempts=max_attempts,
            )
            .on_conflict_do_nothing(index_elements=["deduplication_key"])
            .returning(OutboxEventRecord.id)
        )
        resolved_id = inserted_id or await session.scalar(
            select(OutboxEventRecord.id).where(
                OutboxEventRecord.deduplication_key == deduplication_key
            )
        )
        assert resolved_id is not None
        return resolved_id

    @classmethod
    async def enqueue_game_event(
        cls,
        session: AsyncSession,
        *,
        game_id: UUID,
        sequence: int,
        kind: str,
        parameters: Mapping[str, Any],
    ) -> None:
        participant_ids = select(GameParticipantRecord.player_id).where(
            GameParticipantRecord.game_id == game_id
        )
        observer_ids = select(GameObserverRecord.player_id).where(
            GameObserverRecord.game_id == game_id,
            GameObserverRecord.active.is_(True),
        )
        player_ids = participant_ids.union(observer_ids).subquery()
        telegram_user_ids = tuple(
            (
                await session.execute(
                    select(PlayerRecord.telegram_user_id)
                    .join(player_ids, player_ids.c.player_id == PlayerRecord.id)
                    .where(PlayerRecord.telegram_user_id.is_not(None))
                    .order_by(PlayerRecord.telegram_user_id)
                )
            ).scalars()
        )
        for telegram_user_id in telegram_user_ids:
            assert telegram_user_id is not None
            await cls.enqueue(
                session,
                topic="game.event",
                deduplication_key=(f"game:{game_id}:event:{sequence}:chat:{telegram_user_id}"),
                partition_key=f"telegram:chat:{telegram_user_id}",
                aggregate_type="game",
                aggregate_id=game_id,
                aggregate_sequence=sequence,
                payload={
                    "recipient_telegram_user_id": telegram_user_id,
                    "message_key": f"game.event.{kind}",
                    "parameters": dict(parameters),
                    "game_id": str(game_id),
                    "sequence": sequence,
                },
            )

    @classmethod
    async def enqueue_lobby_event(
        cls,
        session: AsyncSession,
        *,
        lobby_id: UUID,
        sequence: int,
        kind: str,
        parameters: Mapping[str, Any],
    ) -> None:
        telegram_user_ids = tuple(
            (
                await session.execute(
                    select(PlayerRecord.telegram_user_id)
                    .join(
                        PregameLobbyMemberRecord,
                        PregameLobbyMemberRecord.player_id == PlayerRecord.id,
                    )
                    .where(
                        PregameLobbyMemberRecord.lobby_id == lobby_id,
                        PlayerRecord.telegram_user_id.is_not(None),
                    )
                    .order_by(PlayerRecord.telegram_user_id)
                )
            ).scalars()
        )
        for telegram_user_id in telegram_user_ids:
            assert telegram_user_id is not None
            await cls.enqueue(
                session,
                topic="lobby.event",
                deduplication_key=(f"lobby:{lobby_id}:event:{sequence}:chat:{telegram_user_id}"),
                partition_key=f"telegram:chat:{telegram_user_id}",
                aggregate_type="lobby",
                aggregate_id=lobby_id,
                aggregate_sequence=sequence,
                payload={
                    "recipient_telegram_user_id": telegram_user_id,
                    "message_key": f"lobby.event.{kind}",
                    "parameters": dict(parameters),
                    "lobby_id": str(lobby_id),
                    "sequence": sequence,
                },
            )

    async def claim(
        self,
        *,
        topics: tuple[str, ...] = (),
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[OutboxDelivery, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("Outbox claim limit must be between 1 and 500")
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            await session.execute(
                update(OutboxEventRecord)
                .where(
                    OutboxEventRecord.status == "processing",
                    OutboxEventRecord.lease_expires_at <= now,
                    OutboxEventRecord.attempts >= OutboxEventRecord.max_attempts,
                )
                .values(
                    status="failed",
                    failed_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error="Delivery lease expired after the final attempt",
                )
            )
            earlier = aliased(OutboxEventRecord)
            current_sequence = func.coalesce(OutboxEventRecord.aggregate_sequence, 2_147_483_647)
            earlier_sequence = func.coalesce(earlier.aggregate_sequence, 2_147_483_647)
            has_earlier_partition_event = exists(
                select(earlier.id).where(
                    earlier.partition_key == OutboxEventRecord.partition_key,
                    # Order messages within the consumer's topic subscription. Domain
                    # event streams may have no presentation consumer yet.
                    earlier.topic.in_(topics) if topics else True,
                    earlier.status.in_(("pending", "processing")),
                    or_(
                        earlier.created_at < OutboxEventRecord.created_at,
                        and_(
                            earlier.created_at == OutboxEventRecord.created_at,
                            earlier_sequence < current_sequence,
                        ),
                        and_(
                            earlier.created_at == OutboxEventRecord.created_at,
                            earlier_sequence == current_sequence,
                            earlier.id < OutboxEventRecord.id,
                        ),
                    ),
                )
            )
            claim_filters = [
                ~has_earlier_partition_event,
                OutboxEventRecord.available_at <= now,
                OutboxEventRecord.attempts < OutboxEventRecord.max_attempts,
                or_(
                    OutboxEventRecord.status == "pending",
                    OutboxEventRecord.lease_expires_at <= now,
                ),
            ]
            if topics:
                claim_filters.append(OutboxEventRecord.topic.in_(topics))
            records = tuple(
                (
                    await session.execute(
                        select(OutboxEventRecord)
                        .where(*claim_filters)
                        .order_by(OutboxEventRecord.available_at, OutboxEventRecord.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            deliveries: list[OutboxDelivery] = []
            for record in records:
                token = uuid4()
                record.status = "processing"
                record.attempts += 1
                record.lease_token = token
                record.lease_expires_at = now + self.lease_duration
                deliveries.append(
                    OutboxDelivery(
                        record.id,
                        token,
                        record.topic,
                        record.partition_key,
                        dict(record.payload),
                        record.attempts,
                    )
                )
            return tuple(deliveries)

    async def acknowledge(self, event_id: UUID, lease_token: UUID) -> None:
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            record = await self._leased_event(session, event_id, lease_token)
            record.status = "delivered"
            record.delivered_at = now
            record.lease_token = None
            record.lease_expires_at = None
            record.last_error = None

    async def reject(
        self,
        event_id: UUID,
        lease_token: UUID,
        error: str,
        *,
        terminal: bool = False,
        retry_after: timedelta | None = None,
    ) -> None:
        if retry_after is not None and retry_after < timedelta(0):
            raise ValueError("Outbox retry-after delay cannot be negative")
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            record = await self._leased_event(session, event_id, lease_token)
            record.last_error = error[:4000]
            record.lease_token = None
            record.lease_expires_at = None
            if terminal or record.attempts >= record.max_attempts:
                record.status = "failed"
                record.failed_at = now
            else:
                record.status = "pending"
                record.available_at = now + (
                    retry_after if retry_after is not None else retry_delay(record.attempts)
                )

    @staticmethod
    async def _leased_event(
        session: AsyncSession, event_id: UUID, lease_token: UUID
    ) -> OutboxEventRecord:
        record = await session.get(OutboxEventRecord, event_id, with_for_update=True)
        if record is None or record.status != "processing" or record.lease_token != lease_token:
            raise LookupError("Active outbox delivery lease not found")
        return record


class DurableJobQueue:
    """Persistent idempotent job queue with leases and terminal failure records."""

    def __init__(
        self,
        database: Database,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Job lease duration must be positive")
        self.database = database
        self.lease_duration = lease_duration

    async def schedule(
        self,
        kind: str,
        deduplication_key: str,
        payload: Mapping[str, Any],
        *,
        scheduled_at: datetime | None = None,
        priority: int = 100,
        max_attempts: int = 12,
    ) -> UUID:
        if not kind or not deduplication_key:
            raise ValueError("Job kind and deduplication key are required")
        if max_attempts < 1:
            raise ValueError("Job max attempts must be positive")
        async with self.database.transaction() as session:
            job_id = uuid4()
            inserted_id = await session.scalar(
                pg_insert(DurableJobRecord)
                .values(
                    id=job_id,
                    kind=kind,
                    deduplication_key=deduplication_key,
                    payload=dict(payload),
                    scheduled_at=scheduled_at or datetime.now(UTC),
                    priority=priority,
                    max_attempts=max_attempts,
                )
                .on_conflict_do_nothing(index_elements=["deduplication_key"])
                .returning(DurableJobRecord.id)
            )
            if inserted_id is not None:
                return inserted_id
            existing_id = await session.scalar(
                select(DurableJobRecord.id).where(
                    DurableJobRecord.deduplication_key == deduplication_key
                )
            )
            assert existing_id is not None
            return existing_id

    async def claim(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> tuple[ClaimedJob, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("Job claim limit must be between 1 and 500")
        now = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            await session.execute(
                update(DurableJobRecord)
                .where(
                    DurableJobRecord.status == "running",
                    DurableJobRecord.lease_expires_at <= now,
                    DurableJobRecord.attempts >= DurableJobRecord.max_attempts,
                )
                .values(
                    status="failed",
                    failed_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error="Job lease expired after the final attempt",
                )
            )
            records = tuple(
                (
                    await session.execute(
                        select(DurableJobRecord)
                        .where(
                            DurableJobRecord.scheduled_at <= now,
                            DurableJobRecord.attempts < DurableJobRecord.max_attempts,
                            or_(
                                DurableJobRecord.status == "queued",
                                (DurableJobRecord.status == "running")
                                & (DurableJobRecord.lease_expires_at <= now),
                            ),
                        )
                        .order_by(
                            DurableJobRecord.priority,
                            DurableJobRecord.scheduled_at,
                            DurableJobRecord.created_at,
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            jobs: list[ClaimedJob] = []
            for record in records:
                token = uuid4()
                record.status = "running"
                record.attempts += 1
                record.lease_token = token
                record.lease_expires_at = now + self.lease_duration
                jobs.append(
                    ClaimedJob(
                        record.id,
                        token,
                        record.kind,
                        dict(record.payload),
                        record.attempts,
                    )
                )
            return tuple(jobs)

    async def succeed(self, job_id: UUID, lease_token: UUID) -> None:
        async with self.database.transaction() as session:
            record = await self._leased_job(session, job_id, lease_token)
            record.status = "succeeded"
            record.completed_at = datetime.now(UTC)
            record.lease_token = None
            record.lease_expires_at = None
            record.last_error = None

    async def fail(
        self,
        job_id: UUID,
        lease_token: UUID,
        error: str,
        *,
        terminal: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            record = await self._leased_job(session, job_id, lease_token)
            record.last_error = error[:4000]
            record.lease_token = None
            record.lease_expires_at = None
            if terminal or record.attempts >= record.max_attempts:
                record.status = "failed"
                record.failed_at = now
            else:
                record.status = "queued"
                record.scheduled_at = now + retry_delay(record.attempts)

    @staticmethod
    async def _leased_job(
        session: AsyncSession, job_id: UUID, lease_token: UUID
    ) -> DurableJobRecord:
        record = await session.get(DurableJobRecord, job_id, with_for_update=True)
        if record is None or record.status != "running" or record.lease_token != lease_token:
            raise LookupError("Active durable job lease not found")
        return record


JobHandler = Callable[[dict[str, Any]], Awaitable[None]]


class DurableJobWorker:
    def __init__(
        self,
        queue: DurableJobQueue,
        handlers: Mapping[str, JobHandler],
        *,
        poll_interval: float = 0.25,
        batch_size: int = 20,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("Job worker poll interval must be positive")
        self.queue = queue
        self.handlers = dict(handlers)
        self.poll_interval = poll_interval
        self.batch_size = batch_size

    async def run_once(self) -> int:
        jobs = await self.queue.claim(limit=self.batch_size)
        for job in jobs:
            handler = self.handlers.get(job.kind)
            if handler is None:
                await self.queue.fail(
                    job.job_id,
                    job.lease_token,
                    f"No handler is registered for job kind {job.kind}",
                    terminal=True,
                )
                continue
            try:
                await handler(job.payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.queue.fail(job.job_id, job.lease_token, repr(error))
            else:
                await self.queue.succeed(job.job_id, job.lease_token)
        return len(jobs)

    async def run_forever(self) -> None:
        while True:
            processed = await self.run_once()
            if processed == 0:
                await asyncio.sleep(self.poll_interval)
