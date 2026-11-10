import json
from dataclasses import asdict, replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as database_url
from test_lobby_architecture import packet, tournament_fixture
from test_packet_management import edit, save

from sitg_bot.services.library import PacketLibraryService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.testers import TesterService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    OutboxEventRecord,
    PlatformAdministratorRecord,
    PlayerExposureClaimRecord,
    TesterRecord,
    TournamentPacketAssignmentRecord,
)

pytestmark = pytest.mark.integration


async def upload(service, fixture, content):
    result = await service.import_upload(
        json.dumps(asdict(content)).encode(),
        source_filename="testers.json",
        uploader_id=fixture.manager.id,
        tournament_id=fixture.tournament_id,
    )
    return UUID(result["draft_id"])


async def test_namesake_resolution_late_link_future_exposure_and_private_library(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        player, stranger = fixture.players
        service = PacketAdminService(database)
        testers = TesterService(database)
        name = f"First Tester {uuid4().hex}"
        async with database.transaction() as session:
            session.add_all(
                [
                    TesterRecord(display_name=name),
                    PlatformAdministratorRecord(player_id=fixture.manager.id),
                ]
            )
        content = replace(packet(), testers=(name,))
        draft_id = await upload(service, fixture, content)
        created = await service.create_tester(
            draft_id,
            fixture.manager.id,
            first_name="First",
            second_name="Tester",
            surname=name.split()[-1],
            telegram_link="https://t.me/tester_identity",
        )
        assert created["display_name"] == name
        async with database.sessions() as session:
            namesake = await session.get(TesterRecord, UUID(created["tester_id"]))
        with pytest.raises(PermissionError):
            await testers.search(stranger.id, draft_id=draft_id)
        with pytest.raises(PermissionError):
            await service.create_tester(
                draft_id,
                stranger.id,
                first_name="Not",
                surname="Allowed",
                second_name=None,
                telegram_link=None,
            )
        updated = await service.update_draft(
            draft_id,
            fixture.manager.id,
            expected_version=1,
            content=asdict(content),
            tester_bindings={"0": namesake.id},
        )
        assert updated["tester_bindings"] == {"0": str(namesake.id)}
        again = await service.update_draft(
            draft_id,
            fixture.manager.id,
            expected_version=2,
            content=asdict(content),
        )
        assert again["tester_bindings"] == updated["tester_bindings"]
        # Direct Telegram publication uses persisted identity, not the first namesake.
        stored = await service.publish(draft_id, administrator_id=fixture.manager.id)
        assert stored.packet.testers == (name,)
        request = await testers.create_request(player.id, namesake.id)
        assert (await testers.create_request(player.id, namesake.id))["request_id"] == request[
            "request_id"
        ]
        with pytest.raises(PermissionError):
            await testers.decide_request(UUID(request["request_id"]), stranger.id, approve=True)
        await testers.decide_request(UUID(request["request_id"]), fixture.manager.id, approve=True)
        async with database.sessions() as session:
            claims = list(
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == player.id
                    )
                )
            )
            assert len(claims) == 6 and all(c.state == "burnt" for c in claims)
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.packet_id == stored.logical_id
                )
            )
        editor = await edit(service, fixture, assignment)
        editor["packet"]["testers"] = ()
        editor["packet"]["themes"][0]["name"] = "Replacement theme"
        await save(
            service,
            fixture,
            assignment,
            editor,
            {
                "testers": "correction",
                "themes.0.name": "substitution",
            },
        )
        async with database.sessions() as session:
            claims = list(
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == player.id
                    )
                )
            )
            assert len(claims) == 12 and all(c.state == "burnt" for c in claims)
        # Historical testers also receive the replacement content when linking later.
        late = await testers.create_request(stranger.id, namesake.id)
        await testers.decide_request(UUID(late["request_id"]), fixture.manager.id, approve=True)
        async with database.sessions() as session:
            assert (
                len(
                    list(
                        await session.scalars(
                            select(PlayerExposureClaimRecord).where(
                                PlayerExposureClaimRecord.player_id == stranger.id
                            )
                        )
                    )
                )
                == 12
            )
        matches = await testers.search(player.id, name)
        matched = next(t for t in matches["items"] if t["tester_id"] == str(namesake.id))
        assert matched["packets"] == [
            {"packet_id": str(stored.logical_id), "name": stored.packet.name}
        ]
        own = await testers.mine(player.id)
        assert own["items"][0]["tester_id"] == str(namesake.id)
        assert own["items"][0]["packets"] == [
            {"packet_id": str(stored.logical_id), "name": stored.packet.name}
        ]
        assert not (await testers.mine(fixture.manager.id))["items"]
        with pytest.raises(ValueError):
            await testers.search(player.id)
        with pytest.raises(PermissionError):
            await testers.requests(player.id, admin=True)
        with pytest.raises(LookupError):
            await testers.cancel_request(UUID(request["request_id"]), stranger.id)
        view = await PacketLibraryService(database).access(
            fixture.manager.id,
            stored.version_id,
            confirm=True,
            request_key=uuid4().hex,
            administrator=True,
        )
        assert view["testers"] == [name]
        assert "tester_bindings" not in view
        await PacketLibraryService(database).access(
            fixture.manager.id,
            stored.version_id,
            confirm=True,
            download=True,
            request_key=uuid4().hex,
            administrator=True,
        )
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEventRecord).where(
                    OutboxEventRecord.topic == "telegram.library.document",
                    OutboxEventRecord.payload["name"].astext == stored.packet.name,
                )
            )
            assert event.payload["testers"] == [name]
    finally:
        await database.close()


