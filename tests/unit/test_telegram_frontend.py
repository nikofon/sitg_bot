from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.dispatcher.event.bases import CancelHandler
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from sitg_bot.application.contracts import ActionCode, GatewayRequest, GatewayResponse
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.bot.app import BotDependencies, NullTelemetry, create_dispatcher
from sitg_bot.bot.callbacks import CallbackReferenceStore
from sitg_bot.bot.handlers.admin import (
    TOKEN_DECISION_SCOPE,
    handle_admin_authentication_credential,
    handle_admin_authentication_start,
    pending_token_requests_message,
    token_decision_confirmation,
    token_decision_receipt,
)
from sitg_bot.bot.handlers.common import resume_registration
from sitg_bot.bot.handlers.manager import (
    _creation_prompt,
    delivered_token_message,
    handle_manager_tournament_action,
    handle_token_request_commentary,
    handle_tournament_ruleset,
    handle_tournament_type,
    handle_tournament_visibility,
    token_inventory_message,
    token_request_result,
)
from sitg_bot.bot.handlers.player import (
    _setting_value,
    handle_player_menu_action,
    handle_setting_selection,
    handle_setting_value,
    notification_text,
    other_menu_message,
    settings_message,
)
from sitg_bot.bot.handlers.registration import parse_consent
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.i18n.catalog import CatalogError
from sitg_bot.bot.keyboards.common import tournament_language_keyboard
from sitg_bot.bot.middleware.authentication import TelegramDeduplicationMiddleware
from sitg_bot.bot.presenters.common import menu_message, mode_message, registration_prompt
from sitg_bot.bot.presenters.models import (
    InlineKeyboardModel,
    ReplyKeyboardModel,
)
from sitg_bot.bot.state import GatewayCallError
from sitg_bot.bot.state.models import (
    DeliveredCreationTokenState,
    NavigationState,
    RegistrationState,
    SettingsState,
    TokenInventoryState,
    TokenRequestPageState,
    TokenRequestState,
)
from sitg_bot.bot.state.settings import TournamentCreationState
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.telegram_auth import DuplicateTelegramUpdate
from sitg_bot.storage.models import PlayerTelegramNavigationRecord


def registration(*, step: str, locale: str = "ru", version: int = 2) -> RegistrationState:
    completed = ("real_name", "nickname", "telegram_public")
    position = completed.index(step) if step in completed else len(completed)
    return RegistrationState.model_validate(
        {
            "account": {
                "player_id": str(UUID(int=1)),
                "telegram_user_id": 42,
                "public_nickname": None,
                "preferred_locale": locale,
                "registration_status": "registration",
                "registration_completed_at": None,
                "profile_version": version,
            },
            "next_step": step,
            "completed_steps": completed[:position],
        }
    )


def navigation(**changes: object) -> NavigationState:
    values: dict[str, object] = {
        "account": {
            "player_id": str(UUID(int=1)),
            "telegram_user_id": 42,
            "public_nickname": "Player",
            "preferred_locale": "en",
            "registration_status": "active",
            "registration_completed_at": "2026-09-02T12:00:00+00:00",
            "profile_version": 3,
        },
        "available_modes": ["player"],
        "active_mode": "player",
        "context": "menu",
        "navigation_version": 0,
        "selected_player_tournament": None,
        "selected_manager_tournament": None,
        "active_lobby": None,
        "active_game": None,
        "allowed_actions": ["start", "menu", "help"],
    }
    values.update(changes)
    return NavigationState.model_validate(values)


def settings() -> SettingsState:
    return SettingsState.model_validate(
        {
            "profile_version": 4,
            "updated_at": "2026-09-03T10:15:00+00:00",
            "settings": [
                {
                    "key": "real_name",
                    "value_type": "text",
                    "labels": {"ru": "Настоящее имя", "en": "Real name"},
                    "value": "<Private Name>",
                    "localized_value": {
                        "ru": "<Private Name>",
                        "en": "<Private Name>",
                    },
                },
                {
                    "key": "telegram_public",
                    "value_type": "boolean",
                    "labels": {"ru": "Публичный Telegram", "en": "Public Telegram"},
                    "value": False,
                    "localized_value": {"ru": "Нет", "en": "No"},
                    "choices": [
                        {"value": False, "labels": {"ru": "Нет", "en": "No"}},
                        {"value": True, "labels": {"ru": "Да", "en": "Yes"}},
                    ],
                },
            ],
        }
    )


def token_inventory() -> TokenInventoryState:
    return TokenInventoryState.model_validate(
        {
            "items": [
                {
                    "source": "requested",
                    "request_id": str(UUID(int=20)),
                    "request_status": "approved",
                    "requested_at": "2026-09-03T09:00:00+00:00",
                    "tournament_name": "Autumn Open",
                    "decision_note": "Looks good",
                    "token_id": str(UUID(int=21)),
                    "fingerprint": "abcdef123456",
                    "token_status": "issued",
                    "issued_at": "2026-09-03T10:00:00+00:00",
                    "expires_at": "2026-09-04T10:00:00+00:00",
                    "used_at": None,
                    "tournament_id": None,
                    "revoked_at": None,
                    "delivery": {
                        "status": "pending",
                        "delivered_at": None,
                        "failed_at": None,
                        "failure_reason": None,
                    },
                }
            ],
            "next_cursor": None,
        }
    )


