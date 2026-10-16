import base64
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sitg_bot.application.contracts import (
    ActionCode,
    TrainingActOperation,
    TrainingCurrentOperation,
    TrainingDraftDecideOperation,
    TrainingPacketListOperation,
    TrainingStartOperation,
    TrainingUploadOperation,
    TrainingViewOperation,
)
from sitg_bot.bot.miniapps import mini_app_route_url
from sitg_bot.bot.presenters.game import plain_chunks
from sitg_bot.bot.state import GatewayCallError
from sitg_bot.domain.training import TrainingSettings

router = Router(name=__name__)


class TrainingMenuAction(Filter):
    async def __call__(self, message, navigation, localization):
        if navigation is None or navigation.active_mode != "training" or not message.text:
            return False
        for action in ("training.train", "training.upload", "training.knowledge"):
            if action in navigation.allowed_actions and any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"training_action": action}
        return False


async def call(backend, claim, operation):
    return (await backend._execute(claim, operation)).data


async def packets(message, backend, claim, localization, locale, cursor=None):
    result = await call(
        backend,
        claim,
        TrainingPacketListOperation(
            action=ActionCode.TRAINING_PACKETS,
            cursor=cursor,
        ),
    )
    rows = [
        [
            InlineKeyboardButton(
                text=packet["name"][:100],
                callback_data=f"ts:{UUID(packet['id']).hex}:0:0:0",
            )
        ]
        for packet in result["items"]
    ]
    if result["next_cursor"]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=localization.text("training.more", locale),
                    callback_data=f"tp:{UUID(result['next_cursor']).hex}",
                )
            ]
        )
    await message.answer(
        localization.text("training.help" if rows else "training.empty", locale),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


async def start(message, backend, claim, localization, locale, version_id, settings, confirm=False):
    result = await call(
        backend,
        claim,
        TrainingStartOperation(
            action=ActionCode.TRAINING_START,
            version_id=version_id,
            settings=settings.to_dict(),
            confirm=confirm,
        ),
    )
    if result.get("confirmation_required"):
        data = f"tc:{version_id.hex}:{int(settings.reversed)}:{settings.hidden_after or 0}:"
        data += str(int(settings.best_buzz))
        await message.answer(
            localization.text("training.confirm", locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localization.text("training.train", locale),
                            callback_data=data,
                        )
                    ]
                ]
            ),
        )
    elif result.get("preparation"):
        await message.answer(localization.text("training.preparation_unavailable", locale))
    # Successful starts are delivered by the transactional outbox.


@router.message(Command("train"))
async def handle_train(message: Message, backend, telegram_update_claim, localization, locale):
    args = (message.text or "").split()[1:]
    try:
        if not args:
            result = await call(
                backend,
                telegram_update_claim,
                TrainingCurrentOperation(
                    action=ActionCode.TRAINING_CURRENT,
                ),
            )
            if result.get("session"):
                state = result["session"]
                navigation = await backend.navigation(telegram_update_claim)
                await backend.training_delivery(
                    {
                        "owner_id": str(navigation.account.player_id),
                        "session_id": state["id"],
                        "force": True,
                    }
                )
            else:
                await packets(message, backend, telegram_update_claim, localization, locale)
            return
        options = {}
        for option in args[1:]:
            if option == "reverse":
                options["reversed"] = True
            elif option == "bestbuzz":
                options["best_buzz"] = True
            elif option.startswith("hidden="):
                options["hidden_after"] = float(option.split("=", 1)[1])
            else:
                raise ValueError("Unknown training option")
        await start(
            message,
            backend,
            telegram_update_claim,
            localization,
            locale,
            UUID(args[0]),
            TrainingSettings(**options),
        )
    except (ValueError, GatewayCallError):
        await message.answer(localization.text("training.unavailable", locale))


