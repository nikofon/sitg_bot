from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import (
    language_keyboard,
    mode_keyboard,
    navigation_keyboard,
    registration_consent_keyboard,
)
from sitg_bot.bot.presenters.models import MessageModel, RemoveKeyboardModel
from sitg_bot.bot.state.models import NavigationState, RegistrationState


def registration_prompt(
    draft: RegistrationState,
    localization: LocalizationService,
    locale: str,
    *,
    guidance_key: str | None = None,
) -> MessageModel:
    if draft.next_step is None:
        raise ValueError("Registration has no incomplete step")
    key = f"registration.{draft.next_step}.prompt"
    text = localization.text(key, locale)
    if draft.next_step == "real_name":
        text = f"{localization.text('registration.sensitive_warning', locale)}\n\n{text}"
    if guidance_key is not None:
        text = f"{localization.text(guidance_key, locale)}\n\n{text}"
    keyboard = (
        registration_consent_keyboard(localization, locale)
        if draft.next_step == "telegram_public"
        else RemoveKeyboardModel()
    )
    return MessageModel(text, keyboard)


def menu_message(
    navigation: NavigationState, localization: LocalizationService, locale: str
) -> MessageModel:
    if navigation.context == "unavailable":
        return MessageModel(localization.text("account.unavailable", locale))
    if navigation.active_game is not None:
        finished = navigation.active_game.status not in {"active", "lobby"}
        return MessageModel(
            localization.text("flow.quit_hint" if finished else "game.controls", locale),
            RemoveKeyboardModel()
            if finished
            else navigation_keyboard(navigation, localization, locale),
        )
    selected = (
        navigation.selected_manager_tournament
        if navigation.active_mode == "manager"
        else navigation.selected_player_tournament
    )
    if navigation.context == "chat" and navigation.active_chat is not None:
        return MessageModel(
            localization.text(
                "tournament_chat.open_menu",
                locale,
                tournament=navigation.active_chat.tournament_name,
            ),
            RemoveKeyboardModel(),
        )
    if navigation.context in {"lobby", "lobby_other"}:
        return MessageModel(
            localization.text("lobby.menu", locale),
            navigation_keyboard(navigation, localization, locale),
        )
    if navigation.context == "tournament" and selected is not None:
        return MessageModel(
            localization.text("navigation.tournament", locale, tournament=selected.name),
            navigation_keyboard(navigation, localization, locale),
        )
    return MessageModel(
        localization.text(f"menu.{navigation.active_mode}", locale),
        navigation_keyboard(navigation, localization, locale),
    )


def help_message(
    navigation: NavigationState | None, localization: LocalizationService, locale: str
) -> MessageModel:
    registering = navigation is None or navigation.context == "registration"
    if navigation is not None and navigation.context == "unavailable":
        return MessageModel(localization.text("help.unavailable", locale))
    text = localization.text("help.registration" if registering else "help.active", locale)
    if not registering and (navigation.active_lobby or navigation.active_game):
        text += "\n\n" + localization.text("chat.help", locale)
    return MessageModel(text)


def language_message(localization: LocalizationService, locale: str) -> MessageModel:
    return MessageModel(
        localization.text("language.prompt", locale), language_keyboard(localization)
    )


def mode_message(
    navigation: NavigationState, localization: LocalizationService, locale: str
) -> MessageModel:
    return MessageModel(
        localization.text("mode.prompt", locale),
        mode_keyboard(navigation, localization, locale),
    )
