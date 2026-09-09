from dataclasses import dataclass
from uuid import UUID


class TelegramUpdateRejected(PermissionError):
    """Raised when an update cannot establish an authenticated private-chat user."""


class DuplicateTelegramUpdate(ValueError):
    """Raised when a bot already claimed the Telegram update ID."""


@dataclass(frozen=True, slots=True)
class TelegramUpdateClaim:
    receipt_id: UUID
    correlation_id: UUID
    bot_id: int
    environment: str
    update_id: int
    telegram_user_id: int
