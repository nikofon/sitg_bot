import asyncio
import logging

from sitg_bot.bot.app import run_polling
from sitg_bot.bot.logging_config import configure_bot_logging
from sitg_bot.config import Settings


async def main(settings: Settings | None = None) -> None:
    await run_polling(settings or Settings())  # type: ignore[call-arg]


def run() -> None:
    settings = Settings()  # type: ignore[call-arg]
    log_path = configure_bot_logging(settings)
    logger = logging.getLogger(__name__)
    logger.info(
        "Telegram bot process starting environment=%s log_path=%s",
        settings.telegram_environment,
        log_path,
    )
    try:
        asyncio.run(main(settings))
    except KeyboardInterrupt:
        logger.info("Telegram bot process stopped by operator")
    except Exception:
        logger.exception("Telegram bot process terminated unexpectedly")
        raise


if __name__ == "__main__":
    run()
