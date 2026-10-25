"""Keyboard-driven solo training lobby and question controls."""

import base64
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sitg_bot.application.contracts import (
    ActionCode,
    TrainingActOperation,
    TrainingCurrentOperation,
    TrainingDraftDecideOperation,
    TrainingLobbyOperation,
    TrainingStartOperation,
    TrainingUploadOperation,
    TrainingViewOperation,
)
from sitg_bot.bot.handlers.common import send_menu_with_notification_alert
from sitg_bot.bot.handlers.knowledge import send_message_model
from sitg_bot.bot.miniapps import mini_app_route_url
from sitg_bot.bot.state import GatewayCallError
from sitg_bot.bot.state.settings import TrainingUploadState

router = Router(name=__name__)


class TrainingMenuAction(Filter):
    async def __call__(self, message, navigation, localization):
        if navigation is None or navigation.active_mode != "training" or not message.text:
            return False
        for action in (
            "training.train",
            "training.upload",
            "training.knowledge",
            "training.history",
            "training.settings",
            "training.start",
            "training.quit",
        ):
            if action in navigation.allowed_actions and any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"training_action": action}
        return False


async def call(backend, claim, operation):
    return (await backend._execute(claim, operation)).data


async def show_menu(message, backend, claim, localization, locale):
    navigation = await backend.navigation(claim)
    await send_menu_with_notification_alert(
        message, backend=backend, claim=claim, navigation=navigation,
        localization=localization, locale=locale,
    )


async def open_app(message, localization, locale, base_url, action):
    if not base_url:
        await message.answer(localization.text("training.miniapp_unavailable", locale))
        return
    route = (
        "knowledge" if action == "training.knowledge"
        else "training/history" if action == "training.history"
        else "training"
    )
    url = mini_app_route_url(base_url, route)
    await message.answer(
        localization.text(action, locale),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=localization.text(action, locale), web_app={"url": url})]
            ]
        ),
    )


async def start_lobby(
    message, backend, claim, localization, locale, *, confirm=False, expected_version=None
):
    result = await call(
        backend, claim, TrainingCurrentOperation(action=ActionCode.TRAINING_CURRENT)
    )
    lobby = result.get("lobby")
    if not lobby or not lobby.get("version_id"):
        await message.answer(localization.text("training.select_packet", locale))
        return
    version = expected_version if expected_version is not None else lobby["version"]
    result = await call(
        backend,
        claim,
        TrainingStartOperation(
            action=ActionCode.TRAINING_START,
            version_id=UUID(lobby["version_id"]),
            lobby_version=version,
            confirm=confirm,
        ),
    )
    if result.get("confirmation_required"):
        await message.answer(
            localization.text("training.confirm", locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localization.text("button.training.start", locale),
                            callback_data=f"tl:start:{version}",
                        )
                    ]
                ]
            ),
        )
    elif result.get("preparation"):
        await message.answer(localization.text("training.preparation_unavailable", locale))
    else:
        await show_menu(message, backend, claim, localization, locale)


@router.message(TrainingMenuAction())
async def handle_training_menu(
    message: Message,
    backend,
    telegram_update_claim,
    localization,
    locale,
    training_action,
    launch_links: str | None = None,
    knowledge_backend=None,
    state: FSMContext | None = None,
):
    try:
        if state is not None:
            await state.clear()
        if training_action == "training.upload":
            if state is None:
                raise RuntimeError("Training upload conversation state is unavailable")
            await state.set_state(TrainingUploadState.waiting_document)
            await message.answer(localization.text("training.document_prompt", locale))
        elif training_action == "training.train":
            await call(
                backend,
                telegram_update_claim,
                TrainingLobbyOperation(action=ActionCode.TRAINING_LOBBY, command="create"),
            )
            await show_menu(message, backend, telegram_update_claim, localization, locale)
        elif training_action == "training.start":
            await start_lobby(message, backend, telegram_update_claim, localization, locale)
        elif training_action == "training.quit":
            current = await call(
                backend,
                telegram_update_claim,
                TrainingCurrentOperation(action=ActionCode.TRAINING_CURRENT),
            )
            if current.get("session"):
                session_state = current["session"]
                await track_input(backend, session_state, message, telegram_update_claim)
                await call(
                    backend,
                    telegram_update_claim,
                    TrainingActOperation(
                        action=ActionCode.TRAINING_ACT,
                        session_id=UUID(session_state["id"]),
                        expected_version=session_state["version"],
                        question_revision_id=UUID(session_state["question_revision_id"]),
                        command="stop",
                    ),
                )
            elif current.get("lobby"):
                await call(
                    backend,
                    telegram_update_claim,
                    TrainingLobbyOperation(
                        action=ActionCode.TRAINING_LOBBY,
                        command="quit",
                        expected_version=current["lobby"]["version"],
                    ),
                )
            await show_menu(message, backend, telegram_update_claim, localization, locale)
        elif training_action == "training.knowledge" and not launch_links:
            from sitg_bot.bot.handlers.knowledge import facts_message
            from sitg_bot.bot.knowledge_backend import KnowledgeTelegramBackend

            adapter = knowledge_backend or KnowledgeTelegramBackend(backend)
            page = await adapter.facts_page(telegram_update_claim)
            await send_message_model(message, facts_message(page, localization, locale))
        else:
            await open_app(
                message,
                localization,
                locale,
                launch_links,
                training_action,
            )
    except (ValueError, GatewayCallError):
        await message.answer(localization.text("training.unavailable", locale))


