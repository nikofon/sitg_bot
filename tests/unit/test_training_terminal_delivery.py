from contextlib import asynccontextmanager
from datetime import UTC, datetime
from itertools import count, cycle
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.training_delivery import TrainingDelivery
from sitg_bot.domain.training import TrainingSettings
from sitg_bot.services.navigation import NavigationGame, NavigationLobby, TelegramNavigationService
from sitg_bot.services.training import TrainingService
from sitg_bot.storage.models import (
    PlayerRecord,
    PlayerTelegramNavigationRecord,
    PregameLobbyRecord,
    TrainingMemberRecord,
)

OWNER, RUN, QUESTION, OTHER = (UUID(int=index) for index in range(1, 5))
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def terminal_delivery_harness(monkeypatch):
    record = SimpleNamespace(
        id=RUN, owner_id=OWNER, packet_version_id=OTHER, version=1, presentation_version=1,
        status="active", phase="revealed", question_index=0, token_cursor=1,
        settings=TrainingSettings().to_dict(), text_hidden=False, hidden_deadline=None,
        deadline=None,
    )
    player = SimpleNamespace(
        id=OWNER, telegram_user_id=42, preferred_locale="en", public_nickname="Player",
        status="active", registration_completed_at=NOW, profile_version=1, training_lobby=None,
    )
    navigation = SimpleNamespace(
        interaction_mode="training", player_context="tournament", version=1,
        selected_player_tournament_id=None, selected_manager_tournament_id=None,
    )
    member = SimpleNamespace(presentation={})
    rows = {
        PlayerRecord: player, PlayerTelegramNavigationRecord: navigation,
        TrainingMemberRecord: member, PregameLobbyRecord: SimpleNamespace(creator_player_id=OTHER),
    }
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, key: rows[model]),
        scalar=AsyncMock(return_value=None),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    service = TrainingService(SimpleNamespace(transaction=transaction))
    service._owned = AsyncMock(return_value=record)
    service.packets.authorize = AsyncMock()
    service._question = AsyncMock(return_value=(
        SimpleNamespace(value=10),
        SimpleNamespace(id=QUESTION, text="Question", answer="Answer", commentary="Comment"),
        "Theme",
    ))
    service._attempt = AsyncMock(return_value=SimpleNamespace(
        answer="Answer", correct=True, outcome="answered", appeal_status=None,
    ))
    service._score = AsyncMock(return_value=10)
    service._notice = AsyncMock()
    game_lookup, lobby_lookup = AsyncMock(return_value=None), AsyncMock(return_value=None)
    monkeypatch.setattr(TelegramNavigationService, "_active_game", game_lookup)
    monkeypatch.setattr(TelegramNavigationService, "_active_lobby", lobby_lookup)
    monkeypatch.setattr(TelegramNavigationService, "_available_modes", AsyncMock(
        return_value=("player", "training", "manager", "admin"),
    ))
    monkeypatch.setattr(
        TelegramNavigationService, "_selected_tournament", AsyncMock(return_value=None),
    )

    async def request(action, **params):
        return await service.delivery(OWNER, RUN, presentation=params.get("presentation"))

    identifiers = count(10)
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace(
            message_id=next(identifiers),
        )),
        edit_message_text=AsyncMock(),
    )
    delivery = TrainingDelivery(bot, LocalizationService(), SimpleNamespace(request=request))
    return SimpleNamespace(
        service=service, record=record, player=player, navigation=navigation, member=member,
        session=session, game_lookup=game_lookup, lobby_lookup=lobby_lookup,
        bot=bot, delivery=delivery, payload={"owner_id": str(OWNER), "session_id": str(RUN)},
    )


async def finish(harness, status):
    harness.session.scalar.return_value = 1
    await harness.service.act(
        OWNER, RUN, "next" if status == "completed" else "stop",
        expected_version=harness.record.version, question_revision_id=QUESTION, now=NOW,
    )
    harness.session.scalar.return_value = None
    assert harness.record.status == status


