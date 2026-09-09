import logging
import stat
from uuid import uuid4

from sitg_bot.bot.log_context import (
    correlation_id_context,
    telegram_update_id_context,
)
from sitg_bot.bot.logging_config import configure_bot_logging
from sitg_bot.config import Settings


def test_bot_logging_persists_context_and_redacts_secrets(tmp_path) -> None:  # type: ignore[no-untyped-def]
    token = "123456:private-bot-token"
    database_url = "postgresql+asyncpg://private:password@localhost/sitg"
    log_path = tmp_path / "bot.log"
    settings = Settings(
        bot_token=token,
        database_url=database_url,
        bot_log_path=log_path,
        bot_log_max_bytes=1024,
        bot_log_backup_count=2,
        _env_file=None,
    )
    root = logging.getLogger()
    original_handlers = tuple(root.handlers)
    original_level = root.level
    correlation_id = uuid4()
    correlation_token = correlation_id_context.set(correlation_id)
    update_token = telegram_update_id_context.set(987)
    try:
        configured_path = configure_bot_logging(settings)
        logging.getLogger("sitg_bot.test").error(
            "Failure token=%s database=%s", token, database_url
        )
        for handler in root.handlers:
            handler.flush()
    finally:
        telegram_update_id_context.reset(update_token)
        correlation_id_context.reset(correlation_token)
        for handler in tuple(root.handlers):
            root.removeHandler(handler)
            handler.close()
        root.setLevel(original_level)
        for handler in original_handlers:
            root.addHandler(handler)

    contents = log_path.read_text(encoding="utf-8")
    assert configured_path == log_path.resolve()
    assert f"correlation={correlation_id}" in contents
    assert "update=987" in contents
    assert token not in contents
    assert database_url not in contents
    assert contents.count("[REDACTED]") == 2
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
