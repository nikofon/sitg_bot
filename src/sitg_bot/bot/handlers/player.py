from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from sitg_bot.application.contracts import ErrorCode
from sitg_bot.application.telegram import TelegramUpdateClaim
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import (
    OTHER_MENU_ACTIONS,
    PLAYER_MENU_ACTIONS,
    PLAYER_TOURNAMENT_ACTIONS,
    navigation_keyboard,
    other_menu_keyboard,
    setting_choices_keyboard,
    setting_names_keyboard,
)
from sitg_bot.bot.miniapps import mini_app_route_url, website_url
from sitg_bot.bot.presenters.common import menu_message
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
)
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.bot.state import (
    BotBackend,
    GatewayCallError,
    NavigationState,
    SettingsState,
    SettingState,
)
from sitg_bot.bot.state.settings import BugReportState, SettingEditState

router = Router(name=__name__)

PLAYER_ACTIONS = tuple(
    action
    for row in (*PLAYER_MENU_ACTIONS, *OTHER_MENU_ACTIONS, *PLAYER_TOURNAMENT_ACTIONS)
    for action in row
)
PLACEHOLDER_ACTIONS = {
    "player.rating",
    "player.history",
    "player.author_link",
    "player.ongoing",
}


class PlayerMenuAction(Filter):
    async def __call__(
        self,
        message: Message,
        navigation: NavigationState | None,
        localization: LocalizationService,
    ) -> dict[str, str] | bool:
        if (
            navigation is None
            or navigation.active_mode != "player"
            or navigation.context not in {"menu", "tournament"}
            or message.text is None
        ):
            return False
        for action in PLAYER_ACTIONS:
            if action not in navigation.allowed_actions:
                continue
            if any(
                message.text == localization.text(f"button.{action}", locale)
                for locale in localization.catalogs
            ):
                return {"player_action": action}
        return False


def settings_message(
    settings: SettingsState, localization: LocalizationService, locale: str
) -> MessageModel:
    items = [
        localization.text(
            "settings.item",
            locale,
            label=setting.labels.get(locale, setting.key),
            value=setting.localized_value.get(locale, str(setting.value)),
        )
        for setting in settings.settings
    ]
    updated_at = localization.format_datetime(datetime.fromisoformat(settings.updated_at), locale)
    text = "\n".join(
        (
            localization.text("settings.title", locale),
            *items,
            localization.text("settings.updated_at", locale, updated_at=updated_at),
        )
    )
    return MessageModel(text)


def other_menu_message(
    navigation: NavigationState, localization: LocalizationService, locale: str
) -> MessageModel:
    return MessageModel(
        localization.text("menu.player.other", locale),
        other_menu_keyboard(navigation, localization, locale),
    )


def notification_text(
    kind: str, payload: dict[str, object], localization: LocalizationService, locale: str
) -> str:
    if kind == "tournament.start_due":
        return localization.text(
            "notification.tournament.start_due", locale, name=payload.get("name", ""),
        )
    if kind == "packet.substituted":
        return localization.text(
            "notification.packet.substituted", locale,
            packet_name=payload.get("packet_name", ""),
            manager_name=payload.get("manager_name", ""),
        )
    if kind == "author.registered":
        return localization.text(
            "notification.author.registered",
            locale,
            author_name=payload.get("author_name", ""),
            tournament_name=payload.get("tournament_name", ""),
        )
    if kind == "author_link.request_decided":
        return localization.text(
            "notification.author_link.decided",
            locale,
            status=payload.get("status", ""),
        )
    if kind == "author_link.request_created":
        return localization.text(
            "author_link.request_created",
            locale,
            player_nickname=payload.get("player_nickname", ""),
            author_name=payload.get("author_name", ""),
        )
    if kind == "tournament_token.request_created":
        return localization.text(
            "tournament_token.request_created",
            locale,
            requester_nickname=payload.get("requester_nickname", ""),
            tournament_name=payload.get("tournament_name", ""),
        )
    if kind == "tournament_token.request_decided":
        return localization.text(
            "tournament_token.request_decided",
            locale,
            status=payload.get("status", ""),
        )
    if kind == "bug_report":
        created_raw = str(payload.get("created_at", ""))
        try:
            created_at: str = localization.format_datetime(
                datetime.fromisoformat(created_raw), locale
            )
        except ValueError:
            created_at = created_raw
        return localization.text(
            "notification.bug_report",
            locale,
            nickname=(
                payload.get("reporter_nickname") or payload.get("reporter_telegram_username") or ""
            ),
            created_at=created_at,
            commentary=payload.get("commentary", ""),
        )
    return localization.text("notification.generic", locale, kind=kind)


