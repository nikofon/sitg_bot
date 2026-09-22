import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import packet, tournament_fixture

from sitg_bot.server import ConsoleApplicationServer
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.persistent_game import PersistentGameService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GamePacketVersionRecord,
    GameResultRecord,
    LogicalQuestionRecord,
    PacketDraftRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerExposureClaimRecord,
    PlayerNotificationRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
)
from sitg_bot.storage.packets import PostgresPacketRepository

pytestmark = pytest.mark.integration
database_url = _database_url


async def test_add_existing_packet_checks_both_roles_and_confirmation(database_url):
    database = Database(database_url)
    try:
        source = await tournament_fixture(database, player_count=1)
        target = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        args = (target.tournament_id, source.packet_id, target.manager.id)
        with pytest.raises(LookupError):
            await service.existing_packet(*args)
        with pytest.raises(PermissionError):
            await service.existing_packet(target.tournament_id, source.packet_id, source.manager.id)
        async with database.transaction() as session:
            session.add(TournamentManagerRecord(
                tournament_id=source.tournament_id, player_id=target.manager.id,
            ))
        preview = await service.existing_packet(*args)
        assert preview["name"].startswith("Architecture ")
        version_id = UUID(preview["packet_version_id"])
        assert preview["theme_count"] == len(packet().themes)
        async with database.transaction() as session:
            assert await session.scalar(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == target.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == source.packet_id,
            )) is None
        with pytest.raises(StaleWriteError):
            await service.existing_packet(*args, expected_version_id=UUID(int=999))
        async with database.transaction() as session:
            role = await session.get(
                TournamentManagerRecord, (source.tournament_id, target.manager.id)
            )
            role.revoked_at = datetime.now(UTC)
        with pytest.raises(LookupError):
            await service.existing_packet(*args, expected_version_id=version_id)
        async with database.transaction() as session:
            role = await session.get(
                TournamentManagerRecord, (source.tournament_id, target.manager.id)
            )
            role.revoked_at = None
        await service.existing_packet(*args, expected_version_id=UUID(preview["packet_version_id"]))
        async with database.transaction() as session:
            assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == target.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == source.packet_id,
            ))
            assert assignment.adopted_version_id == UUID(preview["packet_version_id"])
            assert assignment.assigned_by_id == target.manager.id
            assert await session.scalar(select(PlayerExposureClaimRecord).where(
                PlayerExposureClaimRecord.player_id == target.manager.id,
                PlayerExposureClaimRecord.packet_version_id == assignment.adopted_version_id,
                PlayerExposureClaimRecord.state == "burnt",
            )) is not None
        with pytest.raises(ValueError, match="already assigned"):
            await service.existing_packet(*args, expected_version_id=version_id)
    finally:
        await database.close()