def token_request() -> TokenRequestState:
    return TokenRequestState.model_validate(
        {
            "request_id": str(UUID(int=20)),
            "requester_id": str(UUID(int=1)),
            "requester_nickname": "Manager <One>",
            "requester_real_name": "Manager One",
            "requester_telegram_username": "manager_one",
            "requester_telegram_user_id": 42,
            "tournament_name": "Autumn Open",
            "status": "pending",
            "justification": "Original packet",
            "decision_note": None,
            "decided_by_id": None,
            "created_at": "2026-09-03T09:00:00+00:00",
            "decided_at": None,
            "token_id": None,
            "token_fingerprint": None,
            "token_expires_at": None,
            "delivery": None,
        }
    )


def test_catalogs_are_complete_and_escape_user_controlled_html() -> None:
    localization = LocalizationService()

    assert set(localization.catalogs["ru"]) == set(localization.catalogs["en"])
    rendered = localization.text(
        "navigation.tournament", "en", tournament="<script>alert(1)</script>"
    )

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_catalog_validation_rejects_missing_keys_and_invalid_markup() -> None:
    with pytest.raises(CatalogError, match="key mismatch"):
        LocalizationService({"ru": {"one": "Один"}, "en": {}})
    with pytest.raises(CatalogError, match="Unsupported translated HTML"):
        LocalizationService({"ru": {"one": "<div>Один</div>"}, "en": {"one": "<div>One</div>"}})


@pytest.mark.parametrize("step", ("real_name", "nickname", "telegram_public"))
def test_every_registration_step_has_a_localized_presenter(step: str) -> None:
    localization = LocalizationService()

    for locale in ("ru", "en"):
        model = registration_prompt(registration(step=step, locale=locale), localization, locale)
        expected = localization.text(f"registration.{step}.prompt", locale)
        if step == "real_name":
            expected = (
                localization.text("registration.sensitive_warning", locale) + "\n\n" + expected
            )
        assert model.text == expected
        if step == "telegram_public":
            assert isinstance(model.keyboard, ReplyKeyboardModel)


def test_registration_consent_accepts_explicit_answers_in_both_locales() -> None:
    localization = LocalizationService()

    assert parse_consent(" дА ", localization) is True
    assert parse_consent("YES", localization) is True
    assert parse_consent("Нет", localization) is False
    assert parse_consent("Default", localization) is None
    assert parse_consent("maybe", localization) is None


def test_game_keyboard_overrides_saved_manager_context() -> None:
    state = navigation(
        available_modes=["player", "manager"],
        active_mode="player",
        context="game",
        active_game={
            "id": str(UUID(int=4)),
            "tournament_id": str(UUID(int=5)),
            "status": "active",
            "phase": "question",
            "version": 8,
            "joined": True,
            "active": True,
            "can_reconnect": False,
        },
    )

    model = menu_message(state, LocalizationService(), "en")

    assert model.keyboard.rows == (("+",), ("||", "!"))
    assert "buzz" in model.text.lower()


def test_player_menu_keyboard_contains_only_authorized_snapshot_actions() -> None:
    state = navigation(
        available_modes=["player", "manager"],
        allowed_actions=[
            "player.menu",
            "player.settings",
            "player.history",
            "player.other",
            "mode.switch",
        ],
    )

    model = menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    labels = {label for row in model.keyboard.rows for label in row}
    assert labels == {"Other...", "Switch mode"}
    assert "Settings" not in labels
    assert "Library" not in labels


def test_player_menu_exposes_notifications_when_authorized() -> None:
    state = navigation(allowed_actions=["player.menu", "notifications", "player.other"])

    model = menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    assert tuple(label for row in model.keyboard.rows for label in row) == (
        "Notifications",
        "Other...",
    )


def test_registered_author_notification_uses_requested_copy() -> None:
    text = notification_text(
        "author.registered",
        {"author_name": "Ada Lovelace", "tournament_name": "Autumn Open"},
        LocalizationService(),
        "en",
    )

    assert text == (
        'You have been registered as author "Ada Lovelace" for tournament "Autumn Open". '
        "Consider linking your account to your author profile with Other -> Link author option. "
        "If you think there has been a mistake, contact tournament's manager or admin."
    )


def test_other_menu_groups_profile_actions_and_back_navigation() -> None:
    state = navigation(
        allowed_actions=[
            "player.menu",
            "player.settings",
            "player.setting.set",
            "player.author_link",
            "player.rating",
            "player.history",
            "player.other",
        ]
    )

    model = other_menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    assert tuple(label for row in model.keyboard.rows for label in row) == (
        "My rating",
        "History",
        "Settings",
        "Set setting",
        "Link to author",
        "Back",
    )


def test_mode_selector_localizes_only_authorized_alternatives() -> None:
    state = navigation(
        available_modes=["player", "manager", "admin"],
        allowed_actions=["mode.switch", "player.menu"],
    )

    model = mode_message(state, LocalizationService(), "ru")

    assert isinstance(model.keyboard, InlineKeyboardModel)
    assert tuple(button.text for row in model.keyboard.rows for button in row) == (
        "Организатор",
        "Администратор",
    )
    assert tuple(button.callback_data for row in model.keyboard.rows for button in row) == (
        "mode:set:manager",
        "mode:set:admin",
    )


