"""Ordered gameplay messages with durable message identities and cleanup."""

import asyncio
import hashlib
import html
import time
from collections import defaultdict

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ForceReply, ReplyKeyboardRemove

from sitg_bot.application.protocol import RetryableDeliveryError, TerminalDeliveryError
from sitg_bot.bot.presenters.game import (
    appeal_ballot,
    game_keyboard,
    join_message,
    plain_chunks,
    rating_message,
    score_text,
    start_message,
)
from sitg_bot.bot.presenters.models import InlineKeyboardModel, MessageModel
from sitg_bot.bot.presenters.render import telegram_keyboard


class GameDelivery:
    def __init__(self, bot, localization, protocol, references):
        self.bot = bot
        self.localization = localization
        self.protocol = protocol
        self.references = references
        self.locks = defaultdict(asyncio.Lock)
        self.last_edit = {}

    async def record(self, chat_id, game_id, **values):
        return await self.protocol.request(
            "telegram.game.record", telegram_user_id=chat_id, game_id=str(game_id), **values
        )

    async def track(self, chat_id, game_id, message_id):
        await self.record(chat_id, game_id, key=f"input:{message_id}", value={"ids": [message_id]})

    async def send(self, chat_id, game_id, key, model, **metadata):
        async with self.locks[chat_id]:
            state = await self.protocol.request(
                "telegram.game.presentation", telegram_user_id=chat_id, game_id=str(game_id)
            )
            if state["dismissed"]:
                return None
            return await self._send(
                chat_id, str(game_id), state["messages"], key, model, **metadata
            )

    async def _send(self, chat, game, messages, key, model, *, edit=False, markup=None, **metadata):
        entry = dict(messages.get(key, {}))
        ids = list(entry.get("ids", []))
        inline = (
            telegram_keyboard(model.keyboard)
            if isinstance(model.keyboard, InlineKeyboardModel)
            else None
        )
        digest = hashlib.sha256((model.text + repr(model.keyboard)).encode()).hexdigest()
        if ids and (not edit or entry.get("digest") == digest):
            return ids[0]
        if edit and ids:
            try:
                await self.bot.edit_message_text(
                    model.text, chat_id=chat, message_id=ids[0], reply_markup=inline
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    if not any(
                        s in str(error).lower()
                        for s in ("message to edit not found", "message can't be edited")
                    ):
                        raise
                    ids = []
        if not ids:
            sent = await self.bot.send_message(
                chat,
                model.text,
                reply_markup=markup if markup is not None else telegram_keyboard(model.keyboard),
            )
            ids = [sent.message_id]
        value = {**entry, **metadata, "ids": ids, "digest": digest}
        messages[key] = value
        await self.record(chat, game, key=key, value=value)
        return ids[0]

    async def _question(self, chat, game, messages, round_id, text):
        chunks = plain_chunks(text) or [self.localization.text("flow.hidden", "ru")]
        prefix = f"question:{round_id}:"
        old_keys = [key for key in messages if key.startswith(prefix)]
        for index, chunk in enumerate(chunks):
            await self._send(
                chat,
                game,
                messages,
                prefix + str(index),
                MessageModel(chunk),
                edit=True,
                kind="question",
                round_id=round_id,
            )
        # A long question may occupy several messages; hide all of them together.
        for key in old_keys:
            if int(key.rsplit(":", 1)[1]) >= len(chunks):
                await self._send(
                    chat,
                    game,
                    messages,
                    key,
                    MessageModel(html.escape(text)),
                    edit=True,
                    kind="question",
                    round_id=round_id,
                )
        await self.record(chat, game, key="screen", value={"round_id": round_id})
        messages["screen"] = {"round_id": round_id}

    async def __call__(self, payload):
        try:
            await self.sync(int(payload["recipient_telegram_user_id"]), str(payload["game_id"]))
        except TelegramRetryAfter as error:
            raise RetryableDeliveryError(
                "Telegram flood wait", retry_after_seconds=int(error.retry_after)
            ) from error
        except TelegramForbiddenError as error:
            await self.protocol.request(
                "telegram.game.disconnect",
                telegram_user_id=int(payload["recipient_telegram_user_id"]),
                game_id=str(payload["game_id"]),
            )
            raise TerminalDeliveryError("Telegram chat is unavailable") from error

    async def sync(self, chat, game):
        async with self.locks[chat]:
            while True:
                delivery = await self.protocol.request(
                    "telegram.game.delivery", telegram_user_id=chat, game_id=str(game)
                )
                if delivery["skip"]:
                    return
                view, messages = delivery["view"], delivery["messages"]
                events = delivery["events"]
                index = 0
                while index < len(events):
                    event = events[index]
                    # Collapse only consecutive reveal frames; never cross a buzz,
                    # verdict, question cost, completion or other lifecycle boundary.
                    if event["kind"] == "question_token_revealed":
                        while (
                            index + 1 < len(events)
                            and events[index + 1]["kind"] == "question_token_revealed"
                            and events[index + 1]["parameters"].get("round_id")
                            == event["parameters"].get("round_id")
                        ):
                            index += 1
                            event = events[index]
                        if index + 1 < len(events) and events[index + 1]["kind"] == "player_buzzed":
                            await self.record(chat, game, sequence=event["sequence"])
                            index += 1
                            continue
                    await self._event(chat, str(game), messages, view, event)
                    await self.record(chat, game, sequence=event["sequence"])
                    index += 1
                if len(events) < 100:
                    return

    async def _event(self, chat, game, messages, view, event):
        locale = view["locale"]

        def t(key, **values):
            return self.localization.text(key, locale, **values)

        kind, params = event["kind"], event["parameters"]
        keyboard = (
            game_keyboard()
            if view["status"] == "active" and any(p["self"] for p in view["participants"])
            else None
        )
        key = f"event:{event['sequence']}"
        round_id = params.get("round_id")
        if params.get("ballot"):
            await self._send(
                chat,
                game,
                messages,
                f"ballot:{params['appeal_id']}",
                appeal_ballot(params["ballot"], self.localization, locale),
                edit=True,
                kind="ballot",
                appeal_id=params["appeal_id"],
            )
        if kind in {"game_created", "players_assigned"}:
            if view["status"] == "lobby" and any(p["self"] for p in view["participants"]):
                await self._send(
                    chat, game, messages, "join", join_message(view, self.localization, locale)
                )
        elif kind in {"ready_countdown", "game_started"}:
            await self._send(
                chat, game, messages, "start", start_message(view, self.localization, locale)
            )
        elif kind == "themes_announced":
            text = t(
                "game.themes",
                themes="\n".join(
                    f"{i}) {theme['name']}" for i, theme in enumerate(params["themes"], 1)
                ),
            )
            await self._send(chat, game, messages, key, MessageModel(text))
        elif kind == "theme_started":
            await self._send(
                chat,
                game,
                messages,
                key,
                MessageModel(
                    t(
                        "flow.theme",
                        name=params["name"],
                        author=params.get("author") or "—",
                        packet=params.get("packet_name") or "—",
                    )
                ),
            )
        elif kind == "question_cost_announced":
            await self._send(
                chat,
                game,
                messages,
                key,
                MessageModel(t("flow.cost", theme=params["theme"], value=params["value"])),
            )
        elif kind == "question_token_revealed":
            if time.monotonic() - self.last_edit.get(chat, 0) < 0.8:
                raise RetryableDeliveryError("Coalesced reveal", retry_after_seconds=1)
            await self._question(chat, game, messages, round_id, params["text"])
            self.last_edit[chat] = time.monotonic()
        elif kind == "player_buzzed":
            await self._question(chat, game, messages, round_id, t("flow.hidden"))
            if params.get("mine"):
                # Prompt identity lives in the message ledger, so answers survive restart.
                await self._send(
                    chat,
                    game,
                    messages,
                    key,
                    MessageModel(t("flow.form", form=params["form"] or "—")),
                    markup=ForceReply(selective=True),
                    kind="answer",
                    round_id=round_id,
                )
        elif kind == "answer_judged":
            if params.get("answer") is not None and not params.get("mine"):
                for i, chunk in enumerate(plain_chunks(params["name"] + ": " + params["answer"])):
                    await self._send(
                        chat, game, messages, key + f":answer:{i}", MessageModel(chunk)
                    )
            verdict = (
                "flow.timeout"
                if params["timed_out"]
                else ("flow.correct" if params["original_correct"] else "flow.incorrect")
            )
            keyboard = game_keyboard() if any(p["self"] for p in view["participants"]) else None
            await self._send(chat, game, messages, key, MessageModel(t(verdict), keyboard))
            if not params["original_correct"] and params.get("revealed_text"):
                await self._question(chat, game, messages, round_id, params["revealed_text"])
        elif kind == "question_completed":
            await self._question(chat, game, messages, round_id, params["text"])
            text = t(
                "flow.answer",
                answer=params["answer"],
                commentary=params["commentary"],
                author=params["author"],
            )
            for i, chunk in enumerate(plain_chunks(html.unescape(text))):
                await self._send(chat, game, messages, key + f":{i}", MessageModel(chunk))
        elif kind == "theme_completed":
            await self._send(
                chat,
                game,
                messages,
                key,
                MessageModel(t("game.theme_completed", name=params["name"])),
            )
        elif kind == "scoreboard":
            text = (
                t("game.scores")
                + "\n"
                + "\n".join(f"{html.escape(p['name'])}: {p['score']}" for p in params["players"])
            )
            await self._send(chat, game, messages, key, MessageModel(text))
        elif kind in {"game_completed", "game_finalized"}:
            await self._send(
                chat,
                game,
                messages,
                "results",
                MessageModel(score_text(view, self.localization, locale, final=True)),
                edit=True,
                markup=ReplyKeyboardRemove(),
            )
            if (
                any(p["self"] for p in view["participants"])
                and sum(not p.get("is_chair", False) for p in view["participants"]) > 1
            ):
                await self._send(chat, game, messages, "rate_title", MessageModel(t("flow.rate")))
                for i, player in enumerate(view["participants"]):
                    if not player["self"] and not player.get("is_chair", False):
                        await self._send(
                            chat,
                            game,
                            messages,
                            f"rate:{i}",
                            rating_message(view, i, self.localization, locale),
                        )
        elif kind in {"game_paused", "game_resumed"}:
            keyboard = game_keyboard() if any(p["self"] for p in view["participants"]) else None
            await self._send(chat, game, messages, key, MessageModel(t("flow." + kind), keyboard))
        elif kind in {"appeal_voting_started", "appeal_vote_cast"}:
            return
        elif kind in {"appeal_vote_rejected", "appeal_commentary_requested", "appeal_escalated"}:
            await self._send(
                chat,
                game,
                messages,
                key,
                MessageModel(t("flow." + kind), keyboard),
                kind=kind,
                appeal_id=params["appeal_id"],
            )
        elif kind == "appeal_resolved":
            await self._send(
                chat,
                game,
                messages,
                key,
                MessageModel(
                    t(
                        "game.appeal_decision",
                        decision=t("game.approved" if params["approved"] else "game.rejected"),
                    ),
                    keyboard,
                ),
            )
        elif kind == "appeal_score_corrected" and view["status"] in {"completed", "finalized"}:
            await self._send(
                chat,
                game,
                messages,
                "results",
                MessageModel(score_text(view, self.localization, locale, final=True)),
                edit=True,
                markup=ReplyKeyboardRemove(),
            )
        elif kind in {"game_cancelled", "game_abandoned", "game_failed_to_start"}:
            await self._send(
                chat,
                game,
                messages,
                key,
                MessageModel(t("game.notice." + kind)),
                markup=ReplyKeyboardRemove(),
            )
        elif kind == "player_reconnected" and params["mine"]:
            await self._send(
                chat, game, messages, key, MessageModel(t("flow.reconnected"), keyboard)
            )
            question = view.get("question")
            if question:
                await self._send(
                    chat,
                    game,
                    messages,
                    key + ":cost",
                    MessageModel(
                        t(
                            "flow.cost",
                            theme=(view.get("theme") or {}).get("name", "—"),
                            value=question["value"],
                        )
                    ),
                )
                await self._question(
                    chat,
                    game,
                    messages,
                    str(question["round_id"]),
                    t("flow.hidden") if question["accepted_buzzer_id"] else question["text"],
                )
                if question["revealed_answer"] is not None:
                    text = t(
                        "flow.answer",
                        answer=question["revealed_answer"],
                        commentary=question["commentary"] or "",
                        author=question["author"],
                    )
                    for i, chunk in enumerate(plain_chunks(html.unescape(text))):
                        await self._send(
                            chat, game, messages, key + f":answer:{i}", MessageModel(chunk)
                        )
            if view.get("appeal"):
                ballot = {**view["appeal"], "can_vote": "vote" in view["actions"]}
                await self._send(
                    chat,
                    game,
                    messages,
                    f"ballot:{ballot['id']}",
                    appeal_ballot(ballot, self.localization, locale),
                    edit=True,
                    kind="ballot",
                    appeal_id=str(ballot["id"]),
                )

    async def cleanup(self, payload):
        chat, game = int(payload["recipient_telegram_user_id"]), str(payload["game_id"])
        async with self.locks[chat]:
            state = await self.protocol.request(
                "telegram.game.presentation", telegram_user_id=chat, game_id=game
            )
            messages = state["messages"]
            done = set(messages.get("_deleted", {}).get("ids", []))
            ids = set(payload["message_ids"])
            for mid in sorted(ids - done):
                try:
                    await self.bot.delete_message(chat, mid)
                except TelegramRetryAfter as error:
                    raise RetryableDeliveryError(
                        "Telegram flood wait", retry_after_seconds=int(error.retry_after)
                    ) from error
                except (TelegramBadRequest, TelegramForbiddenError):
                    # Telegram cannot delete old messages or messages in blocked chats.
                    pass
                done.add(mid)
                await self.record(chat, game, key="_deleted", value={"ids": sorted(done)})


def game_delivery_handler(bot, localization, protocol, references):
    return GameDelivery(bot, localization, protocol, references)
