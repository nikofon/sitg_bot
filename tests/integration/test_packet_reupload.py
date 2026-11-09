import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture
from test_packet_management import assigned, edit, save

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    GamePacketVersionRecord,
    PacketDraftRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    QuestionRevisionRecord,
    TournamentManagerRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
)
from sitg_bot.storage.packets import PostgresPacketRepository

pytestmark = pytest.mark.integration
database_url = _database_url


async def test_reupload_preserves_shared_identities_rights_and_game_snapshot(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        async with database.transaction() as session:
            session.add(TournamentManagerRecord(
                tournament_id=fixture.tournament_id, player_id=other.manager.id,
            ))
        await service.existing_packet(
            other.tournament_id, fixture.packet_id, other.manager.id,
            expected_version_id=assignment.adopted_version_id,
        )
        async with database.transaction() as session:
            current = await session.get(TournamentPacketAssignmentRecord, assignment.id)
            current.library_viewing_rule = "anytime"
            session.add(TournamentPacketEntitlementRecord(
                assignment_id=assignment.id, player_id=fixture.players[0].id,
                content_visible=True, discoverable=False, playable=True,
            ))
            version = await session.get(PacketVersionRecord, assignment.adopted_version_id)
            version.library_released_at = datetime.now(UTC)
        matchmaking = InvitationMatchmakingService(database)
        lobby = await matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1,
        )
        await matchmaking.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id,
        )
        await matchmaking.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        started = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert started.started
        async with database.sessions() as session:
            old = await PostgresPacketRepository().get(session, assignment.adopted_version_id)
            old_version = await session.get(PacketVersionRecord, old.version_id)
            _, old_rows = await service._version_fields(session, old_version)
            exposure_before = (await session.execute(select(
                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.claim_id,
            ).where(PlayerExposureClaimRecord.player_id == fixture.players[0].id))).all()
        editor = await edit(service, fixture, assignment)
        prepared = await service.prepare_reupload(
            fixture.tournament_id, assignment.id, fixture.manager.id, expected_version=1,
        )
        draft_id = UUID(prepared["draft_id"])
        content = deepcopy(editor["packet"])
        content["name"] = "Reinterpreted packet"
        content["themes"][0]["name"] = "Reinterpreted theme"
        content["themes"][0]["authors"] = ["Source spelling"]
        content["themes"][0]["questions"][0]["text"] = "Corrected parsing"
        imported = await service.import_reupload(
            draft_id, fixture.manager.id, json.dumps(content).encode(),
            source_filename="fixed.json",
        )
        assert imported["can_publish"]
        preview = await service.editable_draft(draft_id, fixture.manager.id)
        assert "_reupload_target" not in preview["packet"]
        content["themes"][0]["questions"][0]["commentary"] = "Reviewed in upload editor"
        # An injected target cannot redirect this draft.
        content["_reupload_target"] = {"assignment_id": str(UUID(int=999))}
        author = await service.create_author(
            draft_id, fixture.manager.id, first_name="Resolved", second_name=None,
            surname="Author", telegram_link=None,
        )
        updated = await service.update_draft(
            draft_id, fixture.manager.id, expected_version=imported["version"], content=content,
            author_bindings={"Source spelling": UUID(author["author_id"])},
            lead_author_id=UUID(author["author_id"]),
        )
        assert updated["reupload_target"] == prepared["reupload_target"]
        with pytest.raises(StaleWriteError):
            await service.publish(
                draft_id, administrator_id=fixture.manager.id, expected_version=imported["version"],
            )
        new = await service.publish(
            draft_id, administrator_id=fixture.manager.id, expected_version=updated["version"],
        )
        assert new.logical_id == old.logical_id
        assert new.version_id != old.version_id
        assert new.packet.lead_author == author["display_name"]
        assert new.packet.themes[0].authors == (author["display_name"],)
        async with database.sessions() as session:
            new_version = await session.get(PacketVersionRecord, new.version_id)
            _, new_rows = await service._version_fields(session, new_version)
            assert [t.theme_id for t, _ in new_rows] == [t.theme_id for t, _ in old_rows]
            assert [q.question_id for _, rows in new_rows for _, q in rows] == [
                q.question_id for _, rows in old_rows for _, q in rows
            ]
            assignments = (await session.scalars(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            ))).all()
            assert len(assignments) == 2
            assert all(a.adopted_version_id == new.version_id for a in assignments)
            current = await session.get(TournamentPacketAssignmentRecord, assignment.id)
            assert current.library_viewing_rule == "anytime"
            entitlement = await session.get(
                TournamentPacketEntitlementRecord, (assignment.id, fixture.players[0].id),
            )
            assert entitlement.content_visible and entitlement.playable
            assert entitlement.discoverable is False
            historical = await PostgresPacketRepository().get(session, old.version_id)
            assert historical.packet == old.packet
            assert (await session.scalar(select(GamePacketVersionRecord).where(
                GamePacketVersionRecord.packet_version_id == old.version_id,
            ))) is not None
            revision = await session.get(QuestionRevisionRecord, old_rows[0][1][0][1].id)
            assert revision.text == old.packet.themes[0].questions[0].text
            version = await session.get(PacketVersionRecord, new.version_id)
            assert version.change_type == "correction"
            assert version.previous_version_id == old.version_id
            assert version.source_draft_id == draft_id
            assert version.library_released_at is not None
            assert exposure_before == (await session.execute(select(
                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.claim_id,
            ).where(PlayerExposureClaimRecord.player_id == fixture.players[0].id))).all()
    finally:
        await database.close()


