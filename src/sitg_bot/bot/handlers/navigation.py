from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LOCALE_NAMES, LocalizationService
from sitg_bot.bot.presenters.common import (
    language_message,
    menu_message,
    mode_message,
    registration_prompt,
)
from sitg_bot.bot.presenters.models import MessageModel, RemoveKeyboardModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, GatewayCallError, NavigationState

router = Router(name=__name__)


class NotificationMenuAction(Filter):
    async def __call__(
        self,
        message: Message,
        navigation: NavigationState | None,
        localization: LocalizationService,
    ) -> bool:
        return bool(
            navigation is not None
            and navigation.context != "game"
            and "notifications" in navigation.allowed_actions
            and message.text is not None
            and any(
                message.text == localization.text("button.notifications", known_locale)
                for known_locale in localization.catalogs
            )
        )


@router.message(StateFilter(None), NotificationMenuAction())
async def handle_notifications(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
) -> None:
    audience = cast(Literal["player", "manager", "admin"], navigation.active_mode)
    unseen_page = await backend.notifications(
        telegram_update_claim,
        audience=audience,
        read_state="unseen",
        limit=100,
    )
    unseen_notifications = list(unseen_page.items)
    while unseen_page.next_cursor is not None:
        unseen_page = await backend.notifications(
            telegram_update_claim,
            audience=audience,
            read_state="unseen",
            cursor=unseen_page.next_cursor,
            limit=100,
        )
        unseen_notifications.extend(unseen_page.items)
    seen_page = await backend.notifications(
        telegram_update_claim,
        audience=audience,
        read_state="seen",
        limit=5,
    )
    seen_notifications = list(seen_page.items)
    if not unseen_notifications and not seen_notifications:
        await send_message_model(
            message, MessageModel(localization.text("notifications.empty", locale))
        )
        return
    from sitg_bot.bot.handlers.player import notification_text

    chunks: list[tuple[str, list[UUID]]] = []
    rendered = [localization.text("notifications.title", locale)]
    rendered_unseen_ids: list[UUID] = []
    for heading, notifications in (
        ("notifications.unseen", unseen_notifications),
        ("notifications.seen", seen_notifications),
    ):
        section_heading = localization.text(heading, locale)
        heading_pending = True
        for notification in notifications:
            item = localization.text(
                "notifications.item",
                locale,
                message=notification_text(
                    notification.kind, notification.payload, localization, locale
                ),
                timestamp=localization.format_datetime(
                    datetime.fromisoformat(notification.created_at), locale
                ),
            )
            additions = [section_heading, item] if heading_pending else [item]
            if len("\n\n".join((*rendered, *additions))) > 4000:
                chunks.append(("\n\n".join(rendered), rendered_unseen_ids))
                rendered = [section_heading, item]
                rendered_unseen_ids = []
            else:
                rendered.extend(additions)
            heading_pending = False
            if heading == "notifications.unseen":
                rendered_unseen_ids.append(notification.notification_id)
    chunks.append(("\n\n".join(rendered), rendered_unseen_ids))
    for text, notification_ids in chunks:
        await send_message_model(message, MessageModel(text))
        for notification_id in notification_ids:
            await backend.mark_notification_read(telegram_update_claim, notification_id)


