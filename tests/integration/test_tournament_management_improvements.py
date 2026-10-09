import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_classic import first_round, mutate, setup
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import packet, tournament_fixture

from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.packet_notifications import PacketAvailabilityService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PacketVersionRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    PregameLobbyMemberRecord,
    PregameLobbyRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


@pytest.mark.parametrize("scheduled_open", [True, False])
async def test_manual_registration_disables_schedule(database_url, scheduled_open):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.registration_open = True
            tournament.registration_open_override = None
            tournament.registration_starts_at = datetime.now(UTC) + timedelta(
                days=-1 if scheduled_open else 1
            )
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        assert view.registration_open is scheduled_open
        view = await service.set_registration_override(
            fixture.tournament_id,
            fixture.manager.id,
            registration_open=not scheduled_open,
            expected_version=view.settings_version,
        )
        assert view.registration_open is not scheduled_open
        settings = await service.manager_settings(fixture.tournament_id, fixture.manager.id)
        assert settings.registration_enabled is False
        view = await service.set_registration_override(
            fixture.tournament_id,
            fixture.manager.id,
            registration_open=scheduled_open,
            expected_version=view.settings_version,
        )
        assert view.registration_open is scheduled_open
    finally:
        await database.close()


async def test_viewing_rules_are_per_assignment_and_default_only_applies_to_uploads(database_url):
    database = Database(database_url)
    try:
        first = await tournament_fixture(database, player_count=1)
        second = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        assignment_id = await service.assign_packet(
            second.tournament_id,
            first.packet_id,
            second.manager.id,
            content_visible=True, library_viewing_rule="anytime",
        )
        view = await service.manager_management(first.tournament_id, first.manager.id)
        first_assignment = view.packets[0].assignment_id
        async with database.transaction() as session:
            assignment = await session.get(TournamentPacketAssignmentRecord, first_assignment)
            assignment.content_visible_by_members = True
            version = await session.get(PacketVersionRecord, assignment.adopted_version_id)
            version.library_released_at = datetime.now(UTC)
        await service.set_management_packet_access(
            first.tournament_id,
            first_assignment,
            first.manager.id,
            right="library_viewing_rule",
            library_viewing_rule="never",
            player_id=None,
            expected_version=view.settings_version,
        )
        assert not await service.can_read_packet_library(
            first.tournament_id, first.packet_id, first.players[0].id
        )
        assert await service.can_read_packet_library(
            second.tournament_id, first.packet_id, second.players[0].id
        )
        settings = await service.manager_settings(first.tournament_id, first.manager.id)
        await service.update_policy(
            first.tournament_id,
            first.manager.id,
            default_parameters=settings.default_parameters,
            player_mutable_parameters=set(settings.player_mutable_parameters),
            policies={**settings.policies, "library_viewing_rule_default": "anytime"},
        )
        packets = PacketAdminService(database)
        draft = await packets.create_draft(
            packet(),
            source_filename="rules.json",
            uploader_id=first.manager.id,
            tournament_id=first.tournament_id,
        )
        published = await packets.publish(draft, administrator_id=first.manager.id)
        async with database.sessions() as session:
            old = await session.get(TournamentPacketAssignmentRecord, first_assignment)
            other = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            new = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.packet_id == published.logical_id
                )
            )
            assert old.library_viewing_rule == "never"
            assert other.library_viewing_rule == new.library_viewing_rule == "anytime"
            assert not await service.has_assignment_access(
                session, new, first.players[0].id, "playable"
            )
    finally:
        await database.close()


@pytest.mark.parametrize("type_key", ["ladder", "classic"])
@pytest.mark.parametrize("rule", ["never", "after-play", "anytime"])
async def test_library_viewing_rules_do_not_change_playability_or_readiness(
    database_url, type_key, rule
):
    database = Database(database_url)
    try:
        if type_key == "classic":
            fixture = await setup(database, 1, stage_type="quiz", scheme=None)
            round_info, assignment_id = await first_round(database, fixture)
            await mutate(database, fixture, "round", round_id=round_info["id"],
                assignment_id=str(assignment_id))
            await mutate(database, fixture, "start")
        else:
            fixture = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        assignment_id = view.packets[0].assignment_id
        async with database.transaction() as session:
            assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            assignment.content_visible_by_members = True
            version = await session.get(PacketVersionRecord, assignment.adopted_version_id)
            version.library_released_at = datetime.now(UTC)
        lobbies = InvitationMatchmakingService(database)
        lobby = await lobbies.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        await lobbies.select_packet(lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id)
        ready = await lobbies.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        await service.set_management_packet_access(
            fixture.tournament_id, assignment_id, fixture.manager.id,
            right="library_viewing_rule", library_viewing_rule=rule, player_id=None,
            expected_version=view.settings_version,
        )
        async with database.sessions() as session:
            assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            for right in ("playable", "discoverable", "content_visible"):
                assert await service.has_assignment_access(
                    session, assignment, fixture.players[0].id, right
                )
            current = await session.get(PregameLobbyRecord, lobby.id)
            assert current.version == ready.version
            member = await session.scalar(select(PregameLobbyMemberRecord).where(
                PregameLobbyMemberRecord.lobby_id == lobby.id,
                PregameLobbyMemberRecord.player_id == fixture.players[0].id,
            ))
            assert member.ready
        assert await service.can_read_packet_library(
            fixture.tournament_id, fixture.packet_id, fixture.players[0].id
        ) is (rule == "anytime")
        result = await lobbies.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert result.started
    finally:
        await database.close()


