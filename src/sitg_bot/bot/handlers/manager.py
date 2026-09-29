import asyncio
import re
from datetime import datetime
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import (
    MANAGER_MENU_ACTIONS,
    MANAGER_TOURNAMENT_ACTIONS,
    TOURNAMENT_LANGUAGE_OPTIONS,
    optional_commentary_keyboard,
    suggested_value_keyboard,
    tournament_creation_confirmation_keyboard,
    tournament_language_keyboard,
)
from sitg_bot.bot.miniapps import mini_app_launch_url, mini_app_route_url
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
    ReplyKeyboardModel,
)
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import (
    BotBackend,
    DeliveredCreationTokenState,
    NavigationState,
    PacketDraftState,
    TokenInventoryState,
    TokenRequestState,
)
from sitg_bot.bot.state.settings import (
    PacketUploadState,
    TournamentCreationState,
    TournamentTokenRequestState,
)

router = Router(name=__name__)

MANAGER_ACTIONS = tuple(action for row in MANAGER_MENU_ACTIONS for action in row)
MANAGER_TOURNAMENT_CONTEXT_ACTIONS = tuple(
    action for row in MANAGER_TOURNAMENT_ACTIONS for action in row
)
PLACEHOLDER_ACTIONS = {
    "manager.appeals",
}


class ManagerMenuAction(Filter):
    async def __call__(
        self,
        message: Message,
        navigation: NavigationState | None,
        localization: LocalizationService,
    ) -> dict[str, str] | bool:
        if (
            navigation is None
            or navigation.active_mode != "manager"
            or navigation.context != "menu"
            or message.text is None
        ):
            return False
        for action in MANAGER_ACTIONS:
            if action not in navigation.allowed_actions:
                continue
            if any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"manager_action": action}
        return False


class ManagerTournamentAction(Filter):
    async def __call__(
        self,
        message: Message,
        navigation: NavigationState | None,
        localization: LocalizationService,
    ) -> dict[str, str] | bool:
        if (
            navigation is None
            or navigation.active_mode != "manager"
            or navigation.context != "tournament"
            or message.text is None
        ):
            return False
        for action in MANAGER_TOURNAMENT_CONTEXT_ACTIONS:
            if action not in navigation.allowed_actions:
                continue
            if any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"manager_tournament_action": action}
        return False


