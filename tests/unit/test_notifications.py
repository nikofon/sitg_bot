from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.bot.handlers.player import notification_text
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.storage.models import (
    PlayerNotificationAlertRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
)


@pytest.mark.parametrize("locale", ["en", "ru"])
def test_packet_available_notification_renders_roster_deadline_and_escapes_names(locale):
    text = notification_text("packet.available", {
        "packet_name": "<Packet>", "tournament_name": "Cup",
        "opponents": [{"name": "<Ada>", "chair": False}, {"name": "", "chair": True}],
        "start_deadline": "2026-10-01T12:00:00+00:00",
    }, LocalizationService(), locale)
    assert "&lt;Packet&gt;" in text and "&lt;Ada&gt;" in text
    assert "2026-10-01T12:00:00+00:00" in text
    assert ("Chair" if locale == "en" else "Стул") in text


class AlertSession:
    def __init__(self, player: PlayerRecord, *, mode: str = "player") -> None:
        self.player = player
        self.navigation = PlayerTelegramNavigationRecord(
            player_id=player.id,
            interaction_mode=mode,
            version=1,
        )
        self.alert: PlayerNotificationAlertRecord | None = None

    async def scalar(self, _query: object) -> PlayerRecord:
        return self.player

    async def get(
        self, model: type[object], _identity: object, **_kwargs: object
    ) -> object | None:
        if model is PlayerNotificationAlertRecord:
            return self.alert
        if model is PlayerTelegramNavigationRecord:
            return self.navigation
        return None

    def add(self, value: object) -> None:
        if isinstance(value, PlayerNotificationAlertRecord):
            self.alert = value


@pytest.fixture
def alert_dependencies(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(TelegramNavigationService, "_active_game", AsyncMock(return_value=None))
    monkeypatch.setattr(
        TelegramNavigationService,
        "_available_modes",
        AsyncMock(return_value=("player", "manager", "admin")),
    )
    enqueue = AsyncMock(return_value=UUID(int=100))
    monkeypatch.setattr(TransactionalOutbox, "enqueue", enqueue)
    return enqueue


async def test_live_alert_names_role_and_throttles_across_audiences(
    alert_dependencies: AsyncMock,
) -> None:
    player = PlayerRecord(
        id=UUID(int=1),
        telegram_user_id=42,
        preferred_locale="en",
        status="active",
    )
    session = AlertSession(player, mode="player")

    await NotificationWriter._schedule_alert(  # type: ignore[arg-type]
        session,
        recipient_player_id=player.id,
        audience="admin",
        deduplication_key="first",
    )
    await NotificationWriter._schedule_alert(  # type: ignore[arg-type]
        session,
        recipient_player_id=player.id,
        audience="manager",
        deduplication_key="second",
    )

    alert_dependencies.assert_awaited_once()
    assert alert_dependencies.await_args.kwargs["payload"] == {
        "recipient_telegram_user_id": 42,
        "locale": "en",
        "audience": "admin",
        "same_role": False,
    }


async def test_live_alert_is_suppressed_during_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = PlayerRecord(id=UUID(int=1), telegram_user_id=42, status="active")
    session = AlertSession(player)
    monkeypatch.setattr(
        TelegramNavigationService,
        "_active_game",
        AsyncMock(return_value=SimpleNamespace(id=UUID(int=2))),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(TransactionalOutbox, "enqueue", enqueue)

    await NotificationWriter._schedule_alert(  # type: ignore[arg-type]
        session,
        recipient_player_id=player.id,
        audience="player",
        deduplication_key="in-game",
    )

    enqueue.assert_not_awaited()
    assert session.alert is None