def test_manager_menu_keyboard_is_derived_from_allowed_actions() -> None:
    state = navigation(
        available_modes=["player", "manager"],
        active_mode="manager",
        allowed_actions=[
            "manager.menu",
            "manager.tournament.select",
            "manager.token.request",
            "manager.tokens",
            "manager.appeals",
            "manager.tournament.create",
            "notifications",
            "mode.switch",
        ],
    )

    model = menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    assert tuple(label for row in model.keyboard.rows for label in row) == (
        "Select tournament",
        "Request tournament token",
        "My tokens",
        "Appeals",
        "Create tournament",
        "Notifications",
        "Switch mode",
    )


def test_manager_tournament_context_renders_settings_profile_and_quit() -> None:
    state = navigation(
        available_modes=["player", "manager"],
        active_mode="manager",
        context="tournament",
        selected_manager_tournament={
            "id": str(UUID(int=8)),
            "name": "Managed Cup",
            "slug": "managed-cup",
            "status": "active",
        },
        allowed_actions=[
            "manager.tournament",
            "manager.tournament.settings",
            "manager.tournament.profile",
            "notifications",
            "manager.tournament.quit",
        ],
    )

    model = menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    assert tuple(label for row in model.keyboard.rows for label in row) == (
        "Settings",
        "Tournament profile",
        "Quit to menu",
    )


def test_player_tournament_context_is_capability_derived() -> None:
    state = navigation(
        context="tournament",
        selected_player_tournament={
            "id": str(UUID(int=8)),
            "name": "Player Cup",
            "slug": "player-cup",
            "status": "active",
        },
        allowed_actions=[
            "player.tournament.create_lobby",
            "player.tournament.info",
            "notifications",
            "player.tournament.quit",
        ],
    )

    model = menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    assert tuple(label for row in model.keyboard.rows for label in row) == (
        "Create lobby",
        "Info",
        "Quit to menu",
    )


async def test_player_lobby_creation_uses_the_selected_tournament() -> None:
    created = SimpleNamespace(
        value="opaque-lobby",
        telegram_payload="lr_opaque-lobby",
    )
    lobby_nav = navigation(
        context="lobby",
        active_lobby={
            "id": str(UUID(int=9)),
            "tournament_id": str(UUID(int=8)),
            "version": 1,
            "status": "assembling",
        },
        allowed_actions=["lobby.ready", "lobby.players", "lobby.leave", "back", "lobby.other"],
    )
    backend = SimpleNamespace(
        create_lobby=AsyncMock(return_value=created),
        navigation=AsyncMock(return_value=lobby_nav),
        lobby_info=AsyncMock(
            return_value={
                "tournament_name": "Player Cup",
                "members": [{"role": "player"}],
                "max_players": 4,
                "expires_at": "2099-01-01",
                "invitation_code": "a" * 32,
            }
        ),
    )
    message = SimpleNamespace(
        answer=AsyncMock(),
        bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot"))),
    )
    claim = SimpleNamespace()
    selected = {
        "id": str(UUID(int=8)),
        "name": "Player Cup",
        "slug": "player-cup",
        "status": "active",
    }

    await handle_player_menu_action(
        message,  # type: ignore[arg-type]
        player_action="player.tournament.create_lobby",
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=claim,  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(
            context="tournament",
            selected_player_tournament=selected,
            allowed_actions=["player.tournament.create_lobby"],
        ),
        state=SimpleNamespace(),  # type: ignore[arg-type]
        launch_links="https://mini.example.test/app",
    )

    backend.create_lobby.assert_awaited_once_with(claim, tournament_id=UUID(int=8))
    assert message.answer.await_count == 1
    text = message.answer.await_args.args[0]
    assert "Player Cup" in text and "1/4" in text and "https://t.me/test_bot?start=join_" in text
    labels = [
        button.text
        for row in message.answer.await_args.kwargs["reply_markup"].keyboard
        for button in row
    ]
    assert "Ready" in labels and "Players" in labels and "Back" in labels


async def test_manager_settings_action_uses_an_actor_bound_launch_reference() -> None:
    backend = SimpleNamespace(
        tournament_settings_link=AsyncMock(
            return_value=SimpleNamespace(
                value="opaque-reference",
                telegram_payload="lr_opaque-reference",
            )
        )
    )
    message = SimpleNamespace(answer=AsyncMock())
    claim = SimpleNamespace()

    await handle_manager_tournament_action(
        message,  # type: ignore[arg-type]
        manager_tournament_action="manager.tournament.settings",
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=claim,  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(
            available_modes=["player", "manager"],
            active_mode="manager",
            context="tournament",
            selected_manager_tournament={
                "id": str(UUID(int=8)),
                "name": "Managed Cup",
                "slug": "managed-cup",
                "status": "active",
            },
            allowed_actions=["manager.tournament.settings"],
        ),
        launch_links="https://mini.example.test/app",
    )

    backend.tournament_settings_link.assert_awaited_once_with(claim)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].web_app.url == (
        "https://mini.example.test/app/manager/tournaments/opaque-reference/settings"
        "?tgWebAppStartParam=lr_opaque-reference"
    )


