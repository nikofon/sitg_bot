"""Relay media inside Telegram; no file download or upload is performed here."""

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from sitg_bot.application.protocol import RetryableDeliveryError, TerminalDeliveryError
from sitg_bot.bot.presenters.game import plain_chunks


def chat_delivery_handler(bot, localization, protocol, game_delivery):
    async def deliver(payload):
        chat = int(payload["recipient_telegram_user_id"])
        prefix = f"chat:{payload['source_chat_id']}:{payload['source_message_id']}"

        async def state(**values):
            return await protocol.request("telegram.chat.delivery", payload=payload, **values)

        async def record(key, message_id):
            result = await state(key=key, message_id=message_id)
            return not result["skip"]

        try:
            # Share the gameplay lock so chat delivery and cleanup cannot interleave locally.
            async with game_delivery.locks[chat]:
                delivery = await state()
                if delivery["skip"]:
                    return
                locale = payload["locale"]
                label = "chat.whisper_header" if payload["whisper"] else "chat.header"
                header = localization.text(label, locale, name=payload["sender_name"])
                texts = [header]
                if payload["text"] is not None:
                    chunks = plain_chunks(payload["text"])
                    texts = [header + "\n" + chunks[0], *chunks[1:]]
                for index, text in enumerate(texts):
                    key = prefix + f":{index}"
                    if key in delivery["messages"]:
                        continue
                    if (await state())["skip"]:
                        return
                    sent = await bot.send_message(chat, text)
                    if not await record(key, sent.message_id):
                        return
                if payload["text"] is None:
                    key = prefix + ":media"
                    if key in delivery["messages"] or (await state())["skip"]:
                        return
                    values = (
                        {"caption": payload["caption"], "parse_mode": None}
                        if payload.get("caption") is not None
                        else {}
                    )
                    sent = await bot.copy_message(
                        chat_id=chat,
                        from_chat_id=payload["source_chat_id"],
                        message_id=payload["source_message_id"],
                        **values,
                    )
                    await record(key, sent.message_id)
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except (TelegramForbiddenError, TelegramBadRequest) as error:
            raise TerminalDeliveryError("Telegram message cannot be delivered") from error

    return deliver