def _setting(settings: SettingsState, key: str) -> SettingState | None:
    return next((setting for setting in settings.settings if setting.key == key), None)


def _setting_value(setting: SettingState, raw: str) -> str | bool:
    normalized = raw.strip().casefold()
    for choice in setting.choices:
        if normalized == str(choice.value).casefold() or any(
            normalized == label.casefold() for label in choice.labels.values()
        ):
            return choice.value
    return raw


def _is_back(value: str, localization: LocalizationService) -> bool:
    return any(
        value == localization.text("button.back", candidate) for candidate in localization.catalogs
    )


async def _save_setting(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    key: str,
    raw_value: str,
    return_to_other: bool = False,
) -> None:
    current = await backend.settings(claim)
    descriptor = _setting(current, key)
    if descriptor is None:
        await send_message_model(
            message, MessageModel(localization.text("settings.unknown", locale))
        )
        return
    updated = await backend.update_setting(
        claim,
        key=key,
        value=_setting_value(descriptor, raw_value),
        expected_version=current.profile_version,
    )
    saved = _setting(updated, key)
    if key == "language" and saved is not None and isinstance(saved.value, str):
        locale = localization.locale(saved.value)
    await send_message_model(
        message,
        MessageModel(
            localization.text(
                "settings.saved",
                locale,
                label=saved.labels.get(locale, key) if saved is not None else key,
                value=(
                    saved.localized_value.get(locale, str(saved.value))
                    if saved is not None
                    else raw_value
                ),
            ),
            (
                other_menu_keyboard(navigation, localization, locale)
                if return_to_other
                else navigation_keyboard(navigation, localization, locale)
            ),
        ),
    )


@router.message(Command("profile"))
async def handle_profile_command(
    message: Message,
    command: CommandObject,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    launch_links: str | None,
) -> None:
    if navigation is None or navigation.account.registration_status != "active":
        await send_message_model(
            message,
            MessageModel(localization.text("error.authentication_required", locale)),
        )
        return
    reference = (command.args or "").strip()
    if not reference:
        await send_message_model(
            message,
            MessageModel(localization.text("profile.command.usage", locale)),
        )
        return
    try:
        resolved = await backend.resolve_player(telegram_update_claim, reference=reference)
    except GatewayCallError as error:
        if error.error.code not in {ErrorCode.VALIDATION_FAILED, ErrorCode.NOT_FOUND}:
            raise
        key = (
            "profile.command.not_found"
            if error.error.code == ErrorCode.NOT_FOUND
            else "profile.command.invalid"
        )
        await send_message_model(message, MessageModel(localization.text(key, locale)))
        return
    if launch_links is None:
        await send_message_model(
            message,
            MessageModel(localization.text("error.capability_unavailable", locale)),
        )
        return
    nickname = str(resolved.get("nickname") or resolved["player_id"])
    url = mini_app_route_url(launch_links, f"players/{resolved['player_id']}")
    await send_message_model(
        message,
        MessageModel(
            localization.text("profile.command.prompt", locale, nickname=nickname),
            InlineKeyboardModel(
                rows=(
                    (
                        InlineButtonModel(
                            localization.text("miniapp.players.open", locale),
                            web_app_url=url,
                        ),
                    ),
                )
            ),
        ),
    )


