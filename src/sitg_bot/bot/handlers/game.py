import html
from contextlib import suppress
from uuid import UUID

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.protocol import RetryableDeliveryError
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.game import (
    abandon_confirmation,
    game_keyboard,
    plain_chunks,
    score_text,
)
from sitg_bot.bot.presenters.models import InlineButtonModel, InlineKeyboardModel, MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import GatewayCallError

router = Router(name=__name__)


@router.message(Command("appeals"))
async def handle_manager_appeals(
    message: Message,
    backend,
    telegram_update_claim,
    localization,
    locale,
    navigation,
    callback_references,
):
    if navigation is None:
        return
    tickets = await backend.game_appeal_tickets(telegram_update_claim)
    if not tickets:
        await message.answer(localization.text("game.tickets_empty", locale))
    for ticket in tickets:
        text = localization.text(
            "game.ticket",
            locale,
            question=ticket["question_text"],
            answer=ticket["official_answer"],
            submitted=ticket["submitted_answer"],
            kind=localization.text(f"game.appeal_kind.{ticket['kind']}", locale),
            commentary=ticket["commentary"],
            deadline=ticket["expires_at"],
        )
        for chunk in plain_chunks(html.unescape(text)):
            await message.answer(chunk)
        rows = []
        for approve in (True, False):
            reference = callback_references.issue(
                scope="game_manager",
                actor_id=navigation.account.player_id,
                resource_id=UUID(ticket["id"]),
                action="approve" if approve else "reject",
            )
            rows.append(
                InlineButtonModel(
                    localization.text(
                        "game.button.vote_yes" if approve else "game.button.vote_no", locale
                    ),
                    callback_data=f"gamerule:{reference}",
                )
            )
        await send_message_model(
            message,
            MessageModel(
                localization.text("game.rule_pick", locale),
                InlineKeyboardModel(rows=(tuple(rows),)),
            ),
        )


