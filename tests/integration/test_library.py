import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.services.library import PacketLibraryService
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    OutboxEventRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def configure(database, fixture, *, level="anytime", readable=True, released=True):
    async with database.transaction() as session:
        assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
            TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
        ))
        assignment.content_visible_by_members = readable
        assignment.library_viewing_rule = level
        version = await session.get(PacketVersionRecord, assignment.adopted_version_id)
        version.library_released_at = datetime.now(UTC) if released else None
        return assignment.id, version.id


async def claims_for(database, player_id, version_id=None):
    async with database.sessions() as session:
        query = select(PlayerExposureClaimRecord).where(
            PlayerExposureClaimRecord.player_id == player_id,
            PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
        )
        if version_id is not None:
            query = query.where(PlayerExposureClaimRecord.packet_version_id == version_id)
        return list(await session.scalars(query))


@pytest.mark.parametrize("download", [False, True])
@pytest.mark.parametrize("level,released", [
    ("never", True), ("after-play", True),
    ("anytime", False),
])
async def test_cards_are_listed_but_actions_check_release_and_view_rules(
    database_url, level, released, download,
):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, fixture, level=level, released=released)
        library = PacketLibraryService(database)
        player_id = fixture.players[0].id
        listed = await library.list_packets(player_id)
        assert [item["version_id"] for item in listed["items"]] == [str(version_id)]
        card = listed["items"][0]
        assert card["fresh_play_unit_count"] == card["total_play_unit_count"] == 1
        assert "pages" not in card
        with pytest.raises(PermissionError):
            await library.access(player_id, version_id, confirm=True, download=download,
                                 request_key=str(uuid4()))
        assert not await claims_for(database, player_id)
    finally:
        await database.close()


@pytest.mark.parametrize("level", ["never", "after-play", "anytime"])
async def test_manager_ignores_release_readability_and_view_rules(database_url, level):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1, finalized=False)
        _, version_id = await configure(
            database, fixture, level=level, readable=False, released=False
        )
        library = PacketLibraryService(database)
        listed = await library.list_packets(fixture.manager.id)
        assert listed["items"][0]["version_id"] == str(version_id)
        assert await TournamentService(database).can_read_packet_library(
            fixture.tournament_id, fixture.packet_id, fixture.manager.id
        )
        assert not (await library.list_packets(fixture.players[0].id))["items"]
        result = await library.access(
            fixture.manager.id, version_id, confirm=True, request_key="view"
        )
        assert result["pages"][0]["questions"][0]["answer"] == "answer 10"
    finally:
        await database.close()


async def test_confirmation_burns_all_claims_and_download_is_durable_and_idempotent(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, fixture)
        player_id = fixture.players[0].id
        library = PacketLibraryService(database)
        for download in (False, True):
            warning = await library.access(
                player_id, version_id, download=download, request_key="probe"
            )
            assert warning == {"confirmation_required": True, "fresh_unit_count": 1}
        assert not await claims_for(database, player_id)
        results = await asyncio.gather(*[
            library.access(player_id, version_id, confirm=True, request_key=f"read-{i}")
            for i in range(2)
        ])
        assert all(result["pages"] for result in results)
        claims = await claims_for(database, player_id)
        assert len(claims) == 6  # One logical SI theme and its five questions.
        assert all(claim.state == "burnt" and claim.game_id is None for claim in claims)
        async with database.sessions() as session:
            assert not await SIContentAdapter().available_play_units(
                session, [PacketSelection(version_id, 0)], [player_id]
            )
        for _ in range(2):
            result = await library.access(
                player_id, version_id, download=True, request_key="download"
            )
            assert result == {"confirmation_required": False, "queued": True}
        async with database.sessions() as session:
            deliveries = list(await session.scalars(select(OutboxEventRecord).where(
                OutboxEventRecord.deduplication_key == f"library:{player_id}:download"
            )))
            assert len(deliveries) == 1
            assert deliveries[0].topic == "telegram.library.document"
            assert deliveries[0].payload["recipient_telegram_user_id"] == (
                fixture.inputs[0].telegram_user_id
            )
            assert deliveries[0].payload["pages"] == results[0]["pages"]
    finally:
        await database.close()


@pytest.mark.parametrize(
    "revocation", ["readable", "membership", "release", "assignment", "manager"]
)
async def test_confirmation_rechecks_authorization(database_url, revocation):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        assignment_id, version_id = await configure(database, fixture)
        player_id = fixture.manager.id if revocation == "manager" else fixture.players[0].id
        if revocation == "manager":
            # Publication burns every manager of the destination tournament, so
            # this case reads a packet that reached the tournament without a
            # publication burn and is still fresh for its managers.
            other = await tournament_fixture(database, player_count=1)
            async with database.transaction() as session:
                foreign = await session.scalar(
                    select(TournamentPacketAssignmentRecord).where(
                        TournamentPacketAssignmentRecord.tournament_id == other.tournament_id,
                        TournamentPacketAssignmentRecord.packet_id == other.packet_id,
                    )
                )
                version_id = foreign.adopted_version_id
                session.add(
                    TournamentPacketAssignmentRecord(
                        tournament_id=fixture.tournament_id,
                        packet_id=other.packet_id,
                        adopted_version_id=version_id,
                        playable_by_members=True,
                    )
                )
        library = PacketLibraryService(database)
        warning = await library.access(player_id, version_id, request_key="probe")
        assert warning["confirmation_required"]
        async with database.transaction() as session:
            if revocation == "membership":
                row = await session.get(
                    TournamentMembershipRecord, (fixture.tournament_id, player_id)
                )
                row.status = "suspended"
            elif revocation == "manager":
                row = await session.get(TournamentManagerRecord, (fixture.tournament_id, player_id))
                row.revoked_at = datetime.now(UTC)
            elif revocation == "release":
                row = await session.get(PacketVersionRecord, version_id)
                row.library_released_at = None
            else:
                row = await session.get(TournamentPacketAssignmentRecord, assignment_id)
                if revocation == "readable":
                    row.content_visible_by_members = False
                else:
                    row.status = "retired"
        with pytest.raises(PermissionError):
            await library.access(player_id, version_id, confirm=True, request_key="confirmed")
        assert not await claims_for(database, player_id, version_id)
    finally:
        await database.close()


