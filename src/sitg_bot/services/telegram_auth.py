import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from sitg_bot.application.telegram import (
    DuplicateTelegramUpdate,
    TelegramUpdateClaim,
    TelegramUpdateRejected,
)
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import TelegramUpdateReceiptRecord


class TelegramUpdateAuthService:
    """Durably deduplicates identities extracted from verified Bot API updates."""

    def __init__(
        self,
        database: Database,
        *,
        environment: str = "production",
        processing_lease: timedelta = timedelta(minutes=5),
    ) -> None:
        if environment not in {"production", "test"}:
            raise ValueError("Telegram environment must be production or test")
        if processing_lease <= timedelta(0):
            raise ValueError("Telegram update processing lease must be positive")
        self.database = database
        self.environment = environment
        self.processing_lease = processing_lease

    async def claim(
        self,
        *,
        bot_id: int,
        update_id: int,
        telegram_user_id: int,
        correlation_id: UUID | None = None,
    ) -> TelegramUpdateClaim:
        if min(bot_id, telegram_user_id) <= 0 or update_id < 0:
            raise TelegramUpdateRejected("Invalid Telegram update identity")
        correlation_id = correlation_id or uuid4()
        async with self.database.transaction() as session:
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(self._lock_key(bot_id, self.environment, update_id))
                )
            )
            existing = await session.scalar(
                select(TelegramUpdateReceiptRecord)
                .where(
                    TelegramUpdateReceiptRecord.bot_id == bot_id,
                    TelegramUpdateReceiptRecord.environment == self.environment,
                    TelegramUpdateReceiptRecord.update_id == update_id,
                )
                .with_for_update()
            )
            if existing is not None:
                raise DuplicateTelegramUpdate("Telegram update was already claimed")
            now = datetime.now(UTC)
            receipt = TelegramUpdateReceiptRecord(
                bot_id=bot_id,
                environment=self.environment,
                update_id=update_id,
                correlation_id=correlation_id,
                status="processing",
                lease_expires_at=now + self.processing_lease,
            )
            session.add(receipt)
            await session.flush()
            return TelegramUpdateClaim(
                receipt.id,
                correlation_id,
                bot_id,
                self.environment,
                update_id,
                telegram_user_id,
            )

    async def complete(
        self,
        receipt_id: UUID,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        async with self.database.transaction() as session:
            receipt = await session.get(
                TelegramUpdateReceiptRecord, receipt_id, with_for_update=True
            )
            if receipt is None:
                raise LookupError("Telegram update receipt not found")
            if receipt.status != "processing":
                return
            receipt.status = "completed" if succeeded else "failed"
            receipt.error_code = error_code
            receipt.completed_at = datetime.now(UTC)

    @staticmethod
    def _lock_key(bot_id: int, environment: str, update_id: int) -> int:
        digest = hashlib.sha256(f"{bot_id}:{environment}:{update_id}".encode()).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)
