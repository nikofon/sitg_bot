from uuid import uuid4

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as database_url
from test_lobby_architecture import tournament_fixture
from test_telegram_gameplay_integration import act, assigned_game, progress_until

from sitg_bot.application.contracts import ActionCode, ChatSendOperation
from sitg_bot.services.chat import ParticipantChatService
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.telegram_game import TelegramGameService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    OutboxEventRecord,
    PlayerRecord,
    TelegramGameViewRecord,
    TournamentPolicyVersionRecord,
)

pytestmark = pytest.mark.integration


def operation(scope, scope_id, message_id=123, **values):
    return ChatSendOperation(
        action=ActionCode.CHAT_SEND,
        scope=scope,
        scope_id=scope_id,
        message_id=message_id,
        text="Hello",
        **values,
    )


async def payloads(database, scope_id, topic="telegram.chat.message"):
    async with database.sessions() as session:
        return list(
            (
                await session.scalars(
                    select(OutboxEventRecord.payload).where(
                        OutboxEventRecord.aggregate_id == scope_id, OutboxEventRecord.topic == topic
                    )
                )
            ).all()
        )


async def test_lobby_broadcast_observer_whisper_and_membership_revalidation(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        async with database.transaction() as session:
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
                )
            )
            policy.policies = {**policy.policies, "observing": "unlimited"}
        matchmaking = InvitationMatchmakingService(database)
        lobby = await matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id
        )
        await matchmaking.join(lobby.invitation_code, fixture.inputs[1])
        await matchmaking.join(
            lobby.invitation_code, fixture.inputs[2], role="observer", confirm_fresh=True
        )
        service = ParticipantChatService(database)
        sender = fixture.inputs[0].telegram_user_id
        assert (await service.send(sender, operation("lobby", lobby.id)))["recipients"] == 2
        # Retried input does not enqueue duplicate deliveries.
        await service.send(sender, operation("lobby", lobby.id))
        messages = await payloads(database, lobby.id)
        assert len(messages) == 2
        assert {p["recipient_id"] for p in messages} == {str(p.id) for p in fixture.players[1:]}
        for payload in messages:
            assert not (await service.delivery(payload))["skip"]
        await service.send(
            sender, operation("lobby", lobby.id, 124, target=str(fixture.players[2].id))
        )
        whispers = [p for p in await payloads(database, lobby.id) if p["whisper"]]
        assert len(whispers) == 1 and whispers[0]["recipient_id"] == str(fixture.players[2].id)
        # Following album items retain the private target after frontend state loss.
        await service.send(
            sender,
            operation(
                "lobby", lobby.id, 130, target=str(fixture.players[2].id), media_group_id="album"
            ),
        )
        await ParticipantChatService(database).send(
            sender, operation("lobby", lobby.id, 131, media_group_id="album")
        )
        album = [p for p in await payloads(database, lobby.id) if p["media_group_id"] == "album"]
        assert len(album) == 2
        assert all(p["whisper"] and p["recipient_id"] == str(fixture.players[2].id) for p in album)
        async with database.transaction() as session:
            for p in fixture.players[1:]:
                record = await session.get(PlayerRecord, p.id)
                record.public_nickname = "Same name"
        with pytest.raises(ValueError):
            await service.send(sender, operation("lobby", lobby.id, 127, target="Same name"))
        for target in (str(uuid4()), str(fixture.players[0].id)):
            with pytest.raises(ValueError):
                await service.send(sender, operation("lobby", lobby.id, 125, target=target))
        with pytest.raises(PermissionError):
            await service.send(fixture.manager.telegram_user_id, operation("lobby", lobby.id))
        await matchmaking.leave(lobby.id, fixture.inputs[1].telegram_user_id)
        left = next(p for p in messages if p["recipient_id"] == str(fixture.players[1].id))
        assert (await service.delivery(left))["skip"]
        with pytest.raises(PermissionError):
            await service.send(fixture.inputs[1].telegram_user_id, operation("lobby", lobby.id))
    finally:
        await database.close()


