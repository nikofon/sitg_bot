"""Relay media inside Telegram; no file download or upload is performed here."""

from datetime import datetime

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from sitg_bot.application.protocol import RetryableDeliveryError, TerminalDeliveryError
from sitg_bot.bot.presenters.game import plain_chunks


def tournament_chat_round_label(payload, localization, locale):
    if payload.get("multiple_matches"):
        return localization.text(
            "tournament_chat.round_match",
            locale,
            number=payload.get("round_number"),
            match=payload.get("match_number"),
        )
    return localization.text(
        "tournament_chat.round", locale, number=payload.get("round_number")
    )


def _planned_parts(value, localization, locale):
    rendered = localization.format_datetime(value, locale)
    date, _, time = rendered.partition(" ")
    return date or rendered, time or ""


def tournament_chat_system_text(payload, localization, locale):
    system = payload.get("system") or {}
    planned = None
    raw = system.get("planned_at")
    if raw:
        try:
            planned = datetime.fromisoformat(str(raw))
        except ValueError:
            planned = None
    date, time = _planned_parts(planned, localization, locale) if planned else ("", "")
    key = (
        "tournament_chat.game_time_set"
        if system.get("event") == "game_time_set"
        else "tournament_chat.game_time_cleared"
    )
    return localization.text(
        key,
        locale,
        round=tournament_chat_round_label(payload, localization, locale),
        tournament=payload.get("tournament_name") or "",
        date=date,
        time=time,
        actor=system.get("actor_name") or "",
    )


def chat_delivery_handler(bot, localization, protocol, game_delivery):
    async def deliver(payload):
        chat = int(payload["recipient_telegram_user_id"])
        if payload.get("scope") == "tournament_chat":
            prefix = (
                f"chat:{payload['scope_id']}:{payload.get('message_row')}"
                f":{payload.get('source_message_id')}"
            )
        else:
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
                if delivery.get("notify"):
                    key = prefix + ":notify"
                    text = localization.text(
                        "tournament_chat.new_messages",
                        locale,
                        round=tournament_chat_round_label(payload, localization, locale),
                        tournament=payload.get("tournament_name") or "",
                    )
                    sent = await bot.send_message(chat, text)
                    await record(key, sent.message_id)
                    return
                if payload.get("system") is not None:
                    key = prefix + ":system"
                    if key in delivery["messages"] or (await state())["skip"]:
                        return
                    sent = await bot.send_message(
                        chat, tournament_chat_system_text(payload, localization, locale)
                    )
                    await record(key, sent.message_id)
                    return
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
                    try:
                        sent = await bot.copy_message(
                            chat_id=chat,
                            from_chat_id=payload["source_chat_id"],
                            message_id=payload["source_message_id"],
                            **values,
                        )
                    except TelegramBadRequest:
                        if not payload.get("history") or payload.get("source_chat_id") is None:
                            raise
                        # A restored media item may already be deleted; skip it.
                        return
                    await record(key, sent.message_id)
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except (TelegramForbiddenError, TelegramBadRequest) as error:
            raise TerminalDeliveryError("Telegram message cannot be delivered") from error

    return deliver


def chat_cleanup_delivery_handler(bot):
    """Best-effort deletion of tournament chat messages tracked in member ledgers."""

    async def deliver(payload):
        chat = int(payload["recipient_telegram_user_id"])
        for message_id in payload.get("message_ids") or ():
            try:
                await bot.delete_message(chat, int(message_id))
            except TelegramBadRequest:
                continue

    return deliver
