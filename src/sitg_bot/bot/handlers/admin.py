from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.callbacks import CallbackReferenceStore
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import ADMIN_MENU_ACTIONS, optional_commentary_keyboard
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
    RemoveKeyboardModel,
)
from sitg_bot.bot.presenters.render import send_message_model, telegram_keyboard
from sitg_bot.bot.state import (
    BotBackend,
    GatewayCallError,
    NavigationState,
    TokenRequestPageState,
    TokenRequestState,
)
from sitg_bot.bot.state.settings import AdminAuthenticationState, AdminTokenDecisionState

router = Router(name=__name__)

ADMIN_ACTIONS = tuple(action for row in ADMIN_MENU_ACTIONS for action in row)
PLACEHOLDER_ACTIONS = {"admin.ban", "admin.unban"}
TOKEN_DECISION_SCOPE = "admin.token.decision"


@router.message(Command("admin"))
async def handle_admin_authentication_start(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if navigation is None or navigation.account.registration_status != "active":
        await send_message_model(
            message,
            MessageModel(localization.text("error.authentication_required", locale)),
        )
        return
    if not await backend.admin_authentication_available(telegram_update_claim):
        await send_message_model(
            message,
            MessageModel(localization.text("admin.authentication.unavailable", locale)),
        )
        return
    await state.set_state(AdminAuthenticationState.entering_credential)
    await send_message_model(
        message,
        MessageModel(
            localization.text("admin.authentication.prompt", locale),
            RemoveKeyboardModel(),
        ),
    )


@router.message(StateFilter(AdminAuthenticationState.entering_credential), F.text)
async def handle_admin_authentication_credential(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
) -> None:
    credential = message.text or ""
    await state.clear()
    with suppress(TelegramAPIError):
        await message.delete()
    try:
        navigation = await backend.authenticate_admin(telegram_update_claim, credential=credential)
    except GatewayCallError as error:
        if error.error.code not in {
            ErrorCode.CAPABILITY_UNAVAILABLE,
            ErrorCode.FORBIDDEN,
        }:
            raise
        key = (
            "admin.authentication.unavailable"
            if error.error.code == ErrorCode.CAPABILITY_UNAVAILABLE
            else "admin.authentication.failed"
        )
        await send_message_model(message, MessageModel(localization.text(key, locale)))
        return

    alerts = await backend.claim_notification_alerts(
        telegram_update_claim, audience="admin"
    )
    if alerts.audiences:
        await send_message_model(
            message,
            MessageModel(localization.text("notifications.unseen_alert", locale)),
        )
    menu = menu_message(navigation, localization, locale)
    await send_message_model(
        message,
        MessageModel(
            f"{localization.text('admin.authentication.success', locale)}\n\n{menu.text}",
            menu.keyboard,
        ),
    )
    try:
        pending = await backend.pending_token_requests(telegram_update_claim, limit=1)
    except GatewayCallError as error:
        if error.error.code != ErrorCode.CAPABILITY_UNAVAILABLE:
            raise
        pending = None
    if pending is not None and pending.items:
        await send_message_model(
            message,
            MessageModel(localization.text("mode.admin.pending_tokens", locale)),
        )


class AdminMenuAction(Filter):
    async def __call__(
        self,
        message: Message,
        navigation: NavigationState | None,
        localization: LocalizationService,
    ) -> dict[str, str] | bool:
        if (
            navigation is None
            or navigation.active_mode != "admin"
            or navigation.context != "menu"
            or message.text is None
        ):
            return False
        for action in ADMIN_ACTIONS:
            if action not in navigation.allowed_actions:
                continue
            if any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"admin_action": action}
        return False


