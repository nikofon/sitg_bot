import asyncio
from uuid import UUID

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from test_initial_schema import baseline_database as _baseline_database
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import packet, tournament_fixture

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.subscriptions import SubscriptionService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    Base,
    OutboxEventRecord,
    PlayerNotificationRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TournamentRecord,
    TournamentSubscriptionRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url
baseline_database = _baseline_database


def test_subscription_migration_round_trip(baseline_database):
    url, config = baseline_database
    affected = {
        "tournament_subscription_cards",
        "tournament_subscriptions",
        "tournament_packet_entitlements",
    }

    async def verify():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:

                def check(sync):
                    context = MigrationContext.configure(
                        sync,
                        opts={
                            "compare_type": True,
                            "compare_server_default": True,
                            "include_object": lambda obj, name, kind, reflected, compare_to: (
                                name in affected if kind == "table" else obj.table.name in affected
                            ),
                        },
                    )
                    assert compare_metadata(context, Base.metadata) == []

                await connection.run_sync(check)
        finally:
            await engine.dispose()

    command.upgrade(config, "head")
    asyncio.run(verify())
    command.downgrade(config, "0014_contact_rejected_answers")
    command.upgrade(config, "head")
    asyncio.run(verify())


async def change(service, fixture, command, **values):
    view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
    return await SubscriptionService(service).mutate(
        fixture.tournament_id,
        fixture.manager.id,
        expected_version=view.settings_version,
        command=command,
        values=values,
    )