async def test_console_manager_packet_workflow_uses_tournament_permissions(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        server = ConsoleApplicationServer(database)
        connection = SimpleNamespace(
            session=SimpleNamespace(player_id=fixture.manager.id, admin=False)
        )
        params = {
            "tournament_id": str(fixture.tournament_id),
            "source_filename": "console.json",
            "source_base64": base64.b64encode(json.dumps(asdict(packet())).encode()).decode(),
        }
        draft = await server._dispatch(connection, "packet_import", params)
        assert draft["status"] == "awaiting_confirmation"
        assert draft["can_publish"] is True
        target = {"draft_id": draft["draft_id"]}
        preview = await server._dispatch(connection, "packet_preview", target)
        assert preview["packet"]["themes"]
        connection.session.player_id = fixture.players[0].id
        with pytest.raises(PermissionError):
            await server._dispatch(connection, "packet_import", params)
        for action in ("packet_preview", "packet_publish", "packet_reject"):
            with pytest.raises(PermissionError):
                await server._dispatch(connection, action, target)
        connection.session.player_id = fixture.manager.id
        published = await server._dispatch(connection, "packet_publish", target)
        assert published["status"] == "published"
        repeated = await server._dispatch(connection, "packet_publish", target)
        assert repeated["status"] == "published"
        invalid = await server._dispatch(
            connection, "packet_import",
            {**params, "source_base64": base64.b64encode(b"invalid JSON").decode()},
        )
        assert invalid["status"] == "validation_failed" and invalid["errors"]
        rejected = await server._dispatch(
            connection, "packet_reject", {"draft_id": invalid["draft_id"]}
        )
        assert rejected["status"] == "rejected"
        with pytest.raises(ValueError, match="valid base64"):
            await server._dispatch(connection, "packet_import", {**params, "source_base64": "!"})
    finally:
        await database.close()


async def test_upload_creates_independent_drafts_with_shared_provenance(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        first = asdict(packet())
        second = {**first, "name": "Second packet"}
        result = await service.import_upload(
            json.dumps([first, second]).encode(), source_filename="batch.json",
            uploader_id=fixture.manager.id, tournament_id=fixture.tournament_id,
        )
        summaries = result["drafts"]
        assert [item["packet_name"] for item in summaries] == [first["name"], "Second packet"]
        ids = [UUID(item["draft_id"]) for item in summaries]
        assert ids[0] != ids[1]
        async with database.sessions() as session:
            drafts = [await session.get(PacketDraftRecord, draft_id) for draft_id in ids]
            assert drafts[0].source_checksum == drafts[1].source_checksum
            assert all(draft.source_filename == "batch.json" for draft in drafts)
            assert all(draft.uploader_id == fixture.manager.id for draft in drafts)
        await service.reject(ids[0], actor_id=fixture.manager.id)
        assert (await service.draft_summary(ids[0], fixture.manager.id))["status"] == "rejected"
        assert (await service.draft_summary(ids[1], fixture.manager.id))["can_publish"]
        assert ".pdf" in (await service.upload_eligibility(
            fixture.tournament_id, fixture.manager.id
        ))["accepted_extensions"]
    finally:
        await database.close()


async def assigned(database, fixture):
    async with database.sessions() as session:
        return await session.scalar(
            select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            )
        )


async def edit(service, fixture, assignment):
    return await service.management_editor(fixture.tournament_id, assignment.id, fixture.manager.id)


async def save(service, fixture, assignment, editor, changes):
    await service.modify_packet(
        fixture.tournament_id,
        assignment.id,
        fixture.manager.id,
        expected_version=editor["version"],
        content=editor["packet"],
        changes=changes,
        field_author_ids={
            key: UUID(value) if value else None for key, value in editor["field_author_ids"].items()
        },
    )


async def test_correction_and_deletion_preserve_game_history_and_entitlements(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        tournaments = TournamentService(database)
        assignment = await assigned(database, fixture)
        async with database.transaction() as session:
            current = await session.get(TournamentPacketAssignmentRecord, assignment.id)
            current.library_viewing_rule = "anytime"
            session.add(
                TournamentPacketEntitlementRecord(
                    assignment_id=assignment.id,
                    player_id=fixture.players[0].id,
                    content_visible=True,
                    discoverable=True,
                    playable=True,
                )
            )
        matchmaking = InvitationMatchmakingService(database)
        lobby = await matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
        )
        await matchmaking.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        await matchmaking.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        started = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert started.started
        editor = await edit(service, fixture, assignment)
        old_text = editor["packet"]["themes"][0]["questions"][0]["text"]
        with pytest.raises(ValueError, match="Every enabled field"):
            await save(service, fixture, assignment, editor, {"name": "correction"})
        editor["packet"]["themes"][0]["questions"][0]["text"] = "Corrected text"
        await save(
            service, fixture, assignment, editor, {"themes.0.questions.0.text": "correction"}
        )
        with pytest.raises(StaleWriteError):
            await save(
                service, fixture, assignment, editor, {"themes.0.questions.0.text": "correction"}
            )
        updated = await assigned(database, fixture)
        assert updated.library_viewing_rule == "anytime"
        async with database.sessions() as session:
            old = await session.get(PacketVersionRecord, assignment.adopted_version_id)
            assert old.state == "archived" and old.deleted_at is not None
            pinned = await session.scalar(
                select(GamePacketVersionRecord).where(
                    GamePacketVersionRecord.game_id == started.game.id
                )
            )
            assert pinned.packet_version_id == old.id
            historical = await PostgresPacketRepository().get(session, old.id)
            assert historical.packet.themes[0].questions[0].text == old_text
            _, old_rows = await service._version_fields(session, old)
            new = await session.get(PacketVersionRecord, updated.adopted_version_id)
            _, new_rows = await service._version_fields(session, new)
            assert old_rows[0][1][0][1].question_id == new_rows[0][1][0][1].question_id
            assert await session.get(
                TournamentPacketEntitlementRecord, (assignment.id, fixture.players[0].id)
            )
        assert not await tournaments.can_read_packet_library(
            fixture.tournament_id, fixture.packet_id, fixture.players[0].id
        )
        await service.management_action(
            fixture.tournament_id,
            assignment.id,
            fixture.manager.id,
            expected_version=2,
            delete=False,
        )
        assert await tournaments.can_read_packet_library(
            fixture.tournament_id, fixture.packet_id, fixture.players[0].id
        )
        assert not await tournaments.can_read_packet_library(
            fixture.tournament_id,
            fixture.packet_id,
            fixture.players[0].id,
            version_id=assignment.adopted_version_id,
        )
        games = PersistentGameService(database)
        await games.join(started.game.id, fixture.inputs[0].telegram_user_id)
        await games.advance(started.game.id)
        async with database.transaction() as session:
            game = await games._locked_game(session, started.game.id)
            await games._finish_abandoned_game(session, game, reason="test cleanup")
        async with database.sessions() as session:
            results = (
                await session.scalars(
                    select(GameResultRecord).where(GameResultRecord.game_id == started.game.id)
                )
            ).all()
            result_values = [(result.player_id, result.score, result.place) for result in results]
            assert result_values
        await service.management_action(
            fixture.tournament_id,
            assignment.id,
            fixture.manager.id,
            expected_version=2,
            delete=True,
        )
        assert not await tournaments.can_read_packet_library(
            fixture.tournament_id, fixture.packet_id, fixture.players[0].id
        )
        with pytest.raises(LookupError):
            await tournaments.require_packet_access(
                fixture.tournament_id, fixture.packet_id, fixture.players[0].id, "playable"
            )
        async with database.sessions() as session:
            assert (
                list(
                    (
                        await session.execute(
                            select(
                                GameResultRecord.player_id,
                                GameResultRecord.score,
                                GameResultRecord.place,
                            ).where(GameResultRecord.game_id == started.game.id)
                        )
                    ).tuples()
                )
                == result_values
            )
        with pytest.raises(LookupError):
            await edit(service, fixture, assignment)
    finally:
        await database.close()


@pytest.mark.parametrize("whole_theme", [False, True])
async def test_shared_corrections_and_tournament_only_substitutions(database_url, whole_theme):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        async with database.transaction() as session:
            shared = TournamentPacketAssignmentRecord(
                tournament_id=other.tournament_id,
                packet_id=fixture.packet_id,
                adopted_version_id=assignment.adopted_version_id,
                playable_by_members=True,
            )
            session.add(shared)
        editor = await edit(service, fixture, assignment)
        editor["packet"]["name"] = "Corrected name"
        await save(service, fixture, assignment, editor, {"name": "correction"})
        async with database.sessions() as session:
            assert (
                await session.get(TournamentPacketAssignmentRecord, shared.id)
            ).adopted_version_id == (
                await session.get(TournamentPacketAssignmentRecord, assignment.id)
            ).adopted_version_id
        editor = await edit(service, fixture, assignment)
        if whole_theme:
            async with database.transaction() as session:
                following = await session.get(TournamentPacketAssignmentRecord, shared.id)
                following.adopted_version_id = None
            editor["packet"]["themes"][0]["name"] = "Replacement theme"
            changes = {"themes.0.name": "substitution"}
        else:
            editor["packet"]["themes"][0]["questions"][0]["answer"] = "Replacement answer"
            changes = {"themes.0.questions.0.answer": "substitution"}
        await save(service, fixture, assignment, editor, changes)
        async with database.sessions() as session:
            current = await session.get(TournamentPacketAssignmentRecord, assignment.id)
            other_assignment = await session.get(TournamentPacketAssignmentRecord, shared.id)
            assert current.adopted_version_id != other_assignment.adopted_version_id
            old = await session.get(PacketVersionRecord, other_assignment.adopted_version_id)
            new = await session.get(PacketVersionRecord, current.adopted_version_id)
            assert old.state == "published" and old.deleted_at is None
            _, before = await service._version_fields(session, old)
            _, after = await service._version_fields(session, new)
            assert (before[0][0].theme_id != after[0][0].theme_id) is whole_theme
            for index in range(5):
                replaced = before[0][1][index][1].question_id != after[0][1][index][1].question_id
                assert replaced is (whole_theme or index == 0)
            notification = await session.scalar(
                select(PlayerNotificationRecord).where(
                    PlayerNotificationRecord.recipient_player_id == other.manager.id,
                    PlayerNotificationRecord.kind == "packet.substituted",
                )
            )
            assert notification.payload["manager_name"] == fixture.manager.public_nickname
            assert notification.payload["packet_name"] == "Corrected name"
        editor = await edit(service, fixture, assignment)
        editor["packet"]["name"] = "Corrected branch name"
        await save(service, fixture, assignment, editor, {"name": "correction"})
        async with database.sessions() as session:
            other_assignment = await session.get(TournamentPacketAssignmentRecord, shared.id)
            other_version = await session.get(
                PacketVersionRecord, other_assignment.adopted_version_id
            )
            assert other_version.name == "Corrected name" and other_version.state == "published"
            branch = await session.get(PacketVersionRecord, new.id)
            assert branch.state == "archived" and branch.deleted_at is not None
        with pytest.raises(PermissionError):
            await service.management_editor(
                fixture.tournament_id, assignment.id, fixture.players[0].id
            )
        with pytest.raises(LookupError):
            await service.management_editor(other.tournament_id, assignment.id, other.manager.id)
    finally:
        await database.close()


async def test_author_correction_transfers_attribution_and_preserves_burns(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        async with database.transaction() as session:
            author = AuthorRecord(display_name="Question author")
            replacement = AuthorRecord(display_name="Correct author")
            session.add_all(
                [author, replacement, PlatformAdministratorRecord(player_id=fixture.manager.id)]
            )
        links = AuthorLinkService(database)
        for player, target in zip(fixture.players[:2], (author, replacement), strict=True):
            request = await links.create_request(player.id, target.id)
            await links.decide_request(request.request_id, fixture.manager.id, approve=True)
        editor = await edit(service, fixture, assignment)
        editor["packet"]["themes"][0]["questions"][0]["author"] = author.display_name
        editor["field_author_ids"]["themes.0.questions.0.author"] = str(author.id)
        await save(
            service, fixture, assignment, editor, {"themes.0.questions.0.author": "correction"}
        )
        async with database.sessions() as session:
            old_burns = dict(
                (
                    await session.execute(
                        select(
                            PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.burnt_at
                        ).where(PlayerExposureClaimRecord.player_id == fixture.players[0].id)
                    )
                ).all()
            )
            assert len(old_burns) == 6
        editor = await edit(service, fixture, assignment)
        editor["packet"]["themes"][0]["questions"][0]["author"] = replacement.display_name
        editor["field_author_ids"]["themes.0.questions.0.author"] = str(replacement.id)
        await save(
            service, fixture, assignment, editor, {"themes.0.questions.0.author": "correction"}
        )
        request = await links.create_request(fixture.players[2].id, replacement.id)
        await links.decide_request(request.request_id, fixture.manager.id, approve=True)
        current = await assigned(database, fixture)
        async with database.sessions() as session:
            version = await session.get(PacketVersionRecord, current.adopted_version_id)
            _, rows = await service._version_fields(session, version)
            question = await session.get(LogicalQuestionRecord, rows[0][1][0][1].question_id)
            assert question.statistical_author_id == replacement.id
            assert (
                dict(
                    (
                        await session.execute(
                            select(
                                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.burnt_at
                            ).where(PlayerExposureClaimRecord.player_id == fixture.players[0].id)
                        )
                    ).all()
                )
                == old_burns
            )
            for player in fixture.players:
                burns = (
                    await session.scalars(
                        select(PlayerExposureClaimRecord).where(
                            PlayerExposureClaimRecord.player_id == player.id,
                            PlayerExposureClaimRecord.state == "burnt",
                        )
                    )
                ).all()
                assert len(burns) == 6
            assert (await links._author_summary(session, author)).authorship.question_count == 0
            assert (
                await links._author_summary(session, replacement)
            ).authorship.question_count == 1
    finally:
        await database.close()


@pytest.mark.parametrize("released", [False, True])
async def test_automatic_release_does_not_grant_read_eligibility(database_url, released):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        tournaments = TournamentService(database)
        settings = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
        await tournaments.update_policy(
            fixture.tournament_id,
            fixture.manager.id,
            default_parameters=settings.default_parameters,
            player_mutable_parameters=set(settings.player_mutable_parameters),
            policies={**settings.policies, "packets_released_by_default": released},
        )
        draft = await service.create_draft(
            packet(), source_filename="release.json", tournament_id=fixture.tournament_id
        )
        stored = await service.publish(draft, administrator_id=fixture.manager.id)
        async with database.sessions() as session:
            version = await session.get(PacketVersionRecord, stored.version_id)
            assert bool(version.library_released_at) is released
        assert not await tournaments.can_read_packet_library(
            fixture.tournament_id, stored.logical_id, fixture.players[0].id
        )
    finally:
        await database.close()


async def version_claims(session, version_id):
    themes = (
        await session.scalars(
            select(ThemeRevisionRecord).where(
                ThemeRevisionRecord.packet_version_id == version_id
            )
        )
    ).all()
    questions = (
        await session.scalars(
            select(QuestionRevisionRecord)
            .join(
                PacketQuestionRecord,
                PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
            )
            .where(PacketQuestionRecord.packet_version_id == version_id)
        )
    ).all()
    return {("theme", theme.theme_id) for theme in themes} | {
        ("question", question.question_id) for question in questions
    }


async def burnt_version_claims(database, player_id, version_id):
    async with database.sessions() as session:
        burns = (
            await session.scalars(
                select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.player_id == player_id,
                    PlayerExposureClaimRecord.packet_version_id == version_id,
                )
            )
        ).all()
    assert all(
        burn.state == "burnt" and burn.burnt_at is not None and burn.game_id is None
        for burn in burns
    )
    return {(burn.claim_namespace, burn.claim_id) for burn in burns}


async def insert_manager(database, fixture, player_id):
    async with database.transaction() as session:
        session.add(
            TournamentManagerRecord(
                tournament_id=fixture.tournament_id,
                player_id=player_id,
                granted_by_id=fixture.manager.id,
            )
        )


async def test_publication_burns_content_for_uploader_and_tournament_managers(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        await insert_manager(database, fixture, fixture.players[0].id)
        service = PacketAdminService(database)
        draft = await service.create_draft(
            packet(),
            source_filename="manager-burn.json",
            uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
        )
        stored = await service.publish(draft, administrator_id=fixture.manager.id)
        async with database.sessions() as session:
            expected = await version_claims(session, stored.version_id)
        for player_id in (fixture.manager.id, fixture.players[0].id):
            assert await burnt_version_claims(database, player_id, stored.version_id) == expected
        async with database.sessions() as session:
            assert (
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == fixture.players[1].id
                    )
                )
            ).all() == []
    finally:
        await database.close()


