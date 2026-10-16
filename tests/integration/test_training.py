import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.domain.training import TrainingSettings, text_digest
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.library import PacketLibraryService
from sitg_bot.services.reliable_scheduling import AutomaticJobScheduler
from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    GameRecord,
    LogicalPacketRecord,
    PacketDraftRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerExposureClaimRecord,
    QuestionRevisionRecord,
    RatingLedgerRecord,
    ThemeRevisionRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TrainingAttemptRecord,
    TrainingQuestionRecord,
    TrainingSessionRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def upload(database, owner):
    packets = TrainingPacketService(database)
    source = json.dumps(
        {
            "name": "Private <training>",
            "themes": [
                {
                    "name": "Theme",
                    "questions": [
                        {
                            "value": value,
                            "text": "One two three <four> five",
                            "answer": "Paris",
                            "accepted_answers": ["City of Paris"],
                            "rejected_answers": ["Paris Texas"],
                        }
                        for value in (10, 20, 30, 40, 50)
                    ],
                }
            ],
        }
    ).encode()
    result = await packets.import_upload(owner, source, "training.json")
    draft = result["drafts"][0]
    assert draft["errors"] == []
    published = await packets.decide_draft(
        owner,
        UUID(draft["draft_id"]),
        expected_version=draft["version"],
        publish=True,
    )
    return UUID(published["version_id"]), UUID(draft["draft_id"])


async def test_owner_isolation_private_publication_and_retirement(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        owner, other = (player.id for player in fixture.players)
        version_id, draft_id = await upload(database, owner)
        service = TrainingPacketService(database)
        assert str(version_id) in {
            str(c["id"]) for c in (await service.list_packets(owner))["items"]
        }
        assert str(version_id) not in {
            str(c["id"]) for c in (await service.list_packets(other))["items"]
        }
        with pytest.raises(LookupError):
            await service.draft(other, draft_id)
        async with database.transaction() as session:
            session.add(PlatformAdministratorRecord(player_id=fixture.manager.id))
        with pytest.raises(LookupError):
            await PacketLibraryService(database).access(
                fixture.manager.id,
                version_id,
                administrator=True,
                confirm=True,
                request_key="private",
            )
        async with database.transaction() as session:
            version = await session.get(PacketVersionRecord, version_id)
            assert not await session.scalar(
                select(TournamentPacketAssignmentRecord.id).where(
                    TournamentPacketAssignmentRecord.packet_id == version.packet_id,
                )
            )
        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                session.add(
                    TournamentPacketAssignmentRecord(
                        tournament_id=fixture.tournament_id,
                        packet_id=version.packet_id,
                    )
                )
                await session.flush()
        training = TrainingService(database)
        with pytest.raises(LookupError):
            await training.start(other, version_id, confirm=True)
        async with database.sessions() as session:
            version = await session.get(PacketVersionRecord, version_id)
            draft = await session.get(PacketDraftRecord, draft_id)
            packet = service.packets._packet_from_content(draft.content)
            assert await service.packets._similar_packet_warnings(session, packet) == []
        result = await service.access(
            owner, version_id, confirm=True, download=True, request_key="private-download"
        )
        assert result["queued"]
    finally:
        await database.close()


async def test_finish_restart_replay_stale_commands_and_no_competitive_accounting(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        version_id, _ = await upload(database, owner)
        training = TrainingService(database)
        now = datetime.now(UTC)
        async with database.sessions() as session:
            baseline = [
                await session.scalar(select(func.count()).select_from(model))
                for model in (GameRecord, RatingLedgerRecord)
            ]
        confirmation = await training.start(owner, version_id, now=now)
        assert confirmation["confirmation_required"]
        state = (await training.start(owner, version_id, confirm=True, now=now))["session"]
        session_id = UUID(state["id"])
        pinned = state["question_revision_id"]
        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                revision = await session.get(QuestionRevisionRecord, UUID(pinned))
                revision.text = "Edited after pinning"
                await session.flush()
        for index in range(5):
            training = TrainingService(database)
            state = (await training.view(owner, session_id, now=now))["session"]
            assert state["question_index"] == index
            params = {
                "expected_version": state["version"],
                "question_revision_id": UUID(state["question_revision_id"]),
                "now": now,
            }
            state = (await training.act(owner, session_id, "buzz", **params))["session"]
            with pytest.raises(StaleWriteError):
                await training.act(owner, session_id, "buzz", **params)
            params["expected_version"] = state["version"]
            state = (await training.act(owner, session_id, "answer", answer="Париж", **params))[
                "session"
            ]
            # Use accepted English spelling; Russian translation is deliberately not inferred.
            assert state["correct"] is False
            params["expected_version"] = state["version"]
            state = (await training.act(owner, session_id, "next", **params))["session"]
        assert state["status"] == "completed"
        replay = await training.start(owner, version_id, now=now)
        assert replay["session"]["question_revision_id"] == pinned
        async with database.sessions() as session:
            assert baseline == [
                await session.scalar(select(func.count()).select_from(model))
                for model in (GameRecord, RatingLedgerRecord)
            ]
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingAttemptRecord)
                    .where(
                        TrainingAttemptRecord.session_id == session_id,
                    )
                )
                == 5
            )
    finally:
        await database.close()