@router.callback_query(F.data.startswith("tl:start:"))
async def handle_training_start(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale
):
    if not isinstance(callback.message, Message):
        await callback.answer(localization.text("training.unavailable", locale), show_alert=True)
        return
    try:
        await start_lobby(
            callback.message,
            backend,
            telegram_update_claim,
            localization,
            locale,
            confirm=True,
            expected_version=int(callback.data.split(":")[-1]),
        )
        await callback.answer()
    except (ValueError, GatewayCallError):
        await callback.answer(localization.text("training.unavailable", locale), show_alert=True)


@router.callback_query(F.data.startswith("tr:"))
async def handle_training_action(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale
):
    try:
        if not isinstance(callback.message, Message):
            raise ValueError("Question message is unavailable")
        _, identifier, version, command = callback.data.split(":")
        # Obsolete action keyboards must not keep controlling a recovered session.
        if command != "continue":
            raise ValueError("Use the reply keyboard")
        result = await call(
            backend,
            telegram_update_claim,
            TrainingViewOperation(action=ActionCode.TRAINING_VIEW, session_id=UUID(identifier)),
        )
        state = result["session"]
        if callback.message.message_id not in state.get("message_ids", []):
            raise ValueError("Question message has changed")
        await call(
            backend,
            telegram_update_claim,
            TrainingActOperation(
                action=ActionCode.TRAINING_ACT,
                session_id=UUID(identifier),
                expected_version=int(version),
                question_revision_id=UUID(state["question_revision_id"]),
                command=command,
            ),
        )
        await callback.answer()
    except (ValueError, GatewayCallError):
        await callback.answer(localization.text("training.unavailable", locale), show_alert=True)


@router.callback_query(F.data.startswith("td:"))
async def handle_training_draft(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale
):
    try:
        _, draft, version, publish = callback.data.split(":")
        await call(
            backend,
            telegram_update_claim,
            TrainingDraftDecideOperation(
                action=ActionCode.TRAINING_DRAFT_DECIDE,
                draft_id=UUID(draft),
                expected_version=int(version),
                publish=publish == "1",
            ),
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
    except (ValueError, GatewayCallError):
        await callback.answer(localization.text("training.unavailable", locale), show_alert=True)


class TrainingContext(Filter):
    async def __call__(self, message, navigation):
        return navigation is not None and navigation.active_mode == "training"


@router.message(TrainingContext(), StateFilter(TrainingUploadState.waiting_document), F.document)
async def handle_training_upload(
    message: Message, backend, telegram_update_claim, localization, locale
):
    document = message.document
    assert document is not None
    maximum_bytes = 4 * 1024 * 1024
    filename = (document.file_name or "").strip()
    if not filename.casefold().endswith((".docx", ".pdf", ".json")):
        await message.answer(localization.text("training.file_invalid", locale))
        return

    async def too_large():
        await message.answer(
            localization.text("packet_upload.file_too_large", locale, maximum_mb=4)
        )

    if document.file_size is not None and document.file_size > maximum_bytes:
        await too_large()
        return
    downloaded = await message.bot.download(document)
    if downloaded is None:
        await message.answer(localization.text("training.unavailable", locale))
        return
    downloaded.seek(0)
    source = downloaded.read(maximum_bytes + 1)
    if len(source) > maximum_bytes:
        await too_large()
        return
    try:
        result = await call(
            backend,
            telegram_update_claim,
            TrainingUploadOperation(
                action=ActionCode.TRAINING_UPLOAD,
                source_filename=filename,
                source_base64=base64.b64encode(source).decode(),
            ),
        )
    except (ValueError, GatewayCallError):
        await message.answer(localization.text("training.unavailable", locale))
        return
    # Like tournament uploads, retain the prompt for multiple files/albums until
    # /cancel or another action. The training operation derives private ownership
    # from the authenticated principal and never asks for a tournament.
    def diagnostics(items):
        # Bound even HTML-escaped parser diagnostics below Telegram's message
        # limit, like the tournament upload summary. Report omitted items.
        displayed = [f"• {item[:80]}" for item in items[:2]]
        if len(items) > len(displayed):
            displayed.append(
                localization.text("packet_upload.more", locale, count=len(items) - len(displayed))
            )
        return "\n".join(displayed) or "—"

    for draft in result["drafts"]:
        packet = draft["packet"]
        themes = packet.get("themes", [])
        text = localization.text(
            "packet_upload.result", locale,
            name=(packet.get("name") or filename)[:80],
            ruleset="si",
            themes=len(themes),
            questions=sum(len(theme.get("questions", [])) for theme in themes),
            authors=(packet.get("lead_author") or "—")[:60],
            warning_count=len(draft.get("warnings", [])),
            error_count=len(draft["errors"]),
            warnings=diagnostics(draft.get("warnings", [])),
            errors=diagnostics(draft["errors"]),
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=localization.text(
                        "training.publish" if publish else "training.reject", locale
                    ),
                    callback_data=f"td:{UUID(draft['draft_id']).hex}:{draft['version']}:{int(publish)}",
                )
            ]
            for publish in ([False] if draft["errors"] else [True, False])
        ]
        await message.answer(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


@router.message(TrainingContext(), StateFilter(TrainingUploadState.waiting_document))
async def handle_training_document_expected(message: Message, localization, locale):
    await message.answer(localization.text("training.document_prompt", locale))


async def track_input(backend, state, message, claim):
    delivery = getattr(backend, "training_delivery", None)
    if delivery is not None:
        if "owner_id" not in state:
            navigation = await backend.navigation(claim)
            state = {**state, "owner_id": str(navigation.account.player_id)}
        await delivery.track_input(state, message)


class TrainingControl(Filter):
    async def __call__(self, message, localization):
        text = message.text or ""
        symbols = {"+": "buzz", "||": "pause_toggle", "!": "appeal"}
        if text in symbols:
            return {"training_command": symbols[text]}
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0].split("@")[0][1:]
            if command in {"score", "pause", "resume", "appeal", "quit"}:
                return {"training_command": "stop" if command == "quit" else command}
        for locale in localization.catalogs:
            for command in ("next", "reveal", "skip", "score"):
                if text == localization.text(f"training.{command}", locale):
                    return {"training_command": command}
            for approve, key in ((True, "button.yes"), (False, "button.no")):
                if text == localization.text(key, locale):
                    return {"training_command": "vote", "training_approve": approve}
        return False


