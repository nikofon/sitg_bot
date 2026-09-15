from typing import Literal, cast

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.common import menu_message, registration_prompt
from sitg_bot.bot.presenters.models import MessageModel
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import BotBackend, GatewayCallError, NavigationState

router = Router(name=__name__)


class RegistrationRequired(Filter):
    async def __call__(self, message: Message, navigation: NavigationState | None) -> bool:
        return navigation is not None and navigation.context == "registration"


def parse_consent(value: str, localization: LocalizationService) -> bool | None:
    normalized = " ".join(value.casefold().strip().split())
    yes = {localization.text("button.yes", locale).casefold() for locale in localization.catalogs}
    no = {localization.text("button.no", locale).casefold() for locale in localization.catalogs}
    if normalized in yes:
        return True
    if normalized in no:
        return False
    return None


@router.message(RegistrationRequired(), F.text)
async def handle_registration_input(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
) -> None:
    username = message.from_user.username if message.from_user is not None else None
    draft = await backend.start_registration(telegram_update_claim, telegram_username=username)
    locale = localization.locale(draft.account.preferred_locale)
    if draft.next_step is None:
        await backend.complete_registration(
            telegram_update_claim, expected_version=draft.account.profile_version
        )
        await _send_completed_menu(
            message,
            backend=backend,
            claim=telegram_update_claim,
            localization=localization,
            locale=locale,
            state=state,
        )
        return
    if message.text.startswith("/"):
        await send_message_model(message, registration_prompt(draft, localization, locale))
        return

    value: str | bool = message.text
    if draft.next_step == "telegram_public":
        consent = parse_consent(message.text, localization)
        if consent is None:
            await send_message_model(
                message,
                registration_prompt(
                    draft,
                    localization,
                    locale,
                    guidance_key="registration.invalid.telegram_public",
                ),
            )
            return
        value = consent
    try:
        saved = await backend.save_registration_step(
            telegram_update_claim,
            step=draft.next_step,
            value=value,
            expected_version=draft.account.profile_version,
        )
    except GatewayCallError as error:
        if error.error.code != ErrorCode.VALIDATION_FAILED:
            raise
        await send_message_model(
            message,
            registration_prompt(
                draft,
                localization,
                locale,
                guidance_key=f"registration.invalid.{draft.next_step}",
            ),
        )
        return

    if saved.next_step is not None:
        await send_message_model(message, registration_prompt(saved, localization, locale))
        return
    await backend.complete_registration(
        telegram_update_claim, expected_version=saved.account.profile_version
    )
    await _send_completed_menu(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        state=state,
    )


async def _send_completed_menu(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    state: FSMContext | None = None,
) -> None:
    navigation = await backend.navigation(claim)
    if navigation is None:
        raise RuntimeError("Completed registration has no navigation snapshot")
    alerts = await backend.claim_notification_alerts(
        claim,
        audience=cast(Literal["player", "manager", "admin"], navigation.active_mode),
    )
    if alerts.audiences:
        await send_message_model(
            message,
            MessageModel(localization.text("notifications.unseen_alert", locale)),
        )
    menu = menu_message(navigation, localization, locale)
    text = f"{localization.text('registration.complete', locale)}\n\n{menu.text}"
    await send_message_model(message, MessageModel(text, menu.keyboard))
    if state is not None:
        from sitg_bot.bot.handlers.common import resume_invitation

        await resume_invitation(message, backend, claim, localization, locale, state)


@router.message(RegistrationRequired())
async def handle_unsupported_registration_message(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
) -> None:
    from sitg_bot.bot.handlers.common import resume_registration

    await resume_registration(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
    )