async def test_game_chat_abandon_reconnect_cleanup_and_late_send(database_url):
    database = Database(database_url)
    try:
        fixture, game_id = await assigned_game(database, 2)
        games = TelegramGameService(database)
        for participant in fixture.inputs:
            await act(games, participant, game_id, "join")
        await progress_until(database, game_id, fixture.inputs[0], lambda v: "buzz" in v["actions"])
        service = ParticipantChatService(database)
        sender, recipient = fixture.inputs
        await service.send(sender.telegram_user_id, operation("game", game_id))
        payload = (await payloads(database, game_id))[0]
        assert not (await service.delivery(payload))["skip"]
        await service.delivery(payload, key="chat:header", message_id=801)
        await service.delivery(payload, key="chat:media", message_id=802)
        await act(games, recipient, game_id, "abandon")
        assert (await service.delivery(payload))["skip"]
        cleanup = await payloads(database, game_id, "telegram.game.cleanup")
        assert {801, 802} <= set(cleanup[-1]["message_ids"])
        with pytest.raises(PermissionError):
            await service.send(recipient.telegram_user_id, operation("game", game_id, 124))
        with pytest.raises(ValueError):
            await service.send(
                sender.telegram_user_id,
                operation("game", game_id, 125, target=str(fixture.players[1].id)),
            )
        # A Telegram send finishing after abandonment must get its own cleanup job.
        await service.delivery(payload, key="chat:late", message_id=803)
        assert any(
            p["message_ids"] == [803]
            for p in await payloads(database, game_id, "telegram.game.cleanup")
        )
        await act(games, recipient, game_id, "join")
        assert (await service.delivery(payload))["skip"]  # No old-session backlog on reconnect.
        await service.send(sender.telegram_user_id, operation("game", game_id, 126))
        fresh = next(p for p in await payloads(database, game_id) if p["source_message_id"] == 126)
        assert not (await service.delivery(fresh))["skip"]
        await service.delivery(fresh, key="chat:new", message_id=804)
        async with database.sessions() as session:
            cursor = await session.get(TelegramGameViewRecord, (game_id, fixture.players[1].id))
            assert cursor.messages["chat:new"]["ids"] == [804]
            assert "chat:late" not in cursor.messages
        await act(games, sender, game_id, "abandon")
        assert (await service.delivery(fresh))["skip"]
        cleanup = await payloads(database, game_id, "telegram.game.cleanup")
        assert any(123 in p["message_ids"] for p in cleanup)
        await progress_until(database, game_id, recipient, lambda v: v["status"] == "finalized")
        await act(games, recipient, game_id, "quit")
        assert (await service.delivery(fresh))["skip"]
        cleanup = await payloads(database, game_id, "telegram.game.cleanup")
        assert any(804 in p["message_ids"] for p in cleanup)
    finally:
        await database.close()


async def test_game_observers_send_receive_and_stop_on_departure(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        async with database.transaction() as session:
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
                )
            )
            policy.policies = {**policy.policies, "observing": "unlimited"}
        matchmaking = InvitationMatchmakingService(database)
        lobby = await matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=2
        )
        await matchmaking.join(lobby.invitation_code, fixture.inputs[1])
        await matchmaking.join(
            lobby.invitation_code, fixture.inputs[2], role="observer", confirm_fresh=True
        )
        await matchmaking.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        await matchmaking.set_role(
            lobby.id, fixture.inputs[2].telegram_user_id, "observer", confirm_fresh=True
        )
        for player in fixture.inputs[:2]:
            await matchmaking.set_ready(lobby.id, player.telegram_user_id)
        game_id = (await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)).game.id
        service = ParticipantChatService(database)
        observer = fixture.inputs[2].telegram_user_id
        assert (await service.send(observer, operation("game", game_id)))["recipients"] == 2
        await service.send(
            fixture.inputs[0].telegram_user_id,
            operation("game", game_id, 124, target=str(fixture.players[2].id)),
        )
        whisper = next(p for p in await payloads(database, game_id) if p["whisper"])
        assert not (await service.delivery(whisper))["skip"]
        await PersistentGameService(database).stop_observing(game_id, observer)
        assert (await service.delivery(whisper))["skip"]
        with pytest.raises(PermissionError):
            await service.send(observer, operation("game", game_id, 125))
    finally:
        await database.close()
