from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.services.navigation import NavigationGame, TelegramNavigationService
from sitg_bot.storage.models import PlayerRecord, PlayerTelegramNavigationRecord


@pytest.mark.parametrize("active", [True, False])
async def test_active_normal_game_outranks_saved_training_lobby(active):
    owner = UUID(int=1)
    player = SimpleNamespace(id=owner, training_lobby={"version": 1})
    saved = SimpleNamespace(
        interaction_mode="training", version=2, player_context="tournament",
        selected_player_tournament_id=None, selected_manager_tournament_id=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[player, None, None]),
        get=AsyncMock(side_effect=lambda model, key: {
            PlayerRecord: player, PlayerTelegramNavigationRecord: saved,
        }[model]),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    service = TelegramNavigationService(
        SimpleNamespace(transaction=transaction),
        player_accounts=SimpleNamespace(_account_snapshot=lambda value: SimpleNamespace(
            player_id=owner, registration_status="active",
        )),
    )
    game = NavigationGame(UUID(int=2), UUID(int=3), "active", "reading", 4, True, active, False)
    service._available_modes = AsyncMock(return_value=("player", "training"))
    service._selected_tournament = AsyncMock(return_value=None)
    service._active_game = AsyncMock(return_value=game)
    service._active_lobby = AsyncMock(return_value=None)
    result = await service.snapshot(123)
    if active:
        assert result.context == "game" and result.active_mode == "player"
        assert "game.open" in result.allowed_actions
        assert "training.start" not in result.allowed_actions
    else:
        assert result.context == "training_lobby" and result.active_mode == "training"
        assert result.allowed_actions == ("training.settings", "training.start", "training.quit")
