import asyncio
from dataclasses import asdict, replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_admin_management import administrator
from test_lobby_architecture import database_url as database_url
from test_lobby_architecture import packet, tournament_fixture

from sitg_bot.services.admin_management import AdminManagementService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.testers import TesterService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PacketDraftRecord,
    PacketVersionTesterRecord,
    PlayerExposureClaimRecord,
    PlayerTesterLinkRecord,
    PlayerTesterLinkRequestRecord,
    TesterRecord,
)

pytestmark = pytest.mark.integration


async def test_private_tester_management_namesakes_metadata_and_stale_edits(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        await administrator(database, fixture.manager.id)
        service = AdminManagementService(database)
        name = f"Tester {uuid4().hex}"
        with pytest.raises(PermissionError):
            await service.catalogue(fixture.players[0].id, "testers")
        with pytest.raises(PermissionError):
            await service.save_tester(fixture.players[0].id, display_name=name)
        for values in ({"display_name": " "}, {"display_name": name,
                                               "telegram_link": "https://invalid.example/test"}):
            with pytest.raises(ValueError):
                await service.save_tester(fixture.manager.id, **values)
        first = await service.save_tester(fixture.manager.id, display_name=name)
        second = await service.save_tester(fixture.manager.id, display_name=name)
        assert first["id"] != second["id"]
        values = dict(tester_id=first["id"], expected_version=first["version"],
                      display_name=f"  {name}  Renamed ", first_name=" Ada ",
                      telegram_link="@ada")
        with pytest.raises(PermissionError):
            await service.save_tester(fixture.players[0].id, **values)
        result = await service.save_tester(fixture.manager.id, **values)
        assert result["display_name"] == f"{name} Renamed"
        assert result["first_name"] == "Ada"
        assert result["telegram_link"] == "https://t.me/ada"
        with pytest.raises(StaleWriteError):
            await service.save_tester(fixture.manager.id, **values)
        catalogue = await service.catalogue(fixture.manager.id, "testers")
        card = next(item for item in catalogue["items"] if item["id"] == first["id"])
        assert card["version"] == result["version"]
        assert card["packets"] == [] and card["players"] == []
        assert not {"questions", "themes", "tournaments", "statistics"} & card.keys()
        public = await TesterService(database).search(fixture.players[0].id, name)
        assert all(set(item) == {"tester_id", "display_name", "packets"}
                   for item in public["items"])
        cleared = await service.save_tester(
            fixture.manager.id, tester_id=first["id"], expected_version=result["version"],
            display_name=name,
        )
        assert cleared["first_name"] is None and cleared["telegram_link"] is None
        competing = await asyncio.gather(*(
            service.save_tester(fixture.manager.id, tester_id=first["id"],
                                expected_version=cleared["version"],
                                display_name=f"{name} {suffix}")
            for suffix in ("A", "B")
        ), return_exceptions=True)
        assert sum(isinstance(value, dict) for value in competing) == 1
        assert sum(isinstance(value, StaleWriteError) for value in competing) == 1
    finally:
        await database.close()


async def test_tester_merge_preserves_credits_drafts_links_requests_and_exposure(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=4)
        await administrator(database, fixture.manager.id)
        service = AdminManagementService(database)
        testers = TesterService(database)
        packets = PacketAdminService(database)
        name = f"Namesake {uuid4().hex}"
        primary = await service.save_tester(fixture.manager.id, display_name=name)
        secondary = await service.save_tester(fixture.manager.id, display_name=name)
        unrelated = await service.save_tester(fixture.manager.id, display_name=name)
        primary_id, secondary_id, unrelated_id = primary["id"], secondary["id"], unrelated["id"]
        source = replace(packet(), testers=(name, name))
        published = []
        for bindings in ({"0": primary_id, "1": secondary_id}, {"0": secondary_id,
                                                               "1": unrelated_id}):
            draft_id = await packets.create_draft(
                source, source_filename="tester-admin.json", uploader_id=fixture.manager.id,
                tournament_id=fixture.tournament_id,
            )
            await packets.update_draft(draft_id, fixture.manager.id, expected_version=1,
                                       content=asdict(source), tester_bindings=bindings)
            published.append(await packets.publish(draft_id, administrator_id=fixture.manager.id))
        unpublished = await packets.create_draft(
            source, source_filename="tester-admin-draft.json", uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
        )
        saved = await packets.update_draft(
            unpublished, fixture.manager.id, expected_version=1, content=asdict(source),
            tester_bindings={"0": secondary_id, "1": unrelated_id},
        )
        # Default-resolution slots in an as-yet-unedited upload must not recreate a merged identity.
        only_secondary = await service.save_tester(
            fixture.manager.id, display_name=f"Unique {uuid4().hex}",
        )
        unbound_source = replace(source, testers=(only_secondary["display_name"],))
        unbound = await packets.create_draft(
            unbound_source, source_filename="unbound.json", uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
        )
        await service.merge_testers(fixture.manager.id, primary_id, only_secondary["id"],
                                    confirm=True)
        async with database.sessions() as session:
            draft = await session.get(PacketDraftRecord, unbound)
            assert draft.tester_bindings == {"0": str(primary_id)} and draft.version == 2
        player, other, pending_player, cancel_player = fixture.players
        pending = await testers.create_request(player.id, secondary_id)
        with pytest.raises(PermissionError):
            await service.link_tester(player.id, secondary_id, str(player.id))
        await service.link_tester(fixture.manager.id, secondary_id, str(player.id))
        await service.link_tester(fixture.manager.id, secondary_id, str(player.id))
        await service.link_tester(fixture.manager.id, primary_id, str(other.id))
        # Duplicate link rows must collapse, but all approval/request history survives.
        await service.link_tester(fixture.manager.id, primary_id, str(player.id))
        first_pending = await testers.create_request(pending_player.id, primary_id)
        duplicate = await testers.create_request(pending_player.id, secondary_id)
        impossible = await testers.create_request(other.id, secondary_id)
        cancelled = await testers.create_request(cancel_player.id, secondary_id)
        await testers.cancel_request(UUID(cancelled["request_id"]), cancel_player.id)
        rejected = await testers.create_request(cancel_player.id, secondary_id)
        await testers.decide_request(UUID(rejected["request_id"]), fixture.manager.id,
                                     approve=False)
        async with database.sessions() as session:
            draft_content_before = (await session.get(PacketDraftRecord, unpublished)).content
            before = dict((await session.execute(select(
                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.burnt_at,
            ).where(PlayerExposureClaimRecord.player_id.in_((player.id, other.id))))).all())
            credits_before = list((await session.execute(select(
                PacketVersionTesterRecord.packet_version_id, PacketVersionTesterRecord.position,
                PacketVersionTesterRecord.tester_id,
            ).where(PacketVersionTesterRecord.packet_version_id.in_(
                [p.version_id for p in published]
            )))).all())
        with pytest.raises(PermissionError):
            await service.merge_testers(player.id, primary_id, secondary_id, confirm=True)
        with pytest.raises(ValueError):
            await service.merge_testers(fixture.manager.id, primary_id, secondary_id, confirm=False)
        with pytest.raises(ValueError):
            await service.merge_testers(fixture.manager.id, primary_id, primary_id, confirm=True)
        await service.merge_testers(fixture.manager.id, primary_id, secondary_id, confirm=True)
        with pytest.raises(LookupError):
            await service.merge_testers(fixture.manager.id, primary_id, secondary_id, confirm=True)
        with pytest.raises(StaleWriteError):
            await packets.update_draft(unpublished, fixture.manager.id,
                                       expected_version=saved["version"], content=asdict(source))
        async with database.sessions() as session:
            assert await session.get(TesterRecord, secondary_id) is None
            assert await session.get(TesterRecord, unrelated_id) is not None
            assert await session.get(PlayerTesterLinkRecord, (player.id, primary_id))
            assert await session.get(PlayerTesterLinkRecord, (other.id, primary_id))
            assert await session.get(
                PlayerTesterLinkRecord, (pending_player.id, primary_id),
            ) is None
            draft = await session.get(PacketDraftRecord, unpublished)
            assert draft.content == draft_content_before
            assert draft.tester_bindings == {"0": str(primary_id), "1": str(unrelated_id)}
            assert draft.version == saved["version"] + 1
            records = {str(r.id): r for r in await session.scalars(
                select(PlayerTesterLinkRequestRecord).where(
                    PlayerTesterLinkRequestRecord.tester_id == primary_id,
                )
            )}
            assert records[pending["request_id"]].status == "approved"
            assert records[first_pending["request_id"]].status == "pending"
            assert records[duplicate["request_id"]].status == "cancelled"
            assert records[impossible["request_id"]].status == "cancelled"
            assert records[cancelled["request_id"]].status == "cancelled"
            assert records[rejected["request_id"]].status == "rejected"
            after = dict((await session.execute(select(
                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.burnt_at,
            ).where(PlayerExposureClaimRecord.player_id.in_((player.id, other.id))))).all())
            assert all(after[identity] == burnt_at for identity, burnt_at in before.items())
            # Both linked players now have the complete combined packet exposure.
            for linked in (player, other):
                assert len(list(await session.scalars(select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.player_id == linked.id,
                )))) == 12
            credits = list((await session.execute(select(
                PacketVersionTesterRecord.packet_version_id, PacketVersionTesterRecord.position,
                PacketVersionTesterRecord.tester_id,
            ).where(PacketVersionTesterRecord.packet_version_id.in_(
                [p.version_id for p in published]
            )))).all())
            assert len(credits_before) == 4 and len(credits) == 3
            assert all(row.tester_id != secondary_id for row in credits)
        with pytest.raises(PermissionError):
            await service.unlink_tester(player.id, primary_id, player.id, confirm=True)
        with pytest.raises(ValueError):
            await service.unlink_tester(fixture.manager.id, primary_id, player.id, confirm=False)
        await service.unlink_tester(fixture.manager.id, primary_id, player.id, confirm=True)
        await service.unlink_tester(fixture.manager.id, primary_id, player.id, confirm=True)
        async with database.sessions() as session:
            assert await session.get(PlayerTesterLinkRecord, (player.id, primary_id)) is None
            claims = list(await session.scalars(select(PlayerExposureClaimRecord).where(
                PlayerExposureClaimRecord.player_id == player.id,
            )))
            assert len(claims) == 12 and all(c.state == "burnt" for c in claims)
        # The retained pending request is still actionable after canonicalization.
        await testers.decide_request(UUID(first_pending["request_id"]), fixture.manager.id,
                                     approve=True)
        assert any(item["tester_id"] == str(primary_id)
                   for item in (await testers.mine(pending_player.id))["items"])
        catalogue = await service.catalogue(fixture.manager.id, "testers")
        card = next(item for item in catalogue["items"] if item["id"] == primary_id)
        assert card["packet_count"] == 2 and len(card["packets"]) == 2
    finally:
        await database.close()
