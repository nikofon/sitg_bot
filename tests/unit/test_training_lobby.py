from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from test_telegram_frontend import navigation
from training_delivery_support import deliver, delivery_case

from sitg_bot.application.contracts import ActionCode
from sitg_bot.bot.handlers.training import handle_training_menu, start_lobby
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.keyboards.common import navigation_keyboard


def test_training_home_and_lobby_keyboards():
    localization = LocalizationService()
    home = navigation(
        active_mode="training",
        context="menu",
        allowed_actions=[
            "training.train",
            "training.knowledge",
            "training.upload",
            "mode.switch",
        ],
    )
    assert navigation_keyboard(home, localization, "en").rows == (
        ("Start training",),
        ("Knowledge library",),
        ("Upload packet for training",),
        ("Change mode",),
    )
    lobby = navigation(
        active_mode="training",
        context="training_lobby",
        allowed_actions=[
            "training.settings",
            "training.start",
            "training.quit",
        ],
    )
    assert navigation_keyboard(lobby, localization, "en").rows == (
        ("Packets and settings",),
        ("Start",),
        ("Quit",),
    )


async def test_start_training_creates_lobby_without_starting_questions():
    state = navigation(
        active_mode="training",
        context="training_lobby",
        allowed_actions=[
            "training.settings",
            "training.start",
            "training.quit",
        ],
    )
    backend = SimpleNamespace(
        _execute=AsyncMock(return_value=SimpleNamespace(data={"lobby": {"version": 1}})),
        navigation=AsyncMock(return_value=state),
    )
    message = SimpleNamespace(answer=AsyncMock())
    claim = object()
    await handle_training_menu(
        message,
        backend,
        claim,
        LocalizationService(),
        "en",
        "training.train",
        launch_links="https://mini.example.test",
    )
    operation = backend._execute.await_args.args[1]
    assert operation.action == ActionCode.TRAINING_LOBBY
    assert operation.command == "create"
    assert backend._execute.await_count == 1
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert all(button.web_app is None for row in keyboard.keyboard for button in row)
    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        ["Packets and settings"],
        ["Start"],
        ["Quit"],
    ]


async def test_start_needs_a_saved_packet():
    backend = SimpleNamespace(
        _execute=AsyncMock(
            return_value=SimpleNamespace(
                data={"session": None, "lobby": {"version": 1, "version_id": None}}
            )
        )
    )
    message = SimpleNamespace(answer=AsyncMock())
    await start_lobby(message, backend, object(), LocalizationService(), "en")
    assert backend._execute.await_count == 1
    assert "save" in message.answer.await_args.args[0]


async def test_start_uses_saved_lobby_version_and_confirmation():
    backend = SimpleNamespace(
        _execute=AsyncMock(
            side_effect=[
                SimpleNamespace(data={"lobby": {"version": 7, "version_id": str(UUID(int=1))}}),
                SimpleNamespace(data={"confirmation_required": True}),
            ]
        )
    )
    message = SimpleNamespace(answer=AsyncMock())
    await start_lobby(message, backend, object(), LocalizationService(), "en")
    operation = backend._execute.await_args.args[1]
    assert operation.action == ActionCode.TRAINING_START
    assert operation.lobby_version == 7
    assert operation.version_id == UUID(int=1)
    assert operation.confirm is False
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "tl:start:7"


@pytest.mark.parametrize("outbox_first", [True, False])
async def test_successful_start_has_one_opening_and_keeps_durable_delivery(outbox_first):
    case = delivery_case(phase="intermission")
    # Actual launch begins with the start event, before theme/question announcements.
    case.state["events"] = case.state["events"][:1]
    nav = navigation(active_mode="training", context="training",
                     active_training_id=UUID(case.payload["session_id"]), allowed_actions=[])
    backend = SimpleNamespace(
        _execute=AsyncMock(side_effect=[
            SimpleNamespace(data={"lobby": {"version": 7, "version_id": str(UUID(int=1))}}),
            SimpleNamespace(data={"session": case.state["session"]}),
        ]),
        navigation=AsyncMock(return_value=nav), training_delivery=case.delivery,
    )
    async def answer(text, **kwargs):
        return await case.bot.send_message(42, text, **kwargs)

    message = SimpleNamespace(answer=answer)
    if outbox_first:
        await deliver(case)
    await start_lobby(message, backend, object(), LocalizationService(), "en")
    await deliver(case)
    opening = LocalizationService().text("training.controls", "en")
    assert [text for _, text, _ in case.sends] == [opening]
    assert case.state["presentation"]["sequence"] == 1
    assert case.state["presentation"]["messages"]["start"]["ids"]
    # Outbox redelivery/restart must not replay the opening or lose the next announcement.
    await deliver(case)
    case.event("themes_announced", themes=[{"name": "Theme"}])
    await deliver(case)
    assert [text for _, text, _ in case.sends].count(opening) == 1
    assert "Theme" in case.sends[-1][1]


async def test_settings_keeps_inline_miniapp_launch():
    message = SimpleNamespace(answer=AsyncMock())
    for action, suffix in [
        ("training.settings", "/training?_launch=1"),
    ]:
        await handle_training_menu(
            message,
            None,
            object(),
            LocalizationService(),
            "en",
            action,
            launch_links="https://mini.example.test",
        )
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        assert keyboard.inline_keyboard[0][0].web_app.url == f"https://mini.example.test{suffix}"


async def test_quit_discards_lobby_and_restores_home_keyboard():
    home = navigation(
        active_mode="training",
        context="menu",
        allowed_actions=[
            "training.train",
            "training.knowledge",
            "training.upload",
            "mode.switch",
        ],
    )
    backend = SimpleNamespace(
        _execute=AsyncMock(
            side_effect=[
                SimpleNamespace(data={"session": None, "lobby": {"version": 5}}),
                SimpleNamespace(data={"lobby": None}),
            ]
        ),
        navigation=AsyncMock(return_value=home),
    )
    message = SimpleNamespace(answer=AsyncMock())
    await handle_training_menu(
        message, backend, object(), LocalizationService(), "en", "training.quit"
    )
    operation = backend._execute.await_args.args[1]
    assert operation.command == "quit"
    assert operation.expected_version == 5
    assert message.answer.await_args.kwargs["reply_markup"].keyboard[0][0].text == "Start training"