async def test_theme_substitution_burns_fresh_content_for_actor_and_managers(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        old_version_id = assignment.adopted_version_id
        await insert_manager(database, fixture, fixture.players[0].id)
        editor = await edit(service, fixture, assignment)
        editor["packet"]["themes"][0]["name"] = "Replacement theme"
        await save(service, fixture, assignment, editor, {"themes.0.name": "substitution"})
        async with database.sessions() as session:
            new_version_id = (
                await session.get(TournamentPacketAssignmentRecord, assignment.id)
            ).adopted_version_id
            assert new_version_id != old_version_id
            expected = await version_claims(session, new_version_id)
            old_expected = await version_claims(session, old_version_id)
        # The substituted theme and its questions received fresh identities.
        assert {identity for namespace, identity in expected if namespace == "theme"}.isdisjoint(
            {identity for namespace, identity in old_expected if namespace == "theme"}
        )
        # The editing actor and every fellow manager are burnt for the new version;
        # the plain member keeps fresh content.
        for player_id in (fixture.manager.id, fixture.players[0].id):
            assert await burnt_version_claims(database, player_id, new_version_id) == expected
        async with database.sessions() as session:
            assert (
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == fixture.players[1].id
                    )
                )
            ).all() == []
    finally:
        await database.close()


async def test_assigning_published_packet_burns_tournament_managers(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        other = await tournament_fixture(database, player_count=1)
        tournaments = TournamentService(database)
        await tournaments.add_manager(
            fixture.tournament_id, fixture.players[0].id, granted_by_id=fixture.manager.id
        )
        async with database.sessions() as session:
            foreign = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == other.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == other.packet_id,
                )
            )
            version_id = foreign.adopted_version_id
            expected = await version_claims(session, version_id)
        await tournaments.assign_packet(
            fixture.tournament_id, other.packet_id, fixture.manager.id, playable=True
        )
        for player_id in (fixture.manager.id, fixture.players[0].id):
            assert await burnt_version_claims(database, player_id, version_id) == expected
        async with database.sessions() as session:
            assert (
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == fixture.players[1].id,
                        PlayerExposureClaimRecord.packet_version_id == version_id,
                    )
                )
            ).all() == []
    finally:
        await database.close()


async def test_new_manager_retroactively_burns_assigned_packets(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        tournaments = TournamentService(database)
        async with database.sessions() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
                )
            )
            version_id = assignment.adopted_version_id
            expected = await version_claims(session, version_id)
        await tournaments.add_manager(
            fixture.tournament_id, fixture.players[0].id, granted_by_id=fixture.manager.id
        )
        assert (
            await burnt_version_claims(database, fixture.players[0].id, version_id) == expected
        )
        async with database.sessions() as session:
            assert (
                await session.scalars(
                    select(PlayerExposureClaimRecord).where(
                        PlayerExposureClaimRecord.player_id == fixture.players[1].id
                    )
                )
            ).all() == []
    finally:
        await database.close()
