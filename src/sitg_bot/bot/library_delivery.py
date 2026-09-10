import re

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from sitg_bot.application.protocol import RetryableDeliveryError, TerminalDeliveryError
from sitg_bot.packet_export import library_docx


def library_document_delivery_handler(bot: Bot):
    async def deliver(payload: dict[str, object]) -> None:
        name = str(payload["name"])
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:120] or "packet"
        content = library_docx(name, payload["pages"])
        try:
            await bot.send_document(
                chat_id=int(payload["recipient_telegram_user_id"]),
                document=BufferedInputFile(content, filename=f"{filename}.docx"),
            )
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    return deliver
