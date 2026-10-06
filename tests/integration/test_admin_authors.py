from dataclasses import asdict, replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_admin_management import administrator
from test_lobby_architecture import database_url as database_url
from test_lobby_architecture import packet, tournament_fixture

from sitg_bot.services.admin_management import AdminManagementService, author_version
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.author_profiles import AuthorProfileService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    LogicalPacketRecord,
    PacketDraftRecord,
    PacketVersionRecord,
    PlayerAuthorLinkRecord,
    PlayerAuthorLinkRequestRecord,
    PlayerExposureClaimRecord,
    TournamentAuthorRecord,
)

pytestmark = pytest.mark.integration


async def test_admin_edits_author_metadata_with_validation_and_stale_guard(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        await administrator(database, fixture.manager.id)
        async with database.transaction() as session:
            author = AuthorRecord(display_name=f"Writer {uuid4()}")
            session.add(author)
        service = AdminManagementService(database)
        version = author_version(author)
        values = dict(expected_version=version, display_name=" Ada   Lovelace ",
                      first_name=" Ada ", surname=" Lovelace ", telegram_link="@ada")
        with pytest.raises(PermissionError):
            await service.update_author(fixture.players[0].id, author.id, **values)
        with pytest.raises(ValueError):
            await service.update_author(fixture.manager.id, author.id, **{
                **values, "display_name": " ",
            })
        with pytest.raises(ValueError):
            await service.update_author(fixture.manager.id, author.id, **{
                **values, "telegram_link": "https://invalid.example/ada",
            })
        result = await service.update_author(fixture.manager.id, author.id, **values)
        assert result["id"] == author.id
        assert result["display_name"] == "Ada Lovelace"
        assert result["first_name"] == "Ada"
        assert result["surname"] == "Lovelace"
        assert result["telegram_link"] == "https://t.me/ada"
        assert result["telegram_username"] == "ada"
        with pytest.raises(StaleWriteError):
            await service.update_author(fixture.manager.id, author.id, **values)
        catalogue = await service.catalogue(fixture.manager.id, "authors")
        card = next(item for item in catalogue["items"] if item["id"] == author.id)
        assert card["version"] == result["version"]
        assert card["split_names"] == []
        cleared = await service.update_author(
            fixture.manager.id, author.id, expected_version=result["version"], display_name="Alias",
        )
        assert cleared["first_name"] is None
        assert cleared["telegram_link"] is None and cleared["telegram_username"] is None
    finally:
        await database.close()


async def test_split_creates_new_authors_transfers_attribution_and_chosen_links(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        await administrator(database, fixture.manager.id)
        suffix = uuid4().hex
        names = [f"Alice {suffix}", f"Bob {suffix}"]
        combined = ", ".join(names)
        async with database.transaction() as session:
            original = AuthorRecord(display_name=combined, telegram_username="combined",
                                    telegram_link="https://t.me/combined")
            existing = AuthorRecord(display_name=names[0])
            other = AuthorRecord(display_name=f"Other {suffix}")
            session.add_all([original, existing, other])
        service = AdminManagementService(database)
        await service.link_author(fixture.manager.id, original.id, str(fixture.players[0].id))
        pending = await AuthorLinkService(database).create_request(
            fixture.players[1].id, original.id,
        )
        source = packet()
        questions = list(source.themes[0].questions)
        questions[1] = replace(questions[1], authors=(combined, other.display_name))
        source = replace(source, lead_author=combined, themes=(replace(
            source.themes[0], authors=(combined,), questions=tuple(questions),
        ),))
        packets = PacketAdminService(database)
        draft_id = await packets.create_draft(
            source, source_filename="combined.json", uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
        )
        stored = await packets.publish(draft_id, administrator_id=fixture.manager.id)
        # Unpublished work may independently reference an existing namesake.
        content = asdict(source)
        content["themes"][0]["questions"][2]["authors"] = [names[0]]
        unpublished = await packets.create_draft(
            source, source_filename="draft.json", uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
        )
        saved = await packets.update_draft(
            unpublished, fixture.manager.id, expected_version=1, content=content,
            author_bindings={combined: original.id, names[0]: existing.id},
        )
        async with database.transaction() as session:
            session.add(PacketDraftRecord(
                status="validation_failed", source_filename="invalid.json", source_checksum="test",
                content={"themes": None, "lead_author": []},
                creation_tournament_id=fixture.tournament_id,
            ))
            burns = dict((await session.execute(select(
                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.burnt_at,
            ).where(PlayerExposureClaimRecord.player_id == fixture.players[0].id))).all())
        version = author_version(original)
        options = dict(expected_version=version, recipient_index=1, confirm=True)
        with pytest.raises(PermissionError):
            await service.split_author(fixture.players[0].id, original.id, **options)
        for invalid in ({"confirm": False}, {"recipient_index": 2}, {"recipient_index": -1}):
            with pytest.raises(ValueError):
                await service.split_author(
                    fixture.manager.id, original.id, **{**options, **invalid},
                )
        with pytest.raises(StaleWriteError):
            await service.split_author(fixture.manager.id, original.id, **{
                **options, "expected_version": "0" * 64,
            })
        result = await service.split_author(fixture.manager.id, original.id, **options)
        new_ids = tuple(UUID(item["id"]) for item in result["authors"])
        assert [item["display_name"] for item in result["authors"]] == names
        assert len(set(new_ids)) == 2 and existing.id not in new_ids and original.id not in new_ids
        assert result["recipient_author_id"] == str(new_ids[1])
        async with database.sessions() as session:
            assert await session.get(AuthorRecord, original.id) is None
            assert (await session.get(AuthorRecord, existing.id)).display_name == names[0]
            assert (await session.get(AuthorRecord, new_ids[0])).telegram_username is None
            assert (await session.get(AuthorRecord, new_ids[1])).telegram_username == "combined"
            assert await session.get(PlayerAuthorLinkRecord, (fixture.players[0].id, new_ids[1]))
            assert await session.get(
                PlayerAuthorLinkRecord, (fixture.players[0].id, new_ids[0]),
            ) is None
            request = await session.get(PlayerAuthorLinkRequestRecord, pending.request_id)
            assert request.author_id == new_ids[1]
            version_record = await session.get(PacketVersionRecord, stored.version_id)
            assert version_record.lead_author_id == new_ids[1]
            logical = await session.get(LogicalPacketRecord, stored.logical_id)
            assert logical.statistical_author_id == new_ids[1]
            _, rows = await packets._version_fields(session, version_record)
            theme, questions = rows[0]
            assert theme.author_ids == new_ids
            assert questions[0][1].author_ids == new_ids
            assert questions[0][1].inherits_theme_authors
            assert questions[1][1].author_ids == (*new_ids, other.id)
            assert not questions[1][1].inherits_theme_authors
            tournament_authors = set(await session.scalars(select(TournamentAuthorRecord.author_id)
                .where(TournamentAuthorRecord.tournament_id == fixture.tournament_id)))
            assert set(new_ids) <= tournament_authors and original.id not in tournament_authors
            assert dict((await session.execute(select(
                PlayerExposureClaimRecord.id, PlayerExposureClaimRecord.burnt_at,
            ).where(PlayerExposureClaimRecord.player_id == fixture.players[0].id))).all()) == burns
            draft = await session.get(PacketDraftRecord, unpublished)
            assert draft.version == saved["version"] + 1
            assert str(original.id) not in draft.author_bindings.values()
            assert draft.author_bindings[names[0]] == str(existing.id)
        for new_id in new_ids:
            profile = await AuthorProfileService(database).profile(new_id)
            assert profile["author"]["question_count"] == 5
        published = await packets.publish(unpublished, administrator_id=fixture.manager.id)
        async with database.sessions() as session:
            _, rows = await packets._version_fields(
                session, await session.get(PacketVersionRecord, published.version_id),
            )
            assert rows[0][0].author_ids == new_ids
            assert rows[0][1][2][1].author_ids == (existing.id,)
        with pytest.raises(LookupError):
            await service.split_author(fixture.manager.id, original.id, **options)
    finally:
        await database.close()
