import re
from typing import Literal, cast

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.common import help_message, menu_message, registration_prompt
from sitg_bot.bot.presenters.models import MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, NavigationState

router = Router(name=__name__)


async def send_menu_with_notification_alert(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    navigation: NavigationState,
    localization: LocalizationService,
    locale: str,
) -> None:
    if navigation.context == "game":
        from sitg_bot.bot.handlers.game import show_game

        await show_game(
            message,
            backend,
            claim,
            localization,
            locale,
            backend.game_callback_references,
            navigation.active_game.id,
        )
        return
    if navigation.context != "game":
        alerts = await backend.claim_notification_alerts(
            claim,
            audience=cast(Literal["player", "manager", "admin"], navigation.active_mode),
        )
        if alerts.audiences:
            await send_message_model(
                message,
                MessageModel(localization.text("notifications.unseen_alert", locale)),
            )
    await send_message_model(message, menu_message(navigation, localization, locale))


async def resume_registration(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
) -> None:
    username = message.from_user.username if message.from_user is not None else None
    draft = await backend.start_registration(claim, telegram_username=username)
    locale = localization.locale(draft.account.preferred_locale)
    if draft.next_step is None:
        await backend.complete_registration(claim, expected_version=draft.account.profile_version)
        navigation = await backend.navigation(claim)
        if navigation is None:
            raise RuntimeError("Completed registration has no navigation snapshot")
        await send_message_model(
            message, MessageModel(localization.text("registration.complete", locale))
        )
        await send_menu_with_notification_alert(
            message,
            backend=backend,
            claim=claim,
            navigation=navigation,
            localization=localization,
            locale=locale,
        )
        return
    await send_message_model(message, registration_prompt(draft, localization, locale))


@router.message(CommandStart())
async def handle_start(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    invite = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)? join_([A-Za-z0-9_-]{32})", message.text or "")
    if invite and navigation is not None and navigation.context != "registration":
        await backend.join_lobby(telegram_update_claim, invitation_code=invite.group(1))
        updated = await backend.navigation(telegram_update_claim)
        await send_message_model(message, menu_message(updated, localization, locale))
        return
    if invite:
        await send_message_model(
            message, MessageModel(localization.text("lobby.register_first", locale))
        )
    if navigation is None or navigation.context == "registration":
        await resume_registration(
            message,
            backend=backend,
            claim=telegram_update_claim,
            localization=localization,
            locale=locale,
        )
        return
    await send_menu_with_notification_alert(
        message,
        backend=backend,
        claim=telegram_update_claim,
        navigation=navigation,
        localization=localization,
        locale=locale,
    )


@router.message(Command("help"))
async def handle_help(
    message: Message,
    backend: BotBackend,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    if navigation is not None and navigation.context == "game":
        await backend.game_delivery.track(
            message.chat.id, navigation.active_game.id, message.message_id
        )
        await backend.game_delivery.send(
            message.chat.id,
            navigation.active_game.id,
            f"command:{message.message_id}:help",
            MessageModel(
                localization.text("game.help", locale)
                + "\n\n" + localization.text("chat.help", locale),
                menu_message(navigation, localization, locale).keyboard,
            ),
        )
        return
    await send_message_model(message, help_message(navigation, localization, locale))
