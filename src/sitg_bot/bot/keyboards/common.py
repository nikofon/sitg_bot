import logging

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    ReplyKeyboardModel,
)
from sitg_bot.bot.state.models import NavigationState, SettingsState

LOGGER = logging.getLogger(__name__)

PLAYER_MENU_ACTIONS = (
    ("player.tournaments",),
    ("lobby.reopen",),
    ("player.profile", "player.library"),
    ("player.ongoing", "notifications"),
    ("player.other",),
    ("mode.switch",),
)

OTHER_MENU_ACTIONS = (
    ("player.rating", "player.history"),
    ("player.settings",),
    ("player.setting.set",),
    ("player.author_link",),
)

MANAGER_MENU_ACTIONS = (
    ("manager.tournament.select",),
    ("manager.token.request", "manager.tokens"),
    ("manager.appeals", "manager.tournament.create"),
    ("notifications",),
    ("mode.switch",),
)

PLAYER_TOURNAMENT_ACTIONS = (
    ("player.tournament.create_lobby",),
    ("player.tournament.info",),
    ("player.tournament.leaders",),
    ("lobby.reopen",),
    ("player.tournament.quit",),
)

LOBBY_ACTIONS = (
    ("lobby.ready", "lobby.unready"),
    ("lobby.start", "lobby.search", "lobby.search_cancel"),
    ("lobby.players", "lobby.leave"),
    ("back", "lobby.other"),
)
LOBBY_OTHER_ACTIONS = (
    ("lobby.packets",),
    ("lobby.invite",),
    ("lobby.options",),
    ("back",),
)

MANAGER_TOURNAMENT_ACTIONS = (
    ("manager.tournament.management",),
    ("manager.tournament.packet_upload",),
    ("manager.tournament.settings",),
    ("manager.tournament.profile",),
    ("manager.tournament.quit",),
)

ADMIN_MENU_ACTIONS = (
    ("admin.token_requests.pending",),
    ("admin.suspicion_ledger",),
    ("admin.ban", "admin.unban"),
    ("notifications",),
    ("mode.switch",),
)


def registration_consent_keyboard(
    localization: LocalizationService, locale: str
) -> ReplyKeyboardModel:
    return ReplyKeyboardModel(
        rows=(
            (
                localization.text("button.yes", locale),
                localization.text("button.no", locale),
            ),
        ),
        one_time=True,
    )


def language_keyboard(localization: LocalizationService) -> InlineKeyboardModel:
    return InlineKeyboardModel(
        rows=(
            (
                InlineButtonModel(
                    localization.text("button.russian", "ru"), callback_data="language:ru"
                ),
                InlineButtonModel(
                    localization.text("button.english", "en"), callback_data="language:en"
                ),
            ),
        )
    )


def navigation_keyboard(
    navigation: NavigationState,
    localization: LocalizationService,
    locale: str,
) -> ReplyKeyboardModel:
    layouts = {
        "player": PLAYER_MENU_ACTIONS,
        "manager": MANAGER_MENU_ACTIONS,
        "admin": ADMIN_MENU_ACTIONS,
    }
    layout = (
        PLAYER_TOURNAMENT_ACTIONS
        if navigation.active_mode == "player" and navigation.context == "tournament"
        else (
            MANAGER_TOURNAMENT_ACTIONS
            if navigation.active_mode == "manager" and navigation.context == "tournament"
            else layouts.get(navigation.active_mode, (("mode.switch",),))
        )
    )
    if navigation.active_mode == "player" and navigation.context in {"lobby", "lobby_other"}:
        layout = LOBBY_OTHER_ACTIONS if navigation.context == "lobby_other" else LOBBY_ACTIONS
    if navigation.context == "game":
        return ReplyKeyboardModel(rows=(("+",), ("||", "!")), persistent=True)
    allowed = set(navigation.allowed_actions)
    if navigation.active_mode == "player" and navigation.context == "tournament":
        selected = navigation.selected_player_tournament
        known = {action for row in PLAYER_TOURNAMENT_ACTIONS for action in row}
        if selected is not None:
            for descriptor in selected.capability_actions:
                if descriptor.key not in known:
                    LOGGER.warning(
                        "Ignoring unknown tournament action descriptor key=%s version=%s",
                        descriptor.key,
                        descriptor.descriptor_version,
                    )
    rows = tuple(
        tuple(localization.text(f"button.{action}", locale) for action in row if action in allowed)
        for row in layout
    )
    return ReplyKeyboardModel(rows=tuple(row for row in rows if row))


