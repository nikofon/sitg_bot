"""Run explicitly with TEST_DATABASE_URL; no Telegram credentials are needed."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.application.contracts import ActionCode, GameActOperation
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.telegram_game import TelegramGameService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    GameRecord,
    GameThemeRecord,
    OutboxEventRecord,
    PlayerRecord,
    RatingLedgerRecord,
    TelegramGameViewRecord,
    ThemeRevisionRecord,
    TournamentMembershipRecord,
    TournamentPolicyVersionRecord,
)

pytestmark = pytest.mark.integration


async def test_abandon_dismisses_only_leaving_players_and_survives_restart(database_url):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 2)
        service = TelegramGameService(database)
        for player in fixture.inputs:
            await act(service, player, game_id, "join")
        await progress_until(database, game_id, fixture.inputs[0], lambda v: "buzz" in v["actions"])
        await service.record_delivery(
            fixture.inputs[0].telegram_user_id, game_id, key="question", value={"ids": [321]}
        )
        await act(service, fixture.inputs[0], game_id, "abandon")
        restarted = TelegramGameService(database)
        assert (await restarted.delivery(fixture.inputs[0].telegram_user_id, game_id))["skip"]
        assert not (await restarted.delivery(fixture.inputs[1].telegram_user_id, game_id))["skip"]
        with pytest.raises(LookupError):
            await restarted.view(fixture.inputs[0].telegram_user_id)
        with pytest.raises(PermissionError):
            await act(restarted, fixture.inputs[0], game_id, "buzz")
        async with database.sessions() as session:
            assert (
                await TelegramNavigationService._active_game(session, fixture.players[0].id) is None
            )
            assert (
                await TelegramNavigationService._active_game(session, fixture.players[1].id)
                is not None
            )
        # Explicit reconnection uses join and starts a fresh presentation ledger.
        reconnect = await restarted.view(fixture.inputs[0].telegram_user_id, reconnect=True)
        assert reconnect["id"] == str(game_id)
        assert (await act(restarted, fixture.inputs[0], game_id, "join"))["accepted"]
        assert await restarted.presentation(fixture.inputs[0].telegram_user_id, game_id) == {
            "messages": {}, "dismissed": False
        }
        await act(restarted, fixture.inputs[0], game_id, "abandon")
        # Finishing the old game must not pull an abandoned player back into it.
        await progress_until(
            database, game_id, fixture.inputs[1], lambda v: v["status"] == "finalized"
        )
        with pytest.raises(LookupError):
            await restarted.view(fixture.inputs[0].telegram_user_id)
        with pytest.raises(PermissionError):
            await act(
                restarted,
                fixture.inputs[0],
                game_id,
                "reputation",
                target_id=fixture.players[1].id,
                approve=True,
            )
    finally:
        await database.close()


async def assigned_game(database, count, *, escalation=False):
    fixture = await tournament_fixture(database, player_count=count)
    if escalation:
        async with database.transaction() as session:
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
                )
            )
            policy.policies = {**policy.policies, "appeal_escalation_enabled": True}
    matchmaking = InvitationMatchmakingService(database)
    lobby = await matchmaking.create_lobby(
        fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=count
    )
    for participant in fixture.inputs[1:]:
        await matchmaking.join(lobby.invitation_code, participant)
    await matchmaking.select_packet(lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id)
    for participant in fixture.inputs:
        await matchmaking.set_ready(lobby.id, participant.telegram_user_id)
    result = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
    return fixture, result.game.id


@pytest.mark.parametrize("rating_enabled", [None, False])
async def test_ladder_default_rating_settlement(database_url, rating_enabled):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 2)
        async with database.transaction() as session:
            game = await session.get(GameRecord, game_id)
            policy = await session.get(
                TournamentPolicyVersionRecord, game.tournament_policy_version_id
            )
            policies = dict(policy.policies)
            policies.pop("rating_enabled", None)
            if rating_enabled is not None:
                policies["rating_enabled"] = rating_enabled
            policy.policies = policies
        service = TelegramGameService(database)
        for player in fixture.inputs:
            await act(service, player, game_id, "join")
        player = fixture.inputs[0]
        view = await progress_until(database, game_id, player, lambda v: "buzz" in v["actions"])
        round_id = view["question"]["round_id"]
        await act(service, player, game_id, "buzz", round_id=round_id)
        await act(service, player, game_id, "answer", round_id=round_id, text="answer 10")
        result = await progress_until(
            database, game_id, player, lambda v: v["status"] == "finalized"
        )
        changes = result["rating_changes"]
        assert len([c for c in changes if c["scope"] == "ruleset"]) == 2
        local = [c for c in changes if c["scope"] == "tournament"]
        assert len(local) == (2 if rating_enabled is None else 0)
        async with database.sessions() as session:
            ledger = list(await session.scalars(
                select(RatingLedgerRecord).where(RatingLedgerRecord.game_id == game_id)
            ))
            assert len(ledger) == len(local)
            for row in ledger:
                membership = await session.get(
                    TournamentMembershipRecord, (fixture.tournament_id, row.player_id)
                )
                assert row.delta != 0
                assert membership.rating == row.rating_after == row.rating_before + row.delta
        await PersistentGameService(database).settle_pending_ratings()
        assert (await service.view(player.telegram_user_id, game_id))["rating_changes"] == changes
    finally:
        await database.close()


@pytest.mark.parametrize("approve_second", [True, False])
async def test_second_wrong_answer_can_be_appealed_after_rejection_during_pause(
    database_url, approve_second
):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 2)
        service = TelegramGameService(database)
        games = PersistentGameService(database)
        first, second = fixture.inputs
        for player in fixture.inputs:
            await act(service, player, game_id, "join")
        view = await progress_until(database, game_id, first, lambda v: "buzz" in v["actions"])
        round_id = view["question"]["round_id"]
        for player in fixture.inputs:
            await act(service, player, game_id, "buzz", round_id=round_id)
            await act(service, player, game_id, "answer", round_id=round_id, text="wrong")
        await act(service, first, game_id, "pause", round_id=round_id)
        await act(service, first, game_id, "appeal", round_id=round_id)
        voting = await service.view(first.telegram_user_id, game_id)
        first_appeal = voting["appeal"]["id"]
        assert "appeal" not in (await service.view(second.telegram_user_id, game_id))["actions"]
        await act(service, second, game_id, "vote", appeal_id=first_appeal, approve=False)
        assert "appeal" not in (await service.view(first.telegram_user_id, game_id))["actions"]
        with pytest.raises(ValueError, match="no answer"):
            await games.submit_appeal(
                game_id, first.telegram_user_id, {p.telegram_user_id for p in fixture.inputs}
            )
        # An expired progression deadline cannot close the window during a manual pause.
        async with database.transaction() as session:
            game = await session.get(GameRecord, game_id)
            game.progression_deadline = datetime.now(UTC) - timedelta(seconds=1)
        assert not (await games.progress_due(game_id)).accepted
        view = await service.view(second.telegram_user_id, game_id)
        assert view["paused"] and "appeal" in view["actions"]
        assert len(view["appeal_targets"]) == 1
        assert view["appeal_targets"][0]["player_id"] == str(fixture.players[1].id)
        await act(service, second, game_id, "appeal", round_id=round_id)
        view = await TelegramGameService(database).view(second.telegram_user_id, game_id)
        second_appeal = view["appeal"]["id"]
        assert second_appeal != first_appeal
        await act(service, first, game_id, "vote", appeal_id=second_appeal, approve=approve_second)
        if approve_second:
            await act(service, second, game_id, "vote", appeal_id=second_appeal, approve=True)
        view = await service.view(second.telegram_user_id, game_id)
        assert view["paused"]
        assert "appeal" not in view["actions"]
        scores = {p["id"]: p["score"] for p in view["participants"]}
        assert scores[str(fixture.players[0].id)] == -10
        assert scores[str(fixture.players[1].id)] == (10 if approve_second else -10)
    finally:
        await database.close()


async def test_rejected_appeal_escalates_and_manager_corrects_completed_game(database_url):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 1, escalation=True)
        service = TelegramGameService(database)
        player = fixture.inputs[0]
        await act(service, player, game_id, "join")
        view = await progress_until(database, game_id, player, lambda v: "buzz" in v["actions"])
        round_id = view["question"]["round_id"]
        await act(service, player, game_id, "buzz", round_id=round_id)
        await act(service, player, game_id, "answer", round_id=round_id, text="wrong")
        await act(service, player, game_id, "appeal", round_id=round_id)
        appeal_id = (await service.view(player.telegram_user_id, game_id))["appeal"]["id"]
        await act(service, player, game_id, "vote", appeal_id=appeal_id, approve=False)
        assert "escalate" in (await service.view(player.telegram_user_id, game_id))["actions"]
        await act(service, player, game_id, "escalate", appeal_id=appeal_id, approve=True)
        await act(service, player, game_id, "commentary", appeal_id=appeal_id, text="Please review")
        await progress_until(database, game_id, player, lambda v: v["status"] == "completed")
        games = PersistentGameService(database)
        tickets = await games.manager_appeal_tickets(fixture.manager.id)
        assert len(tickets) == 1
        assert tickets[0].commentary == "Please review"
        assert not hasattr(tickets[0], "player_id")
        with pytest.raises(PermissionError):
            await games.decide_escalated_appeal(tickets[0].id, fixture.players[0].id, approve=True)
        await games.decide_escalated_appeal(tickets[0].id, fixture.manager.id, approve=True)
        final = await service.view(player.telegram_user_id, game_id)
        assert final["status"] == "finalized"
        assert final["participants"][0]["score"] == 10
        assert final["participants"][0]["correct_points"] == 10
        with pytest.raises(ValueError):
            await games.decide_escalated_appeal(tickets[0].id, fixture.manager.id, approve=False)
    finally:
        await database.close()


async def act(service, player, game_id, command, **values):
    return await service.act(
        player.telegram_user_id,
        GameActOperation(action=ActionCode.GAME_ACT, game_id=game_id, command=command, **values),
    )


async def progress_until(database, game_id, player, predicate):
    service = TelegramGameService(database)
    games = PersistentGameService(database)
    for _ in range(150):
        view = await service.view(player.telegram_user_id, game_id)
        if predicate(view):
            return view
        async with database.transaction() as session:
            game = await session.get(GameRecord, game_id, with_for_update=True)
            for field in ("progression_deadline", "buzz_deadline", "answer_deadline"):
                if getattr(game, field) is not None:
                    setattr(game, field, datetime.now(UTC) - timedelta(seconds=1))
        await games.progress_due(game_id)
    pytest.fail("Game did not reach the requested state")


async def test_native_si_join_answer_appeal_pause_finish_and_recovery(database_url):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 1)
        player = fixture.inputs[0]
        service = TelegramGameService(database)
        initial = await service.view(player.telegram_user_id, game_id)
        assert "join" in initial["actions"]
        assert "themes" not in initial["actions"]
        assert initial["themes"] == []
        await act(service, player, game_id, "join")
        question = await progress_until(database, game_id, player, lambda v: "buzz" in v["actions"])
        assert "themes" in question["actions"]
        assert question["themes"][0]["position"] == 1
        assert question["themes"][0]["name"]
        round_id = question["question"]["round_id"]
        assert question["question"]["revealed_answer"] is None
        assert question["question"]["form"] is None
        with pytest.raises(StaleWriteError):
            await act(service, player, game_id, "buzz", round_id=uuid4())
        await act(service, player, game_id, "buzz", round_id=round_id)
        answering = await service.view(player.telegram_user_id, game_id)
        assert answering["question"]["form"] == "ANSWER"
        await act(service, player, game_id, "answer", round_id=round_id, text="incorrect answer")
        after = await service.view(player.telegram_user_id, game_id)
        assert after["participants"][0]["score"] == -10
        assert "appeal" in after["actions"]
        await act(service, player, game_id, "pause", round_id=round_id)
        assert "resume" in (await service.view(player.telegram_user_id, game_id))["actions"]
        await act(service, player, game_id, "resume", round_id=round_id)
        await act(
            service,
            player,
            game_id,
            "appeal",
            round_id=round_id,
            target_id=after["appeal_targets"][0]["id"],
        )
        voting = await service.view(player.telegram_user_id, game_id)
        assert "vote" in voting["actions"]
        assert "resume" not in voting["actions"]
        await act(service, player, game_id, "vote", appeal_id=voting["appeal"]["id"], approve=True)
        corrected = await service.view(player.telegram_user_id, game_id)
        assert corrected["participants"][0]["score"] == 10
        assert corrected["participants"][0]["correct_points"] == 10
        final = await progress_until(
            database, game_id, player, lambda v: v["status"] == "finalized"
        )
        assert final["participants"][0]["place"] == 1
        recovered = await TelegramGameService(database).view(player.telegram_user_id)
        assert recovered["id"] == str(game_id)
        assert recovered["status"] == "finalized"
        delivery = await service.delivery(player.telegram_user_id, game_id)
        assert any(n["kind"] == "question_completed" for n in delivery["events"])
        kinds = [e["kind"] for e in delivery["events"]]
        assert kinds.index("question_cost_announced") < kinds.index("question_token_revealed")
        buzz = next(e for e in delivery["events"] if e["kind"] == "player_buzzed")
        assert buzz["parameters"]["round_id"] == str(round_id)
        assert buzz["parameters"]["mine"]
        assert buzz["parameters"]["name"] == question["participants"][0]["name"]
        assert buzz["parameters"]["form"] == "ANSWER"
        await service.record_delivery(
            player.telegram_user_id,
            game_id,
            sequence=final["last_sequence"],
            key="prompt",
            value={"ids": [123], "kind": "answer", "round_id": str(round_id)},
        )
        restarted = TelegramGameService(database)
        restored = await restarted.delivery(player.telegram_user_id, game_id)
        assert restored["messages"]["prompt"]["ids"] == [123]
        assert restored["events"] == []
        navigation = TelegramNavigationService(database)
        before_exit = await navigation.snapshot(player.telegram_user_id)
        await navigation.set_mode(
            player.telegram_user_id, "manager", expected_version=before_exit.navigation_version
        )
        await act(service, player, game_id, "quit")
        after_exit = await navigation.snapshot(player.telegram_user_id)
        assert after_exit.context == "menu"
        assert after_exit.active_mode == "manager"
        assert (await restarted.delivery(player.telegram_user_id, game_id))["skip"]
        assert (await restarted.presentation(player.telegram_user_id, game_id))["dismissed"]
        with pytest.raises(LookupError):
            await restarted.view(player.telegram_user_id)
        async with database.sessions() as session:
            assert (
                await TelegramNavigationService._active_game(session, fixture.players[0].id) is None
            )
            cleanup = await session.scalar(
                select(OutboxEventRecord).where(
                    OutboxEventRecord.aggregate_id == game_id,
                    OutboxEventRecord.topic == "telegram.game.cleanup",
                )
            )
            assert cleanup.payload["recipient_telegram_user_id"] == player.telegram_user_id
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(OutboxEventRecord.id)
                    .where(
                        OutboxEventRecord.aggregate_id == game_id,
                        OutboxEventRecord.topic == "game.event",
                    )
                    .limit(1)
                )
                is not None
            )
    finally:
        await database.close()


async def test_simultaneous_buzzes_privacy_reputation_and_duplicate_reports(database_url):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 2)
        service = TelegramGameService(database)
        for player in fixture.inputs:
            await act(service, player, game_id, "join")
        view = await progress_until(
            database, game_id, fixture.inputs[0], lambda v: "buzz" in v["actions"]
        )
        round_id = view["question"]["round_id"]
        results = await asyncio.gather(
            *(
                act(service, player, game_id, "buzz", round_id=round_id)
                for player in fixture.inputs
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(r, dict) and r["accepted"] for r in results) == 1
        async with database.transaction() as session:
            private = await session.get(PlayerRecord, fixture.players[1].id)
            private.real_name = "DO NOT DISCLOSE"
            private.telegram_username = "private_username"
            private.telegram_public = False
        private_view = await service.view(fixture.inputs[0].telegram_user_id, game_id)
        encoded = json.dumps(private_view, default=str)
        assert "DO NOT DISCLOSE" not in encoded
        assert "private_username" not in encoded
        assert '"telegram_user_id"' not in encoded
        with pytest.raises(PermissionError):
            await service.view(fixture.manager.telegram_user_id, game_id)
        await progress_until(
            database,
            game_id,
            fixture.inputs[0],
            lambda v: v["status"] in {"completed", "finalized"},
        )
        target_id = fixture.players[1].id
        await act(
            service, fixture.inputs[0], game_id, "reputation", target_id=target_id, approve=True
        )
        result = await service.view(fixture.inputs[0].telegram_user_id, game_id)
        assert result["participants"][1]["vote_reason"] == "voted"
        with pytest.raises(ValueError):
            await act(
                service,
                fixture.inputs[0],
                game_id,
                "reputation",
                target_id=target_id,
                approve=False,
            )
        report = await act(
            service,
            fixture.inputs[0],
            game_id,
            "report",
            target_id=target_id,
            report_kind="toxicity",
            text="test report",
        )
        assert report["receipt"]["report_id"]
        with pytest.raises(ValueError):
            await act(
                service,
                fixture.inputs[0],
                game_id,
                "report",
                target_id=target_id,
                report_kind="cheating",
            )
        async with database.sessions() as session:
            assert await session.get(TelegramGameViewRecord, (game_id, fixture.players[0].id))
    finally:
        await database.close()


async def test_theme_commentary_is_announced_between_theme_and_first_question(database_url):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 2)
        async with database.transaction() as session:
            theme = await session.scalar(
                select(ThemeRevisionRecord)
                .join(GameThemeRecord, GameThemeRecord.theme_revision_id == ThemeRevisionRecord.id)
                .where(GameThemeRecord.game_id == game_id)
            )
            assert theme is not None
            theme.commentary = "Integration theme commentary"
        service = TelegramGameService(database)
        for player in fixture.inputs:
            await act(service, player, game_id, "join")
        await progress_until(database, game_id, fixture.inputs[0], lambda v: "buzz" in v["actions"])
        events = await PersistentGameService(database).events(game_id)
        kinds = [event["kind"] for event in events]
        assert "theme_commentary_announced" in kinds
        started = kinds.index("theme_started")
        commentary = kinds.index("theme_commentary_announced")
        cost = kinds.index("question_cost_announced")
        assert started < commentary < cost
        payload = events[commentary]["payload"]
        assert payload["commentary"] == "Integration theme commentary"
        assert payload["name"] == "Theme"
    finally:
        await database.close()