async def test_manager_management_action_uses_an_actor_bound_launch_reference() -> None:
    backend = SimpleNamespace(
        tournament_management_link=AsyncMock(
            return_value=SimpleNamespace(
                value="opaque-reference",
                telegram_payload="lr_opaque-reference",
            )
        )
    )
    message = SimpleNamespace(answer=AsyncMock())
    claim = SimpleNamespace()

    await handle_manager_tournament_action(
        message,  # type: ignore[arg-type]
        manager_tournament_action="manager.tournament.management",
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=claim,  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(
            available_modes=["player", "manager"],
            active_mode="manager",
            context="tournament",
            selected_manager_tournament={
                "id": str(UUID(int=8)),
                "name": "Managed Cup",
                "slug": "managed-cup",
                "status": "active",
            },
            allowed_actions=["manager.tournament.management"],
        ),
        launch_links="https://mini.example.test/app",
    )

    backend.tournament_management_link.assert_awaited_once_with(claim)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].web_app.url == (
        "https://mini.example.test/app/manager/tournaments/opaque-reference/management"
        "?tgWebAppStartParam=lr_opaque-reference"
    )


def test_manager_token_presenters_show_metadata_and_protect_one_time_secret() -> None:
    localization = LocalizationService()
    inventory_model = token_inventory_message(token_inventory(), localization, "en")
    request_model = token_request_result(
        TokenRequestState.model_validate(
            {
                "request_id": str(UUID(int=20)),
                "requester_id": str(UUID(int=1)),
                "requester_nickname": "Manager",
                "status": "pending",
                "justification": None,
                "decision_note": None,
                "decided_by_id": None,
                "created_at": "2026-09-03T09:00:00+00:00",
                "decided_at": None,
                "token_id": None,
                "token_fingerprint": None,
                "token_expires_at": None,
                "delivery": None,
            }
        ),
        localization,
        "en",
    )
    delivered_model = delivered_token_message(
        DeliveredCreationTokenState.model_validate(
            {
                "token_id": str(UUID(int=21)),
                "token": "<one-time-secret>",
                "fingerprint": "abcdef123456",
                "expires_at": "2026-09-04T10:00:00+00:00",
            }
        ),
        localization,
        "en",
    )

    assert "approved" in inventory_model.text
    assert "Autumn Open" in inventory_model.text
    assert "Looks good" in inventory_model.text
    assert "abcdef123456" in inventory_model.text
    assert isinstance(inventory_model.keyboard, InlineKeyboardModel)
    assert inventory_model.keyboard.rows[0][0].callback_data == (
        f"manager:token_claim:{UUID(int=20)}"
    )
    assert "pending" in request_model.text
    assert delivered_model.protect_content is True
    assert "&lt;one-time-secret&gt;" in delivered_model.text
    assert "<one-time-secret>" not in delivered_model.text


async def test_token_request_commentary_is_submitted_or_skipped() -> None:
    request = token_request()
    backend = SimpleNamespace(request_tournament_token=AsyncMock(return_value=request))
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"tournament_name": "Autumn Open"}),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(text="Skip", answer=AsyncMock())
    claim = SimpleNamespace()

    await handle_token_request_commentary(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=claim,  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(
            available_modes=["player", "manager"],
            active_mode="manager",
            allowed_actions=["manager.menu", "manager.token.request"],
        ),
        state=state,  # type: ignore[arg-type]
    )

    backend.request_tournament_token.assert_awaited_once_with(
        claim,
        tournament_name="Autumn Open",
        commentary=None,
    )
    state.clear.assert_awaited_once()


async def test_back_from_token_commentary_cancels_workflow_and_restores_menu() -> None:
    backend = SimpleNamespace(request_tournament_token=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(text="Back", answer=AsyncMock())

    await handle_token_request_commentary(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(
            available_modes=["player", "manager"],
            active_mode="manager",
            allowed_actions=["manager.menu", "manager.token.request"],
        ),
        state=state,  # type: ignore[arg-type]
    )

    state.clear.assert_awaited_once()
    backend.request_tournament_token.assert_not_awaited()
    assert "Manager" in message.answer.await_args.args[0]


async def test_back_in_creation_wizard_returns_to_previous_step() -> None:
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"game_ruleset_key": "si"}),
        set_state=AsyncMock(),
    )
    message = SimpleNamespace(text="Back", answer=AsyncMock())

    await handle_tournament_visibility(
        message,  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        state=state,  # type: ignore[arg-type]
    )

    state.set_state.assert_awaited_once_with(TournamentCreationState.entering_ruleset)
    assert "ruleset" in message.answer.await_args.args[0].lower()


@pytest.mark.parametrize("value", ["Ladder", "Classical", "classic"])
async def test_creation_type_skips_dates_and_back_returns_to_type(value: str) -> None:
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"type_key": "classic"}),
        set_state=AsyncMock(), update_data=AsyncMock(),
    )
    message = SimpleNamespace(text=value, answer=AsyncMock())
    localization = LocalizationService()
    await handle_tournament_type(message, localization, "en", state)
    state.update_data.assert_awaited_once_with(
        type_key="ladder" if value == "Ladder" else "classic"
    )
    state.set_state.assert_awaited_once_with(TournamentCreationState.entering_ruleset)
    message.text = "Back"
    await handle_tournament_ruleset(message, localization, "en", state)
    state.set_state.assert_awaited_with(TournamentCreationState.entering_type)


@pytest.mark.parametrize("locale", ["en", "ru"])
def test_creation_keyboard_offers_types_and_visibilities(locale: str) -> None:
    localization = LocalizationService()
    for field, choices in [
        ("type", ("Ladder", "Classical")), ("visibility", ("Private", "Public"))
    ]:
        model = _creation_prompt(field, choices[0], localization, locale)
        assert isinstance(model.keyboard, ReplyKeyboardModel)
        assert model.keyboard.rows == (choices, (localization.text("button.back", locale),))