def token_request_result(
    request: TokenRequestState,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    return MessageModel(
        localization.text(
            "token_request.result",
            locale,
            tournament_name=request.tournament_name or "—",
            status=_status("request", request.status, localization, locale),
            created_at=_date(request.created_at, localization, locale),
        )
    )


def token_inventory_message(
    inventory: TokenInventoryState,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    if not inventory.items:
        return MessageModel(localization.text("tokens.empty", locale))
    sections: list[str] = [localization.text("tokens.title", locale)]
    claim_rows: list[tuple[InlineButtonModel, ...]] = []
    for item in inventory.items:
        lines: list[str] = []
        if item.request_status is not None and item.requested_at is not None:
            lines.append(
                localization.text(
                    "tokens.request",
                    locale,
                    tournament_name=item.tournament_name or "—",
                    status=_status("request", item.request_status, localization, locale),
                    requested_at=_date(item.requested_at, localization, locale),
                )
            )
            if item.decision_note:
                lines.append(
                    localization.text(
                        "tokens.decision_commentary",
                        locale,
                        commentary=item.decision_note,
                    )
                )
        if item.token_status is not None:
            lines.append(
                localization.text(
                    "tokens.token",
                    locale,
                    fingerprint=item.fingerprint or "—",
                    status=_status("token", item.token_status, localization, locale),
                    expires_at=(
                        _date(item.expires_at, localization, locale)
                        if item.expires_at is not None
                        else "—"
                    ),
                )
            )
        if item.delivery is not None and item.token_status == "issued":
            lines.append(
                localization.text(
                    "tokens.delivery",
                    locale,
                    status=_status("delivery", item.delivery.status, localization, locale),
                )
            )
        sections.append("\n".join(lines))
        if (
            item.request_id is not None
            and item.token_status == "issued"
            and item.delivery is not None
            and item.delivery.status == "pending"
        ):
            claim_rows.append(
                (
                    InlineButtonModel(
                        localization.text(
                            "tokens.show",
                            locale,
                            fingerprint=item.fingerprint or "—",
                        ),
                        callback_data=f"manager:token_claim:{item.request_id}",
                    ),
                )
            )
    if inventory.next_cursor is not None:
        sections.append(localization.text("tokens.more", locale))
    keyboard = InlineKeyboardModel(rows=tuple(claim_rows)) if claim_rows else None
    return MessageModel("\n\n".join(sections), keyboard)


def delivered_token_message(
    token: DeliveredCreationTokenState,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    return MessageModel(
        localization.text(
            "tokens.plaintext",
            locale,
            token=token.token,
            fingerprint=token.fingerprint,
            expires_at=_date(token.expires_at, localization, locale),
        ),
        protect_content=True,
    )


def _status(
    kind: str,
    status: str,
    localization: LocalizationService,
    locale: str,
) -> str:
    key = f"tokens.status.{kind}.{status}"
    if key not in localization.catalogs[localization.locale(locale)]:
        return status
    return localization.text(key, locale)


def _date(value: str, localization: LocalizationService, locale: str) -> str:
    return localization.format_datetime(datetime.fromisoformat(value), locale)


def _manager_menu_available(navigation: NavigationState | None, action: str) -> bool:
    return (
        navigation is not None
        and navigation.active_mode == "manager"
        and navigation.context == "menu"
        and action in navigation.allowed_actions
    )


def _is_label(value: str, key: str, localization: LocalizationService) -> bool:
    return any(value == localization.text(key, item) for item in localization.catalogs)


def _creation_prompt(
    field: str, suggested: str, localization: LocalizationService, locale: str
) -> MessageModel:
    choices = {"type": ("Ladder", "Classical"), "visibility": ("Private", "Public")}
    return MessageModel(
        localization.text(f"tournament_create.{field}.prompt", locale, suggested=suggested),
        ReplyKeyboardModel(
            rows=(choices[field], (localization.text("button.back", locale),)), one_time=True
        )
        if field in choices
        else suggested_value_keyboard(suggested, localization, locale),
    )


def _language_prompt(localization: LocalizationService, locale: str) -> MessageModel:
    return MessageModel(
        localization.text("tournament_create.language.prompt", locale),
        tournament_language_keyboard(localization, locale),
    )


async def _answer_with_flood_retry(message: Message, model: MessageModel) -> Message:
    while True:
        try:
            return await send_message_model(message, model)
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)


def packet_draft_message(
    draft: PacketDraftState,
    localization: LocalizationService,
    locale: str,
    *,
    launch_links: str | None,
) -> MessageModel:
    def diagnostics(items: tuple[str, ...]) -> str:
        # 500 characters keep similarity warnings readable, including the existing
        # packet ID and the tournaments that already use that packet.
        displayed = [f"• {item[:500]}" for item in items[:5]]
        if len(items) > len(displayed):
            displayed.append(
                localization.text("packet_upload.more", locale, count=len(items) - len(displayed))
            )
        return "\n".join(displayed) or "—"

    warnings = diagnostics(draft.warnings)
    errors = diagnostics(draft.errors)
    authors = ", ".join(name[:60] for name in draft.detected_authors[:10]) or "—"
    if len(draft.detected_authors) > 10:
        authors += "…"
    text = localization.text(
        "packet_upload.result",
        locale,
        name=(draft.packet_name or draft.source_filename)[:300],
        ruleset=draft.ruleset_key,
        themes=(draft.theme_count if draft.ruleset_key == "si" and draft.theme_count is not None
                else "—"),
        questions=draft.question_count if draft.question_count is not None else "—",
        authors=authors,
        warning_count=len(draft.warnings),
        error_count=len(draft.errors),
        warnings=warnings,
        errors=errors,
    )
    rows: list[tuple[InlineButtonModel, ...]] = []
    if launch_links is not None and draft.launch_reference:
        rows.append(
            (
                InlineButtonModel(
                    localization.text("packet_upload.preview", locale),
                    web_app_url=mini_app_launch_url(
                        launch_links,
                        "manager/packets",
                        draft,  # type: ignore[arg-type]
                    ),
                ),
            )
        )
    decisions: list[InlineButtonModel] = []
    if draft.can_publish:
        decisions.append(
            InlineButtonModel(
                localization.text("packet_upload.publish", locale),
                callback_data=f"packet:draft:publish:{draft.draft_id}",
            )
        )
    if draft.can_reject:
        decisions.append(
            InlineButtonModel(
                localization.text("packet_upload.reject", locale),
                callback_data=f"packet:draft:reject:{draft.draft_id}",
            )
        )
    if decisions:
        rows.append(tuple(decisions))
    return MessageModel(text, InlineKeyboardModel(rows=tuple(rows)) if rows else None)


@router.message(StateFilter(PacketUploadState.waiting_document), F.document)
async def handle_packet_document(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
    launch_links: str | None,
) -> None:
    document = message.document
    assert document is not None
    data = await state.get_data()
    maximum_bytes = int(data.get("maximum_bytes", 4 * 1024 * 1024))
    filename = (document.file_name or "").strip()
    if not filename.casefold().endswith((".docx", ".pdf", ".json")):
        await send_message_model(
            message, MessageModel(localization.text("packet_upload.file_invalid", locale))
        )
        return
    if document.file_size is not None and document.file_size > maximum_bytes:
        await send_message_model(
            message,
            MessageModel(
                localization.text(
                    "packet_upload.file_too_large",
                    locale,
                    maximum_mb=max(1, maximum_bytes // (1024 * 1024)),
                )
            ),
        )
        return
    downloaded = await message.bot.download(document)
    if downloaded is None:
        raise RuntimeError("Telegram did not return the uploaded document")
    downloaded.seek(0)
    source = downloaded.read(maximum_bytes + 1)
    if len(source) > maximum_bytes:
        await send_message_model(
            message,
            MessageModel(
                localization.text(
                    "packet_upload.file_too_large",
                    locale,
                    maximum_mb=max(1, maximum_bytes // (1024 * 1024)),
                )
            ),
        )
        return
    drafts = await backend.upload_packet(
        telegram_update_claim,
        tournament_id=UUID(str(data["tournament_id"])),
        source_filename=filename,
        source=source,
    )
    await state.clear()
    for draft in drafts:
        model = packet_draft_message(draft, localization, locale, launch_links=launch_links)
        sent_message = await _answer_with_flood_retry(message, model)
        await backend.bind_packet_draft_message(
            telegram_update_claim,
            draft_id=draft.draft_id,
            chat_id=sent_message.chat.id,
            message_id=sent_message.message_id,
            locale=locale,
        )
        if draft.themes_missing:
            await _answer_with_flood_retry(
                message,
                MessageModel(localization.text("packet_upload.no_themes_warning", locale)),
            )


@router.message(StateFilter(PacketUploadState.waiting_document))
async def handle_packet_document_expected(
    message: Message, localization: LocalizationService, locale: str
) -> None:
    await send_message_model(
        message, MessageModel(localization.text("packet_upload.document_prompt", locale))
    )


@router.message(StateFilter(TournamentTokenRequestState.entering_name), F.text)
async def handle_token_request_name(
    message: Message,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 200:
        await send_message_model(
            message, MessageModel(localization.text("token_request.name.invalid", locale))
        )
        return
    await state.update_data(tournament_name=name)
    await state.set_state(TournamentTokenRequestState.entering_commentary)
    await send_message_model(
        message,
        MessageModel(
            localization.text("token_request.commentary.prompt", locale),
            optional_commentary_keyboard(localization, locale),
        ),
    )


@router.message(StateFilter(TournamentTokenRequestState.entering_commentary), F.text)
async def handle_token_request_commentary(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
) -> None:
    value = (message.text or "").strip()
    if _is_label(value, "button.back", localization):
        await state.clear()
        await send_message_model(message, menu_message(navigation, localization, locale))
        return
    if len(value) > 2000:
        await send_message_model(
            message, MessageModel(localization.text("token_request.commentary.invalid", locale))
        )
        return
    data = await state.get_data()
    commentary = None if _is_label(value, "button.skip", localization) else value or None
    request = await backend.request_tournament_token(
        telegram_update_claim,
        tournament_name=str(data["tournament_name"]),
        commentary=commentary,
    )
    await state.clear()
    await send_message_model(message, token_request_result(request, localization, locale))
    await send_message_model(message, menu_message(navigation, localization, locale))


@router.callback_query(
    StateFilter(TournamentCreationState.confirming),
    F.data.startswith("manager:tournament:create:"),
)
async def handle_tournament_creation_confirmation(
    callback: CallbackQuery,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback.data is None or callback.data.endswith(":no"):
        await state.clear()
        await callback.message.edit_text(localization.text("tournament_create.cancelled", locale))
        if navigation is not None:
            await send_message_model(
                callback.message, menu_message(navigation, localization, locale)
            )
        return
    data = await state.get_data()
    suggested = str(data.get("suggested_name") or "New tournament")
    await state.set_state(TournamentCreationState.entering_name)
    await callback.message.edit_text(localization.text("tournament_create.started", locale))
    await send_message_model(
        callback.message,
        _creation_prompt("name", suggested, localization, locale),
    )


@router.message(StateFilter(TournamentCreationState.entering_name), F.text)
async def handle_tournament_name(
    message: Message, localization: LocalizationService, locale: str, state: FSMContext
) -> None:
    value = (message.text or "").strip()
    if _is_label(value, "button.back", localization):
        await state.set_state(TournamentCreationState.confirming)
        await send_message_model(
            message,
            MessageModel(
                localization.text("tournament_create.confirm", locale),
                tournament_creation_confirmation_keyboard(localization, locale),
            ),
        )
        return
    if not value or len(value) > 200:
        await send_message_model(
            message, MessageModel(localization.text("tournament_create.name.invalid", locale))
        )
        return
    await state.update_data(name=value)
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        data = await state.get_data()
        slug = f"tournament-{str(data['token_id']).replace('-', '')[:8]}"
    await state.update_data(suggested_slug=slug)
    await state.set_state(TournamentCreationState.entering_slug)
    await send_message_model(message, _creation_prompt("slug", slug, localization, locale))


@router.message(StateFilter(TournamentCreationState.entering_slug), F.text)
async def handle_tournament_slug(
    message: Message, localization: LocalizationService, locale: str, state: FSMContext
) -> None:
    value = (message.text or "").strip().casefold()
    if _is_label(message.text or "", "button.back", localization):
        data = await state.get_data()
        await state.set_state(TournamentCreationState.entering_name)
        await send_message_model(
            message,
            _creation_prompt(
                "name",
                str(data.get("name") or data.get("suggested_name") or "New tournament"),
                localization,
                locale,
            ),
        )
        return
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        await send_message_model(
            message, MessageModel(localization.text("tournament_create.slug.invalid", locale))
        )
        return
    await state.update_data(slug=value)
    await state.set_state(TournamentCreationState.entering_type)
    await send_message_model(message, _creation_prompt("type", "ladder", localization, locale))


@router.message(StateFilter(TournamentCreationState.entering_type), F.text)
async def handle_tournament_type(
    message: Message,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
) -> None:
    value = (message.text or "").strip().casefold()
    if value == "classical":
        value = "classic"
    if _is_label(message.text or "", "button.back", localization):
        data = await state.get_data()
        await state.set_state(TournamentCreationState.entering_slug)
        await send_message_model(
            message,
            _creation_prompt(
                "slug",
                str(data.get("slug") or data.get("suggested_slug") or "tournament"),
                localization,
                locale,
            ),
        )
        return
    if value not in {"ladder", "classic"}:
        await send_message_model(
            message, MessageModel(localization.text("tournament_create.type.invalid", locale))
        )
        return
    await state.update_data(type_key=value)
    await state.set_state(TournamentCreationState.entering_ruleset)
    await send_message_model(message, _creation_prompt("ruleset", "si", localization, locale))


@router.message(StateFilter(TournamentCreationState.entering_ruleset), F.text)
async def handle_tournament_ruleset(
    message: Message, localization: LocalizationService, locale: str, state: FSMContext
) -> None:
    value = (message.text or "").strip().casefold()
    if _is_label(message.text or "", "button.back", localization):
        data = await state.get_data()
        await state.set_state(TournamentCreationState.entering_type)
        await send_message_model(
            message,
            _creation_prompt(
                "type", str(data.get("type_key") or "ladder"), localization, locale
            ),
        )
        return
    if value != "si":
        await send_message_model(
            message, MessageModel(localization.text("tournament_create.ruleset.invalid", locale))
        )
        return
    await state.update_data(game_ruleset_key=value)
    await state.set_state(TournamentCreationState.entering_visibility)
    await send_message_model(
        message, _creation_prompt("visibility", "private", localization, locale)
    )


@router.message(StateFilter(TournamentCreationState.entering_visibility), F.text)
async def handle_tournament_visibility(
    message: Message, localization: LocalizationService, locale: str, state: FSMContext
) -> None:
    value = (message.text or "").strip().casefold()
    if _is_label(message.text or "", "button.back", localization):
        data = await state.get_data()
        await state.set_state(TournamentCreationState.entering_ruleset)
        await send_message_model(
            message,
            _creation_prompt(
                "ruleset", str(data.get("game_ruleset_key") or "si"), localization, locale
            ),
        )
        return
    if value not in {"private", "public"}:
        await send_message_model(
            message,
            MessageModel(localization.text("tournament_create.visibility.invalid", locale)),
        )
        return
    await state.update_data(visibility=value)
    await state.set_state(TournamentCreationState.entering_language)
    await send_message_model(message, _language_prompt(localization, locale))


@router.message(StateFilter(TournamentCreationState.entering_language), F.text)
async def handle_tournament_language(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
) -> None:
    raw_value = (message.text or "").strip()
    if _is_label(raw_value, "button.back", localization):
        data = await state.get_data()
        await state.set_state(TournamentCreationState.entering_visibility)
        await send_message_model(
            message,
            _creation_prompt(
                "visibility", str(data.get("visibility") or "private"), localization, locale
            ),
        )
        return
    if _is_label(raw_value, "button.other_language", localization):
        await state.set_state(TournamentCreationState.entering_other_language)
        await send_message_model(
            message,
            MessageModel(
                localization.text("tournament_create.language.other.prompt", locale),
                ReplyKeyboardModel(
                    rows=((localization.text("button.back", locale),),), one_time=True
                ),
            ),
        )
        return
    value = next(
        (
            code
            for code, label_key in TOURNAMENT_LANGUAGE_OPTIONS
            if _is_label(raw_value, label_key, localization)
        ),
        None,
    )
    if value is None:
        await send_message_model(message, _language_prompt(localization, locale))
        return
    await _finish_tournament_creation(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        navigation=navigation,
        state=state,
        language=value,
    )


@router.message(StateFilter(TournamentCreationState.entering_other_language), F.text)
async def handle_tournament_other_language(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
) -> None:
    raw_value = (message.text or "").strip()
    if _is_label(raw_value, "button.back", localization):
        await state.set_state(TournamentCreationState.entering_language)
        await send_message_model(message, _language_prompt(localization, locale))
        return
    value = raw_value.casefold()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", value):
        await send_message_model(
            message, MessageModel(localization.text("tournament_create.language.invalid", locale))
        )
        return
    await _finish_tournament_creation(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        navigation=navigation,
        state=state,
        language=value,
    )


async def _finish_tournament_creation(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
    language: str,
) -> None:
    data = await state.get_data()
    tournament = await backend.create_tournament(
        claim,
        token_id=UUID(str(data["token_id"])),
        name=str(data["name"]),
        slug=str(data["slug"]),
        type_key=str(data["type_key"]),
        game_ruleset_key=str(data["game_ruleset_key"]),
        visibility=str(data["visibility"]),
        language=language,
    )
    await state.clear()
    await send_message_model(
        message,
        MessageModel(
            localization.text(
                "tournament_create.success",
                locale,
                name=tournament.name,
                tournament_id=tournament.id,
            )
        ),
    )
    await send_message_model(message, menu_message(navigation, localization, locale))


@router.message(StateFilter(None), ManagerMenuAction())
async def handle_manager_menu_action(
    message: Message,
    manager_action: str,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
    launch_links: str | None,
) -> None:
    await state.clear()
    if manager_action == "manager.appeals":
        from sitg_bot.bot.handlers.game import handle_manager_appeals

        await handle_manager_appeals(
            message,
            backend,
            telegram_update_claim,
            localization,
            locale,
            await backend.navigation(telegram_update_claim),
            backend.game_callback_references,
        )
        return
    if manager_action == "manager.tournament.select":
        if launch_links is None:
            await send_message_model(
                message,
                MessageModel(
                    localization.text(
                        "feature.placeholder",
                        locale,
                        feature=localization.text(f"button.{manager_action}", locale),
                    )
                ),
            )
            return
        route = "tournaments"
        url = mini_app_route_url(
            launch_links,
            route,
            query={"role": "manager", "relationship": "managed", "select": "1"},
        )
        await send_message_model(
            message,
            MessageModel(
                localization.text(f"miniapp.{route}.prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text(f"miniapp.{route}.open", locale),
                                web_app_url=url,
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    if manager_action == "manager.token.request":
        await state.set_state(TournamentTokenRequestState.entering_name)
        await send_message_model(
            message,
            MessageModel(localization.text("token_request.name.prompt", locale)),
        )
        return
    if manager_action == "manager.tokens":
        inventory = await backend.token_inventory(telegram_update_claim)
        await send_message_model(message, token_inventory_message(inventory, localization, locale))
        return
    if manager_action == "manager.tournament.create":
        inventory = await backend.token_inventory(telegram_update_claim, limit=100)
        available = next((item for item in inventory.items if item.token_status == "issued"), None)
        if available is None or available.token_id is None:
            await send_message_model(
                message,
                MessageModel(localization.text("tournament_create.no_tokens", locale)),
            )
            return
        await state.set_state(TournamentCreationState.confirming)
        await state.update_data(
            token_id=str(available.token_id),
            suggested_name=available.tournament_name or "New tournament",
        )
        await send_message_model(
            message,
            MessageModel(
                localization.text("tournament_create.confirm", locale),
                tournament_creation_confirmation_keyboard(localization, locale),
            ),
        )
        return
    if manager_action in PLACEHOLDER_ACTIONS:
        await send_message_model(
            message,
            MessageModel(
                localization.text(
                    "feature.placeholder",
                    locale,
                    feature=localization.text(f"button.{manager_action}", locale),
                )
            ),
        )


@router.callback_query(F.data.startswith("manager:token_request:"))
async def handle_token_request_decision(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    if not _manager_menu_available(navigation, "manager.token.request"):
        await callback.answer(localization.text("mode.access_changed", locale), show_alert=True)
        if isinstance(callback.message, Message) and navigation is not None:
            await send_message_model(
                callback.message, menu_message(navigation, localization, locale)
            )
        return
    approved = callback.data is not None and callback.data.endswith(":yes")
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if not approved:
        await callback.message.edit_text(localization.text("token_request.cancelled", locale))
    else:
        await callback.message.edit_text(localization.text("token_request.cancelled", locale))
    assert navigation is not None
    await send_message_model(callback.message, menu_message(navigation, localization, locale))


@router.callback_query(F.data.startswith("manager:token_claim:"))
async def handle_token_claim(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    if not _manager_menu_available(navigation, "manager.tokens"):
        await callback.answer(localization.text("mode.access_changed", locale), show_alert=True)
        return
    raw_request_id = callback.data.rpartition(":")[2] if callback.data is not None else ""
    try:
        request_id = str(UUID(raw_request_id))
    except ValueError:
        await callback.answer(localization.text("error.not_found", locale), show_alert=True)
        return
    token = await backend.claim_tournament_token(telegram_update_claim, request_id=request_id)
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await send_message_model(callback.message, delivered_token_message(token, localization, locale))
    refreshed = await backend.token_inventory(telegram_update_claim)
    await send_message_model(
        callback.message, token_inventory_message(refreshed, localization, locale)
    )


@router.message(StateFilter(None), ManagerTournamentAction())
async def handle_manager_tournament_action(
    message: Message,
    manager_tournament_action: str,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    launch_links: str | None,
    state: FSMContext | None = None,
) -> None:
    if manager_tournament_action == "manager.tournament.quit":
        updated = await backend.select_tournament(
            telegram_update_claim,
            mode="manager",
            tournament_id=None,
            expected_version=navigation.navigation_version,
        )
        await send_message_model(message, menu_message(updated, localization, locale))
        return
    if manager_tournament_action == "manager.tournament.packet_upload":
        if state is None:
            raise RuntimeError("Packet upload conversation state is unavailable")
        selected = navigation.selected_manager_tournament
        if selected is None:
            await send_message_model(message, menu_message(navigation, localization, locale))
            return
        eligibility = await backend.packet_upload_eligibility(
            telegram_update_claim, tournament_id=selected.id
        )
        await state.set_state(PacketUploadState.waiting_document)
        await state.update_data(
            tournament_id=str(selected.id),
            maximum_bytes=int(eligibility["maximum_bytes"]),
        )
        await send_message_model(
            message,
            MessageModel(localization.text("packet_upload.document_prompt", locale)),
        )
        return
    if manager_tournament_action == "manager.tournament.profile" and launch_links is not None:
        selected = navigation.selected_manager_tournament
        if selected is None:
            await send_message_model(message, menu_message(navigation, localization, locale))
            return
        url = mini_app_route_url(launch_links, f"tournaments/{selected.id}")
        await send_message_model(
            message,
            MessageModel(
                localization.text("miniapp.tournament_profile.prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text(
                                    "button.manager.tournament.profile", locale
                                ),
                                web_app_url=url,
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    if manager_tournament_action == "manager.tournament.management" and launch_links is not None:
        reference = await backend.tournament_management_link(telegram_update_claim)
        url = mini_app_launch_url(
            launch_links,
            "manager/tournament-management",
            reference,  # type: ignore[arg-type]
        )
        await send_message_model(
            message,
            MessageModel(
                localization.text("miniapp.manager_management.prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("miniapp.manager_management.open", locale),
                                web_app_url=url,
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    await send_message_model(
        message,
        MessageModel(
            localization.text(
                "feature.placeholder",
                locale,
                feature=localization.text(f"button.{manager_tournament_action}", locale),
            )
        ),
    )


@router.callback_query(F.data.startswith("packet:draft:"))
async def handle_packet_draft_decision(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
) -> None:
    data = callback.data or ""
    parts = data.split(":", 3)
    if (
        len(parts) != 4
        or parts[:2] != ["packet", "draft"]
        or parts[2]
        not in {
            "publish",
            "reject",
        }
    ):
        await callback.answer(localization.text("error.not_found", locale), show_alert=True)
        return
    decision, raw_draft_id = parts[2], parts[3]
    try:
        draft_id = UUID(raw_draft_id)
    except ValueError:
        await callback.answer(localization.text("error.not_found", locale), show_alert=True)
        return
    result = await backend.decide_packet_draft(
        telegram_update_claim,
        draft_id=draft_id,
        publish=decision == "publish",
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        status = str(result.get("status"))
        if status not in {"published", "rejected"}:
            return
        await send_message_model(
            callback.message,
            MessageModel(
                localization.text(
                    (
                        "packet_upload.published"
                        if status == "published"
                        else "packet_upload.rejected"
                    ),
                    locale,
                )
            ),
        )
