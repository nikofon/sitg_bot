"""Training history read projection: ownership, pagination, visibility, batching.

Runs against the disposable per-test database (conftest training_database);
never the running training-mode copy. The projection is read-only: no
competitive tables, no mutations of gameplay state.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import create_player
from sqlalchemy import event, select
from test_training import upload

from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_history import TrainingHistoryService
from sitg_bot.storage.models import (
    GameRecord,
    RatingLedgerRecord,
    TrainingAttemptRecord,
    TrainingQuestionRecord,
    TrainingSessionRecord,
)

pytestmark = pytest.mark.integration


async def read_select_count(database, operation):
    counter = 0

    def count(connection, cursor, statement, parameters, context, executemany):
        nonlocal counter
        if statement.lstrip().upper().startswith("SELECT"):
            counter += 1

    event.listen(database.engine.sync_engine, "before_cursor_execute", count)
    try:
        await operation
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", count)
    return counter


async def play_one_question(database, owner, session_id, *, answer, now):
    training = TrainingService(database)
    state = (await training.view(owner, session_id, now=now))["session"]
    from test_training import open_question

    state, now = await open_question(training, owner, state, now)
    state = (
        await training.act(
            owner, session_id, "buzz", expected_version=state["version"],
            question_revision_id=UUID(state["question_revision_id"]), now=now,
        )
    )["session"]
    return (
        await training.act(
            owner, session_id, "answer", answer=answer, expected_version=state["version"],
            question_revision_id=UUID(state["question_revision_id"]), now=now,
        )
    )["session"]


async def test_owner_isolation_list_and_detail(database):
    owner = await create_player(database, 9201, "owner")
    other = await create_player(database, 9202, "other")
    version_id, _ = await upload(database, owner)
    training = TrainingService(database)
    now = datetime.now(UTC)
    state = (
        await training.start(owner, version_id, confirm=True, now=now)
    )["session"]
    session_id = UUID(state["id"])
    await play_one_question(database, owner, session_id, answer="Paris", now=now)

    history = TrainingHistoryService(database)
    listing = await history.list_sessions(other)
    assert listing["items"] == [] and listing["next_cursor"] is None
    with pytest.raises(LookupError):
        await history.session_detail(other, session_id)
    # The owner still sees the run; other players never learn it exists.
    own = await history.list_sessions(owner)
    assert [item["id"] for item in own["items"]] == [str(session_id)]
    detail = await history.session_detail(owner, session_id)
    assert detail["id"] == str(session_id)
    assert detail["packets"][0]["name"] == "Private <training>"
    assert detail["score"] == 10  # first question value answered correctly
    first = detail["rows"][0]
    assert first["own"]["answer"] == "Paris" and first["own"]["correct"] is True
    # The intake writer has no timing columns. Combined gameplay records these
    # fields; legacy null behavior is also exercised by the pure projection tests.
    if not hasattr(TrainingQuestionRecord, "buzzed_at"):
        assert first["buzz"] is None
        assert first["stats"]["buzz_timings"] is None
    # The run is still active: unvisited questions stay metadata-only.
    assert [row["protected"] for row in detail["rows"]] == [None, "pending", "pending",
                                                            "pending", "pending"]
    assert detail["rows"][1]["text"] is None and detail["rows"][1]["answer"] is None


async def test_list_pagination_stable_boundaries_and_batched_projection(database):
    owner = await create_player(database, 9211, "pager")
    version_id, _ = await upload(database, owner)
    # Pin the packet's questions directly: sessions are inserted with explicit
    # distinct timestamps so the keyset boundary is deterministic.
    async with database.sessions() as session:
        from sitg_bot.storage.models import PacketQuestionRecord

        pinned = list(
            await session.execute(
                select(
                    PacketQuestionRecord.question_revision_id,
                    PacketQuestionRecord.theme_revision_id,
                    PacketQuestionRecord.value,
                ).where(PacketQuestionRecord.packet_version_id == version_id)
                .order_by(PacketQuestionRecord.position)
            )
        )
    history = TrainingHistoryService(database)
    async with database.transaction() as session:
        for index in range(12):
            record = TrainingSessionRecord(
                owner_id=owner,
                packet_version_id=version_id,
                disclosure_receipt={},
                settings={"reversed": index % 2 == 0},
                status="completed" if index % 3 else "stopped",
                phase="revealed",
                question_index=0,
                ended_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index),
            )
            record.created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index // 3)
            session.add(record)
            await session.flush()
            for position, (question_revision, theme_revision, value) in enumerate(pinned):
                session.add(
                    TrainingQuestionRecord(
                        session_id=record.id,
                        position=position,
                        question_revision_id=question_revision,
                        theme_revision_id=theme_revision,
                        value=value,
                    )
                )
            # Correct on the first question only, so every session scores +10.
            session.add(
                TrainingAttemptRecord(
                    session_id=record.id,
                    player_id=owner,
                    question_revision_id=pinned[0][0],
                    question_index=0,
                    answer="Paris",
                    correct=True,
                    outcome="answered",
                    token_cursor=3,
                )
            )
    # Explicit timestamps make newest-first order equal insertion order reversed.
    listing = await history.list_sessions(owner, limit=5)
    collected = [item["id"] for item in listing["items"]]
    stamps = [(item["created_at"], item["id"]) for item in listing["items"]]
    cursor = listing["next_cursor"]
    guard = 0
    while cursor is not None and guard < 10:
        page = await history.list_sessions(owner, cursor=cursor, limit=5)
        collected.extend(item["id"] for item in page["items"])
        stamps.extend((item["created_at"], item["id"]) for item in page["items"])
        cursor = page["next_cursor"]
        guard += 1
    assert guard < 10
    assert len(collected) == 12 and len(set(collected)) == 12
    assert stamps == sorted(stamps, reverse=True)
    # Each card carries packets, score, settings and derived modifiers.
    for item in listing["items"]:
        assert item["packets"] and item["packets"][0]["name"]
        assert item["score"] == 10 and item["question_total"] == 5
        assert item["modifiers"] == (["reversed"] if item["settings"]["reversed"] else [])
    # An invalid cursor is a validation error, not a silent reset.
    with pytest.raises(ValueError):
        await history.list_sessions(owner, cursor="not-a-cursor")
    with pytest.raises(ValueError):
        await history.list_sessions(owner, limit=26)

    # Batched projection: SELECT count stays bounded regardless of page size.
    small = await read_select_count(database, history.list_sessions(owner, limit=3))
    full = await read_select_count(database, history.list_sessions(owner, limit=12))
    assert small == full == 5  # account/ban + sessions + grouped score + grouped totals
    another_owner = await create_player(database, 9212, "cursor-owner")
    with pytest.raises(ValueError):
        await history.list_sessions(another_owner, cursor=listing["next_cursor"])


async def test_detail_pagination_and_training_only_statistics(database):
    owner = await create_player(database, 9221, "statistic")
    version_id, _ = await upload(database, owner)
    training = TrainingService(database)
    now = datetime.now(UTC)
    first = (await training.start(owner, version_id, confirm=True, now=now))["session"]
    await play_one_question(database, owner, UUID(first["id"]), answer="Paris", now=now)
    state = (await training.view(owner, UUID(first["id"]), now=now))["session"]
    await training.act(
        owner, UUID(first["id"]), "next", expected_version=state["version"],
        question_revision_id=UUID(state["question_revision_id"]), now=now,
    )
    await play_one_question(database, owner, UUID(first["id"]), answer="London", now=now)
    state = (await training.view(owner, UUID(first["id"]), now=now))["session"]
    await training.act(
        owner, UUID(first["id"]), "stop", expected_version=state["version"],
        question_revision_id=UUID(state["question_revision_id"]), now=now,
    )

    history = TrainingHistoryService(database)
    detail = await history.session_detail(owner, UUID(first["id"]), limit=1)
    assert [row["position"] for row in detail["rows"]] == [0]
    rows = list(detail["rows"])
    cursor = detail["next_cursor"]
    guard = 0
    while cursor is not None and guard < 10:
        page = await history.session_detail(owner, UUID(first["id"]), cursor=cursor, limit=2)
        rows.extend(page["rows"])
        cursor = page["next_cursor"]
        guard += 1
    assert guard < 10 and len(rows) == 5
    # A stopped run exposes the played questions and their recorded answers.
    assert rows[0]["text"] and rows[0]["answer"] == "Paris"
    assert rows[1]["answer"] == "Paris" and rows[1]["own"]["correct"] is False
    # Effective score: +10 for the correct answer, -20 for the wrong one.
    assert detail["score"] == -10
    # Training-only statistics aggregate this player's training attempts.
    assert rows[0]["stats"]["attempts"] == 1 and rows[0]["stats"]["correct"] == 1
    assert rows[1]["stats"]["incorrect"] == 1
    # Ending a run never discloses its unvisited tail, even though the entire
    # packet was pinned/burnt at start (also true after schema 0027 is merged).
    assert rows[4]["protected"] == "pending" and rows[4]["text"] is None
    # Cross-session cursor reuse is rejected.
    with pytest.raises(ValueError):
        await history.session_detail(owner, UUID(first["id"]), cursor="bogus")

    # A replay session on the same packet must not mutate the first run's rows.
    second = (await training.start(owner, version_id, now=now))["session"]
    await play_one_question(database, owner, UUID(second["id"]), answer="Paris", now=now)
    replay = await history.session_detail(owner, UUID(second["id"]))
    assert replay["rows"][0]["stats"]["attempts"] == 2
    assert replay["rows"][0]["stats"]["correct"] == 2
    assert replay["score"] == 10
    first_again = await history.session_detail(owner, UUID(first["id"]))
    assert first_again["score"] == -10
    small = await read_select_count(
        database, history.session_detail(owner, UUID(first["id"]), limit=1)
    )
    full = await read_select_count(
        database, history.session_detail(owner, UUID(first["id"]), limit=5)
    )
    assert small == full and full <= 16
    # A valid cursor is still bound to the original session.
    with pytest.raises(ValueError):
        await history.session_detail(owner, UUID(second["id"]), cursor=detail["next_cursor"])
    # Nothing leaked into competitive accounting.
    async with database.sessions() as session:
        from sqlalchemy import func

        assert await session.scalar(select(func.count()).select_from(GameRecord)) == 0
        assert await session.scalar(
            select(func.count()).select_from(RatingLedgerRecord)
        ) == 0


async def test_retired_source_keeps_metadata_but_denies_protected_text(database):
    owner = await create_player(database, 9231, "retired")
    version_id, _ = await upload(database, owner)
    training = TrainingService(database)
    now = datetime.now(UTC)
    started = (await training.start(owner, version_id, confirm=True, now=now))["session"]
    session_id = UUID(started["id"])
    await play_one_question(database, owner, session_id, answer="Paris", now=now)
    state = (await training.view(owner, session_id, now=now))["session"]
    await training.act(
        owner, session_id, "stop", expected_version=state["version"],
        question_revision_id=UUID(state["question_revision_id"]), now=now,
    )
    from sitg_bot.services.training_packets import TrainingPacketService

    await TrainingPacketService(database).retire(owner, version_id)

    history = TrainingHistoryService(database)
    # The list keeps every card with packet metadata.
    listing = await history.list_sessions(owner)
    assert [item["id"] for item in listing["items"]] == [str(session_id)]
    assert listing["items"][0]["packets"][0]["name"] == "Private <training>"
    # The detail keeps the summary but denies protected question text.
    detail = await history.session_detail(owner, session_id)
    assert detail["protected_source"] is True
    assert detail["packets"][0]["name"] == "Private <training>"
    for row in detail["rows"]:
        assert row["protected"] == "revoked"
        assert row["text"] is None and row["answer"] is None and row["theme"] is None
    # The owner's own attempt facts (their answers) remain visible.
    assert detail["rows"][0]["own"]["answer"] == "Paris"


@pytest.mark.parametrize("state", ["archived", "deleted"])
async def test_unpublished_or_deleted_source_denies_history_content(database, state):
    from sitg_bot.storage.models import PacketVersionRecord

    owner = await create_player(database, 9241, "unpublished")
    version_id, _ = await upload(database, owner)
    training = TrainingService(database)
    now = datetime.now(UTC)
    started = (await training.start(owner, version_id, confirm=True, now=now))["session"]
    session_id = UUID(started["id"])
    await play_one_question(database, owner, session_id, answer="Paris", now=now)
    async with database.transaction() as session:
        version = await session.get(PacketVersionRecord, version_id)
        if state == "archived":
            version.state = "archived"
        else:
            version.deleted_at = now
    detail = await TrainingHistoryService(database).session_detail(owner, session_id)
    assert detail["protected_source"] is True
    assert all(row["text"] is None and row["answer"] is None for row in detail["rows"])


async def test_library_revocation_and_anonymous_training_statistics(database):
    from test_lobby_architecture import tournament_fixture

    from sitg_bot.storage.models import (
        PacketVersionRecord,
        TournamentPacketAssignmentRecord,
        TournamentPacketEntitlementRecord,
    )

    fixture = await tournament_fixture(database, player_count=2)
    owner, other = (player.id for player in fixture.players)
    now = datetime.now(UTC)
    async with database.transaction() as session:
        assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
            TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
        ))
        version_id = assignment.adopted_version_id
        assignment_id = assignment.id
        assignment.content_visible_by_members = True
        assignment.library_viewing_rule = "anytime"
        (await session.get(PacketVersionRecord, version_id)).library_released_at = now
    training = TrainingService(database)
    first = (await training.start(owner, version_id, confirm=True, now=now))["session"]
    second = (await training.start(other, version_id, confirm=True, now=now))["session"]
    # The fixture's correct answer to question 10 is "answer 10".
    await play_one_question(database, owner, UUID(first["id"]), answer="answer 10", now=now)
    await play_one_question(database, other, UUID(second["id"]), answer="wrong", now=now)
    history = TrainingHistoryService(database)
    detail = await history.session_detail(owner, UUID(first["id"]))
    assert detail["rows"][0]["stats"]["attempts"] == 2
    assert detail["rows"][0]["stats"]["correct"] == 1
    assert detail["rows"][0]["stats"]["incorrect"] == 1
    assert detail["rows"][0]["own"]["answer"] == "answer 10"
    with pytest.raises(LookupError):
        await history.session_detail(owner, UUID(second["id"]))
    async with database.transaction() as session:
        session.add(TournamentPacketEntitlementRecord(
            assignment_id=assignment_id, player_id=owner, readable_override=False,
        ))
    denied = await history.session_detail(owner, UUID(first["id"]))
    assert denied["protected_source"] is True
    assert all(row["text"] is None for row in denied["rows"])
    assert (await history.list_sessions(owner))["items"][0]["id"] == first["id"]


async def test_recorded_buzz_without_attempt_and_bounded_samples(database):
    from sitg_bot.storage.models import PacketQuestionRecord

    owner = await create_player(database, 9251, "timings")
    version_id, _ = await upload(database, owner)
    async with database.sessions() as session:
        pinned = (await session.execute(select(PacketQuestionRecord).where(
            PacketQuestionRecord.packet_version_id == version_id,
        ).order_by(PacketQuestionRecord.position))).scalars().first()
    now = datetime.now(UTC)
    last_id = None
    async with database.transaction() as session:
        # Playback evidence does not replace the independent disclosure/access gate.
        await TrainingService(database).packets.disclose(session, owner, version_id, confirm=True)
        for index in range(30):
            record = TrainingSessionRecord(
                owner_id=owner, packet_version_id=version_id, disclosure_receipt={},
                settings={}, status="stopped", phase="answering", ended_at=now,
            )
            session.add(record)
            await session.flush()
            session.add(TrainingQuestionRecord(
                session_id=record.id, position=0, value=10,
                question_revision_id=pinned.question_revision_id,
                theme_revision_id=pinned.theme_revision_id,
                opened_at=now, buzzed_at=now + timedelta(seconds=index),
                buzz_delay_seconds=index, buzz_token_cursor=2,
            ))
            last_id = record.id
    history = TrainingHistoryService(database)
    detail = await history.session_detail(owner, last_id)
    row = detail["rows"][0]
    assert row["own"] is None and row["buzz"]["buzz_delay_seconds"] == 29
    assert row["text"] and row["stats"]["buzzes"] == 30
    assert row["stats"]["presentations"] == 30
    assert row["stats"]["buzz_timings"] == list(range(29, 4, -1))
    assert detail["score"] == 0