def test_tournament_language_choices_are_localized_and_extensible() -> None:
    localization = LocalizationService()

    english = tournament_language_keyboard(localization, "en")
    russian = tournament_language_keyboard(localization, "ru")

    assert tuple(label for row in english.rows for label in row) == (
        "English",
        "Русский",
        "Other",
        "Back",
    )
    assert tuple(label for row in russian.rows for label in row) == (
        "English",
        "Русский",
        "Другой",
        "Назад",
    )


@pytest.mark.parametrize("locale", ["en", "ru"])
def test_tournament_start_reminder_is_localized_and_escapes_name(locale):
    text = notification_text(
        "tournament.start_due", {"name": "Cup <test>"}, LocalizationService(), locale,
    )
    assert "Cup &lt;test&gt;" in text
    assert "<test>" not in text


def test_admin_menu_keyboard_is_derived_from_allowed_actions() -> None:
    state = navigation(
        available_modes=["player", "manager", "admin"],
        active_mode="admin",
        allowed_actions=[
            "admin.menu",
            "admin.token_requests.pending",
            "admin.suspicion_ledger",
            "admin.ban",
            "admin.unban",
            "notifications",
            "mode.switch",
        ],
    )

    model = menu_message(state, LocalizationService(), "en")

    assert isinstance(model.keyboard, ReplyKeyboardModel)
    assert tuple(label for row in model.keyboard.rows for label in row) == (
        "Pending token requests",
        "Suspicion ledger",
        "Ban",
        "Unban",
        "Notifications",
        "Switch mode",
    )


async def test_admin_authentication_prompts_then_deletes_credential() -> None:
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
    start_message = SimpleNamespace(answer=AsyncMock())
    start_backend = SimpleNamespace(admin_authentication_available=AsyncMock(return_value=True))

    await handle_admin_authentication_start(
        start_message,  # type: ignore[arg-type]
        backend=start_backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(),
        state=state,  # type: ignore[arg-type]
    )

    state.set_state.assert_awaited_once()
    assert "credential" in start_message.answer.await_args.args[0].lower()

    authenticated = navigation(
        available_modes=["player", "manager", "admin"],
        active_mode="admin",
        allowed_actions=["admin.menu", "mode.switch"],
    )
    backend = SimpleNamespace(
        authenticate_admin=AsyncMock(return_value=authenticated),
        pending_token_requests=AsyncMock(
            return_value=TokenRequestPageState(items=(), next_cursor=None)
        ),
        claim_notification_alerts=AsyncMock(return_value=SimpleNamespace(audiences=())),
    )
    credential_message = SimpleNamespace(
        text="private-admin-secret",
        delete=AsyncMock(),
        answer=AsyncMock(),
    )

    await handle_admin_authentication_credential(
        credential_message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        state=state,  # type: ignore[arg-type]
    )

    credential_message.delete.assert_awaited_once()
    backend.authenticate_admin.assert_awaited_once()
    assert backend.authenticate_admin.await_args.kwargs["credential"] == "private-admin-secret"
    assert "private-admin-secret" not in credential_message.answer.await_args.args[0]
    assert "Admin" in credential_message.answer.await_args.args[0]


async def test_admin_authentication_does_not_prompt_when_server_is_unconfigured() -> None:
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock())
    backend = SimpleNamespace(admin_authentication_available=AsyncMock(return_value=False))

    await handle_admin_authentication_start(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=navigation(),
        state=state,  # type: ignore[arg-type]
    )

    state.set_state.assert_not_awaited()
    assert message.answer.await_args.args[0] == (
        "Administrator authentication is not configured on the server."
    )


async def test_admin_authentication_returns_generic_failure_copy() -> None:
    failure = GatewayCallError(
        GatewayResponse(
            action=ActionCode.ADMIN_AUTHENTICATE,
            correlation_id=UUID(int=99),
            ok=False,
            error={
                "code": "forbidden",
                "message_key": "error.forbidden",
            },
        )
    )
    backend = SimpleNamespace(authenticate_admin=AsyncMock(side_effect=failure))
    message = SimpleNamespace(
        text="wrong-secret",
        delete=AsyncMock(),
        answer=AsyncMock(),
    )

    await handle_admin_authentication_credential(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        state=SimpleNamespace(clear=AsyncMock()),  # type: ignore[arg-type]
    )

    assert message.answer.await_args.args[0] == "Administrator authentication failed."
    assert "wrong-secret" not in message.answer.await_args.args[0]


def test_admin_token_queue_uses_short_lived_actor_bound_opaque_callbacks() -> None:
    now = [datetime(2026, 9, 3, 10, tzinfo=UTC)]
    references = CallbackReferenceStore(clock=lambda: now[0])
    administrator_id = UUID(int=7)
    request = token_request()
    page = TokenRequestPageState(items=(request,), next_cursor=None)

    model = pending_token_requests_message(
        page,
        references,
        administrator_id=administrator_id,
        localization=LocalizationService(),
        locale="en",
    )

    assert "Manager &lt;One&gt;" in model.text
    assert "Autumn Open" in model.text
    assert "Manager One" in model.text
    assert "@manager_one" in model.text
    assert isinstance(model.keyboard, InlineKeyboardModel)
    assert model.keyboard.rows[0][0].text == "Rule"
    callback_data = model.keyboard.rows[0][0].callback_data
    assert callback_data is not None
    assert str(request.request_id) not in callback_data
    reference = callback_data.rpartition(":")[2]
    target = references.resolve(
        reference,
        scope=TOKEN_DECISION_SCOPE,
        actor_id=administrator_id,
    )
    assert target.resource_id == request.request_id
    assert target.action == "approve"
    with pytest.raises(LookupError):
        references.resolve(
            reference,
            scope=TOKEN_DECISION_SCOPE,
            actor_id=UUID(int=8),
        )
    now[0] += timedelta(minutes=6)
    with pytest.raises(LookupError):
        references.resolve(
            reference,
            scope=TOKEN_DECISION_SCOPE,
            actor_id=administrator_id,
        )