def pending_token_requests_message(
    page: TokenRequestPageState,
    references: CallbackReferenceStore,
    *,
    administrator_id: UUID,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    if not page.items:
        return MessageModel(localization.text("admin.tokens.empty", locale))
    sections = [localization.text("admin.tokens.title", locale)]
    rows: list[tuple[InlineButtonModel, ...]] = []
    for request in page.items:
        sections.append(
            localization.text(
                "admin.tokens.item",
                locale,
                nickname=request.requester_nickname,
                real_name=request.requester_real_name or "—",
                telegram_username=(
                    f"@{request.requester_telegram_username}"
                    if request.requester_telegram_username
                    else "—"
                ),
                telegram_user_id=request.requester_telegram_user_id or "—",
                requester_id=request.requester_id,
                tournament_name=request.tournament_name or "—",
                justification=request.justification or "—",
                created_at=_date(request.created_at, localization, locale),
            )
        )
        rule_ref = references.issue(
            scope=TOKEN_DECISION_SCOPE,
            actor_id=administrator_id,
            resource_id=request.request_id,
            action="approve",
        )
        rows.append(
            (
                InlineButtonModel(
                    localization.text("admin.tokens.rule", locale),
                    callback_data=f"admin:token:review:{rule_ref}",
                ),
            )
        )
    if page.next_cursor is not None:
        sections.append(localization.text("admin.tokens.more", locale))
    return MessageModel("\n\n".join(sections), InlineKeyboardModel(tuple(rows)))


def pending_token_request_messages(
    page: TokenRequestPageState,
    references: CallbackReferenceStore,
    *,
    administrator_id: UUID,
    localization: LocalizationService,
    locale: str,
) -> tuple[MessageModel, ...]:
    if not page.items:
        return (MessageModel(localization.text("admin.tokens.empty", locale)),)
    messages = tuple(
        pending_token_requests_message(
            TokenRequestPageState(items=(request,), next_cursor=None),
            references,
            administrator_id=administrator_id,
            localization=localization,
            locale=locale,
        )
        for request in page.items
    )
    if page.next_cursor is None:
        return messages
    return (*messages, MessageModel(localization.text("admin.tokens.more", locale)))


async def _send_pending_token_requests(
    message: Message,
    page: TokenRequestPageState,
    references: CallbackReferenceStore,
    *,
    administrator_id: UUID,
    localization: LocalizationService,
    locale: str,
) -> None:
    for model in pending_token_request_messages(
        page,
        references,
        administrator_id=administrator_id,
        localization=localization,
        locale=locale,
    ):
        await send_message_model(message, model)


def token_decision_confirmation(
    request_id: UUID,
    decision: str,
    reference: str,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    action = localization.text(f"admin.tokens.decision.{decision}", locale)
    return MessageModel(
        localization.text(
            "admin.tokens.confirm",
            locale,
            action=action,
            request_id=request_id,
        ),
        InlineKeyboardModel(
            (
                (
                    InlineButtonModel(
                        localization.text("button.yes", locale),
                        callback_data=f"admin:token:final:yes:{reference}",
                    ),
                    InlineButtonModel(
                        localization.text("button.no", locale),
                        callback_data=f"admin:token:final:no:{reference}",
                    ),
                ),
            )
        ),
    )


def token_ruling_keyboard(
    request_id: UUID,
    references: CallbackReferenceStore,
    *,
    administrator_id: UUID,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    def reference(action: str) -> str:
        return references.issue(
            scope=TOKEN_DECISION_SCOPE,
            actor_id=administrator_id,
            resource_id=request_id,
            action=action,
        )

    return MessageModel(
        localization.text("admin.tokens.rule.prompt", locale),
        InlineKeyboardModel(
            (
                (
                    InlineButtonModel(
                        localization.text("admin.tokens.accept", locale),
                        callback_data=f"admin:token:choose:{reference('approve')}",
                    ),
                    InlineButtonModel(
                        localization.text("admin.tokens.reject", locale),
                        callback_data=f"admin:token:choose:{reference('reject')}",
                    ),
                ),
                (
                    InlineButtonModel(
                        localization.text("button.back", locale),
                        callback_data=f"admin:token:back:{reference('back')}",
                    ),
                ),
            )
        ),
    )


def token_decision_receipt(
    request: TokenRequestState,
    *,
    action_id: UUID,
    decided_at: str,
    localization: LocalizationService,
    locale: str,
) -> MessageModel:
    return MessageModel(
        localization.text(
            "admin.tokens.receipt",
            locale,
            action_id=action_id,
            request_id=request.request_id,
            status=localization.text(f"tokens.status.request.{request.status}", locale),
            decided_at=_date(decided_at, localization, locale),
        )
    )


def _date(value: str, localization: LocalizationService, locale: str) -> str:
    return localization.format_datetime(datetime.fromisoformat(value), locale)


def _admin_menu_available(navigation: NavigationState | None, action: str) -> bool:
    return (
        navigation is not None
        and navigation.active_mode == "admin"
        and navigation.context == "menu"
        and action in navigation.allowed_actions
    )


async def _reject_stale_callback(
    callback: CallbackQuery,
    navigation: NavigationState | None,
    localization: LocalizationService,
    locale: str,
) -> None:
    await callback.answer(localization.text("mode.access_changed", locale), show_alert=True)
    if isinstance(callback.message, Message) and navigation is not None:
        await send_message_model(callback.message, menu_message(navigation, localization, locale))


@router.message(StateFilter(None), AdminMenuAction())
async def handle_admin_menu_action(
    message: Message,
    admin_action: str,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    callback_references: CallbackReferenceStore,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
) -> None:
    await state.clear()
    if admin_action == "admin.token_requests.pending":
        page = await backend.pending_token_requests(telegram_update_claim)
        await _send_pending_token_requests(
            message,
            page,
            callback_references,
            administrator_id=navigation.account.player_id,
            localization=localization,
            locale=locale,
        )
        return
    if admin_action in PLACEHOLDER_ACTIONS:
        await send_message_model(
            message,
            MessageModel(
                localization.text(
                    "feature.placeholder",
                    locale,
                    feature=localization.text(f"button.{admin_action}", locale),
                )
            ),
        )


@router.callback_query(F.data.startswith("admin:token:review:"))
async def handle_token_review(
    callback: CallbackQuery,
    callback_references: CallbackReferenceStore,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    if not _admin_menu_available(navigation, "admin.token_requests.pending"):
        await _reject_stale_callback(callback, navigation, localization, locale)
        return
    assert navigation is not None
    raw_reference = callback.data.rpartition(":")[2] if callback.data is not None else ""
    try:
        target = callback_references.resolve(
            raw_reference,
            scope=TOKEN_DECISION_SCOPE,
            actor_id=navigation.account.player_id,
            consume=True,
        )
    except LookupError:
        await callback.answer(localization.text("admin.callback.expired", locale), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        ruling = token_ruling_keyboard(
            target.resource_id,
            callback_references,
            administrator_id=navigation.account.player_id,
            localization=localization,
            locale=locale,
        )
        await callback.message.edit_text(
            ruling.text,
            reply_markup=telegram_keyboard(ruling.keyboard),
        )


@router.callback_query(F.data.startswith("admin:token:back:"))
async def handle_token_ruling_back(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    callback_references: CallbackReferenceStore,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    if not _admin_menu_available(navigation, "admin.token_requests.pending"):
        await _reject_stale_callback(callback, navigation, localization, locale)
        return
    assert navigation is not None
    raw_reference = callback.data.rpartition(":")[2] if callback.data else ""
    try:
        callback_references.resolve(
            raw_reference,
            scope=TOKEN_DECISION_SCOPE,
            actor_id=navigation.account.player_id,
            consume=True,
        )
    except LookupError:
        await callback.answer(localization.text("admin.callback.expired", locale), show_alert=True)
        return
    page = await backend.pending_token_requests(telegram_update_claim)
    model = pending_token_requests_message(
        page,
        callback_references,
        administrator_id=navigation.account.player_id,
        localization=localization,
        locale=locale,
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(model.text, reply_markup=telegram_keyboard(model.keyboard))


@router.callback_query(F.data.startswith("admin:token:choose:"))
async def handle_token_ruling_choice(
    callback: CallbackQuery,
    callback_references: CallbackReferenceStore,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    if not _admin_menu_available(navigation, "admin.token_requests.pending"):
        await _reject_stale_callback(callback, navigation, localization, locale)
        return
    assert navigation is not None
    raw_reference = callback.data.rpartition(":")[2] if callback.data else ""
    try:
        target = callback_references.resolve(
            raw_reference,
            scope=TOKEN_DECISION_SCOPE,
            actor_id=navigation.account.player_id,
            consume=True,
        )
    except LookupError:
        await callback.answer(localization.text("admin.callback.expired", locale), show_alert=True)
        return
    await state.set_state(AdminTokenDecisionState.entering_commentary)
    await state.update_data(
        token_request_id=str(target.resource_id),
        token_request_approve=target.action == "approve",
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            localization.text(
                "admin.tokens.commentary.prompt",
                locale,
                decision=localization.text(f"admin.tokens.decision.{target.action}", locale),
            )
        )
        await send_message_model(
            callback.message,
            MessageModel(
                localization.text("admin.tokens.commentary.optional", locale),
                optional_commentary_keyboard(localization, locale),
            ),
        )


@router.message(StateFilter(AdminTokenDecisionState.entering_commentary), F.text)
async def handle_token_ruling_commentary(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    callback_references: CallbackReferenceStore,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
    clock: Callable[[], datetime],
) -> None:
    value = (message.text or "").strip()
    if any(value == localization.text("button.back", item) for item in localization.catalogs):
        await state.clear()
        page = await backend.pending_token_requests(telegram_update_claim)
        await _send_pending_token_requests(
            message,
            page,
            callback_references,
            administrator_id=navigation.account.player_id,
            localization=localization,
            locale=locale,
        )
        return
    if len(value) > 2000:
        await send_message_model(
            message,
            MessageModel(localization.text("admin.tokens.commentary.invalid", locale)),
        )
        return
    skip = any(value == localization.text("button.skip", item) for item in localization.catalogs)
    data = await state.get_data()
    request = await backend.decide_token_request(
        telegram_update_claim,
        request_id=UUID(str(data["token_request_id"])),
        approve=bool(data["token_request_approve"]),
        commentary=None if skip else value or None,
    )
    await state.clear()
    receipt = token_decision_receipt(
        request,
        action_id=telegram_update_claim.correlation_id,
        decided_at=request.decided_at or clock().isoformat(),
        localization=localization,
        locale=locale,
    )
    await send_message_model(message, receipt)
    page = await backend.pending_token_requests(telegram_update_claim)
    await _send_pending_token_requests(
        message,
        page,
        callback_references,
        administrator_id=navigation.account.player_id,
        localization=localization,
        locale=locale,
    )


@router.callback_query(F.data.startswith("admin:token:final:"))
async def handle_token_final_decision(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    callback_references: CallbackReferenceStore,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    clock: Callable[[], datetime],
) -> None:
    if not _admin_menu_available(navigation, "admin.token_requests.pending"):
        await _reject_stale_callback(callback, navigation, localization, locale)
        return
    assert navigation is not None
    parts = callback.data.split(":") if callback.data is not None else []
    if len(parts) != 5 or parts[3] not in {"yes", "no"}:
        await callback.answer(localization.text("error.not_found", locale), show_alert=True)
        return
    try:
        target = callback_references.resolve(
            parts[4],
            scope=TOKEN_DECISION_SCOPE,
            actor_id=navigation.account.player_id,
            consume=True,
        )
    except LookupError:
        await callback.answer(localization.text("admin.callback.expired", locale), show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    if parts[3] == "no":
        await callback.answer()
        await callback.message.edit_text(localization.text("admin.tokens.cancelled", locale))
        await send_message_model(callback.message, menu_message(navigation, localization, locale))
        return
    request = await backend.decide_token_request(
        telegram_update_claim,
        request_id=target.resource_id,
        approve=target.action == "approve",
    )
    await callback.answer()
    fallback_time = clock().isoformat()
    receipt = token_decision_receipt(
        request,
        action_id=telegram_update_claim.correlation_id,
        decided_at=request.decided_at or fallback_time,
        localization=localization,
        locale=locale,
    )
    await callback.message.edit_text(receipt.text)
    page = await backend.pending_token_requests(telegram_update_claim)
    await send_message_model(
        callback.message,
        pending_token_requests_message(
            page,
            callback_references,
            administrator_id=navigation.account.player_id,
            localization=localization,
            locale=locale,
        ),
    )