@router.message(Command("menu"))
async def handle_menu(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if navigation is None or navigation.context == "registration":
        from sitg_bot.bot.handlers.common import resume_registration

        await resume_registration(
            message,
            backend=backend,
            claim=telegram_update_claim,
            localization=localization,
            locale=locale,
        )
        return
    from sitg_bot.bot.handlers.common import send_menu_with_notification_alert

    await send_menu_with_notification_alert(
        message,
        backend=backend,
        claim=telegram_update_claim,
        navigation=navigation,
        localization=localization,
        locale=locale,
    )


@router.message(Command("cancel"))
async def handle_cancel(
    message: Message,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    had_prompt = await state.get_state() is not None
    await state.clear()
    key = (
        "prompt.cancelled"
        if had_prompt or navigation is None or navigation.context == "registration"
        else "prompt.none"
    )
    await send_message_model(
        message,
        MessageModel(localization.text(key, locale), RemoveKeyboardModel()),
    )


@router.message(Command("language"))
async def handle_language(
    message: Message,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if navigation is not None and navigation.context == "unavailable":
        await send_message_model(
            message, MessageModel(localization.text("account.unavailable", locale))
        )
        return
    await send_message_model(message, language_message(localization, locale))


@router.callback_query(F.data.startswith("language:"))
async def handle_language_callback(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    selected = callback.data.partition(":")[2] if callback.data is not None else ""
    if selected not in localization.catalogs:
        await callback.answer()
        return
    if navigation is None:
        draft = await backend.start_registration(
            telegram_update_claim, telegram_username=callback.from_user.username
        )
        expected_version = draft.account.profile_version
    else:
        if navigation.context == "unavailable":
            await callback.answer(
                localization.text("account.unavailable", navigation.account.preferred_locale),
                show_alert=True,
            )
            return
        expected_version = navigation.account.profile_version
    draft = await backend.update_language(
        telegram_update_claim,
        locale=selected,
        expected_version=expected_version,
    )
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    language = LOCALE_NAMES[selected][selected]
    await send_message_model(
        callback.message,
        MessageModel(localization.text("language.changed", selected, language=language)),
    )
    if draft is not None and draft.next_step is not None:
        await send_message_model(
            callback.message, registration_prompt(draft, localization, selected)
        )


@router.message(F.text.in_({"Сменить режим", "Switch mode"}))
async def handle_mode_prompt(
    message: Message,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if navigation is None or "mode.switch" not in navigation.allowed_actions:
        if navigation is not None:
            await send_message_model(message, menu_message(navigation, localization, locale))
        return
    await send_message_model(message, mode_message(navigation, localization, locale))


@router.callback_query(F.data.startswith("mode:set:"))
async def handle_mode_callback(
    callback: CallbackQuery,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    selected = callback.data.rpartition(":")[2] if callback.data is not None else ""
    if (
        navigation is None
        or "mode.switch" not in navigation.allowed_actions
        or selected not in navigation.available_modes
    ):
        await callback.answer(localization.text("mode.access_changed", locale), show_alert=True)
        if isinstance(callback.message, Message) and navigation is not None:
            await send_message_model(
                callback.message, menu_message(navigation, localization, locale)
            )
        return
    updated = await backend.set_mode(
        telegram_update_claim,
        mode=selected,
        expected_version=navigation.navigation_version,
    )
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    mode_label = localization.text(f"mode.{selected}", locale)
    await callback.message.edit_text(localization.text("mode.changed", locale, mode=mode_label))
    if selected == "admin":
        try:
            pending = await backend.pending_token_requests(telegram_update_claim, limit=1)
        except GatewayCallError as error:
            if error.error.code != ErrorCode.CAPABILITY_UNAVAILABLE:
                raise
            pending = None
        if pending is not None and pending.items:
            await send_message_model(
                callback.message,
                MessageModel(localization.text("mode.admin.pending_tokens", locale)),
            )
    elif selected == "manager":
        try:
            inventory = await backend.token_inventory(telegram_update_claim, limit=100)
        except GatewayCallError as error:
            if error.error.code != ErrorCode.CAPABILITY_UNAVAILABLE:
                raise
            inventory = None
        if inventory is not None and any(item.token_status == "issued" for item in inventory.items):
            await send_message_model(
                callback.message,
                MessageModel(localization.text("mode.manager.unused_tokens", locale)),
            )
    from sitg_bot.bot.handlers.common import send_menu_with_notification_alert

    await send_menu_with_notification_alert(
        callback.message,
        backend=backend,
        claim=telegram_update_claim,
        navigation=updated,
        localization=localization,
        locale=locale,
    )