async def test_hidden_deadline_recovery_and_best_buzz_continue_once(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        version_id, _ = await upload(database, owner)
        now = datetime.now(UTC)
        training = TrainingService(database)
        state = (
            await training.start(
                owner,
                version_id,
                confirm=True,
                now=now,
                settings=TrainingSettings(hidden_after=2, reversed=True),
            )
        )["session"]
        session_id = UUID(state["id"])
        assert state["text"] == "five <four> three two One"
        await training.act(
            owner, session_id, "pause", expected_version=state["version"],
            question_revision_id=UUID(state["question_revision_id"]),
            now=now + timedelta(seconds=1),
        )
        queue = AsyncMock()
        await AutomaticJobScheduler(database, queue).reconcile(now=now + timedelta(seconds=1))
        assert any(call.args[0] == "training.deadline"
                   and call.args[2].get("session_id") == str(session_id)
                   for call in queue.schedule.await_args_list)
        await training.tick(session_id, now=now + timedelta(seconds=3))
        state = (
            await TrainingService(database).view(
                owner,
                session_id,
                now=now + timedelta(seconds=3),
            )
        )["session"]
        assert state["hidden"] and state["text"] == ""
        assert state["status"] == "paused"
        async with database.sessions() as session:
            assert (await session.get(TrainingSessionRecord, session_id)).text_hidden
        await training.act(
            owner,
            session_id,
            "stop",
            expected_version=state["version"],
            question_revision_id=UUID(state["question_revision_id"]),
            now=now + timedelta(seconds=3),
        )

        async def prepared(session, player_id, source):
            return {
                q.revision_id: {
                    "schema_version": 1,
                    "tokenizer_version": "announcement-v1",
                    "question_revision_id": str(q.revision_id),
                    "text_digest": text_digest(q.text),
                    "source_end": 7,
                }
                for q in source.questions
            }

        provider = AsyncMock()
        provider.prepared.side_effect = prepared
        training = TrainingService(database, best_buzz=provider)
        state = (
            await training.start(
                owner,
                version_id,
                now=now,
                settings=TrainingSettings(best_buzz=True, token_target_chars=3),
            )
        )["session"]
        session_id = UUID(state["id"])
        state = (await training.view(owner, session_id, now=now + timedelta(seconds=2)))["session"]
        assert state["phase"] == "best_buzz" and state["text"] == "One two"
        params = {
            "expected_version": state["version"],
            "question_revision_id": UUID(state["question_revision_id"]),
            "now": now + timedelta(seconds=2),
        }
        await training.act(owner, session_id, "continue", **params)
        with pytest.raises(StaleWriteError):
            await training.act(owner, session_id, "continue", **params)
        state = (await training.view(owner, session_id, now=now + timedelta(seconds=4)))["session"]
        assert state["phase"] != "best_buzz" and state["token_cursor"] > 2
        await TrainingPacketService(database).retire(owner, version_id)
        with pytest.raises(LookupError):
            await training.view(owner, session_id)
        await training.tick(session_id)
        async with database.sessions() as session:
            assert (await session.get(TrainingSessionRecord, session_id)).status == "stopped"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingQuestionRecord)
                    .where(
                        TrainingQuestionRecord.session_id == session_id,
                    )
                )
                == 5
            )
    finally:
        await database.close()


