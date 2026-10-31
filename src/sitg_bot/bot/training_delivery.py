"""Training host adapter for the regular SI Telegram event presenter."""

from uuid import UUID

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sitg_bot.application.protocol import (
    ApplicationProtocolError,
    RetryableDeliveryError,
    TerminalDeliveryError,
)
from sitg_bot.bot.game_delivery import GameDelivery, cleanup_messages
from sitg_bot.bot.keyboards.common import TRAINING_MENU_ACTIONS, training_game_keyboard
from sitg_bot.bot.library_delivery import library_document_delivery_handler
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
    RemoveKeyboardModel,
    ReplyKeyboardModel,
)
from sitg_bot.bot.presenters.render import telegram_keyboard
from sitg_bot.domain.game_settings import GameSettings


def training_keyboard(state, localization, locale):
    if state["status"] != "active" or state["phase"] != "best_buzz":
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=localization.text("training.continue", locale),
        callback_data=f"tr:{UUID(state['id']).hex}:{state['version']}:continue",
    )]])


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
    return localization.text(
        "training.lobby_status", locale, packets=packet[:200],
        modifiers=", ".join(modifiers) or localization.text("training.modifiers_none", locale),
    )


class TrainingDelivery(GameDelivery):
    def __init__(self, bot, localization, protocol):
        super().__init__(bot, localization, protocol, references=None)
        self.payloads = {}
        self.states = {}

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
                        if any(part in reason for part in (
                            "message to edit not found", "message can't be edited",
                        )):
                            identifier = None
                        elif "message is not modified" not in reason:
                            raise
                if identifier is None:
                    identifier = (await self.bot.send_message(chat, text)).message_id
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

    async def record(self, chat_id, game_id, **values):
        payload = self.payloads[str(game_id)]
        return await self.protocol.request("telegram.training.record", **payload, **values)

    async def track_input(self, state, message):
        payload = {"session_id": state["id"], "owner_id": state["owner_id"]}
        async with self.locks[message.chat.id]:
            await self.protocol.request(
                "telegram.training.record", **payload,
                key=f"input:{message.message_id}", value={"ids": [message.message_id]},
            )

    async def _question(self, chat, game, messages, round_id, text, **kwargs):
        state = self.states[game].get("session")
        markup = None
        if state and state["question_revision_id"] == round_id:
            if state["hidden"] or state["phase"] == "answering":
                text = self.localization.text("flow.hidden", self.states[game]["locale"])
            markup = training_keyboard(state, self.localization, self.states[game]["locale"])
        keyboard = InlineKeyboardModel(rows=((InlineButtonModel(
            markup.inline_keyboard[0][0].text,
            callback_data=markup.inline_keyboard[0][0].callback_data,
        ),),)) if markup else None
        await super()._question(chat, game, messages, round_id, text, keyboard=keyboard)

    async def _send(self, chat, game, messages, key, model, **kwargs):
        state = self.states[game]["session"]
        if state and isinstance(model.keyboard, ReplyKeyboardModel):
            keyboard = (training_game_keyboard(
                self.localization, self.states[game]["locale"],
                voting=state.get("appeal_status") == "voting",
            ) if state["status"] in {"active", "paused"} else None)
            model = MessageModel(model.text, keyboard)
        return await super()._send(chat, game, messages, key, model, **kwargs)

    async def _event(self, chat, game, messages, view, event):
        kind = event["kind"]
        state = self.states[game]["session"]
        locale = view["locale"]
        if kind == "game_started":
            await self._send(chat, game, messages, "start", MessageModel(
                self.localization.text("training.controls", locale),
                training_game_keyboard(self.localization, locale),
            ))
            self.states[game]["presentation"]["controls"] = "play"
            await self.protocol.request("telegram.training.record", **self.payloads[game],
                                        presentation={"controls": "play"})
        elif kind == "training_text_hidden":
            await self._question(chat, game, messages, event["parameters"]["round_id"],
                                 self.localization.text("flow.hidden", locale))
        elif kind == "game_completed":
            await self._send(chat, game, messages, "results", MessageModel(
                self.localization.text("training.finished", locale) + "\n"
                + self.localization.text("training.score_value", locale, score=state["score"]),
                RemoveKeyboardModel(),
            ), edit=True)
        else:
            # Theme/cost/text/buzz/answer/verdict/score/pause presentation is shared.
            await super()._event(chat, game, messages, view, event)
            if (kind in {"game_paused", "game_resumed"} and state["phase"] == "best_buzz"
                and event["parameters"]["round_id"] == state["question_revision_id"]):
                text = state["text"] + (" ✅" if state.get("fully_announced") else "")
                await self._question(chat, game, messages, state["question_revision_id"], text)

    async def __call__(self, payload):
        game = payload["session_id"]
        identifiers = {key: payload[key] for key in ("owner_id", "session_id")}
        try:
            initial = await self._state("telegram.training.delivery", identifiers)
            chat = initial["telegram_user_id"]
            if chat is None:
                return
            async with self.locks[chat]:
                self.payloads[game] = identifiers
                while True:
                    state = await self._state("telegram.training.delivery", identifiers)
                    self.states[game] = state
                    question, presentation = state.get("session"), state["presentation"]
                    if question is None or presentation.get("dismissed"):
                        if (state.get("status") == "stopped" and state.get("show_menu")
                            and not presentation.get("menu_restored")):
                            keyboard = ReplyKeyboardModel(rows=tuple(tuple(
                                self.localization.text(
                                    "button.training.mode_switch" if action == "mode.switch"
                                    else f"button.{action}", state["locale"],
                                ) for action in row
                            ) for row in TRAINING_MENU_ACTIONS))
                            # Navigation messages are not gameplay cleanup targets.
                            await self.bot.send_message(
                                chat, self.localization.text("training.finished", state["locale"]),
                                reply_markup=telegram_keyboard(keyboard),
                            )
                            await self.protocol.request(
                                "telegram.training.record", **identifiers,
                                presentation={"menu_restored": True},
                            )
                        return
                    if question["status"] == "completed" and not state.get("show_menu"):
                        return  # A delayed old run must never replace a newer keyboard.
                    messages = dict(presentation.get("messages", {}))
                    view = {
                        "locale": state["locale"], "status": question["status"],
                        "message_delay": GameSettings().message_delay,
                        "participants": [{
                            "self": True, "id": question["owner_id"],
                            "name": question["player_name"], "score": question["score"],
                        }],
                    }
                    events = state["events"]
                    await self._deliver_events(chat, game, messages, view, events)
                    if question["status"] in {"active", "paused"}:
                        controls = "vote" if question.get("appeal_status") == "voting" else "play"
                        if payload.get("force") or presentation.get("controls") != controls:
                            await self._send(chat, game, messages,
                                f"controls:{question['version']}:{controls}:{len(messages)}",
                                MessageModel(
                                    self.localization.text("training.controls", state["locale"]),
                                    training_game_keyboard(self.localization, state["locale"],
                                                           voting=controls == "vote"),
                                ))
                            await self.protocol.request("telegram.training.record", **identifiers,
                                                        presentation={"controls": controls})
                        if payload.get("force"):
                            text = (self.localization.text("flow.hidden", state["locale"])
                                    if question["hidden"] or question["phase"] == "answering"
                                    else question["text"])
                            if (question.get("fully_announced") and not question["hidden"]
                                and question["phase"] != "answering"):
                                text += " ✅"
                            if question["phase"] != "intermission":
                                await self._question(chat, game, messages,
                                                     question["question_revision_id"], text)
                    if len(events) < 100:
                        return
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                str(error), retry_after_seconds=error.retry_after,
            ) from error
        except (TelegramForbiddenError, LookupError, PermissionError) as error:
            raise TerminalDeliveryError("Training delivery is unavailable") from error
        finally:
            self.payloads.pop(game, None)
            self.states.pop(game, None)

    async def cleanup(self, payload):
        identifiers = {key: payload[key] for key in ("owner_id", "session_id")}
        state = await self._state("telegram.training.delivery", identifiers)
        chat = state["telegram_user_id"]
        if chat is None:
            return
        async with self.locks[chat]:
            state = await self._state("telegram.training.delivery", identifiers)
            done = set(state["presentation"].get("messages", {}).get("_deleted", {}).get("ids", []))

            async def record(ids):
                await self.protocol.request(
                    "telegram.training.record", **identifiers, key="_deleted", value={"ids": ids},
                )

            await cleanup_messages(self.bot, chat, payload["message_ids"], done, record)
