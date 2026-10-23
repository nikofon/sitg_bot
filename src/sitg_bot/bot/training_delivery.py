"""Recoverable Telegram question edits for training outbox events."""

import asyncio
import hashlib
from collections import defaultdict
from uuid import UUID

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from sitg_bot.application.protocol import (
    ApplicationProtocolError,
    RetryableDeliveryError,
    TerminalDeliveryError,
)
from sitg_bot.bot.keyboards.common import TRAINING_MENU_ACTIONS, training_game_keyboard
from sitg_bot.bot.library_delivery import library_document_delivery_handler
from sitg_bot.bot.presenters.game import plain_chunks
from sitg_bot.bot.presenters.render import telegram_keyboard


def training_keyboard(state, localization, locale):
    def button(command):
        return InlineKeyboardButton(
            text=localization.text(f"training.{command}", locale),
            callback_data=f"tr:{UUID(state['id']).hex}:{state['version']}:{command}",
        )

    commands = (
        ["continue"]
        if state["status"] == "active" and state["phase"] == "best_buzz"
        else []
    )
    return (
        InlineKeyboardMarkup(
            inline_keyboard=[
                [button(command) for command in commands[start : start + 2]]
                for start in range(0, len(commands), 2)
            ]
        )
        if commands
        else None
    )


def training_lobby_text(lobby, localization, locale):
    if lobby is None:
        return localization.text("training.lobby_closed", locale)
    settings = lobby.get("settings", {})
    modifiers = []
    for key in ("reversed", "hidden", "best_buzz"):
        enabled = settings.get("hidden_after") is not None if key == "hidden" else settings.get(key)
        if enabled:
            label = localization.text("training." + key, locale)
            if key == "hidden":
                label += " (" + localization.text(
                    "training.hidden_duration", locale, seconds=settings["hidden_after"],
                ) + ")"
            modifiers.append(label)
    packet = lobby.get("packet_name") or localization.text("training.lobby_none", locale)
    text = localization.text(
        "training.lobby_status", locale, packets=packet[:200],
        modifiers=", ".join(modifiers) or localization.text("training.modifiers_none", locale),
    )
    return text