def test_admin_token_decision_requires_confirmation_and_has_audit_receipt() -> None:
    localization = LocalizationService()
    request = token_request().model_copy(
        update={"status": "approved", "decided_at": "2026-09-03T10:05:00+00:00"}
    )
    confirmation = token_decision_confirmation(
        request.request_id,
        "approve",
        "opaque-ref",
        localization,
        "en",
    )
    receipt = token_decision_receipt(
        request,
        action_id=UUID(int=30),
        decided_at=request.decided_at or "",
        localization=localization,
        locale="en",
    )

    assert isinstance(confirmation.keyboard, InlineKeyboardModel)
    assert confirmation.keyboard.rows[0][0].callback_data == ("admin:token:final:yes:opaque-ref")
    assert "approved" in receipt.text
    assert str(UUID(int=30)) in receipt.text
    assert "2026-09-03 10:05" in receipt.text


def test_settings_presenter_escapes_values_and_parses_localized_choices() -> None:
    snapshot = settings()

    model = settings_message(snapshot, LocalizationService(), "en")
    telegram_public = next(item for item in snapshot.settings if item.key == "telegram_public")

    assert "&lt;Private Name&gt;" in model.text
    assert "<Private Name>" not in model.text
    assert "2026-09-03 10:15" in model.text
    assert _setting_value(telegram_public, " дА ") is True
    assert _setting_value(telegram_public, "NO") is False


async def test_back_from_setting_selection_returns_to_player_menu_without_mutation() -> None:
    backend = SimpleNamespace(settings=AsyncMock(), update_setting=AsyncMock())
    fsm = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(text="Back", answer=AsyncMock())
    state = navigation(
        available_modes=["player", "manager"],
        allowed_actions=["player.menu", "player.settings", "mode.switch"],
    )

    await handle_setting_selection(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="en",
        navigation=state,
        state=fsm,  # type: ignore[arg-type]
    )

    fsm.clear.assert_awaited_once()
    backend.settings.assert_not_awaited()
    backend.update_setting.assert_not_awaited()
    assert message.answer.await_args.args[0].startswith("<b>Other</b>")


async def test_back_from_setting_value_returns_to_setting_list_without_mutation() -> None:
    snapshot = settings()
    backend = SimpleNamespace(
        settings=AsyncMock(return_value=snapshot),
        update_setting=AsyncMock(),
    )
    fsm = SimpleNamespace(set_state=AsyncMock())
    message = SimpleNamespace(text="Назад", answer=AsyncMock())

    await handle_setting_value(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="ru",
        navigation=navigation(),
        state=fsm,  # type: ignore[arg-type]
    )

    backend.settings.assert_awaited_once()
    backend.update_setting.assert_not_awaited()
    assert message.answer.await_args.args[0] == "Выберите настройку:"


def test_navigation_mutations_have_stale_write_and_idempotency_guards() -> None:
    assert ACTION_POLICIES[ActionCode.NAVIGATION_MODE_SET].idempotency_required
    assert ACTION_POLICIES[ActionCode.NAVIGATION_MODE_SET].stale_write_field == "expected_version"
    assert ACTION_POLICIES[ActionCode.NAVIGATION_TOURNAMENT_SET].idempotency_required
    assert PlayerTelegramNavigationRecord.__tablename__ == "player_telegram_navigation"

    with pytest.raises(ValueError, match="changed"):
        TelegramNavigationService._require_version(SimpleNamespace(version=3), 2)


def test_navigation_contract_is_typed_and_versioned() -> None:
    request = GatewayRequest.model_validate(
        {
            "metadata": {
                "channel": "telegram_bot",
                "client_name": "test-bot",
                "client_version": "1.0.0",
                "idempotency_key": "telegram-update:1",
            },
            "operation": {
                "action": ActionCode.NAVIGATION_MODE_SET,
                "mode": "manager",
                "expected_version": 4,
            },
        }
    )

    assert request.operation.action == ActionCode.NAVIGATION_MODE_SET
    assert request.operation.expected_version == 4  # type: ignore[union-attr]