async def test_reupload_intake_shape_authorization_rejection_and_stale_target(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        editor = await edit(service, fixture, assignment)
        with pytest.raises(PermissionError):
            await service.prepare_reupload(
                fixture.tournament_id, assignment.id, fixture.players[0].id, expected_version=1,
            )
        prepared = await service.prepare_reupload(
            fixture.tournament_id, assignment.id, fixture.manager.id, expected_version=1,
        )
        draft_id = UUID(prepared["draft_id"])
        source = json.dumps(editor["packet"]).encode()
        with pytest.raises(PermissionError):
            await service.import_reupload(
                draft_id, fixture.players[0].id, source, source_filename="fixed.json",
            )
        for payload in ([editor["packet"], editor["packet"]], {
            **editor["packet"],
            "themes": [*editor["packet"]["themes"], editor["packet"]["themes"][0]],
        }):
            with pytest.raises(ValueError):
                await service.import_reupload(
                    draft_id, fixture.manager.id, json.dumps(payload).encode(),
                    source_filename="wrong.json",
                )
        assert (await service.draft_summary(draft_id, fixture.manager.id))["status"] == "uploaded"
        imported = await service.import_reupload(
            draft_id, fixture.manager.id, source, source_filename="fixed.json",
        )
        altered = deepcopy(editor["packet"])
        altered["themes"][0]["questions"].pop()
        with pytest.raises(ValueError, match="same theme count"):
            await service.update_draft(
                draft_id, fixture.manager.id, expected_version=imported["version"], content=altered,
            )
        with pytest.raises(PermissionError):
            await service.publish(draft_id, administrator_id=fixture.players[0].id)
        with pytest.raises(StaleWriteError):
            await service.publish(draft_id, administrator_id=fixture.manager.id)
        editor["packet"]["name"] = "Concurrent correction"
        await save(service, fixture, assignment, editor, {"name": "correction"})
        with pytest.raises(StaleWriteError):
            await service.publish(
                draft_id, administrator_id=fixture.manager.id, expected_version=imported["version"],
            )
        await service.reject(draft_id, actor_id=fixture.manager.id)
        current = await assigned(database, fixture)
        prepared = await service.prepare_reupload(
            fixture.tournament_id, assignment.id, fixture.manager.id, expected_version=2,
        )
        cancelled_id = UUID(prepared["draft_id"])
        await service.reject(cancelled_id, actor_id=fixture.manager.id)
        assert (await assigned(database, fixture)).adopted_version_id == current.adopted_version_id
    finally:
        await database.close()


async def test_reupload_revoked_manager_and_retired_assignment_cannot_confirm(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        editor = await edit(service, fixture, assignment)
        prepared = await service.prepare_reupload(
            fixture.tournament_id, assignment.id, fixture.manager.id, expected_version=1,
        )
        draft_id = UUID(prepared["draft_id"])
        imported = await service.import_reupload(
            draft_id, fixture.manager.id, json.dumps(editor["packet"]).encode(),
            source_filename="fixed.json",
        )
        async with database.transaction() as session:
            manager = await session.get(
                TournamentManagerRecord, (fixture.tournament_id, fixture.manager.id),
            )
            manager.revoked_at = datetime.now(UTC)
        for operation in (
            service.editable_draft(draft_id, fixture.manager.id),
            service.publish(
                draft_id, administrator_id=fixture.manager.id, expected_version=imported["version"],
            ),
        ):
            with pytest.raises(PermissionError):
                await operation
        async with database.transaction() as session:
            manager = await session.get(
                TournamentManagerRecord, (fixture.tournament_id, fixture.manager.id),
            )
            manager.revoked_at = None
        await service.management_action(
            fixture.tournament_id, assignment.id, fixture.manager.id,
            expected_version=1, delete=True,
        )
        with pytest.raises(LookupError):
            await service.publish(
                draft_id, administrator_id=fixture.manager.id, expected_version=imported["version"],
            )
    finally:
        await database.close()


async def test_reupload_confirmation_revalidates_shape_and_serializes_double_submit(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = PacketAdminService(database)
        assignment = await assigned(database, fixture)
        editor = await edit(service, fixture, assignment)
        prepared = await service.prepare_reupload(
            fixture.tournament_id, assignment.id, fixture.manager.id, expected_version=1,
        )
        draft_id = UUID(prepared["draft_id"])
        imported = await service.import_reupload(
            draft_id, fixture.manager.id, json.dumps(editor["packet"]).encode(),
            source_filename="fixed.json",
        )
        async with database.transaction() as session:
            draft = await session.get(PacketDraftRecord, draft_id)
            original_content = deepcopy(draft.content)
            malformed = deepcopy(original_content)
            malformed["themes"][0]["questions"].pop()
            draft.content = malformed
        with pytest.raises(ValueError, match="same theme count"):
            await service.publish(
                draft_id, administrator_id=fixture.manager.id, expected_version=imported["version"],
            )
        async with database.transaction() as session:
            draft = await session.get(PacketDraftRecord, draft_id)
            draft.content = original_content
        results = await asyncio.gather(*(
            service.publish(
                draft_id, administrator_id=fixture.manager.id, expected_version=imported["version"],
            ) for _ in range(2)
        ), return_exceptions=True)
        assert sum(isinstance(result, StaleWriteError) for result in results) == 1
        assert sum(not isinstance(result, Exception) for result in results) == 1
        async with database.sessions() as session:
            versions = (await session.scalars(select(PacketVersionRecord).where(
                PacketVersionRecord.packet_id == fixture.packet_id,
            ))).all()
            assert len(versions) == 2
    finally:
        await database.close()