class TrainingDelivery:
    def __init__(self, bot, localization, protocol):
        self.bot, self.localization, self.protocol = bot, localization, protocol
        self.locks = defaultdict(asyncio.Lock)

    async def lobby(self, payload):
        """One durable, editable summary, refreshed by the existing outbox."""
        async with self.locks[f"lobby:{payload['owner_id']}"]:
            try:
                state = await self._state("telegram.training.lobby.delivery", payload)
                chat, locale = state["telegram_user_id"], state["locale"]
                if chat is None:
                    return
                presentation = dict(state["presentation"])
                if state["lobby"] is None and not presentation.get("id"):
                    return
                text = training_lobby_text(state["lobby"], self.localization, locale)
                if presentation.get("text") == text:
                    return
                identifier = presentation.get("id")
                if identifier:
                    try:
                        await self.bot.edit_message_text(text, chat_id=chat, message_id=identifier)
                    except TelegramBadRequest as error:
                        reason = str(error).lower()
                        if (
                            "message to edit not found" in reason
                            or "message can't be edited" in reason
                        ):
                            identifier = None
                        elif "message is not modified" not in reason:
                            raise
                if identifier is None:
                    sent = await self.bot.send_message(chat, text)
                    identifier = sent.message_id
                await self.protocol.request(
                    "telegram.training.lobby.record", **payload,
                    presentation={"id": identifier, "text": text, "version": state["version"]},
                )
            except TelegramRetryAfter as error:
                raise RetryableDeliveryError(
                    str(error), retry_after_seconds=error.retry_after,
                ) from error
            except (TelegramForbiddenError, LookupError, PermissionError) as error:
                raise TerminalDeliveryError("Training lobby delivery is unavailable") from error

    async def document(self, payload):
        state = await self._state("telegram.training.document", payload)
        await library_document_delivery_handler(self.bot)(state)

    async def _state(self, action, payload):
        try:
            return await self.protocol.request(action, **payload)
        except ApplicationProtocolError as error:
            if str(error).split(":", 1)[0] in {"PermissionError", "LookupError"}:
                raise TerminalDeliveryError("Training access was revoked") from error
            raise

    async def __call__(self, payload):
        async with self.locks[payload["session_id"]]:
            try:
                state = await self._state("telegram.training.delivery", payload)
                chat = state["telegram_user_id"]
                if chat is None:
                    return
                locale = state["locale"]
                question = state.get("session")
                if question is None:
                    text = self.localization.text("training.finished", locale)
                    keyboard = None
                else:
                    text = (
                        f"{question['question_index'] + 1}. "
                        f"{question['theme']} · {question['value']}\n"
                    )
                    text += (
                        self.localization.text("training.text_hidden", locale)
                        if question["hidden"]
                        else question["text"]
                    )
                    if question["phase"] == "answering":
                        text += "\n" + self.localization.text("training.answer", locale)
                    if question["phase"] == "revealed":
                        text += f"\n{question.get('answer', '')}\n{question.get('commentary', '')}"
                        if question.get("correct") is not None:
                            text += "\n" + self.localization.text(
                                "training.correct" if question["correct"] else "training.incorrect",
                                locale,
                            )
                    if question["status"] == "paused":
                        text += "\n" + self.localization.text("training.paused", locale)
                    if question.get("appeal_status"):
                        text += "\n" + self.localization.text(
                            "training.appeal_" + question["appeal_status"], locale,
                        )
                    if question["phase"] == "revealed" or question["status"] == "completed":
                        text += "\n" + self.localization.text(
                            "training.score_value", locale, score=question.get("score", 0),
                        )
                    keyboard = training_keyboard(question, self.localization, locale)
                chunks = plain_chunks(text, size=600) or ("…",)
                digest = hashlib.sha256((text + repr(keyboard)).encode()).hexdigest()
                presentation = dict(state["presentation"])
                if presentation.get("digest") == digest and not payload.get("force"):
                    return
                ids = presentation.get("ids", [])
                controls = (
                    "voting" if question and question.get("appeal_status") == "voting" else "play"
                )
                if question and question["status"] in {"active", "paused"} and (
                    presentation.get("controls") != controls
                ):
                    await self.bot.send_message(
                        chat, self.localization.text("training.controls", locale),
                        reply_markup=telegram_keyboard(training_game_keyboard(
                            self.localization, locale, voting=controls == "voting",
                        )),
                    )
                    presentation["controls"] = controls
                    await self.protocol.request(
                        "telegram.training.record", **payload, presentation=presentation,
                    )
                # Clear every old chunk when hidden text replaces a long question.
                for index in range(max(len(chunks), len(ids))):
                    value = chunks[index] if index < len(chunks) else "…"
                    markup = keyboard if index == len(chunks) - 1 else None
                    if index < len(ids):
                        try:
                            await self.bot.edit_message_text(
                                value,
                                chat_id=chat,
                                message_id=ids[index],
                                reply_markup=markup,
                            )
                            continue
                        except TelegramBadRequest as error:
                            if "message is not modified" in str(error).lower():
                                continue
                            if "message to edit not found" not in str(error).lower():
                                raise
                    sent = await self.bot.send_message(chat, value, reply_markup=markup)
                    if index < len(ids):
                        ids[index] = sent.message_id
                    else:
                        ids.append(sent.message_id)
                    await self.protocol.request(
                        "telegram.training.record",
                        **payload,
                        presentation={**presentation, "ids": ids},
                    )
                if state.get("show_menu"):
                    await self.bot.send_message(
                        chat,
                        self.localization.text("training.finished", locale),
                        reply_markup=ReplyKeyboardMarkup(
                            keyboard=[
                                [
                                    KeyboardButton(
                                        text=self.localization.text(
                                            "button.training.mode_switch"
                                            if action == "mode.switch"
                                            else f"button.{action}",
                                            locale,
                                        )
                                    )
                                    for action in row
                                ]
                                for row in TRAINING_MENU_ACTIONS
                            ],
                            resize_keyboard=True,
                        ),
                    )
                await self.protocol.request(
                    "telegram.training.record",
                    **payload,
                    presentation={
                        **presentation, "ids": ids, "digest": digest,
                        "question_revision_id": (
                            question.get("question_revision_id") if question else None
                        ),
                    },
                )
            except TelegramRetryAfter as error:
                raise RetryableDeliveryError(
                    str(error), retry_after_seconds=error.retry_after
                ) from error
            except (TelegramForbiddenError, LookupError, PermissionError) as error:
                raise TerminalDeliveryError("Training delivery is unavailable") from error