async def test_library_read_rules_replay_revocation_bans_and_cross_mode(database_url):
    from sitg_bot.services.matchmaking import InvitationMatchmakingService
    from sitg_bot.services.navigation import TelegramNavigationService
    from sitg_bot.storage.models import PlayerBanRecord

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        packets = TrainingPacketService(database)
        async with database.transaction() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
                )
            )
            version_id = assignment.adopted_version_id
            assignment.content_visible_by_members = True
            assignment.library_viewing_rule = "anytime"
            (await session.get(PacketVersionRecord, version_id)).library_released_at = datetime.now(
                UTC
            )
        assert version_id in {item["id"] for item in (await packets.list_packets(owner))["items"]}
        training = TrainingService(database)
        now = datetime.now(UTC)
        state = (await training.start(owner, version_id, confirm=True, now=now))["session"]
        navigation = await TelegramNavigationService(database).snapshot(
            fixture.inputs[0].telegram_user_id
        )
        assert navigation.active_mode == "training"
        assert navigation.active_training_id == UUID(state["id"])
        with pytest.raises(ValueError, match="Stop training"):
            await InvitationMatchmakingService(database).create_lobby(
                fixture.inputs[0],
                tournament_id=fixture.tournament_id,
            )
        # Readability is distinct from the packet's permanent burnt state.
        async with database.transaction() as session:
            session.add(
                TournamentPacketEntitlementRecord(
                    assignment_id=assignment.id,
                    player_id=owner,
                    readable_override=False,
                )
            )
        assert version_id not in {
            item["id"] for item in (await packets.list_packets(owner))["items"]
        }
        assert (await training.current(owner, now=now))["session"] is None
        async with database.transaction() as session:
            session.add(
                PlayerBanRecord(
                    player_id=owner, banned_by_id=fixture.manager.id, reason="test", banned_at=now
                )
            )
        with pytest.raises(PermissionError):
            await packets.list_packets(owner)
        with pytest.raises(PermissionError):
            await training.start(owner, version_id, confirm=True)
    finally:
        await database.close()


async def test_training_page_has_bounded_reads_filters_and_stable_cursor(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        async with database.transaction() as session:
            for index in range(29):
                draft = PacketDraftRecord(
                    scope="training",
                    uploader_id=owner,
                    content={},
                    status="published",
                    source_filename="fixture.json",
                    source_checksum="0" * 64,
                )
                logical = LogicalPacketRecord(scope="training", uploader_id=owner)
                session.add_all([draft, logical])
                await session.flush()
                session.add(
                    PacketVersionRecord(
                        packet_id=logical.id,
                        version_number=1,
                        source_draft_id=draft.id,
                        name=f"Bounded {index:02}",
                    )
                )
        reads = []

        def count(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                reads.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", count)
        try:
            service = TrainingPacketService(database)
            first = await service.list_packets(owner, name="Bounded")
            assert len(reads) == 3
            assert len(first["items"]) == 25
            reads.clear()
            second = await service.list_packets(owner, name="Bounded", cursor=first["next_cursor"])
            assert len(reads) == 3
            assert len(second["items"]) == 4 and second["next_cursor"] is None
            assert not {c["id"] for c in first["items"]} & {c["id"] for c in second["items"]}
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", count)
    finally:
        await database.close()


async def test_reserved_content_and_parallel_training_starts(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        version_id, _ = await upload(database, owner)
        async with database.transaction() as session:
            theme_id = await session.scalar(
                select(ThemeRevisionRecord.theme_id).where(
                    ThemeRevisionRecord.packet_version_id == version_id,
                )
            )
            claim = PlayerExposureClaimRecord(
                player_id=owner,
                packet_version_id=version_id,
                claim_namespace="theme",
                claim_id=theme_id,
                state="reserved",
            )
            session.add(claim)
        with pytest.raises(PermissionError, match="reserved"):
            await TrainingService(database).start(owner, version_id, confirm=True)
        async with database.transaction() as session:
            await session.delete(await session.get(PlayerExposureClaimRecord, claim.id))
        now = datetime.now(UTC)
        results = await asyncio.gather(
            *(
                TrainingService(database).start(owner, version_id, confirm=True, now=now)
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        initial = next(result["session"] for result in results if isinstance(result, dict))
        training = TrainingService(database)
        advanced = (
            await training.view(owner, UUID(initial["id"]), now=now + timedelta(seconds=1))
        )["session"]
        assert advanced["token_cursor"] > initial["token_cursor"]
        assert advanced["version"] == initial["version"]
        buzzed = (
            await training.act(
                owner,
                UUID(initial["id"]),
                "buzz",
                expected_version=initial["version"],
                question_revision_id=UUID(initial["question_revision_id"]),
                now=now + timedelta(seconds=1),
            )
        )["session"]
        assert buzzed["phase"] == "answering"
        paused = (
            await training.act(
                owner,
                UUID(initial["id"]),
                "pause",
                expected_version=buzzed["version"],
                question_revision_id=UUID(initial["question_revision_id"]),
                now=now + timedelta(seconds=1),
            )
        )["session"]
        assert paused["status"] == "paused"
        await TrainingPacketService(database).retire(owner, version_id)
        assert (await training.current(owner))["session"] is None
    finally:
        await database.close()
