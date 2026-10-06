from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.lobby_delivery import (
    LobbyDelivery,
    lobby_notice_delivery_handler,
    lobby_open_delivery_handler,
    notification_alert_delivery_handler,
    packet_draft_status_delivery_handler,
)


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("mode,context,action", [
    ("player", "tournament", "player.tournament.create_lobby"),
    ("manager", "menu", "manager.tournament.select"),
    ("player", "lobby", "lobby.info"),
    ("player", "chat", ""),
])
async def test_kick_notice_attaches_current_navigation_keyboard(locale, mode, context, action):
    localization = LocalizationService()
    bot = SimpleNamespace(send_message=AsyncMock())
    protocol = SimpleNamespace(request=AsyncMock(return_value={
        "account": {
            "player_id": str(UUID(int=1)), "telegram_user_id": 42,
            "public_nickname": "Player", "preferred_locale": locale,
            "registration_status": "active", "registration_completed_at": "2026-01-01",
            "profile_version": 1,
        },
        "available_modes": ["player", "manager"], "active_mode": mode,
        "context": context, "navigation_version": 3,
        "selected_player_tournament": None, "selected_manager_tournament": None,
        "active_lobby": None, "active_game": None, "allowed_actions": [action] if action else [],
        "active_chat": {
            "id": str(UUID(int=2)), "tournament_id": str(UUID(int=3)),
            "tournament_name": "Cup", "round_number": 1, "match_number": 1,
            "multiple_matches": False,
        } if context == "chat" else None,
    }))
    deliver = lobby_notice_delivery_handler(bot, localization, protocol=protocol)
    await deliver({"kind": "kicked_self", "recipient_telegram_user_id": 42, "locale": locale})
    protocol.request.assert_awaited_once_with("telegram.navigation.snapshot", telegram_user_id=42)
    sent = bot.send_message.await_args
    assert sent.args == (42, localization.text("lobby.kicked_self", locale))
    if context == "chat":
        assert sent.kwargs["reply_markup"].remove_keyboard is True
        return
    assert [[button.text for button in row] for row in sent.kwargs["reply_markup"].keyboard] == [
        [localization.text(f"button.{action}", locale)],
    ]


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_kick_notice_to_remaining_members_uses_kicked_wording(locale):
    bot = SimpleNamespace(send_message=AsyncMock())
    deliver = lobby_notice_delivery_handler(bot, LocalizationService())
    await deliver({
        "kind": "player_left", "kicked": True, "player_name": "<Alice>",
        "role": "observer", "recipient_telegram_user_id": 42, "locale": locale,
    })
    assert bot.send_message.await_args.args[1] == (
        "&lt;Alice&gt; was kicked from the lobby." if locale == "en"
        else "&lt;Alice&gt; исключён из лобби."
    )
    assert bot.send_message.await_args.kwargs["reply_markup"] is None


