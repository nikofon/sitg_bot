from contextvars import ContextVar
from uuid import UUID

correlation_id_context: ContextVar[UUID | None] = ContextVar("correlation_id", default=None)
telegram_update_id_context: ContextVar[int | None] = ContextVar("telegram_update_id", default=None)