@router.message(TrainingMenuAction())
async def handle_training_menu(
    message: Message,
    backend,
    telegram_update_claim,
    localization,
    locale,
    training_action,
    mini_app_base_url=None,
    knowledge_backend=None,
):
    if training_action == "training.train":
        await handle_train(message, backend, telegram_update_claim, localization, locale)
        return
    if training_action == "training.knowledge":
        from sitg_bot.bot.handlers.knowledge import facts_message
        from sitg_bot.bot.knowledge_backend import KnowledgeTelegramBackend
        from sitg_bot.bot.presenters.render import send_message_model

        adapter = knowledge_backend or KnowledgeTelegramBackend(backend)
        page = await adapter.facts_page(telegram_update_claim)
        await send_message_model(message, facts_message(page, localization, locale))
        if mini_app_base_url:
            await message.answer(
                localization.text("training.knowledge", locale),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=localization.text("training.knowledge", locale),
                                web_app={"url": mini_app_route_url(mini_app_base_url, "knowledge")},
                            )
                        ]
                    ]
                ),
            )
        return
    if mini_app_base_url:
        route = "training"
        await message.answer(
            localization.text(training_action, locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=localization.text(training_action, locale),
                            web_app={"url": mini_app_route_url(mini_app_base_url, route)},
                        )
                    ]
                ]
            ),
        )
    else:
        await message.answer(localization.text("training.document_prompt", locale))


@router.callback_query(F.data.startswith("tp:"))
async def handle_training_page(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale
):
    await callback.answer()
    await packets(
        callback.message,
        backend,
        telegram_update_claim,
        localization,
        locale,
        UUID(callback.data.split(":", 1)[1]),
    )


@router.callback_query(F.data.startswith("ts:") | F.data.startswith("tc:"))
async def handle_training_start(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale
):
    try:
        prefix, version, reversed_, hidden, best_buzz = callback.data.split(":")
        settings = TrainingSettings(
            reversed=bool(int(reversed_)),
            hidden_after=float(hidden) or None,
            best_buzz=bool(int(best_buzz)),
        )
        await start(
            callback.message,
            backend,
            telegram_update_claim,
            localization,
            locale,
            UUID(version),
            settings,
            confirm=prefix == "tc",
        )
        await callback.answer()
    except (ValueError, GatewayCallError):
        await callback.answer(localization.text("training.unavailable", locale), show_alert=True)


@router.callback_query(F.data.startswith("tr:"))
async def handle_training_action(
    callback: CallbackQuery, backend, telegram_update_claim, localization, locale
):
    try:
        _, identifier, version, command = callback.data.split(":")
        result = await call(
            backend,
            telegram_update_claim,
            TrainingViewOperation(
                action=ActionCode.TRAINING_VIEW,
                session_id=UUID(identifier),
            ),
        )
        state = result["session"]
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


@router.message(TrainingContext(), F.document)
async def handle_training_upload(
    message: Message, backend, telegram_update_claim, localization, locale
):
    document = message.document
    if document.file_size is not None and document.file_size > 4 * 1024 * 1024:
        await message.answer(localization.text("training.unavailable", locale))
        return
    downloaded = await message.bot.download(document)
    if downloaded is None:
        return
    result = await call(
        backend,
        telegram_update_claim,
        TrainingUploadOperation(
            action=ActionCode.TRAINING_UPLOAD,
            source_filename=document.file_name or "training.json",
            source_base64=base64.b64encode(downloaded.read()).decode(),
        ),
    )
    for draft in result["drafts"]:
        text = str(draft["packet"]) + "\n" + "\n".join(draft["errors"])
        chunks = plain_chunks(text, size=600)
        for chunk in chunks[:-1]:
            await message.answer(chunk)
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
            chunks[-1] if chunks else "…", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


@router.message(TrainingContext(), F.text)
async def handle_training_answer(
    message: Message, backend, telegram_update_claim, localization, locale
):
    result = await call(
        backend,
        telegram_update_claim,
        TrainingCurrentOperation(
            action=ActionCode.TRAINING_CURRENT,
        ),
    )
    state = result.get("session")
    if state is None or state["phase"] != "answering":
        await message.answer(localization.text("training.help", locale))
        return
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
