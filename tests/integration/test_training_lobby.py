import asyncio
import json
from uuid import UUID

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture
from test_training import upload

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.training import TrainingService
from sitg_bot.storage.database import Database

pytestmark = pytest.mark.integration
database_url = _database_url


async def test_lobby_persists_settings_and_starts_only_on_request(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        owner, other = (player.id for player in fixture.players)
        packet, _ = await upload(database, owner)
        service = TrainingService(database)
        navigation = TelegramNavigationService(database)
        telegram_id = fixture.players[0].telegram_user_id
        home = await navigation.snapshot(telegram_id)
        await navigation.set_mode(telegram_id, "training", expected_version=home.navigation_version)
        created = await service.lobby(owner, "create")
        snapshot = await navigation.snapshot(telegram_id)
        assert snapshot.context == "training_lobby"
        assert snapshot.allowed_actions == ("training.settings", "training.start", "training.quit")
        lobby = created["lobby"]
        assert lobby["version_id"] is None
        assert (await service.current(owner))["session"] is None
        assert (await service.lobby(owner, "create")) == created
        with pytest.raises(ValueError):
            await service.lobby(other, "save", expected_version=lobby["version"], version_id=packet)
        await service.lobby(other, "create")
        with pytest.raises(LookupError):
            await service.lobby(other, "save", expected_version=lobby["version"], version_id=packet)
        saved = (
            await service.lobby(
                owner,
                "save",
                expected_version=lobby["version"],
                version_id=packet,
                settings={"reversed": True},
            )
        )["lobby"]
        assert saved["packet_name"] == "Private <training>"
        assert saved["version"] == lobby["version"] + 1
        assert (await TrainingService(database).current(owner))["lobby"] == saved
        with pytest.raises(StaleWriteError):
            await service.lobby(owner, "quit", expected_version=lobby["version"])
        with pytest.raises(StaleWriteError):
            await service.start(owner, packet, lobby_version=lobby["version"], confirm=True)
        prompt = await service.start(owner, packet, lobby_version=saved["version"])
        assert prompt["confirmation_required"]
        assert (await service.current(owner))["lobby"] == saved
        state = (await service.start(owner, packet, lobby_version=saved["version"], confirm=True))[
            "session"
        ]
        assert state["settings"]["reversed"] is True
        with pytest.raises(StaleWriteError):
            await service.start(owner, packet, lobby_version=saved["version"], confirm=True)
        await service.act(
            owner,
            UUID(state["id"]),
            "stop",
            expected_version=state["version"],
            question_revision_id=UUID(state["question_revision_id"]),
        )
        assert (await service.current(owner))["lobby"] is None
    finally:
        await database.close()


async def test_start_does_not_hold_account_while_waiting_for_source(database_url, monkeypatch):
    from sitg_bot.storage.models import PlayerRecord

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        packet, _ = await upload(database, owner)
        service = TrainingService(database)
        waiting = asyncio.Event()
        release = asyncio.Event()
        lock_access = service.packets.lock_access

        async def wait_for_source(*args, **kwargs):
            waiting.set()
            await release.wait()
            return await lock_access(*args, **kwargs)

        monkeypatch.setattr(service.packets, "lock_access", wait_for_source)
        task = asyncio.create_task(service.start(owner, packet, confirm=True))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            async with database.transaction() as session:
                assert await session.get(PlayerRecord, owner, with_for_update={"nowait": True})
        finally:
            release.set()
            result = await asyncio.wait_for(task, 10)
        assert result["session"] is not None
    finally:
        await database.close()


async def test_private_author_cards_and_search_remain_owner_scoped(database):
    from conftest import create_player

    from sitg_bot.services.training_packets import TrainingPacketService
    from sitg_bot.storage.models import AuthorRecord

    owner = await create_player(database, 8311, "private-authors")
    other = await create_player(database, 8312, "other-authors")
    packets = TrainingPacketService(database)
    content = {
        "name": "Attributed private packet", "lead_author": "Lead Person",
        "themes": [{"name": "Theme", "authors": ["Theme Person"], "questions": [
            {"value": value, "text": "Question", "answer": "Answer", "authors": ["Question Person"]}
            for value in (10, 20, 30, 40, 50)
        ]}],
    }
    imported = await packets.import_upload(owner, json.dumps(content).encode(), "authors.json")
    draft = imported["drafts"][0]
    published = await packets.decide_draft(
        owner, UUID(draft["draft_id"]), expected_version=draft["version"], publish=True,
    )
    version_id = UUID(published["version_id"])
    card = (await packets.list_packets(owner))["items"][0]
    assert card["authors"] == [
        {"id": None, "name": name} for name in ("Lead Person", "Question Person", "Theme Person")
    ]
    for name in ("lead", "QUESTION", " theme person "):
        page = await packets.list_packets(owner, author=name)
        assert [item["id"] for item in page["items"]] == [version_id]
        assert (await packets.list_packets(other, author=name))["items"] == []
    async with database.sessions() as session:
        assert not list(await session.scalars(select(AuthorRecord).where(
            AuthorRecord.display_name.in_(["Lead Person", "Question Person", "Theme Person"])
        )))
    await packets.retire(owner, version_id)
    assert (await packets.list_packets(owner, author="Person"))["items"] == []


async def test_quit_clears_saved_lobby(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        owner = fixture.players[0].id
        service = TrainingService(database)
        lobby = (await service.lobby(owner, "create"))["lobby"]
        assert await service.lobby(owner, "quit", expected_version=lobby["version"]) == {
            "lobby": None
        }
        assert (await service.current(owner)) == {"session": None, "lobby": None}
        recreated = (await service.lobby(owner, "create"))["lobby"]
        assert recreated["version"] > lobby["version"]
        with pytest.raises(StaleWriteError):
            await service.lobby(owner, "quit", expected_version=lobby["version"])
    finally:
        await database.close()
