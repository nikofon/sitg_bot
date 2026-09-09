import logging
import os
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sitg_bot.bot.log_context import (
    correlation_id_context,
    telegram_update_id_context,
)
from sitg_bot.config import Settings

LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "correlation=%(correlation_id)s update=%(telegram_update_id)s | %(message)s"
)


class BotLogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        correlation_id = correlation_id_context.get()
        update_id = telegram_update_id_context.get()
        record.correlation_id = str(correlation_id) if correlation_id is not None else "-"
        record.telegram_update_id = str(update_id) if update_id is not None else "-"
        return True


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, *, secrets: tuple[str, ...]) -> None:
        super().__init__(fmt)
        self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self.secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        with suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream


def configure_bot_logging(settings: Settings) -> Path:
    """Configure stderr and private rotating-file logs for the Telegram process."""

    path = settings.bot_log_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter(LOG_FORMAT, secrets=_sensitive_values(settings))
    context_filter = BotLogContextFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(context_filter)

    file_handler = PrivateRotatingFileHandler(
        path,
        maxBytes=settings.bot_log_max_bytes,
        backupCount=settings.bot_log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    root = logging.getLogger()
    for handler in tuple(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(settings.bot_log_level)
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.captureWarnings(True)
    return path


def _sensitive_values(settings: Settings) -> tuple[str, ...]:
    values: list[str] = [
        settings.bot_token.get_secret_value(),
        settings.database_url,
    ]
    for secret in (
        settings.application_security_key,
        settings.application_client_token,
        settings.token_delivery_key,
    ):
        if secret is not None:
            values.append(secret.get_secret_value())
    return tuple(values)