async def test_summary_refresh_replacement_and_restart_follow_saved_message_ids():
    state = {
        "active": True, "messages": {},
        "lobby": {"tournament_name": "Cup", "members": [], "selected_packets": []},
        "launch_reference": "opaque-lobby", "expires_at": "2099-01-01",
    }

    async def request(action, **params):
        if action == "telegram.lobby.record":
            state["messages"] = dict(params["messages"])
        return {**state, "messages": dict(state["messages"])}

    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=i) for i in range(1, 5)]),
        edit_message_text=AsyncMock(), delete_message=AsyncMock(),
    )
    protocol = SimpleNamespace(request=AsyncMock(side_effect=request))
    delivery = LobbyDelivery(bot, LocalizationService(), protocol, "https://mini.test", "bot")
    payload = {
        "recipient_telegram_user_id": 42, "lobby_id": "lobby", "locale": "en",
        "join_sequence": 1,
    }
    await delivery(payload)
    assert state["messages"] == {"summary": 1, "settings": 2, "join_sequence": 1}
    state["lobby"]["members"] = [{"name": "Alice", "role": "player"}]
    state["lobby"]["selected_packets"] = [{"name": "Round 1"}]
    # A new delivery object represents a restarted bot.
    delivery = LobbyDelivery(bot, LocalizationService(), protocol, "https://mini.test", "bot")
    await delivery(payload)
    assert bot.send_message.await_count == 2
    edited = bot.edit_message_text.await_args
    assert edited.kwargs["message_id"] == 1
    assert "Alice" in edited.args[0] and "Round 1" in edited.args[0]
    await delivery.show(42, "lobby", "en", replace=True)
    assert [call.args for call in bot.delete_message.await_args_list] == [(42, 1), (42, 2)]
    assert state["messages"] == {"summary": 3, "settings": 4, "join_sequence": 1}
    state["lobby"]["members"] = []
    state["lobby"]["selected_packets"] = []
    await delivery(payload)
    edited = bot.edit_message_text.await_args
    assert edited.kwargs["message_id"] == 3
    assert "Alice" not in edited.args[0] and "Round 1" not in edited.args[0]
    state["active"] = False
    await delivery(payload)
    assert bot.send_message.await_count == 4


@pytest.mark.parametrize("ready,count", [(True, 1), (False, 0)])
@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_readiness_notice_includes_escaped_name_and_counts(ready, count, locale):
    bot = SimpleNamespace(send_message=AsyncMock())
    deliver = lobby_notice_delivery_handler(bot, LocalizationService())
    await deliver({
        "recipient_telegram_user_id": 42,
        "locale": locale,
        "kind": "readiness_changed",
        "player_name": "<Player>",
        "ready": ready,
        "ready_count": count,
        "player_count": 4,
    })
    text = bot.send_message.await_args.args[1]
    assert "&lt;Player&gt;" in text
    assert f"{count}/4" in text
    if locale == "en":
        assert ("is ready." if ready else "is not ready.") in text
    else:
        assert ("готов." if ready else "не готов.") in text


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_readiness_notice_lists_every_player_status_with_icons(locale):
    bot = SimpleNamespace(send_message=AsyncMock())
    deliver = lobby_notice_delivery_handler(bot, LocalizationService())
    await deliver({
        "recipient_telegram_user_id": 42,
        "locale": locale,
        "kind": "readiness_changed",
        "player_name": "Bob",
        "ready": True,
        "ready_count": 1,
        "player_count": 2,
        "members": [
            {"name": "<Alice>", "ready": True},
            {"name": "Bob", "ready": False},
        ],
    })
    text = bot.send_message.await_args.args[1]
    assert "1/2" in text
    assert "✅ &lt;Alice&gt;" in text and "⏳ Bob" in text
    if locale == "en":
        assert "Player readiness: 1/2" in text
        assert "✅ &lt;Alice&gt; — Ready" in text and "⏳ Bob — Not ready" in text
    else:
        assert "Готовность игроков: 1/2" in text
        assert "✅ &lt;Alice&gt; — Готов" in text and "⏳ Bob — Не готов" in text


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
        "Open the lobby to change its settings and choose packets for the game.",
    )
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].web_app.url == (
        "https://mini.example.test/app/lobbies/opaque-lobby?tgWebAppStartParam=lr_opaque-lobby"
    )


async def test_classic_packet_notice_lists_prescribed_players_and_solo_rule() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = lobby_notice_delivery_handler(bot, LocalizationService())
    payload = {
        "recipient_telegram_user_id": 42,
        "locale": "en",
        "kind": "packet_selected",
        "packet_name": "Round 1",
        "classic_players": ["Alice <b>", "Chair"],
    }
    await handler(payload)
    text = bot.send_message.await_args.args[1]
    assert "Alice &lt;b&gt;, Chair" in text
    assert "exactly these participants" in text
    await handler({**payload, "classic_players": ["Alice"], "classic_solo": True})
    assert "Play it alone" in bot.send_message.await_args.args[1]


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