@pytest.mark.parametrize("level", ["after-play", "anytime"])
async def test_reading_requires_disclosure_and_preserves_game_provenance(database_url, level):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, fixture, level=level)
        player_id = fixture.players[0].id
        library = PacketLibraryService(database)
        matching = InvitationMatchmakingService(database)
        lobby = await matching.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
        )
        await matching.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        await matching.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        started = await matching.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert started.started
        with pytest.raises(PermissionError):
            await library.access(player_id, version_id, confirm=True, request_key="reserved")
        games = PersistentGameService(database)
        await games.join(started.game.id, fixture.inputs[0].telegram_user_id)
        await games.advance(started.game.id)
        result = await library.access(player_id, version_id, request_key="after-play")
        assert result["confirmation_required"] is False
        assert all(
            claim.game_id == started.game.id for claim in await claims_for(database, player_id)
        )
        async with database.transaction() as session:
            game = await games._locked_game(session, started.game.id)
            await games._finish_abandoned_game(session, game, reason="test cleanup")
    finally:
        await database.close()


async def test_partial_exposure_still_requires_confirmation(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, fixture)
        async with database.transaction() as session:
            theme = await session.scalar(select(ThemeRevisionRecord).where(
                ThemeRevisionRecord.packet_version_id == version_id
            ))
            session.add(PlayerExposureClaimRecord(
                player_id=fixture.players[0].id, packet_version_id=version_id,
                claim_namespace="theme", claim_id=theme.theme_id,
                state="burnt", burnt_at=datetime.now(UTC),
            ))
        library = PacketLibraryService(database)
        card = (await library.list_packets(fixture.players[0].id))["items"][0]
        assert card["fresh_play_unit_count"] == 0
        assert card["total_play_unit_count"] == 1
        result = await library.access(
            fixture.players[0].id, version_id, request_key="partial"
        )
        assert result == {"confirmation_required": True, "fresh_unit_count": 1}
        assert len(await claims_for(database, fixture.players[0].id)) == 1
    finally:
        await database.close()


async def test_shared_version_is_grouped_without_leaking_private_tournaments(database_url):
    database = Database(database_url)
    try:
        first = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        assignment_id, version_id = await configure(database, first, readable=False)
        tournaments = TournamentService(database)
        await tournaments.set_packet_entitlement(
            assignment_id, first.players[0].id, first.manager.id,
            content_visible=True,
        )
        await tournaments.assign_packet(other.tournament_id, first.packet_id, other.manager.id,
                                        adopted_version_id=version_id, content_visible=True,
                                        library_viewing_rule="anytime")
        library = PacketLibraryService(database)
        card = (await library.list_packets(first.players[0].id))["items"][0]
        assert [t["id"] for t in card["tournaments"]] == [str(first.tournament_id)]
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, other.tournament_id)
            tournament.visibility = "public"
        listed = await library.list_packets(first.players[0].id)
        assert len(listed["items"]) == 1
        assert {t["id"] for t in listed["items"][0]["tournaments"]} == {
            str(first.tournament_id), str(other.tournament_id),
        }
        with pytest.raises(PermissionError):
            await library.access(
                other.players[0].id, UUID(int=0), confirm=True, request_key="forged"
            )
        async with database.transaction() as session:
            entitlement = await session.get(TournamentPacketEntitlementRecord,
                                            (assignment_id, first.players[0].id))
            entitlement.revoked_at = datetime.now(UTC)
        assert not (await library.list_packets(first.players[0].id))["items"]
    finally:
        await database.close()


async def test_manager_cannot_read_packets_of_another_tournament(database_url):
    database = Database(database_url)
    try:
        first = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, other)
        library = PacketLibraryService(database)
        for player_id in (first.manager.id, first.players[0].id):
            with pytest.raises(PermissionError):
                await library.access(player_id, version_id, confirm=True, request_key="cross-scope")
    finally:
        await database.close()


async def test_failed_download_enqueue_rolls_back_exposure(database_url, monkeypatch):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, fixture)
        monkeypatch.setattr(TransactionalOutbox, "enqueue", AsyncMock(side_effect=RuntimeError))
        with pytest.raises(RuntimeError):
            await PacketLibraryService(database).access(
                fixture.players[0].id, version_id, confirm=True, download=True, request_key="fail"
            )
        assert not await claims_for(database, fixture.players[0].id)
    finally:
        await database.close()