@router.message(TrainingContext(), StateFilter(None), TrainingControl())
async def handle_training_control(
    message: Message, backend, telegram_update_claim, localization, locale,
    training_command, training_approve=None,
):
    try:
        result = await call(backend, telegram_update_claim, TrainingCurrentOperation(
            action=ActionCode.TRAINING_CURRENT, score_only=training_command == "score",
        ))
        state = result.get("session")
        if state is None:
            raise ValueError("No training session")
        await track_input(backend, state, message, telegram_update_claim)
        if training_command == "score":
            sent = await message.answer(localization.text(
                "training.score_value", locale, score=state["score"],
            ))
            await track_input(backend, state, sent, telegram_update_claim)
            return
        command = training_command
        # Yes/No are ordinary answers outside a ballot, not reserved commands.
        if command == "vote" and state.get("appeal_status") != "voting":
            if state["phase"] != "answering" or state.get("status") == "paused":
                raise ValueError("No appeal ballot")
            command = "answer"
            training_approve = None
        if command == "pause_toggle":
            command = "resume" if state["status"] == "paused" else "pause"
        question_id = state["question_revision_id"]
        if command == "buzz":
            question_id = state.get("presented_question_revision_id")
            if not question_id:
                raise ValueError("Wait for the question to be delivered")
        if command == "answer" and message.reply_to_message and (
            state.get("answer_prompts", {}).get(str(message.reply_to_message.message_id))
            != question_id
        ):
            raise ValueError("Answer prompt has changed")
        await call(backend, telegram_update_claim, TrainingActOperation(
            action=ActionCode.TRAINING_ACT, session_id=UUID(state["id"]),
            expected_version=state["version"], question_revision_id=UUID(question_id),
            command=command, approve=training_approve,
            answer=message.text if command == "answer" else None,
        ))
        if command == "stop":
            await show_menu(message, backend, telegram_update_claim, localization, locale)
    except (ValueError, GatewayCallError):
        await message.answer(localization.text("training.unavailable", locale))


@router.message(TrainingContext(), StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_training_answer(
    message: Message, backend, telegram_update_claim, localization, locale
):
    result = await call(
        backend, telegram_update_claim, TrainingCurrentOperation(action=ActionCode.TRAINING_CURRENT)
    )
    state = result.get("session")
    if state is None or state["phase"] != "answering" or state.get("status") == "paused":
        await message.answer(localization.text("training.help", locale))
        await show_menu(message, backend, telegram_update_claim, localization, locale)
        return
    # Commands are never interpreted as answers.
    if (message.text or "").startswith("/"):
        await message.answer(localization.text("training.help", locale))
        return
    try:
        await track_input(backend, state, message, telegram_update_claim)
        reply = message.reply_to_message
        if reply and state.get("answer_prompts", {}).get(str(reply.message_id)) != (
            state["question_revision_id"]
        ):
            raise ValueError("Answer prompt has changed")
        await call(
            backend,
            telegram_update_claim,
            TrainingActOperation(
                action=ActionCode.TRAINING_ACT,
                session_id=UUID(state["id"]),
                expected_version=state["version"],
                question_revision_id=UUID(state["question_revision_id"]),
                command="answer",
                answer=message.text,
            ),
        )
    except (ValueError, GatewayCallError):
        await message.answer(localization.text("training.unavailable", locale))