@router.message(Command("set"))
async def handle_set_command(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if navigation is None or navigation.active_mode != "player":
        return
    text = message.text or ""
    arguments = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""
    key, separator, value = arguments.partition(" ")
    if not separator or key not in {"real_name", "nickname", "telegram_public", "language"}:
        await send_message_model(
            message, MessageModel(localization.text("settings.command_usage", locale))
        )
        return
    await _save_setting(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        navigation=navigation,
        key=key,
        raw_value=value,
    )


@router.message(StateFilter(SettingEditState.selecting), F.text)
async def handle_setting_selection(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
) -> None:
    if message.text is not None and _is_back(message.text, localization):
        await state.clear()
        await send_message_model(message, other_menu_message(navigation, localization, locale))
        return
    settings = await backend.settings(telegram_update_claim)
    selected = next(
        (setting for setting in settings.settings if message.text in setting.labels.values()),
        None,
    )
    if selected is None:
        await send_message_model(
            message,
            MessageModel(
                localization.text("settings.choose", locale),
                setting_names_keyboard(settings, localization, locale),
            ),
        )
        return
    await state.update_data(setting_key=selected.key)
    await state.set_state(SettingEditState.entering)
    choices = tuple(choice.labels.get(locale, str(choice.value)) for choice in selected.choices)
    keyboard = setting_choices_keyboard(choices, localization, locale)
    await send_message_model(
        message,
        MessageModel(
            localization.text(
                "settings.value_prompt",
                locale,
                label=selected.labels.get(locale, selected.key),
            ),
            keyboard,
        ),
    )


@router.message(StateFilter(SettingEditState.entering), F.text)
async def handle_setting_value(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
) -> None:
    if message.text is not None and _is_back(message.text, localization):
        settings = await backend.settings(telegram_update_claim)
        await state.set_state(SettingEditState.selecting)
        await send_message_model(
            message,
            MessageModel(
                localization.text("settings.choose", locale),
                setting_names_keyboard(settings, localization, locale),
            ),
        )
        return
    data = await state.get_data()
    key = str(data.get("setting_key", ""))
    await _save_setting(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        navigation=navigation,
        key=key,
        raw_value=message.text or "",
        return_to_other=True,
    )
    await state.clear()


@router.message(StateFilter(None), F.text.in_({"Back", "Назад"}))
async def handle_other_menu_back(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState | None,
) -> None:
    if navigation is None:
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


@router.message(PlayerMenuAction())
async def handle_player_menu_action(
    message: Message,
    player_action: str,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    navigation: NavigationState,
    state: FSMContext,
    launch_links: str | None,
) -> None:
    if player_action == "lobby.reopen" and navigation.active_lobby is not None:
        updated = await backend.lobby_context(
            telegram_update_claim, context="lobby", expected_version=navigation.navigation_version
        )
        from sitg_bot.bot.handlers.lobby import show_lobby

        await show_lobby(
            message, backend, telegram_update_claim, updated, localization, locale, launch_links
        )
        return
    if player_action.startswith("player.tournament."):
        selected = navigation.selected_player_tournament
        if navigation.context != "tournament" or selected is None:
            await send_message_model(message, menu_message(navigation, localization, locale))
            return
        if player_action == "player.tournament.quit":
            updated = await backend.select_tournament(
                telegram_update_claim,
                mode="player",
                tournament_id=None,
                expected_version=navigation.navigation_version,
            )
            from sitg_bot.bot.handlers.common import send_menu_with_notification_alert

            await send_menu_with_notification_alert(
                message,
                backend=backend,
                claim=telegram_update_claim,
                navigation=updated,
                localization=localization,
                locale=locale,
            )
            return
        if player_action == "player.tournament.info":
            details = await backend.tournament_info(
                telegram_update_claim, tournament_id=selected.id
            )
            tournament = details.get("tournament", {})
            status = (
                tournament.get("status", selected.status)
                if isinstance(tournament, dict)
                else selected.status
            )
            await send_message_model(
                message,
                MessageModel(
                    localization.text(
                        "tournament.info",
                        locale,
                        tournament=selected.name,
                        status=status,
                    ),
                    InlineKeyboardModel(
                        rows=(
                            (
                                InlineButtonModel(
                                    localization.text("button.player.tournament.profile", locale),
                                    url=website_url(launch_links, "tournaments", selected.slug),
                                ),
                            ),
                        )
                    )
                    if launch_links
                    else None,
                ),
            )
            return
        if player_action == "player.tournament.create_lobby":
            await backend.create_lobby(telegram_update_claim, tournament_id=selected.id)
            updated = await backend.navigation(telegram_update_claim)
            from sitg_bot.bot.handlers.lobby import show_lobby

            await show_lobby(
                message,
                backend,
                telegram_update_claim,
                updated,
                localization,
                locale,
                launch_links,
                created=True,
            )
            return
        if launch_links is None:
            await send_message_model(
                message,
                MessageModel(localization.text("error.capability_unavailable", locale)),
            )
            return
        if player_action != "player.tournament.leaders":
            return
        url = website_url(launch_links, "tournaments", selected.slug, "leaders")
        await send_message_model(
            message,
            MessageModel(
                localization.text("link.open_prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text(f"button.{player_action}", locale), url=url
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    if player_action == "player.other":
        await send_message_model(message, other_menu_message(navigation, localization, locale))
        return
    if player_action == "player.settings":
        await send_message_model(
            message,
            settings_message(await backend.settings(telegram_update_claim), localization, locale),
        )
        return
    if player_action == "player.setting.set":
        settings = await backend.settings(telegram_update_claim)
        await state.set_state(SettingEditState.selecting)
        await send_message_model(
            message,
            MessageModel(
                localization.text("settings.choose", locale),
                setting_names_keyboard(settings, localization, locale),
            ),
        )
        return
    if player_action == "player.profile":
        if launch_links is None:
            await send_message_model(
                message,
                MessageModel(
                    localization.text(
                        "feature.placeholder",
                        locale,
                        feature=localization.text("button.player.profile", locale),
                    )
                ),
            )
            return
        url = mini_app_route_url(
            launch_links, f"players/{navigation.account.player_id}"
        )
        await send_message_model(
            message,
            MessageModel(
                localization.text("miniapp.players.prompt", locale),
                InlineKeyboardModel(
                    rows=(
                        (
                            InlineButtonModel(
                                localization.text("miniapp.players.open", locale),
                                web_app_url=url,
                            ),
                        ),
                    )
                ),
            ),
        )
        return
    if player_action in {"player.tournaments", "player.library"}:
        if launch_links is None:
            await send_message_model(
                message,
                MessageModel(
                    localization.text(
                        "feature.placeholder",
                        locale,
                        feature=localization.text(f"button.{player_action}", locale),
                    )
                ),
            )
            return
        route = "library" if player_action == "player.library" else "tournaments"
        query = {"role": "player"} if route == "tournaments" else None
        url = mini_app_route_url(launch_links, route, query=query)
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
    if player_action in PLACEHOLDER_ACTIONS:
        await send_message_model(
            message,
            MessageModel(
                localization.text(
                    "feature.placeholder",
                    locale,
                    feature=localization.text(f"button.{player_action}", locale),
                )
            ),
        )


@router.message(Command("bug"))
async def handle_bug_report(
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
        await send_message_model(
            message, MessageModel(localization.text("registration.start_required", locale))
        )
        return
    commentary = (message.text or "").partition(" ")[2].strip()
    if not commentary:
        await state.set_state(BugReportState.entering_commentary)
        await send_message_model(
            message, MessageModel(localization.text("bug.prompt", locale))
        )
        return
    await _submit_bug_report(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        commentary=commentary,
        state=state,
    )


@router.message(StateFilter(BugReportState.entering_commentary), F.text)
async def handle_bug_report_commentary(
    message: Message,
    backend: BotBackend,
    telegram_update_claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    state: FSMContext,
) -> None:
    await _submit_bug_report(
        message,
        backend=backend,
        claim=telegram_update_claim,
        localization=localization,
        locale=locale,
        commentary=(message.text or "").strip(),
        state=state,
    )


async def _submit_bug_report(
    message: Message,
    *,
    backend: BotBackend,
    claim: TelegramUpdateClaim,
    localization: LocalizationService,
    locale: str,
    commentary: str,
    state: FSMContext,
) -> None:
    await state.clear()
    if not commentary:
        await send_message_model(message, MessageModel(localization.text("bug.empty", locale)))
        return
    try:
        await backend.submit_bug_report(claim, commentary=commentary)
    except GatewayCallError as error:
        if error.error.code == ErrorCode.VALIDATION_FAILED:
            await send_message_model(
                message, MessageModel(localization.text("bug.commentary.invalid", locale))
            )
            return
        raise
    await send_message_model(message, MessageModel(localization.text("bug.received", locale)))
