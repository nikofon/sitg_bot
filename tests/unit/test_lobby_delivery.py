from types import SimpleNamespace
from unittest.mock import AsyncMock

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.lobby_delivery import (
    lobby_open_delivery_handler,
    notification_alert_delivery_handler,
    packet_draft_status_delivery_handler,
)


async def test_durable_lobby_delivery_builds_localized_reopenable_button() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = lobby_open_delivery_handler(
        bot,  # type: ignore[arg-type]
        LocalizationService(),
        "https://mini.example.test/app",
    )

    await handler(
        {
            "recipient_telegram_user_id": 42,
            "locale": "en",
            "launch_reference": "opaque-lobby",
            "expires_at": "2026-09-04T10:00:00+00:00",
        }
    )

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[:2] == (
        42,
        "This lobby link remains available while the lobby is active.",
    )
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].web_app.url == (
        "https://mini.example.test/app/lobbies/opaque-lobby?tgWebAppStartParam=lr_opaque-lobby"
    )


async def test_notification_alert_uses_matching_role_copy() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = notification_alert_delivery_handler(  # type: ignore[arg-type]
        bot, LocalizationService()
    )

    await handler(
        {
            "recipient_telegram_user_id": 42,
            "locale": "en",
            "audience": "manager",
            "same_role": True,
        }
    )

    bot.send_message.assert_awaited_once_with(42, "You have a new notification!")


async def test_notification_alert_names_a_different_role() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = notification_alert_delivery_handler(  # type: ignore[arg-type]
        bot, LocalizationService()
    )

    await handler(
        {
            "recipient_telegram_user_id": 42,
            "locale": "en",
            "audience": "admin",
            "same_role": False,
        }
    )

    bot.send_message.assert_awaited_once_with(42, "You have new notification for admin!")


async def test_packet_status_removes_draft_buttons_and_sends_confirmation() -> None:
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(),
        send_message=AsyncMock(),
    )
    handler = packet_draft_status_delivery_handler(  # type: ignore[arg-type]
        bot, LocalizationService()
    )

    await handler(
        {
            "chat_id": 42,
            "message_id": 17,
            "locale": "ru",
            "status": "published",
        }
    )

    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42,
        message_id=17,
        reply_markup=None,
    )
    bot.send_message.assert_awaited_once_with(
        42,
        "Пакет опубликован. Доступ к его турнирному назначению изначально отключён.",
    )


async def test_lobby_invitation_and_change_notices_escape_names_and_localize_settings() -> None:
    from sitg_bot.bot.lobby_delivery import lobby_notice_delivery_handler

    bot = SimpleNamespace(send_message=AsyncMock())
    deliver = lobby_notice_delivery_handler(bot, LocalizationService())
    common = {"recipient_telegram_user_id": 42, "locale": "en"}
    await deliver(
        {
            **common,
            "kind": "invitation",
            "sender": "<Alice>",
            "tournament": "Cup",
            "invitation_code": "a" * 32,
            "allow_observer": True,
        }
    )
    text = bot.send_message.await_args.args[1]
    assert "&lt;Alice&gt;" in text
    keyboard = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert keyboard[0][0].callback_data == "lobbyjoin:p:" + "a" * 32
    assert keyboard[1][0].callback_data == "lobbyjoin:o:" + "a" * 32
    await deliver({**common, "kind": "settings_changed", "changes": {"theme_count": 2}})
    assert "Theme count: 2" in bot.send_message.await_args.args[1]
    await deliver(
        {
            **common,
            "kind": "packet_rejected",
            "packet_name": "<Packet>",
            "reason": "packet_not_playable",
        }
    )
    assert "&lt;Packet&gt;" in bot.send_message.await_args.args[1]
    assert "no permission to play" in bot.send_message.await_args.args[1]


async def test_lobby_membership_and_packet_removal_notices_are_localized() -> None:
    from sitg_bot.bot.lobby_delivery import lobby_notice_delivery_handler

    bot = SimpleNamespace(send_message=AsyncMock())
    deliver = lobby_notice_delivery_handler(bot, LocalizationService())
    common = {"recipient_telegram_user_id": 42, "locale": "ru"}
    for kind, expected in (("player_joined", "присоединился"), ("player_left", "покинул")):
        await deliver({**common, "kind": kind, "player_name": "<Игрок>", "role": "observer"})
        text = bot.send_message.await_args.args[1]
        assert "&lt;Игрок&gt;" in text and "наблюдатель" in text and expected in text
    await deliver({**common, "kind": "packet_removed", "packet_name": "Пакет № 1"})
    assert "Пакет № 1" in bot.send_message.await_args.args[1]
    assert "убран" in bot.send_message.await_args.args[1]
