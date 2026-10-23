"""Exercise registered aiogram routes with production dependency names and real alert contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    User,
)
from pydantic import ValidationError
from test_telegram_frontend import navigation

from sitg_bot.application.contracts import ActionCode, NotificationAlertsClaimOperation
from sitg_bot.bot.handlers.navigation import router as navigation_router
from sitg_bot.bot.handlers.training import router as training_router
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.state.backend import BotBackend

URL = "https://mini.example.test/"
HOME = [
    "training.train", "training.history", "training.knowledge", "training.upload", "mode.switch",
]
LOBBY = ["training.settings", "training.start", "training.quit"]
LABELS = {
    "en": [
        ["Start training"],
        ["Training history"],
        ["Knowledge library"],
        ["Upload packet for training"],
        ["Change mode"],
    ],
    "ru": [
        ["Начать тренировку"],
        ["История тренировок"],
        ["Библиотека знаний"],
        ["Загрузить пакет для тренировки"],
        ["Сменить режим"],
    ],
}
LOBBY_LABELS = {
    "en": [["Packets and settings"], ["Start"], ["Quit"]],
    "ru": [["Пакеты и настройки"], ["Старт"], ["Выйти"]],
}


def message(text=None):
    return Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text=text
    )


def training_navigation(lobby=False):
    return navigation(
        active_mode="training",
        context="training_lobby" if lobby else "menu",
        available_modes=["player", "training"],
        allowed_actions=LOBBY if lobby else HOME,
    )


def backend():
    result = BotBackend(SimpleNamespace())
    # Keep claim_notification_alerts real: an invalid audience must fail before this RPC seam.
    result._execute = AsyncMock(return_value=SimpleNamespace(data={"audiences": []}))
    return result


def labels(markup):
    assert isinstance(markup, ReplyKeyboardMarkup)
    return [[button.text for button in row] for row in markup.keyboard]


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("lobby", [False, True])
async def test_mode_callback_sends_replacement_training_keyboard(locale, lobby):
    adapter = backend()
    adapter.set_mode = AsyncMock(return_value=training_navigation(lobby))
    callback = CallbackQuery(
        id="mode",
        from_user=User(id=42, is_bot=False, first_name="Player"),
        chat_instance="chat",
        data="mode:set:training",
        message=message(),
    )
    claim = object()
    loc = LocalizationService()
    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as acknowledge,
    ):
        await navigation_router.propagate_event(
            update_type="callback_query",
            event=callback,
            backend=adapter,
            telegram_update_claim=claim,
            localization=loc,
            locale=locale,
            navigation=navigation(
                available_modes=["player", "training"], allowed_actions=["mode.switch"]
            ),
            state=SimpleNamespace(clear=AsyncMock()),
        )
        adapter.set_mode.assert_awaited_once_with(claim, mode="training", expected_version=0)
        acknowledge.assert_awaited_once()
        edit.assert_awaited_once_with(
            loc.text("mode.changed", locale, mode=loc.text("mode.training", locale))
        )
        answer.assert_awaited_once()
        assert (
            labels(answer.await_args.kwargs["reply_markup"])
            == (LOBBY_LABELS if lobby else LABELS)[locale]
        )
    adapter._execute.assert_not_awaited()


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("lobby", [False, True])
async def test_menu_command_restores_training_keyboard_without_notification_rpc(locale, lobby):
    adapter = backend()
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await navigation_router.propagate_event(
            update_type="message",
            event=message("/menu"),
            backend=adapter,
            bot=object(),
            telegram_update_claim=object(),
            localization=LocalizationService(),
            locale=locale,
            navigation=training_navigation(lobby),
            state=SimpleNamespace(clear=AsyncMock()),
        )
        answer.assert_awaited_once()
        assert (
            labels(answer.await_args.kwargs["reply_markup"])
            == (LOBBY_LABELS if lobby else LABELS)[locale]
        )
    adapter._execute.assert_not_awaited()


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("mode", ["player", "manager", "admin"])
@pytest.mark.parametrize("unseen", [False, True])
async def test_supported_modes_keep_alert_claiming_and_menu_delivery(locale, mode, unseen):
    adapter = backend()
    adapter._execute.return_value = SimpleNamespace(data={"audiences": [mode] if unseen else []})
    claim = object()
    loc = LocalizationService()
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await navigation_router.propagate_event(
            update_type="message",
            event=message("/menu"),
            backend=adapter,
            bot=object(),
            telegram_update_claim=claim,
            localization=loc,
            locale=locale,
            navigation=navigation(
                active_mode=mode,
                available_modes=[mode],
                allowed_actions=["mode.switch", "notifications"],
            ),
            state=SimpleNamespace(clear=AsyncMock()),
        )
        adapter._execute.assert_awaited_once()
        actual_claim, operation = adapter._execute.await_args.args
        assert actual_claim is claim
        assert isinstance(operation, NotificationAlertsClaimOperation)
        assert operation.action == ActionCode.NOTIFICATION_ALERTS_CLAIM
        assert operation.audience == mode
        assert answer.await_count == (2 if unseen else 1)
        if unseen:
            assert answer.await_args_list[0].args == (
                loc.text("notifications.unseen_alert", locale),
            )
        assert labels(answer.await_args.kwargs["reply_markup"])


def test_training_is_not_a_notification_audience():
    with pytest.raises(ValidationError, match="audience"):
        NotificationAlertsClaimOperation(
            action=ActionCode.NOTIFICATION_ALERTS_CLAIM, audience="training"
        )


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("configured", [False, True])
async def test_settings_registered_route_uses_polling_launch_links(locale, configured):
    loc = LocalizationService()
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await training_router.propagate_event(
            update_type="message",
            event=message(loc.text("button.training.settings", locale)),
            backend=None,
            telegram_update_claim=object(),
            localization=loc,
            locale=locale,
            navigation=training_navigation(lobby=True),
            launch_links=URL if configured else None,
        )
        answer.assert_awaited_once()
        if configured:
            markup = answer.await_args.kwargs["reply_markup"]
            assert isinstance(markup, InlineKeyboardMarkup)
            button = markup.inline_keyboard[0][0]
            assert button.text == loc.text("training.settings", locale)
            assert button.web_app.url == URL + "training?_launch=1"
        else:
            assert answer.await_args.args == (loc.text("training.miniapp_unavailable", locale),)
            assert answer.await_args.kwargs.get("reply_markup") is None


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_create_lobby_registered_route_keeps_settings_as_text_action(locale):
    adapter = SimpleNamespace(
        _execute=AsyncMock(return_value=SimpleNamespace(data={})),
        navigation=AsyncMock(return_value=training_navigation(lobby=True)),
    )
    loc = LocalizationService()
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await training_router.propagate_event(
            update_type="message",
            event=message(loc.text("button.training.train", locale)),
            backend=adapter,
            telegram_update_claim=object(),
            localization=loc,
            locale=locale,
            navigation=training_navigation(),
            launch_links=URL,
        )
        adapter._execute.assert_awaited_once()
        operation = adapter._execute.await_args.args[1]
        assert operation.action == ActionCode.TRAINING_LOBBY
        assert operation.command == "create"
        markup = answer.await_args.kwargs["reply_markup"]
        assert labels(markup) == LOBBY_LABELS[locale]
        assert all(len(row) == 1 for row in markup.keyboard)
        assert all(button.web_app is None for row in markup.keyboard for button in row)