@router.callback_query(F.data.startswith("gamerule:"))
async def handle_manager_appeal_rule(
    callback: CallbackQuery,
    backend,
    telegram_update_claim,
    localization,
    locale,
    navigation,
    callback_references,
):
    if navigation is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    try:
        target = callback_references.resolve(
            callback.data.removeprefix("gamerule:"),
            scope="game_manager",
            actor_id=navigation.account.player_id,
        )
    except LookupError:
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    await callback.answer()
    if target.action == "cancel":
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    if target.action in {"approve", "reject"}:
        reference = callback_references.issue(
            scope="game_manager",
            actor_id=navigation.account.player_id,
            resource_id=target.resource_id,
            action=target.action + "_confirmed",
        )
        cancel_reference = callback_references.issue(
            scope="game_manager",
            actor_id=navigation.account.player_id,
            resource_id=target.resource_id,
            action="cancel",
        )
        await send_message_model(
            callback.message,
            MessageModel(
                localization.text(
                    "game.rule_confirm",
                    locale,
                    decision=localization.text(
                        "game.approved" if target.action == "approve" else "game.rejected", locale
                    ),
                ),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("button.yes", locale),
                                callback_data=f"gamerule:{reference}",
                            ),
                            InlineButtonModel(
                                localization.text("button.no", locale),
                                callback_data=f"gamerule:{cancel_reference}",
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    result = await backend.game_appeal_decide(
        telegram_update_claim, target.resource_id, target.action == "approve_confirmed"
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        localization.text(
            "game.rule_recorded" if result["accepted"] else "game.unavailable", locale
        )
    )


class GameInput(StatesGroup):
    report = State()


async def sync_game(backend, chat_id, game_id):
    # An outbox retry will finish a rate-limited reveal without making a
    # successfully accepted player action look like a failure.
    with suppress(RetryableDeliveryError, TelegramRetryAfter):
        await backend.game_delivery.sync(chat_id, str(game_id))


async def show_game(message, backend, claim, localization, locale, references, game_id=None):
    view = await backend.game_view(claim, game_id)
    await backend.game_delivery.track(message.chat.id, view["id"], message.message_id)
    await sync_game(backend, message.chat.id, view["id"])
    command = (message.text or "").split(maxsplit=1)[0].split("@")[0] if message.text else ""
    if command in {"/start", "/menu", "/cancel"} and view["status"] == "active":
        await backend.game_delivery.send(
            message.chat.id,
            view["id"],
            f"command:{message.message_id}:controls",
            MessageModel(localization.text("game.controls", locale), game_keyboard()),
        )
    return view


async def game_reply(message, backend, view, text, *, suffix="reply", **metadata):
    return await backend.game_delivery.send(
        message.chat.id,
        view["id"],
        f"command:{message.message_id}:{suffix}",
        MessageModel(text),
        **metadata,
    )


async def current_game(message, backend, claim, navigation, localization, locale):
    if navigation is None or navigation.context != "game" or navigation.active_game is None:
        await message.answer(localization.text("flow.no_game", locale))
        return None
    view = await backend.game_view(claim, navigation.active_game.id)
    if view.get("dismissed"):
        return None
    await backend.game_delivery.track(message.chat.id, view["id"], message.message_id)
    return view


async def perform(message, backend, claim, view, localization, locale, command, **values):
    try:
        if command in {"abandon", "quit", "observe_leave", "join"}:
            # Finish in-flight delivery before dismissing or replacing a session.
            # Subsequent sends then see the new presentation state.
            async with backend.game_delivery.locks[message.chat.id]:
                result = await backend.game_action(
                    claim, game_id=UUID(view["id"]), command=command, **values
                )
        else:
            result = await backend.game_action(
                claim, game_id=UUID(view["id"]), command=command, **values
            )
    except GatewayCallError as error:
        if error.error.code == ErrorCode.INTERNAL_ERROR:
            raise
        result = {"accepted": False}
    if not result["accepted"]:
        if view.get("dismissed"):
            await message.answer(localization.text("game.unavailable", locale))
        else:
            await game_reply(message, backend, view, localization.text("game.unavailable", locale))
        return False
    return True


async def prompt(message, backend, view, localization, locale, kind, **metadata):
    text = (
        localization.text("flow.form", locale, form=view["question"]["form"] or "—")
        if kind == "answer"
        else localization.text("game.commentary_prompt", locale)
    )
    await game_reply(
        message,
        backend,
        view,
        text,
        suffix=kind,
        markup=ForceReply(selective=True),
        kind=kind,
        round_id=str(view["question"]["round_id"]) if view.get("question") else None,
        appeal_id=str(view["appeal"]["id"]) if view.get("appeal") else None,
        **metadata,
    )


async def request_appeal(message, backend, claim, view, localization, locale):
    targets = view["appeal_targets"]
    if len(targets) == 1:
        return await perform(
            message,
            backend,
            claim,
            view,
            localization,
            locale,
            "appeal",
            target_id=targets[0]["id"],
            round_id=view["question"]["round_id"],
        )
    lines = [localization.text("flow.appeal_choose", locale)]
    buttons = []
    for index, attempt in enumerate(targets, 1):
        player = next(p for p in view["participants"] if p["id"] == attempt["player_id"])
        answer = attempt["submitted_answer"] or "—"
        preview = answer if len(answer) <= 120 else answer[:117] + "…"
        lines.append(html.escape(f"{index}) {player['name']}: {preview}"))
        kind = "reject_correct" if attempt["original_correct"] else "accept_incorrect"
        buttons.append((InlineButtonModel(
            f"{index}) " + localization.text("game.appeal_kind." + kind, locale),
            callback_data=f"appealpick:{UUID(str(attempt['id'])).hex}",
        ),))
    await backend.game_delivery.send(
        message.chat.id,
        view["id"],
        f"command:{message.message_id}:appeal",
        MessageModel("\n".join(lines), InlineKeyboardModel(rows=tuple(buttons))),
        kind="appeal",
        round_id=str(view["question"]["round_id"]),
        targets=[str(a["id"]) for a in targets],
    )
    return False


@router.callback_query(F.data.startswith("appealpick:"))
async def handle_game_appeal_pick(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale, navigation
):
    if (
        not isinstance(callback.message, Message)
        or navigation is None or navigation.context != "game" or navigation.active_game is None
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    try:
        target_id = UUID(callback.data.removeprefix("appealpick:"))
    except ValueError:
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    view = await backend.game_view(telegram_update_claim, navigation.active_game.id)
    entry = next((
        entry for entry in view["messages"].values()
        if entry.get("kind") == "appeal"
        and callback.message.message_id in entry.get("ids", [])
        and str(target_id) in entry.get("targets", [])
    ), None)
    if (
        view.get("dismissed") or "appeal" not in view["actions"] or entry is None
        or entry.get("round_id") != str((view.get("question") or {}).get("round_id"))
        or not any(str(a["id"]) == str(target_id) for a in view["appeal_targets"])
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    await callback.answer()
    if await perform(
        callback.message, backend, telegram_update_claim, view, localization, locale,
        "appeal", target_id=str(target_id), round_id=entry["round_id"],
    ):
        await callback.message.edit_reply_markup(reply_markup=None)
        await sync_game(backend, callback.message.chat.id, view["id"])


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.in_({"+", "||", "!"}))
async def handle_game_input(
    message: Message, backend, telegram_update_claim, localization, locale, navigation
):
    if navigation is None or navigation.context != "game" or navigation.active_game is None:
        raise SkipHandler
    view = await backend.game_view(telegram_update_claim, navigation.active_game.id)
    entry = next(
        (
            entry
            for entry in view.get("messages", {}).values()
            if entry.get("kind") in {"answer", "commentary", "appeal"}
            and (
                message.reply_to_message is not None
                and message.reply_to_message.message_id in entry.get("ids", [])
                or message.reply_to_message is None
                and entry.get("kind") in {"answer", "commentary"}
                and entry["kind"] in view["actions"]
                and (
                    entry.get("round_id") == str((view.get("question") or {}).get("round_id"))
                    if entry["kind"] == "answer"
                    else entry.get("appeal_id") == str((view.get("appeal") or {}).get("id"))
                )
            )
        ),
        None,
    )
    if view.get("dismissed"):
        raise SkipHandler
    if entry is None:
        # A prompt may still be in flight. Do not broadcast an answer during that gap.
        if message.reply_to_message is None:
            for command in ("answer", "commentary"):
                if command in view["actions"]:
                    await backend.game_delivery.track(
                        message.chat.id, view["id"], message.message_id
                    )
                    await prompt(message, backend, view, localization, locale, command)
                    return
        raise SkipHandler
    await backend.game_delivery.track(message.chat.id, view["id"], message.message_id)
    if len(message.text) > 2000:
        await game_reply(message, backend, view, localization.text("game.input_too_long", locale))
        return
    command = entry["kind"]
    values = {"round_id": entry.get("round_id"), "appeal_id": entry.get("appeal_id")}
    if command == "appeal":
        try:
            index = int(message.text.strip()) - 1
            if not 0 <= index < len(entry["targets"]):
                raise ValueError
            values["target_id"] = entry["targets"][index]
        except ValueError:
            await game_reply(
                message, backend, view, localization.text("flow.number_required", locale)
            )
            return
    else:
        values["text"] = message.text
    if await perform(
        message, backend, telegram_update_claim, view, localization, locale, command, **values
    ):
        await sync_game(backend, message.chat.id, view["id"])


@router.message(StateFilter(None), F.text.in_({"+", "||", "!"}))
async def handle_game_keyboard(
    message: Message, backend, telegram_update_claim, localization, locale, navigation
):
    view = await current_game(
        message, backend, telegram_update_claim, navigation, localization, locale
    )
    if view is None:
        return
    command = {"+": "buzz", "||": "resume" if view["paused"] else "pause", "!": "appeal"}[
        message.text
    ]
    if command not in view["actions"]:
        await game_reply(message, backend, view, localization.text("game.unavailable", locale))
        return
    if command == "appeal":
        accepted = await request_appeal(
            message, backend, telegram_update_claim, view, localization, locale
        )
    else:
        # A bare keyboard press has no embedded round. Bind buzzing to the last
        # question actually delivered in this chat, then let the server reject races.
        round_id = (
            view.get("messages", {}).get("screen", {}).get("round_id")
            if command == "buzz"
            else (view.get("question") or {}).get("round_id")
        )
        accepted = await perform(
            message,
            backend,
            telegram_update_claim,
            view,
            localization,
            locale,
            command,
            round_id=round_id,
        )
    if accepted:
        await sync_game(backend, message.chat.id, view["id"])


@router.message(
    Command(
        "join",
        "reconnect",
        "score",
        "themes",
        "players",
        "appeal",
        "pause",
        "resume",
        "answer",
        "commentary",
        "abandon",
        "results",
        "quit",
        "escalate",
        "report",
    )
)
async def handle_game_command(
    message: Message,
    backend,
    telegram_update_claim,
    localization,
    locale,
    navigation,
    state: FSMContext,
    callback_references,
):
    parts = message.text.split(maxsplit=1)
    command = parts[0].split("@")[0].removeprefix("/")
    argument = parts[1].strip() if len(parts) > 1 else ""
    if command == "reconnect" and (navigation is None or navigation.context != "game"):
        if navigation is not None and navigation.active_lobby is not None:
            await message.answer(localization.text("flow.reconnect_lobby", locale))
            return
        try:
            view = await backend.game_view(telegram_update_claim, reconnect=True)
        except GatewayCallError as error:
            if error.error.code == ErrorCode.INTERNAL_ERROR:
                raise
            await message.answer(localization.text("flow.no_reconnect", locale))
            return
        if await perform(
            message, backend, telegram_update_claim, view, localization, locale, "join"
        ):
            await state.clear()
            await backend.game_delivery.track(message.chat.id, view["id"], message.message_id)
            await sync_game(backend, message.chat.id, view["id"])
        return
    view = await current_game(
        message, backend, telegram_update_claim, navigation, localization, locale
    )
    if view is None:
        return
    if command in {"score", "results"}:
        await game_reply(
            message,
            backend,
            view,
            score_text(view, localization, locale, final=view["status"] not in {"lobby", "active"}),
        )
        return
    if command == "themes":
        text = localization.text("game.unavailable", locale)
        if "themes" in view["actions"]:
            text = localization.text(
                "game.themes", locale,
                themes="\n".join(
                    f"{index}) {theme['name']}"
                    for index, theme in enumerate(view["themes"], 1)
                ),
            )
        for index, chunk in enumerate(plain_chunks(html.unescape(text))):
            await game_reply(message, backend, view, chunk, suffix=f"themes:{index}")
        return
    if command == "players":
        for index, player in enumerate(view["participants"], 1):
            status = (
                "chair"
                if player.get("is_chair")
                else "playing"
                if player.get("active") and player.get("joined")
                else ("waiting" if player.get("active") else "left")
            )
            await game_reply(
                message,
                backend,
                view,
                localization.text(
                    "game.player",
                    locale,
                    number=index,
                    name=player["name"],
                    status=localization.text("game.player_status." + status, locale),
                    link=f"https://t.me/{player['telegram_username']}"
                    if player.get("telegram_username")
                    else "",
                ),
                suffix=f"player:{index}",
            )
        return
    if command == "quit" and view["status"] in {"active", "lobby"}:
        await game_reply(message, backend, view, localization.text("game.quit_active", locale))
        return
    if command == "abandon":
        if not {"abandon", "observe_leave"}.intersection(view["actions"]):
            await game_reply(message, backend, view, localization.text("game.unavailable", locale))
            return
        await backend.game_delivery.send(
            message.chat.id,
            view["id"],
            f"command:{message.message_id}:abandon",
            abandon_confirmation(view, localization, locale, callback_references),
            kind="abandon",
        )
        return
    if command == "reconnect":
        command = "join"
    if command != "quit" and command not in view["actions"]:
        await game_reply(message, backend, view, localization.text("game.unavailable", locale))
        return
    if command == "report":
        await report_command(
            message, backend, telegram_update_claim, view, localization, locale, state, argument
        )
        return
    if command in {"answer", "commentary"}:
        await prompt(message, backend, view, localization, locale, command)
        return
    if command == "appeal":
        if await request_appeal(
            message, backend, telegram_update_claim, view, localization, locale
        ):
            await sync_game(backend, message.chat.id, view["id"])
        return
    values = {"round_id": (view.get("question") or {}).get("round_id")}
    if command == "escalate":
        if argument.lower() not in {"yes", "no", "да", "нет"}:
            await game_reply(
                message, backend, view, localization.text("flow.decision", locale, command=command)
            )
            return
        notice_kind = "appeal_vote_rejected"
        notice = next(
            (
                entry
                for entry in view.get("messages", {}).values()
                if entry.get("kind") == notice_kind
                and entry.get("appeal_id") == str(view["appeal"]["id"])
            ),
            {},
        )
        if not notice:
            await game_reply(message, backend, view, localization.text("game.unavailable", locale))
            await sync_game(backend, message.chat.id, view["id"])
            return
        values.update(approve=argument.lower() in {"yes", "да"}, appeal_id=notice.get("appeal_id"))
    if not await perform(
        message, backend, telegram_update_claim, view, localization, locale, command, **values
    ):
        return
    if command in {"abandon", "quit", "observe_leave"}:
        await state.clear()
        # Cleanup is queued transactionally by the server. Restore the menu
        # immediately; a long chat or flood wait must not delay leaving the game.
        updated = await backend.navigation(telegram_update_claim)
        await send_message_model(message, menu_message(updated, localization, locale))
        return
    await sync_game(backend, message.chat.id, view["id"])


async def report_command(message, backend, claim, view, localization, locale, state, argument):
    if argument == "confirm":
        data = await state.get_data()
        if data.get("report_game_id") != view["id"]:
            await game_reply(message, backend, view, localization.text("flow.report_usage", locale))
            return
        try:
            result = await backend.game_action(
                claim,
                game_id=UUID(view["id"]),
                command="report",
                target_id=data["target_id"],
                report_kind=data["report_kind"],
                text=data["details"],
            )
        except GatewayCallError as error:
            if error.error.code == ErrorCode.INTERNAL_ERROR:
                raise
            await game_reply(message, backend, view, localization.text("game.unavailable", locale))
            return
        await state.clear()
        await game_reply(
            message,
            backend,
            view,
            localization.text(
                "game.report_receipt",
                locale,
                receipt=(result.get("receipt") or {}).get("report_id", "—"),
            ),
        )
        return
    try:
        values = argument.split(maxsplit=2)
        index = int(values[0]) - 1
        kind = values[1]
        if not 0 <= index < len(view["participants"]) or kind not in {"cheating", "toxicity"}:
            raise ValueError
        target = view["participants"][index]
        if target["self"]:
            raise ValueError
        details = values[2] if len(values) > 2 else None
        if details and len(details) > 2000:
            raise ValueError
    except (ValueError, IndexError):
        await game_reply(message, backend, view, localization.text("flow.report_usage", locale))
        return
    await state.set_state(GameInput.report)
    await state.set_data(
        {
            "report_game_id": view["id"],
            "target_id": target["id"],
            "report_kind": kind,
            "details": details,
        }
    )
    await game_reply(
        message,
        backend,
        view,
        localization.text(
            "flow.report_confirm",
            locale,
            name=target["name"],
            kind=localization.text("game.report_kind." + kind, locale),
            details=details or "—",
        ),
    )


@router.callback_query(F.data.startswith("gamejoin:"))
async def handle_game_join(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale, navigation
):
    if not isinstance(callback.message, Message) or navigation is None:
        await callback.answer()
        return
    try:
        game_id = UUID(callback.data.removeprefix("gamejoin:"))
    except ValueError:
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    if (
        navigation.context != "game"
        or navigation.active_game is None
        or navigation.active_game.id != game_id
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    await callback.answer()
    view = await backend.game_view(telegram_update_claim, game_id)
    if view.get("dismissed"):
        return
    if "join" in view["actions"]:
        await perform(
            callback.message, backend, telegram_update_claim, view, localization, locale, "join"
        )
    await sync_game(backend, callback.message.chat.id, game_id)


@router.callback_query(F.data.startswith("gamevote:"))
async def handle_game_rating(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale, navigation
):
    if not isinstance(callback.message, Message) or navigation is None:
        await callback.answer()
        return
    try:
        _, game, index, vote = callback.data.split(":")
        game_id, index, vote = UUID(game), int(index), int(vote)
        if vote not in {-1, 1}:
            raise ValueError
    except ValueError:
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    if (
        navigation.context != "game"
        or navigation.active_game is None
        or navigation.active_game.id != game_id
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    view = await backend.game_view(telegram_update_claim, game_id)
    if (
        view.get("dismissed")
        or "reputation" not in view["actions"]
        or not 0 <= index < len(view["participants"])
        or view["participants"][index]["self"]
        or view["participants"][index].get("is_chair", False)
    ):
        await callback.answer(localization.text("game.unavailable", locale), show_alert=True)
        return
    await callback.answer()
    if view["participants"][index].get("vote_reason"):
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    if await perform(
        callback.message,
        backend,
        telegram_update_claim,
        view,
        localization,
        locale,
        "reputation",
        target_id=view["participants"][index]["id"],
        approve=vote == 1,
    ):
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("gameleave:"))
async def handle_game_abandon_confirmation(
    callback: CallbackQuery,
    backend,
    telegram_update_claim,
    localization,
    locale,
    navigation,
    state: FSMContext,
    callback_references,
):
    if not isinstance(callback.message, Message) or navigation is None:
        await callback.answer()
        return
    try:
        target = callback_references.resolve(
            callback.data.removeprefix("gameleave:"),
            scope="game_abandon",
            actor_id=navigation.account.player_id,
        )
    except LookupError:
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    if (
        navigation.context != "game"
        or navigation.active_game is None
        or navigation.active_game.id != target.resource_id
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    view = await backend.game_view(telegram_update_claim, target.resource_id)
    if view["dismissed"] or not any(
        entry.get("kind") == "abandon" and callback.message.message_id in entry.get("ids", [])
        for entry in view["messages"].values()
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    await callback.answer()
    if target.action == "cancel":
        callback_references.resolve(
            callback.data.removeprefix("gameleave:"),
            scope="game_abandon",
            actor_id=navigation.account.player_id,
            consume=True,
        )
        for key, entry in view["messages"].items():
            if entry.get("kind") == "abandon" and callback.message.message_id in entry.get(
                "ids", []
            ):
                await backend.game_delivery.record(
                    callback.message.chat.id,
                    view["id"],
                    key=key,
                    value={**entry, "kind": "abandon_cancelled"},
                )
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    command = "observe_leave" if "observe_leave" in view["actions"] else "abandon"
    if await perform(
        callback.message, backend, telegram_update_claim, view, localization, locale, command
    ):
        await state.clear()
        updated = await backend.navigation(telegram_update_claim)
        await send_message_model(callback.message, menu_message(updated, localization, locale))
    else:
        await sync_game(backend, callback.message.chat.id, view["id"])


@router.callback_query(F.data.startswith("appealvote:"))
async def handle_game_appeal_vote(
    callback: CallbackQuery,
    backend,
    telegram_update_claim,
    localization,
    locale,
    navigation,
):
    if (
        not isinstance(callback.message, Message)
        or navigation is None
        or navigation.context != "game"
        or navigation.active_game is None
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    try:
        _, appeal_id, approve = callback.data.split(":")
        appeal_id = UUID(appeal_id)
        if approve not in {"0", "1"}:
            raise ValueError
    except ValueError:
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    view = await backend.game_view(telegram_update_claim, navigation.active_game.id)
    ballot = view["messages"].get(f"ballot:{appeal_id}", {})
    if (
        view["dismissed"]
        or not view["appeal"]
        or str(view["appeal"]["id"]) != str(appeal_id)
        or callback.message.message_id not in ballot.get("ids", [])
    ):
        await callback.answer(localization.text("game.expired", locale), show_alert=True)
        return
    await callback.answer()
    if "vote" in view["actions"]:
        await perform(
            callback.message,
            backend,
            telegram_update_claim,
            view,
            localization,
            locale,
            "vote",
            appeal_id=appeal_id,
            approve=approve == "1",
        )
    await sync_game(backend, callback.message.chat.id, view["id"])