async def test_last_revealed_frame_completion_delivers_home_menu_once(monkeypatch):
    harness = terminal_delivery_harness(monkeypatch)
    await harness.delivery(harness.payload)
    assert harness.bot.send_message.await_count == 2
    revealed = harness.bot.send_message.await_args.args[1]
    assert "Answer" in revealed and "Score: 10" in revealed
    await finish(harness, "completed")
    await harness.delivery(harness.payload)
    harness.bot.edit_message_text.assert_not_awaited()
    assert harness.bot.send_message.await_count == 3
    finished = harness.bot.send_message.await_args
    assert finished.args[1] == LocalizationService().text("training.finished", "en")
    assert finished.kwargs["reply_markup"].keyboard[0][0].text == "Start training"
    await harness.delivery(harness.payload)
    assert harness.bot.send_message.await_count == 3
    harness.bot.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("status", ["stopped", "completed"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("destination", [
    "normal_game", "normal_lobby", "normal_lobby_other", "player", "manager", "admin",
    "training_lobby", "new_training",
])
async def test_delayed_terminal_events_preserve_live_navigation_keyboard(
    monkeypatch, status, force, destination,
):
    harness = terminal_delivery_harness(monkeypatch)
    await harness.delivery(harness.payload)
    await finish(harness, status)
    if destination == "normal_game":
        harness.game_lookup.return_value = NavigationGame(
            OTHER, OTHER, "active", "reading", 1, True, True, False,
        )
    elif destination.startswith("normal_lobby"):
        harness.navigation.interaction_mode = "player"
        harness.navigation.player_context = (
            "lobby_other" if destination.endswith("other") else "lobby"
        )
        harness.lobby_lookup.return_value = NavigationLobby(OTHER, OTHER, "assembling", 1)
        harness.session.scalar.side_effect = cycle([
            None, None, SimpleNamespace(role="player", ready=False),
        ])
    elif destination == "training_lobby":
        harness.player.training_lobby = {"version": 1}
    elif destination == "new_training":
        harness.session.scalar.side_effect = cycle([None, OTHER])
    else:
        harness.navigation.interaction_mode = destination
    state = await harness.service.delivery(OWNER, RUN)
    assert state["show_menu"] is False
    harness.bot.send_message.reset_mock()
    await harness.delivery({**harness.payload, "force": force})
    harness.bot.send_message.assert_not_awaited()
    await harness.delivery(harness.payload)
    harness.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("status", ["stopped", "completed"])
async def test_terminal_menu_eligibility_change_is_not_hidden_by_unchanged_text(
    monkeypatch, status,
):
    harness = terminal_delivery_harness(monkeypatch)
    await harness.delivery(harness.payload)
    await finish(harness, status)
    harness.navigation.interaction_mode = "manager"
    await harness.delivery(harness.payload)
    count_before = harness.bot.send_message.await_count
    harness.navigation.interaction_mode = "training"
    await harness.delivery(harness.payload)
    assert harness.bot.send_message.await_count == count_before + 1
    assert harness.bot.send_message.await_args.kwargs["reply_markup"].keyboard[0][0].text == (
        "Start training"
    )
    await harness.delivery(harness.payload)
    assert harness.bot.send_message.await_count == count_before + 1


@pytest.mark.parametrize("status", ["stopped", "completed"])
async def test_retained_normal_game_does_not_block_training_terminal_menu(monkeypatch, status):
    harness = terminal_delivery_harness(monkeypatch)
    await harness.delivery(harness.payload)
    await finish(harness, status)
    harness.game_lookup.return_value = NavigationGame(
        OTHER, OTHER, "finalized", "finished", 1, True, True, False,
    )
    state = await harness.service.delivery(OWNER, RUN)
    assert state["show_menu"] is True
    await harness.delivery(harness.payload)
    assert harness.bot.send_message.await_count == 3
    assert harness.bot.send_message.await_args.kwargs["reply_markup"].keyboard[0][0].text == (
        "Start training"
    )