def test_router_builds_without_telegram_or_postgresql() -> None:
    dependencies = BotDependencies(
        backend=SimpleNamespace(),  # type: ignore[arg-type]
        localization=LocalizationService(),
        launch_links=None,
        clock=lambda: datetime.now(UTC),
        telemetry=NullTelemetry(),
    )

    dispatcher = create_dispatcher(
        dependencies,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert {"message", "callback_query"}.issubset(set(dispatcher.resolve_used_update_types()))


async def test_registration_start_handles_missing_telegram_username() -> None:
    backend = SimpleNamespace(
        start_registration=AsyncMock(return_value=registration(step="real_name"))
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(username=None),
        answer=AsyncMock(),
    )
    claim = SimpleNamespace()

    await resume_registration(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        localization=LocalizationService(),
        locale="ru",
    )

    backend.start_registration.assert_awaited_once_with(claim, telegram_username=None)
    message.answer.assert_awaited_once()


def telegram_update(kind: str) -> Update:
    user = User(id=42, is_bot=False, first_name="Player")
    message = Message(
        message_id=1,
        date=datetime(2026, 9, 2, tzinfo=UTC),
        chat=Chat(id=42, type="private"),
        from_user=user,
        text="value",
    )
    if kind == "message":
        return Update(update_id=10, message=message)
    return Update(
        update_id=10,
        callback_query=CallbackQuery(
            id="callback", from_user=user, chat_instance="chat", message=message
        ),
    )


@pytest.mark.parametrize("kind", ("message", "callback"))
async def test_duplicate_messages_and_callbacks_are_stopped_before_handlers(kind: str) -> None:
    auth_service = SimpleNamespace(
        claim=AsyncMock(side_effect=DuplicateTelegramUpdate("duplicate")),
        complete=AsyncMock(),
    )
    middleware = TelegramDeduplicationMiddleware(auth_service)  # type: ignore[arg-type]
    handler = AsyncMock()

    with pytest.raises(CancelHandler):
        await middleware(
            handler,
            telegram_update(kind),
            {"bot": Bot("123456:test-token")},
        )

    handler.assert_not_awaited()
    auth_service.complete.assert_not_awaited()


async def test_lobby_back_changes_context_without_leaving_membership() -> None:
    from sitg_bot.bot.handlers.lobby import handle_lobby_action

    current = navigation(
        context="lobby",
        active_lobby={
            "id": str(UUID(int=9)),
            "tournament_id": str(UUID(int=8)),
            "version": 1,
            "status": "assembling",
        },
        allowed_actions=["back"],
    )
    updated = current.model_copy(update={"context": "tournament"})
    backend = SimpleNamespace(
        lobby_context=AsyncMock(return_value=updated), lobby_action=AsyncMock()
    )
    message = SimpleNamespace(answer=AsyncMock())
    claim = SimpleNamespace()
    await handle_lobby_action(
        message,
        "back",
        backend,
        claim,
        LocalizationService(),
        "en",
        current,
        SimpleNamespace(clear=AsyncMock()),
        None,
    )
    backend.lobby_context.assert_awaited_once_with(
        claim, context="tournament", expected_version=current.navigation_version
    )
    backend.lobby_action.assert_not_awaited()


async def test_unknown_invitee_keeps_the_username_prompt_active() -> None:
    from sitg_bot.application.contracts import ErrorCode, GatewayError, GatewayResponse
    from sitg_bot.bot.handlers.lobby import handle_invite_username
    from sitg_bot.bot.state import GatewayCallError

    current = navigation(
        context="lobby_other",
        active_lobby={
            "id": str(UUID(int=9)),
            "tournament_id": str(UUID(int=8)),
            "version": 1,
            "status": "assembling",
        },
        allowed_actions=["lobby.invite", "back"],
    )
    failure = GatewayCallError(
        GatewayResponse(
            action=ActionCode.LOBBY_INVITE,
            correlation_id=UUID(int=4),
            ok=False,
            error=GatewayError(
                code=ErrorCode.NOT_FOUND, message_key="error.not_found", retryable=False
            ),
        )
    )
    backend = SimpleNamespace(lobby_action=AsyncMock(side_effect=failure))
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"lobby_id": str(UUID(int=9))}), clear=AsyncMock()
    )
    message = SimpleNamespace(text="@missing_user", answer=AsyncMock())
    await handle_invite_username(
        message, backend, SimpleNamespace(), LocalizationService(), "en", current, state
    )
    assert "No registered bot user" in message.answer.await_args.args[0]
    state.clear.assert_not_awaited()


async def test_readiness_error_in_telegram_displays_the_specific_condition() -> None:
    from unittest.mock import patch

    from aiogram.types import Message, Update

    from sitg_bot.application.contracts import GatewayResponse
    from sitg_bot.application.gateway import ApplicationGateway
    from sitg_bot.bot.middleware.errors import ErrorMappingMiddleware
    from sitg_bot.services.matchmaking import LobbyReadinessError

    error = ApplicationGateway._error(
        LobbyReadinessError("insufficient_fresh_content", {"available": 1, "required": 3}),
        action=ActionCode.LOBBY_READY_UPDATE,
    )
    handler = AsyncMock(
        side_effect=GatewayCallError(
            GatewayResponse(
                action=ActionCode.LOBBY_READY_UPDATE,
                correlation_id=UUID(int=1),
                ok=False,
                error=error,
            )
        )
    )
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 42, "type": "private"},
                "text": "Готов",
            },
        }
    )
    with patch.object(Message, "answer", new=AsyncMock()) as answer:
        await ErrorMappingMiddleware(LocalizationService())(
            handler, update, {"locale": "ru"}
        )
        assert "доступно 1, требуется 3" in answer.await_args.args[0]
        assert "Проверьте введённое значение" not in answer.await_args.args[0]


