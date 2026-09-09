from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.storage.models import (
    PlayerNotificationAlertRecord,
    PlayerRecord,
    PlayerTelegramNavigationRecord,
)


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
