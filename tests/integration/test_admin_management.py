import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_library import claims_for, configure
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import packet, tournament_fixture

from sitg_bot.services.admin_management import AdminManagementService
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.library import PacketLibraryService
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.moderation import PlayerModerationService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameParticipantRecord,
    GameRecord,
    GameResultRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerAuthorLinkRecord,
    PlayerAuthorLinkRequestRecord,
    PlayerRecord,
    RulesetRatingLedgerRecord,
    RulesetRatingRecord,
    TournamentAuthorRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def administrator(database, player_id):
    async with database.transaction() as session:
        session.add(PlatformAdministratorRecord(player_id=player_id))


async def moderate(database, fixture, command, **kwargs):
    async with database.sessions() as session:
        version = (await session.get(TournamentRecord, fixture.tournament_id)).settings_version
    return await AdminManagementService(database).moderate_tournament(
        fixture.manager.id,
        fixture.tournament_id,
        command=command,
        expected_version=version,
        confirm=kwargs.get("confirm", True),
    )


async def test_halt_resume_guards_and_preserves_read_access(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        await administrator(database, fixture.manager.id)
        _, version_id = await configure(database, fixture)
        drafts = PacketAdminService(database)
        draft_id = await drafts.create_draft(
            packet(), source_filename="test.json", tournament_id=fixture.tournament_id
        )
        with pytest.raises(ValueError):
            await moderate(database, fixture, "halt", confirm=False)
        await moderate(database, fixture, "halt")
        tournaments = TournamentService(database)
        with pytest.raises(StaleWriteError):
            await AdminManagementService(database).moderate_tournament(
                fixture.manager.id,
                fixture.tournament_id,
                command="resume",
                expected_version=1,
                confirm=True,
            )
        with pytest.raises(PermissionError):
            await drafts.create_draft(
                packet(), source_filename="blocked.json", tournament_id=fixture.tournament_id
            )
        with pytest.raises(PermissionError):
            await drafts.publish(draft_id, administrator_id=fixture.manager.id)
        with pytest.raises(PermissionError):
            await tournaments.add_manager(
                fixture.tournament_id, fixture.players[1].id, granted_by_id=fixture.manager.id
            )
        with pytest.raises(PermissionError):
            await tournaments.update_policy(
                fixture.tournament_id,
                fixture.manager.id,
                default_parameters={},
                player_mutable_parameters=set(),
                policies={},
            )
        with pytest.raises(ValueError, match="tournament_stage_closed"):
            await InvitationMatchmakingService(database).create_lobby(
                fixture.inputs[0], tournament_id=fixture.tournament_id
            )
        with pytest.raises(ValueError, match="tournament_stage_closed"):
            await PersistentGameService(database).create_game(
                version_id, list(fixture.inputs), tournament_id=fixture.tournament_id
            )
        result = await PacketLibraryService(database).access(
            fixture.players[0].id, version_id, confirm=True, request_key="halt-read"
        )
        assert result["pages"]
        await moderate(database, fixture, "resume")
        async with database.sessions() as session:
            assert (await tournaments.context(session, fixture.tournament_id)).assembly_open
        await drafts.publish(draft_id, administrator_id=fixture.manager.id)
    finally:
        await database.close()


async def test_only_ongoing_tournaments_can_halt_and_only_admins_can_manage(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1, started=False)
        service = AdminManagementService(database)
        for section in ("tournaments", "authors", "players", "packets"):
            with pytest.raises(PermissionError):
                await service.catalogue(fixture.manager.id, section)
        with pytest.raises(PermissionError):
            await moderate(database, fixture, "abolish")
        await administrator(database, fixture.manager.id)
        with pytest.raises(ValueError, match="ongoing"):
            await moderate(database, fixture, "halt")
        with pytest.raises(ValueError, match="halted"):
            await moderate(database, fixture, "resume")
    finally:
        await database.close()


async def test_rating_weight_change_appends_policy_version_and_guards(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = AdminManagementService(database)
        with pytest.raises(PermissionError):
            await service.set_tournament_rating_weight(
                fixture.manager.id, fixture.tournament_id, weight=0.5, expected_version=1
            )
        await administrator(database, fixture.manager.id)
        async with database.sessions() as session:
            version = (await session.get(TournamentRecord, fixture.tournament_id)).settings_version
            previous = (await session.scalars(
                select(TournamentPolicyVersionRecord)
                .where(TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id)
                .order_by(TournamentPolicyVersionRecord.version.desc())
                .limit(1)
            )).first()
            assert previous is not None
            previous_policies = dict(previous.policies)
        with pytest.raises(StaleWriteError):
            await service.set_tournament_rating_weight(
                fixture.manager.id, fixture.tournament_id,
                weight=0.5, expected_version=version + 1,
            )
        with pytest.raises(ValueError, match="between 0.1 and 1"):
            await service.set_tournament_rating_weight(
                fixture.manager.id, fixture.tournament_id,
                weight=0.05, expected_version=version,
            )
        await service.set_tournament_rating_weight(
            fixture.manager.id, fixture.tournament_id, weight=0.5, expected_version=version
        )
        async with database.sessions() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            assert tournament.settings_version == version + 1
            policy = (await session.scalars(
                select(TournamentPolicyVersionRecord)
                .where(TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id)
                .order_by(TournamentPolicyVersionRecord.version.desc())
                .limit(1)
            )).first()
            assert policy is not None
            assert policy.version == previous.version + 1
            assert policy.policies == {**previous_policies, "ruleset_rating_weight": 0.5}
    finally:
        await database.close()


async def test_abolition_revokes_only_its_assignment_and_is_permanent(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        await administrator(database, fixture.manager.id)
        _, version_id = await configure(database, fixture)
        await TournamentService(database).assign_packet(
            other.tournament_id,
            fixture.packet_id,
            other.manager.id,
            adopted_version_id=version_id,
            content_visible=True,
            library_viewing_rule="anytime",
        )
        await moderate(database, fixture, "abolish")
        library = PacketLibraryService(database)
        assert not (await library.list_packets(fixture.players[0].id))["items"]
        for player_id in (fixture.players[0].id, fixture.manager.id):
            with pytest.raises(PermissionError):
                await library.access(player_id, version_id, confirm=True, request_key="blocked")
        result = await library.access(
            other.players[0].id, version_id, confirm=True, request_key="shared"
        )
        assert result["pages"]
        for command in ("halt", "resume", "abolish"):
            with pytest.raises(ValueError, match="permanent"):
                await moderate(database, fixture, command)
    finally:
        await database.close()


@pytest.mark.parametrize("download", [False, True])
async def test_admin_packet_access_bypasses_gates_and_burns_reserved_content(
    database_url, download
):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        _, version_id = await configure(database, fixture, readable=False, released=False)
        player_id = fixture.players[0].id
        library = PacketLibraryService(database)
        with pytest.raises(PermissionError):
            await library.access(
                player_id, version_id, administrator=True, confirm=True, request_key="forbidden"
            )
        await administrator(database, player_id)
        game = await PersistentGameService(database).create_game(
            version_id, list(fixture.inputs), tournament_id=fixture.tournament_id
        )
        assert any(c.state == "reserved" for c in await claims_for(database, player_id))
        async with database.transaction() as session:
            (await session.get(PacketVersionRecord, version_id)).state = "archived"
            assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            ))
            assignment.status = "retired"
        warning = await library.access(
            player_id, version_id, administrator=True, request_key="warning"
        )
        assert warning["confirmation_required"]
        result = await library.access(
            player_id,
            version_id,
            administrator=True,
            confirm=True,
            download=download,
            request_key="admin-read",
        )
        assert result["queued"] if download else result["pages"]
        assert all(c.state == "burnt" for c in await claims_for(database, player_id))
        await PersistentGameService(database).abandon_player(
            game.id, fixture.inputs[0].telegram_user_id
        )
        assert all(c.state == "burnt" for c in await claims_for(database, player_id))
    finally:
        await database.close()


async def settle(database, fixture):
    async with database.transaction() as session:
        tournament = await session.get(TournamentRecord, fixture.tournament_id)
        policy = await session.scalar(
            select(TournamentPolicyVersionRecord).where(
                TournamentPolicyVersionRecord.tournament_id == tournament.id
            )
        )
        game = GameRecord(
            tournament_id=tournament.id,
            tournament_type_version_id=tournament.type_version_id,
            game_ruleset_version_id=tournament.game_ruleset_version_id,
            tournament_policy_version_id=policy.id,
            host_player_id=fixture.players[0].id,
            assignment_plan={},
            status="completed",
            phase="finished",
            completed_at=datetime.now(UTC),
        )
        session.add(game)
        await session.flush()
        participants = []
        for seat, player in enumerate(fixture.players, 1):
            current = await session.get(PlayerRecord, player.id, with_for_update=True)
            membership = await session.get(TournamentMembershipRecord, (tournament.id, player.id))
            membership.rating_sequence += 1
            current.game_sequence += 1
            participant = GameParticipantRecord(
                tournament_id=tournament.id,
                game_id=game.id,
                player_id=player.id,
                seat=seat,
                rating_sequence=membership.rating_sequence,
                global_game_sequence=current.game_sequence,
                active=False,
                final_place=Decimal(seat),
            )
            participants.append(participant)
            session.add(participant)
            session.add(
                GameResultRecord(
                    game_id=game.id,
                    player_id=player.id,
                    tournament_id=tournament.id,
                    tournament_policy_version_id=policy.id,
                    place=Decimal(seat),
                    score=0,
                )
            )
        await session.flush()
        await PersistentGameService(database)._apply_rating_settlement(session, game, participants)
        return game.id


async def test_abolition_appends_reversals_and_suppresses_later_global_settlement(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        await administrator(database, fixture.manager.id)
        game_id = await settle(database, fixture)
        async with database.sessions() as session:
            original = list(
                await session.scalars(
                    select(RulesetRatingLedgerRecord).where(
                        RulesetRatingLedgerRecord.game_id == game_id
                    )
                )
            )
            assert len(original) == 2 and any(row.delta != 0 for row in original)
        await moderate(database, fixture, "abolish")
        late_game = await settle(database, fixture)
        async with database.sessions() as session:
            ledger = list(
                await session.scalars(
                    select(RulesetRatingLedgerRecord).where(
                        RulesetRatingLedgerRecord.tournament_id == fixture.tournament_id
                    )
                )
            )
            assert len(ledger) == 4
            assert not any(row.game_id == late_game for row in ledger)
            for player in fixture.players:
                assert sum(row.delta for row in ledger if row.player_id == player.id) == 0
                assert (await session.get(RulesetRatingRecord, ("si", player.id))).rating == 1000
            assert (await session.get(GameRecord, late_game)).status == "finalized"
            assert not await PersistentGameService(database)._ruleset_rating_history(
                session, "si", fixture.players[0].id
            )
    finally:
        await database.close()


async def test_concurrent_abolition_and_settlement_leave_no_global_delta(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        await administrator(database, fixture.manager.id)
        await asyncio.wait_for(asyncio.gather(
            settle(database, fixture), moderate(database, fixture, "abolish")
        ), timeout=10)
        async with database.sessions() as session:
            entries = list(await session.scalars(select(RulesetRatingLedgerRecord).where(
                RulesetRatingLedgerRecord.tournament_id == fixture.tournament_id)))
            for player in fixture.players:
                assert sum(entry.delta for entry in entries if entry.player_id == player.id) == 0
    finally:
        await database.close()


async def test_catalogues_include_banned_players_and_author_link_burns_content(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        await administrator(database, fixture.manager.id)
        await PlayerModerationService(database).ban_player(
            fixture.manager.id, str(fixture.players[0].id), reason="Review"
        )
        service = AdminManagementService(database)
        for section in ("tournaments", "authors", "players", "packets"):
            assert (await service.catalogue(fixture.manager.id, section))["items"]
        players = (await service.catalogue(fixture.manager.id, "players"))["items"]
        assert next(p for p in players if p["id"] == fixture.players[0].id)["ban"]
        async with database.transaction() as session:
            author = AuthorRecord(display_name=f"Admin author {uuid4()}")
            session.add(author)
            await session.flush()
            author_id = author.id
            player = await session.get(PlayerRecord, fixture.players[0].id)
            player.telegram_username = f"test_{uuid4().hex[:20]}"
            target = f"@{player.telegram_username.upper()}"
            version = await session.scalar(
                select(PacketVersionRecord).where(
                    PacketVersionRecord.packet_id == fixture.packet_id
                )
            )
            version.lead_author_id = author_id
        await service.link_author(fixture.manager.id, author_id, target)
        await service.link_author(fixture.manager.id, author_id, str(fixture.players[0].id))
        async with database.sessions() as session:
            assert await session.get(PlayerAuthorLinkRecord, (fixture.players[0].id, author_id))
        assert all(c.state == "burnt" for c in await claims_for(database, fixture.players[0].id))
    finally:
        await database.close()


async def test_author_merge_transfers_identity_and_drops_conflicts(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        await administrator(database, fixture.manager.id)
        async with database.transaction() as session:
            primary = AuthorRecord(display_name=f"Primary {uuid4()}")
            secondary = AuthorRecord(display_name=f"Secondary {uuid4()}")
            session.add_all((primary, secondary))
            await session.flush()
            session.add_all((
                TournamentAuthorRecord(
                    tournament_id=fixture.tournament_id, author_id=primary.id
                ),
                TournamentAuthorRecord(
                    tournament_id=fixture.tournament_id, author_id=secondary.id
                ),
            ))
            version = await session.scalar(
                select(PacketVersionRecord).where(
                    PacketVersionRecord.packet_id == fixture.packet_id
                )
            )
            version.lead_author_id = secondary.id
            primary_id, secondary_id = primary.id, secondary.id
        service = AdminManagementService(database)
        await service.link_author(fixture.manager.id, secondary_id, str(fixture.players[0].id))
        await AuthorLinkService(database).create_request(
            fixture.players[1].id, secondary_id, note="Join me"
        )
        with pytest.raises(PermissionError):
            await service.merge_authors(
                fixture.players[0].id, primary_id, secondary_id, confirm=True
            )
        with pytest.raises(ValueError):
            await service.merge_authors(
                fixture.manager.id, primary_id, secondary_id, confirm=False
            )
        with pytest.raises(ValueError):
            await service.merge_authors(
                fixture.manager.id, primary_id, primary_id, confirm=True
            )
        result = await service.merge_authors(
            fixture.manager.id, primary_id, secondary_id, confirm=True
        )
        assert result["merged"] is True
        assert result["author_id"] == str(primary_id)
        async with database.sessions() as session:
            assert await session.get(AuthorRecord, secondary_id) is None
            assert await session.get(
                PlayerAuthorLinkRecord, (fixture.players[0].id, primary_id)
            )
            version = await session.scalar(
                select(PacketVersionRecord).where(
                    PacketVersionRecord.packet_id == fixture.packet_id
                )
            )
            assert version.lead_author_id == primary_id
            tournament_authors = tuple(
                await session.scalars(
                    select(TournamentAuthorRecord).where(
                        TournamentAuthorRecord.tournament_id == fixture.tournament_id
                    )
                )
            )
            assert {row.author_id for row in tournament_authors} == {primary_id}
            request = await session.scalar(
                select(PlayerAuthorLinkRequestRecord).where(
                    PlayerAuthorLinkRequestRecord.player_id == fixture.players[1].id
                )
            )
            assert request.status == "pending"
            assert request.author_id == primary_id
        catalogue = await service.catalogue(fixture.manager.id, "link_requests")
        assert next(
            item for item in catalogue["items"] if item["player"]["id"] == fixture.players[1].id
        )["author"]["id"] == primary_id
        assert all(c.state == "burnt" for c in await claims_for(database, fixture.players[0].id))
    finally:
        await database.close()


async def test_ongoing_games_catalogue_lists_lobby_and_active_games(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = AdminManagementService(database)
        with pytest.raises(PermissionError):
            await service.catalogue(fixture.manager.id, "ongoing_games")
        await administrator(database, fixture.manager.id)

        async def add_game(
            session, tournament, policy, *, status: str, phase: str, participants: tuple
        ) -> UUID:
            game = GameRecord(
                tournament_id=tournament.id,
                tournament_type_version_id=tournament.type_version_id,
                game_ruleset_version_id=tournament.game_ruleset_version_id,
                tournament_policy_version_id=policy.id,
                host_player_id=fixture.players[0].id,
                status=status,
                phase=phase,
            )
            session.add(game)
            await session.flush()
            for seat, player in enumerate(participants, start=1):
                session.add(
                    GameParticipantRecord(
                        game_id=game.id,
                        tournament_id=tournament.id,
                        player_id=player.id,
                        seat=seat,
                        rating_sequence=seat,
                        global_game_sequence=seat,
                        joined=True,
                        ready=True,
                        is_chair=seat == 1,
                    )
                )
            return game.id

        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
                )
            )
            assert tournament is not None and policy is not None
            active_id = await add_game(
                session,
                tournament,
                policy,
                status="active",
                phase="question",
                participants=(fixture.players[0], fixture.players[1]),
            )
            lobby_id = await add_game(
                session,
                tournament,
                policy,
                status="lobby",
                phase="lobby",
                participants=(fixture.manager,),
            )
            finished_id = await add_game(
                session,
                tournament,
                policy,
                status="completed",
                phase="finished",
                participants=(),
            )

        catalogue = await service.catalogue(fixture.manager.id, "ongoing_games")
        assert catalogue["section"] == "ongoing_games"
        listed = {str(item["id"]) for item in catalogue["items"]}
        assert str(active_id) in listed
        assert str(lobby_id) in listed
        assert str(finished_id) not in listed
        card = next(
            item for item in catalogue["items"] if str(item["id"]) == str(active_id)
        )
        assert card["name"] == tournament.name
        assert card["status"] == "active"
        assert card["phase"] == "question"
        assert card["ruleset"] == "si"
        assert card["type"] == "ladder"
        assert card["settings"]["policies"]["rating_enabled"] is True
        assert card["host"]["id"] == fixture.players[0].id
        assert card["participant_count"] == 2
        assert [p["seat"] for p in card["participants"]] == [1, 2]
        assert {p["id"] for p in card["participants"]} == {
            fixture.players[0].id,
            fixture.players[1].id,
        }
        assert card["participants"][0]["is_chair"] is True
        assert card["participants"][1]["is_chair"] is False
    finally:
        await database.close()