async def test_banned_player_middleware_blocks_interaction_but_allows_library(
    monkeypatch,
) -> None:
    from sitg_bot.bot.middleware import ban as ban_middleware_module
    from sitg_bot.bot.middleware.ban import BannedPlayerMiddleware

    localization = LocalizationService()
    banned = navigation(
        allowed_actions=["start", "menu", "help", "language", "player.library"],
        ban_reason="cheating",
    )
    send = AsyncMock()
    monkeypatch.setattr(ban_middleware_module, "send_message_model", send)

    def update(text: str) -> Update:
        return Update.model_validate(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            }
        )

    middleware = BannedPlayerMiddleware()
    handler = AsyncMock()

    with pytest.raises(CancelHandler):
        await middleware(
            handler,
            update("/start"),
            {"navigation": banned, "localization": localization, "locale": "en"},
        )
    assert send.await_count == 1
    assert "You have been banned!" in send.await_args.args[1].text
    assert "cheating" in send.await_args.args[1].text

    await middleware(
        handler,
        update("Library"),
        {"navigation": banned, "localization": localization, "locale": "en"},
    )
    handler.assert_awaited_once()
    assert send.await_count == 1


async def test_admin_ban_flow_collects_target_reason_and_receipt() -> None:
    from sitg_bot.bot.handlers.admin import handle_admin_ban_reason, handle_admin_ban_target

    localization = LocalizationService()
    state = SimpleNamespace(
        clear=AsyncMock(),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        get_data=AsyncMock(return_value={"ban_target": "@cheater"}),
    )
    message = SimpleNamespace(answer=AsyncMock(), text="@cheater")

    await handle_admin_ban_target(
        message,  # type: ignore[arg-type]
        localization=localization,
        locale="en",
        navigation=navigation(active_mode="admin"),
        state=state,  # type: ignore[arg-type]
    )
    assert state.update_data.await_args.kwargs == {"ban_target": "@cheater"}
    reason_prompt = message.answer.await_args.args[0]
    assert "reason" in reason_prompt.lower()

    backend = SimpleNamespace(
        ban_player=AsyncMock(
            return_value={
                "player_id": str(UUID(int=2)),
                "display_name": "Cheater",
                "telegram_username": "cheater",
                "reason": "Cheating in games",
                "banned_at": "2026-09-12T10:00:00+00:00",
            }
        )
    )
    reason_message = SimpleNamespace(answer=AsyncMock(), text="Cheating in games")
    await handle_admin_ban_reason(
        reason_message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=localization,
        locale="en",
        state=state,  # type: ignore[arg-type]
    )
    backend.ban_player.assert_awaited_once()
    assert backend.ban_player.await_args.kwargs["target"] == "@cheater"
    assert backend.ban_player.await_args.kwargs["reason"] == "Cheating in games"
    receipt = reason_message.answer.await_args.args[0]
    assert "Player banned" in receipt
    assert "Cheating in games" in receipt


async def test_admin_unban_reports_missing_players() -> None:
    from sitg_bot.application.contracts import ErrorCode, GatewayError
    from sitg_bot.bot.handlers.admin import handle_admin_unban_target

    localization = LocalizationService()
    error_response = GatewayResponse(
        action=ActionCode.PLAYER_UNBAN,
        correlation_id=UUID(int=7),
        ok=False,
        error=GatewayError(code=ErrorCode.NOT_FOUND, message_key="error.not_found"),
    )
    backend = SimpleNamespace(
        unban_player=AsyncMock(side_effect=GatewayCallError(error_response))
    )
    message = SimpleNamespace(answer=AsyncMock(), text=str(UUID(int=2)))
    state = SimpleNamespace(clear=AsyncMock())

    await handle_admin_unban_target(
        message,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=localization,
        locale="en",
        navigation=navigation(active_mode="admin"),
        state=state,  # type: ignore[arg-type]
    )
    assert "not found" in message.answer.await_args.args[0]


async def test_bug_command_submits_commentary_and_prompts_without_one() -> None:
    from sitg_bot.bot.handlers.player import handle_bug_report

    localization = LocalizationService()
    backend = SimpleNamespace(submit_bug_report=AsyncMock(return_value={"report_id": "1"}))
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())

    submitted = SimpleNamespace(
        answer=AsyncMock(), text="/bug The scoreboard shows wrong scores"
    )
    await handle_bug_report(
        submitted,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=localization,
        locale="en",
        navigation=navigation(),
        state=state,  # type: ignore[arg-type]
    )
    backend.submit_bug_report.assert_awaited_once()
    assert (
        backend.submit_bug_report.await_args.kwargs["commentary"]
        == "The scoreboard shows wrong scores"
    )
    assert "Thank you" in submitted.answer.await_args.args[0]

    pending = SimpleNamespace(answer=AsyncMock(), text="/bug")
    await handle_bug_report(
        pending,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        telegram_update_claim=SimpleNamespace(),  # type: ignore[arg-type]
        localization=localization,
        locale="en",
        navigation=navigation(),
        state=state,  # type: ignore[arg-type]
    )
    state.set_state.assert_awaited_once()
    assert "Describe what exactly happened" in pending.answer.await_args.args[0]


def test_notification_text_renders_bug_reports() -> None:
    text = notification_text(
        "bug_report",
        {
            "reporter_nickname": "Reporter",
            "reporter_telegram_username": "reporter",
            "commentary": "The answer prompt disappeared",
            "created_at": "2026-09-12T10:00:00+00:00",
        },
        LocalizationService(),
        "en",
    )
    assert "Bug report" in text
    assert "Reporter" in text
    assert "The answer prompt disappeared" in text
    assert "2026-09-12" in text