async def test_namesake_slots_survive_publication_and_unrelated_correction(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        surname = uuid4().hex
        name = f"First {surname}"
        async with database.transaction() as session:
            first = TesterRecord(display_name=name)
            second = TesterRecord(display_name=name)
            session.add_all([first, second])
        content = replace(packet(), testers=(name, f"{surname} First"))
        draft_id = await upload(service, fixture, content)
        bindings = {"0": first.id, "1": second.id}
        await service.update_draft(
            draft_id,
            fixture.manager.id,
            expected_version=1,
            content=asdict(content),
            tester_bindings=bindings,
        )
        stored = await service.publish(draft_id, administrator_id=fixture.manager.id)
        assert stored.packet.testers == (name, name)
        async with database.sessions() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.packet_id == stored.logical_id
                )
            )
        editor = await edit(service, fixture, assignment)
        expected = {slot: str(identity) for slot, identity in bindings.items()}
        assert editor["tester_bindings"] == expected
        editor["packet"]["name"] += " corrected"
        await save(service, fixture, assignment, editor, {"name": "correction"})
        corrected = await edit(service, fixture, assignment)
        assert corrected["packet"]["testers"] == (name, name)
        assert corrected["tester_bindings"] == expected
    finally:
        await database.close()


async def test_new_membership_backfills_branches_without_exposing_hidden_hints(database_url):
    database = Database(database_url)
    try:
        original = await tournament_fixture(database, player_count=1)
        branch = await tournament_fixture(database, player_count=1)
        player = original.players[0]
        service = PacketAdminService(database)
        testers = TesterService(database)
        async with database.transaction() as session:
            tester = TesterRecord(display_name=f"Tester {uuid4().hex}")
            session.add_all([tester, PlatformAdministratorRecord(player_id=original.manager.id)])
        request = await testers.create_request(player.id, tester.id)
        await testers.decide_request(UUID(request["request_id"]), original.manager.id, approve=True)
        draft_id = await upload(service, original, packet())
        stored = await service.publish(draft_id, administrator_id=original.manager.id)
        branch_assignment_id = await service.tournaments.assign_packet(
            branch.tournament_id,
            stored.logical_id,
            branch.manager.id,
            adopted_version_id=stored.version_id,
            discoverable=False,
        )
        async with database.sessions() as session:
            branch_assignment = await session.get(
                TournamentPacketAssignmentRecord, branch_assignment_id
            )
            original_assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.packet_id == stored.logical_id,
                    TournamentPacketAssignmentRecord.tournament_id == original.tournament_id,
                )
            )
        editor = await edit(service, branch, branch_assignment)
        editor["packet"]["name"] = "Hidden branch"
        editor["packet"]["themes"][0]["name"] = "Replacement"
        await save(
            service,
            branch,
            branch_assignment,
            editor,
            {
                "name": "correction",
                "themes.0.name": "substitution",
            },
        )
        editor = await edit(service, original, original_assignment)
        editor["packet"]["testers"] = (tester.display_name,)
        await save(service, original, original_assignment, editor, {"testers": "correction"})
        async with database.sessions() as session:
            claims = list(
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == player.id
                    )
                )
            )
            assert len(claims) == 12 and all(claim.state == "burnt" for claim in claims)
            manager_claims = await session.scalars(
                select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.player_id == original.manager.id
                )
            )
            assert (
                len(
                    {claim.claim_id for claim in claims}
                    - {claim.claim_id for claim in manager_claims}
                )
                == 6
            )
        expected = [{"packet_id": str(stored.logical_id), "name": stored.packet.name}]
        matches = await testers.search(player.id, tester.display_name)
        assert matches["items"][0]["packets"] == expected
        assert (await testers.mine(player.id))["items"][0]["packets"] == expected
        draft_matches = await testers.search(
            original.manager.id, tester.display_name, draft_id=draft_id
        )
        assert draft_matches["items"][0]["packets"] == expected
        async with database.transaction() as session:
            assignment = await session.get(TournamentPacketAssignmentRecord, original_assignment.id)
            assignment.discoverable_by_members = False
        assert (await testers.search(player.id, tester.display_name))["items"][0]["packets"] == []
        assert (await testers.mine(player.id))["items"][0]["packets"] == []
        outsider_matches = await testers.search(branch.players[0].id, tester.display_name)
        assert outsider_matches["items"][0]["packets"] == []
    finally:
        await database.close()


async def test_direct_publish_matches_word_order_and_existing_links(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        testers = TesterService(database)
        surname = uuid4().hex
        name = f"First {surname}"
        async with database.transaction() as session:
            tester = TesterRecord(display_name=name)
            session.add_all([tester, PlatformAdministratorRecord(player_id=fixture.manager.id)])
        request = await testers.create_request(fixture.players[0].id, tester.id)
        await testers.decide_request(UUID(request["request_id"]), fixture.manager.id, approve=True)
        content = replace(packet(), testers=(f"{surname} FIRST",))
        draft_id = await upload(service, fixture, content)
        stored = await service.publish(draft_id, administrator_id=fixture.manager.id)
        assert stored.packet.testers == (name,)
        async with database.sessions() as session:
            claims = list(
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == fixture.players[0].id,
                        PlayerExposureClaimRecord.packet_version_id == stored.version_id,
                    )
                )
            )
            assert len(claims) == 6 and all(c.state == "burnt" for c in claims)
        pending = await testers.create_request(fixture.manager.id, tester.id)
        cancelled = await testers.cancel_request(UUID(pending["request_id"]), fixture.manager.id)
        assert cancelled["status"] == "cancelled"
        assert (
            await testers.cancel_request(UUID(pending["request_id"]), fixture.manager.id)
        ) == cancelled
    finally:
        await database.close()