async def test_player_limits_enforce_capacity_readiness_and_mutability(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(
            database, player_count=2, minimum_players=4, mutable_parameters=("theme_count",)
        )
        service = TournamentService(database)
        lobbies = InvitationMatchmakingService(database)
        settings = await service.manager_settings(fixture.tournament_id, fixture.manager.id)
        assert (
            settings.default_parameters["minimum_players"]
            == settings.default_parameters["maximum_players"]
            == 4
        )
        with pytest.raises(PermissionError, match="maximum_players"):
            await lobbies.create_lobby(
                fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=2
            )
        lobby = await lobbies.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        lobby = await lobbies.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        assert lobby.max_players == 4
        assert any(
            v["code"] == "ruleset_player_limit_exceeded" for v in lobby.validation_violations
        )
        await service.update_policy(
            fixture.tournament_id,
            fixture.manager.id,
            default_parameters={
                **settings.default_parameters,
                "minimum_players": 1,
                "maximum_players": 2,
            },
            player_mutable_parameters={"minimum_players", "maximum_players"},
            policies=settings.policies,
        )
        async with database.sessions() as session:
            row = await session.get(PregameLobbyRecord, lobby.id)
            assert row.max_players == 2
        changed = await lobbies.set_settings(
            lobby.id,
            fixture.inputs[0].telegram_user_id,
            {"minimum_players": 1, "maximum_players": 1},
        )
        assert changed.max_players == 1
        assert not any(
            v["code"] == "ruleset_player_limit_exceeded" for v in changed.validation_violations
        )
        with pytest.raises(ValueError, match="minimum_players"):
            await lobbies.set_settings(
                lobby.id, fixture.inputs[0].telegram_user_id, {"minimum_players": 2}
            )
    finally:
        await database.close()


@pytest.mark.parametrize("initially_disabled", [False, True])
async def test_packet_notifications_survive_retries_and_wait_for_access(
    database_url, initially_disabled,
):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = TournamentService(database)
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        assignment_id = view.packets[0].assignment_id
        await service.set_management_packet_access(
            fixture.tournament_id,
            assignment_id,
            fixture.manager.id,
            right="playable",
            enabled=False,
            player_id=fixture.players[1].id,
        )
        notifier = PacketAvailabilityService(database)

        async def notices():
            async with database.sessions() as session:
                return list(
                    await session.scalars(
                        select(PlayerNotificationRecord).where(
                            PlayerNotificationRecord.kind == "packet.available",
                            PlayerNotificationRecord.payload["tournament_id"].astext
                            == str(fixture.tournament_id),
                        )
                    )
                )

        async def set_notifications(enabled):
            settings = await service.manager_settings(fixture.tournament_id, fixture.manager.id)
            await service.update_policy(
                fixture.tournament_id,
                fixture.manager.id,
                default_parameters=settings.default_parameters,
                player_mutable_parameters=set(settings.player_mutable_parameters),
                policies={**settings.policies, "packet_notifications_enabled": enabled},
            )

        if initially_disabled:
            await set_notifications(False)
            await notifier.reconcile()
            await notifier.reconcile()
            assert await notices() == []
            await set_notifications(True)
        await notifier.reconcile()
        await notifier.reconcile()
        assert [n.recipient_player_id for n in await notices()] == [fixture.players[0].id]
        await set_notifications(False)
        await service.set_management_packet_access(
            fixture.tournament_id,
            assignment_id,
            fixture.manager.id,
            right="playable",
            enabled=True,
            player_id=fixture.players[1].id,
        )
        await notifier.reconcile()
        assert [n.recipient_player_id for n in await notices()] == [fixture.players[0].id]
        await set_notifications(True)
        await notifier.reconcile()
        await notifier.reconcile()
        assert {n.recipient_player_id for n in await notices()} == {p.id for p in fixture.players}
        assert len(await notices()) == 2
    finally:
        await database.close()


async def test_classic_notification_includes_opponents_and_deadline(database_url):
    database = Database(database_url)
    try:
        fixture = await setup(database, 4, stage_type="playoff", scheme="playoff-8", kind="playoff")
        round_info, assignment_id = await first_round(database, fixture, kind="playoff")
        deadline = datetime.now(UTC) + timedelta(hours=2)
        await mutate(
            database,
            fixture,
            "round",
            "playoff",
            round_id=round_info["id"],
            assignment_id=str(assignment_id),
            start_deadline=deadline.isoformat(),
        )
        notifier = PacketAvailabilityService(database)
        await notifier.reconcile()
        await mutate(database, fixture, "start", "playoff")
        await notifier.reconcile()
        await notifier.reconcile()
        async with database.sessions() as session:
            notices = list(
                await session.scalars(
                    select(PlayerNotificationRecord).where(
                        PlayerNotificationRecord.kind == "packet.available",
                        PlayerNotificationRecord.payload["tournament_id"].astext
                        == str(fixture.tournament_id),
                    )
                )
            )
            assert len(notices) == 4
            assert all(n.payload["start_deadline"] == deadline.isoformat() for n in notices)
            assert all(len(n.payload["opponents"]) == 3 for n in notices)
    finally:
        await database.close()


async def test_default_packet_access_enables_retroactively_and_disables_for_future_only(
    database_url,
):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = TournamentService(database)
        view = await service.manager_management(fixture.tournament_id, fixture.manager.id)
        assignment_id = view.packets[0].assignment_id

        # A positive default change is retroactive for current participants.
        view = await service.set_management_packet_access(
            fixture.tournament_id, assignment_id, fixture.manager.id,
            right="readable", enabled=True, player_id=None, scope="default",
        )
        assert view.packets[0].default_access == {
            "discoverable": True, "playable": True, "readable": True,
        }

        # A negative default change is not retroactive: current access is frozen.
        view = await service.set_management_packet_access(
            fixture.tournament_id, assignment_id, fixture.manager.id,
            right="discoverable", enabled=False, player_id=None, scope="default",
        )
        assert view.packets[0].default_access["discoverable"] is False
        # An explicit denial survives until a retroactive default change overrides it.
        await service.set_management_packet_access(
            fixture.tournament_id, assignment_id, fixture.manager.id,
            right="discoverable", enabled=False, player_id=fixture.players[0].id,
        )
        view = await service.set_management_packet_access(
            fixture.tournament_id, assignment_id, fixture.manager.id,
            right="discoverable", enabled=True, player_id=None, scope="default",
        )
        assert view.packets[0].default_access["discoverable"] is True

        await service.set_management_packet_access(
            fixture.tournament_id, assignment_id, fixture.manager.id,
            right="playable", enabled=False, player_id=None, scope="default",
        )

        future = PlayerRecord(
            telegram_user_id=secrets.randbits(31),
            real_name="Future Player",
            public_nickname="Future Player",
            registration_step="complete",
            registration_completed_at=datetime.now(UTC),
            status="active",
        )
        async with database.transaction() as session:
            session.add(future)
            await session.flush()
            session.add(TournamentMembershipRecord(
                tournament_id=fixture.tournament_id,
                player_id=future.id,
                enrolled_by_id=fixture.manager.id,
            ))

        async with database.sessions() as session:
            assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            for player in (*fixture.players, future):
                assert await service.has_assignment_access(
                    session, assignment, player.id, "discoverable"
                )
                assert await service.has_assignment_access(
                    session, assignment, player.id, "content_visible"
                )
            for player in fixture.players:
                assert await service.has_assignment_access(
                    session, assignment, player.id, "playable"
                )
            assert not await service.has_assignment_access(
                session, assignment, future.id, "playable"
            )

        # Shared packets keep independent defaults per tournament assignment.
        second = await tournament_fixture(database, player_count=1)
        other_assignment_id = await service.assign_packet(
            second.tournament_id, fixture.packet_id, second.manager.id,
            discoverable=True, playable=True,
        )
        async with database.sessions() as session:
            other = await session.get(TournamentPacketAssignmentRecord, other_assignment_id)
            assert other.playable_by_members is True
            first = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            assert first.playable_by_members is False
    finally:
        await database.close()