def mode_keyboard(
    navigation: NavigationState,
    localization: LocalizationService,
    locale: str,
) -> InlineKeyboardModel:
    return InlineKeyboardModel(
        rows=tuple(
            (
                InlineButtonModel(
                    localization.text(f"mode.{mode}", locale),
                    callback_data=f"mode:set:{mode}",
                ),
            )
            for mode in navigation.available_modes
            if mode != navigation.active_mode
        )
    )


def other_menu_keyboard(
    navigation: NavigationState,
    localization: LocalizationService,
    locale: str,
) -> ReplyKeyboardModel:
    allowed = set(navigation.allowed_actions)
    rows = tuple(
        tuple(localization.text(f"button.{action}", locale) for action in row if action in allowed)
        for row in OTHER_MENU_ACTIONS
    )
    return ReplyKeyboardModel(
        rows=(*tuple(row for row in rows if row), (localization.text("button.back", locale),))
    )


def setting_names_keyboard(
    settings: SettingsState,
    localization: LocalizationService,
    locale: str,
) -> ReplyKeyboardModel:
    labels = tuple(setting.labels.get(locale, setting.key) for setting in settings.settings)
    return ReplyKeyboardModel(
        rows=(*tuple((label,) for label in labels), (localization.text("button.back", locale),)),
        one_time=True,
    )


def setting_choices_keyboard(
    labels: tuple[str, ...], localization: LocalizationService, locale: str
) -> ReplyKeyboardModel:
    choice_rows = (labels,) if labels else ()
    return ReplyKeyboardModel(
        rows=(*choice_rows, (localization.text("button.back", locale),)),
        one_time=True,
    )


def token_request_keyboard(localization: LocalizationService, locale: str) -> InlineKeyboardModel:
    return InlineKeyboardModel(
        rows=(
            (
                InlineButtonModel(
                    localization.text("button.yes", locale),
                    callback_data="manager:token_request:yes",
                ),
                InlineButtonModel(
                    localization.text("button.no", locale),
                    callback_data="manager:token_request:no",
                ),
            ),
        )
    )


def optional_commentary_keyboard(
    localization: LocalizationService, locale: str
) -> ReplyKeyboardModel:
    return ReplyKeyboardModel(
        rows=(
            (localization.text("button.skip", locale),),
            (localization.text("button.back", locale),),
        ),
        one_time=True,
    )


def suggested_value_keyboard(
    suggested: str, localization: LocalizationService, locale: str
) -> ReplyKeyboardModel:
    return ReplyKeyboardModel(
        rows=((suggested,), (localization.text("button.back", locale),)), one_time=True
    )


TOURNAMENT_LANGUAGE_OPTIONS = (
    ("en", "button.english"),
    ("ru", "button.russian"),
)


def tournament_language_keyboard(
    localization: LocalizationService, locale: str
) -> ReplyKeyboardModel:
    return ReplyKeyboardModel(
        rows=(
            tuple(
                localization.text(label_key, locale) for _, label_key in TOURNAMENT_LANGUAGE_OPTIONS
            ),
            (localization.text("button.other_language", locale),),
            (localization.text("button.back", locale),),
        ),
        one_time=True,
    )


def tournament_creation_confirmation_keyboard(
    localization: LocalizationService, locale: str
) -> InlineKeyboardModel:
    return InlineKeyboardModel(
        rows=(
            (
                InlineButtonModel(
                    localization.text("button.yes", locale),
                    callback_data="manager:tournament:create:yes",
                ),
                InlineButtonModel(
                    localization.text("button.no", locale),
                    callback_data="manager:tournament:create:no",
                ),
            ),
        )
    )