async def test_assigning_subscription_notifies_only_the_recipient_once_per_instance(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = TournamentService(database)
        view = await give_card(service, fixture, count=52, playable=True)
        card_id = view.subscriptions["cards"][0]["id"]
        first_id = view.subscriptions["instances"][0]["id"]
        await change(service, fixture, "revoke", subscription_id=first_id)
        with pytest.raises(StaleWriteError):
            await SubscriptionService(service).mutate(
                fixture.tournament_id, fixture.manager.id,
                expected_version=view.settings_version, command="assign",
                values={"card_id": card_id, "player_id": str(fixture.players[0].id)},
            )
        view = await change(service, fixture, "assign", card_id=card_id,
                            player_id=str(fixture.players[0].id))
        async with database.sessions() as session:
            notifications = (await session.scalars(select(PlayerNotificationRecord).where(
                PlayerNotificationRecord.kind == "subscription.assigned",
                PlayerNotificationRecord.payload["tournament_id"].astext
                == str(fixture.tournament_id),
            ))).all()
            assert len(notifications) == 2
            assert {item.payload["subscription_id"] for item in notifications} == {
                item["id"] for item in view.subscriptions["instances"]
            }
            for item in notifications:
                assert item.recipient_player_id == fixture.players[0].id
                assert item.audience == "player"
                assert item.payload["card_name"] == "Subscription"
                assert item.payload["tournament_name"] == view.tournament.name
                assert item.payload["packet_count"] == 52
                assert item.deduplication_key == (
                    f"subscription-assigned:{item.payload['subscription_id']}"
                )
            assert await session.scalar(select(OutboxEventRecord.id).where(
                OutboxEventRecord.deduplication_key
                == f"notification-alert:subscription-assigned:{first_id}",
            )) is not None
    finally:
        await database.close()


async def give_card(service, fixture, *, count=1, **rights):
    view = await change(
        service, fixture, "create", name="Subscription", packet_count=count, **rights
    )
    card_id = view.subscriptions["cards"][-1]["id"]
    return await change(
        service, fixture, "assign", card_id=card_id, player_id=str(fixture.players[0].id)
    )


async def publish(database, fixture):
    packets = PacketAdminService(database)
    draft = await packets.create_draft(
        packet(),
        source_filename="subscription.json",
        uploader_id=fixture.manager.id,
        tournament_id=fixture.tournament_id,
    )
    return await packets.publish(draft, administrator_id=fixture.manager.id)


async def test_fifo_exhaustion_default_rights_manual_overrides_and_revocation(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = TournamentService(database)
        await give_card(service, fixture, discoverable=False, readable=True, playable=True)
        view = await give_card(
            service, fixture, count=None, discoverable=True, readable=False, playable=False
        )
        assert "subscriptions" in view.sections
        assert len(view.subscriptions["players"]) == 2
        original = view.packets[0].player_access[0]
        assert original.discoverable and original.playable
        first = await publish(database, fixture)
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        packet_view = next(item for item in view.packets if item.packet_id == first.logical_id)
        subscribed = next(
            item for item in packet_view.player_access if item.player_id == fixture.players[0].id
        )
        other = next(
            item for item in packet_view.player_access if item.player_id == fixture.players[1].id
        )
        assert (subscribed.discoverable, subscribed.readable, subscribed.playable) == (
            False,
            True,
            True,
        )
        assert (other.discoverable, other.readable, other.playable) == (True, False, False)
        assert [item["remaining_packets"] for item in view.subscriptions["instances"]] == [0, None]
        second = await publish(database, fixture)
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        second_view = next(item for item in view.packets if item.packet_id == second.logical_id)
        access = next(
            item for item in second_view.player_access if item.player_id == fixture.players[0].id
        )
        assert (access.discoverable, access.readable, access.playable) == (True, False, False)
        for right in ("discoverable", "readable", "playable"):
            view = await service.set_management_packet_access(
                fixture.tournament_id,
                packet_view.assignment_id,
                fixture.manager.id,
                right=right,
                enabled=False,
                player_id=fixture.players[0].id,
            )
        view = await change(
            service, fixture, "revoke", subscription_id=view.subscriptions["instances"][1]["id"]
        )
        await publish(database, fixture)
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        access = next(
            item
            for item in next(
                p for p in view.packets if p.packet_id == first.logical_id
            ).player_access
            if item.player_id == fixture.players[0].id
        )
        assert not access.discoverable and not access.readable and not access.playable
        assert view.subscriptions["instances"][1]["revoked_at"] is not None
        async with database.sessions() as session:
            grant = await session.get(
                TournamentPacketEntitlementRecord,
                (second_view.assignment_id, fixture.players[0].id),
            )
            assert grant.discoverable_override is True  # Revocation preserves prior rights.
    finally:
        await database.close()


async def test_concurrent_uploads_consume_finite_card_once(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        await give_card(service, fixture, playable=True)
        await asyncio.gather(publish(database, fixture), publish(database, fixture))
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        new_packets = [item for item in view.packets if item.packet_id != fixture.packet_id]
        assert sum(item.player_access[0].playable for item in new_packets) == 1
        assert all(item.player_access[0].discoverable for item in new_packets)
        assert view.subscriptions["instances"][0]["remaining_packets"] == 0
    finally:
        await database.close()


async def test_existing_packet_addition_preview_and_reactivation(database_url):
    database = Database(database_url)
    try:
        source = await tournament_fixture(database, player_count=1)
        target = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        await give_card(service, target, count=3, playable=True)
        async with database.transaction() as session:
            session.add(
                TournamentManagerRecord(
                    tournament_id=source.tournament_id, player_id=target.manager.id
                )
            )
        packets = PacketAdminService(database)
        preview = await packets.existing_packet(
            target.tournament_id, source.packet_id, target.manager.id
        )
        view = await service.manager_management(target.tournament_id, target.manager.id)
        assert view.subscriptions["instances"][0]["remaining_packets"] == 3
        for _ in range(2):
            await packets.existing_packet(
                target.tournament_id,
                source.packet_id,
                target.manager.id,
                expected_version_id=UUID(preview["packet_version_id"]),
            )
            async with database.transaction() as session:
                assignment = await session.scalar(
                    select(TournamentPacketAssignmentRecord).where(
                        TournamentPacketAssignmentRecord.tournament_id == target.tournament_id,
                        TournamentPacketAssignmentRecord.packet_id == source.packet_id,
                    )
                )
                assert await service.has_assignment_access(
                    session, assignment, target.players[0].id, "playable"
                )
                assignment.status = "retired"
        view = await service.manager_management(target.tournament_id, target.manager.id)
        assert view.subscriptions["instances"][0]["remaining_packets"] == 2
        # The legacy assignment path also consumes only on first assignment.
        await service.assign_packet(target.tournament_id, source.packet_id, target.manager.id)
        view = await service.manager_management(target.tournament_id, target.manager.id)
        assert view.subscriptions["instances"][0]["remaining_packets"] == 2
        new_packet = await publish(database, source)
        new_assignment = await service.assign_packet(
            target.tournament_id,
            new_packet.logical_id,
            target.manager.id,
            adopted_version_id=new_packet.version_id,
        )
        editor = await packets.management_editor(
            target.tournament_id,
            new_assignment,
            target.manager.id,
        )
        editor["packet"]["name"] += " corrected"
        await packets.modify_packet(
            target.tournament_id,
            new_assignment,
            target.manager.id,
            expected_version=editor["version"],
            content=editor["packet"],
            changes={"name": "correction"},
            field_author_ids={
                key: UUID(value) if value else None
                for key, value in editor["field_author_ids"].items()
            },
        )
        view = await service.manager_management(target.tournament_id, target.manager.id)
        assert view.subscriptions["instances"][0]["remaining_packets"] == 1
    finally:
        await database.close()


async def test_default_preserves_individual_rights_and_permissions_are_scoped(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        view = await give_card(service, fixture, count=2)
        assignment_id = view.packets[0].assignment_id
        async with database.transaction() as session:
            await service.require_modifiable(session, fixture.tournament_id)
            entitlement = TournamentPacketEntitlementRecord(
                assignment_id=assignment_id,
                player_id=fixture.players[0].id,
                discoverable_override=False,
                readable_override=True,
                playable=False,
            )
            session.add(entitlement)
            await session.flush()
            assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            await SubscriptionService.apply_to_new_assignment(session, assignment, "ladder")
            assert entitlement.discoverable_override is False
            assert entitlement.readable_override is True
            assert entitlement.playable is False
        with pytest.raises(PermissionError):
            await SubscriptionService(service).mutate(
                fixture.tournament_id,
                fixture.players[0].id,
                expected_version=view.settings_version,
                command="create",
                values={"name": "Denied"},
            )
        with pytest.raises(StaleWriteError):
            await SubscriptionService(service).mutate(
                fixture.tournament_id,
                fixture.manager.id,
                expected_version=view.settings_version - 1,
                command="create",
                values={"name": "Stale"},
            )
        other = await tournament_fixture(database, player_count=1)
        with pytest.raises(LookupError):
            await change(
                service,
                other,
                "assign",
                card_id=view.subscriptions["cards"][0]["id"],
                player_id=str(other.players[0].id),
            )
        with pytest.raises(LookupError):
            await change(
                service, other, "revoke", subscription_id=view.subscriptions["instances"][0]["id"]
            )
        classic = await tournament_fixture(database, player_count=1, type_key="classic")
        with pytest.raises(ValueError, match="Ladder"):
            await change(service, classic, "create", name="Classic")
        async with database.transaction() as session:
            membership = await session.get(
                TournamentMembershipRecord, (fixture.tournament_id, fixture.players[0].id)
            )
            membership.status = "suspended"
        await publish(database, fixture)
        async with database.sessions() as session:
            instance = await session.get(
                TournamentSubscriptionRecord, UUID(view.subscriptions["instances"][0]["id"])
            )
            assert instance.remaining_packets == 1
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.moderation_status = "halted"
        with pytest.raises(PermissionError):
            await change(service, fixture, "create", name="Halted")
    finally:
        await database.close()
