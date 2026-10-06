from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as database_url
from test_lobby_architecture import packet, tournament_fixture
from test_packet_management import assigned, edit, save

from sitg_bot.services.admin_management import AdminManagementService
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.author_profiles import AuthorProfileService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    LogicalQuestionRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerExposureClaimRecord,
    TournamentAuthorRecord,
)

pytestmark = pytest.mark.integration


async def test_namesake_coauthors_keep_distinct_identities_in_the_editor(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=0)
        service = PacketAdminService(database)
        name = f"Namesake {uuid4()}"
        async with database.transaction() as session:
            authors = [AuthorRecord(display_name=name) for _ in range(2)]
            session.add_all(authors)
        assignment = await assigned(database, fixture)
        editor = await edit(service, fixture, assignment)
        path = "themes.0.authors"
        editor["packet"]["themes"][0]["authors"] = [name, name]
        ids = [str(author.id) for author in authors]
        editor["field_author_ids"][path] = ids
        await save(service, fixture, assignment, editor, {path: "correction"})
        editor = await edit(service, fixture, assignment)
        assert editor["packet"]["themes"][0]["authors"] == (name, name)
        assert editor["field_author_ids"][path] == ids
        editor["packet"]["themes"][0]["questions"][0]["text"] += " correction"
        await save(service, fixture, assignment, editor, {
            "themes.0.questions.0.text": "correction",
        })
        for author in authors:
            profile = await AuthorProfileService(database).profile(author.id)
            assert profile["author"]["question_count"] == 5
    finally:
        await database.close()


async def test_each_coauthor_receives_full_game_statistics(database_url):
    from decimal import Decimal

    from test_player_profiles import build_fixture, record_game

    from sitg_bot.storage.models import QuestionRevisionRecord

    database = Database(database_url)
    try:
        fixture = await build_fixture(database)
        first, second = fixture.members
        await record_game(database, fixture, results=[
            (first, Decimal(1), Decimal(10)), (second, Decimal(2), Decimal(0)),
        ], attempts={first.id: {10: True, 20: False}})
        async with database.transaction() as session:
            authors = [AuthorRecord(display_name=f"Coauthor {uuid4()}") for _ in range(2)]
            session.add_all(authors)
            await session.flush()
            for question in await session.scalars(select(LogicalQuestionRecord).where(
                LogicalQuestionRecord.id.in_(select(QuestionRevisionRecord.question_id).where(
                    QuestionRevisionRecord.id.in_([question[0] for question in fixture.questions])
                ))
            )):
                question.author_ids = (authors[0].id, authors[1].id, authors[1].id)
        profiles = [await AuthorProfileService(database).profile(author.id) for author in authors]
        assert [profile["author"]["question_count"] for profile in profiles] == [5, 5]
        assert profiles[0]["statistics"] == profiles[1]["statistics"]
        assert profiles[0]["by_value"] == profiles[1]["by_value"]
        assert profiles[1]["statistics"]["presentations"] == 5
        assert profiles[1]["statistics"]["attempts"] == 2
        assert profiles[1]["statistics"]["correct"] == 1
    finally:
        await database.close()


async def test_coauthor_inheritance_corrections_exposure_and_merge(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        service = PacketAdminService(database)
        names = [f"{name} {uuid4().hex}" for name in ("Alice", "Bob", "Carol", "Dana")]
        async with database.transaction() as session:
            authors = [AuthorRecord(display_name=name) for name in names]
            session.add_all([*authors, PlatformAdministratorRecord(player_id=fixture.manager.id)])
        links = AuthorLinkService(database)
        for player, author in zip(fixture.players, authors[1:], strict=True):
            request = await links.create_request(player.id, author.id)
            await links.decide_request(request.request_id, fixture.manager.id, approve=True)
        source = packet()
        theme = source.themes[0]
        questions = list(theme.questions)
        # Explicit authors equal to the theme must still survive a theme correction.
        questions[1] = replace(questions[1], authors=tuple(names[:2]))
        questions[2] = replace(questions[2], authors=(names[2],))
        source = replace(source, themes=(replace(
            theme, authors=tuple(names[:2]), questions=tuple(questions),
        ),))
        draft = await service.create_draft(
            source, source_filename="coauthors.json", uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
        )
        stored = await service.publish(draft, administrator_id=fixture.manager.id)
        fixture = replace(fixture, packet_id=stored.logical_id)
        assignment = await assigned(database, fixture)
        adapter = SIContentAdapter()

        async def rows(version_id):
            async with database.sessions() as session:
                version = await session.get(PacketVersionRecord, version_id)
                _, result = await service._version_fields(session, version)
                return result[0]

        initial_theme, initial_questions = await rows(stored.version_id)
        expected = (authors[0].id, authors[1].id)
        assert initial_theme.author_ids == expected
        assert initial_questions[0][1].author_ids == expected
        assert initial_questions[0][1].inherits_theme_authors
        assert initial_questions[1][1].author_ids == expected
        assert not initial_questions[1][1].inherits_theme_authors
        assert initial_questions[2][1].author_ids == (authors[2].id,)
        async with database.sessions() as session:
            pages = await adapter.library_pages(session, stored.version_id)
            assert pages[0]["authors"] == names[:2]
            assert pages[0]["questions"][0]["authors"] == names[:2]
            assert pages[0]["questions"][2]["authors"] == [names[2]]
            tournament_authors = set(await session.scalars(select(TournamentAuthorRecord.author_id)
                .where(TournamentAuthorRecord.tournament_id == fixture.tournament_id)))
            assert set(expected) | {authors[2].id} <= tournament_authors
            for player in fixture.players[:2]:
                assert not await adapter.available_play_units(
                    session, [PacketSelection(stored.version_id, 1)], [player.id],
                )
            assert await adapter.available_play_units(
                session, [PacketSelection(stored.version_id, 1)], [fixture.players[2].id],
            )
        profiles = AuthorProfileService(database)
        for author in authors[:2]:
            assert (await profiles.profile(author.id))["author"]["question_count"] == 4
        assert (await profiles.profile(authors[2].id))["author"]["question_count"] == 1

        editor = await edit(service, fixture, assignment)
        assert editor["packet"]["themes"][0]["questions"][0]["authors"] == ()
        editor["packet"]["themes"][0]["authors"] = [names[0], names[3]]
        editor["field_author_ids"]["themes.0.authors"] = [str(authors[i].id) for i in (0, 3)]
        await save(service, fixture, assignment, editor, {"themes.0.authors": "correction"})
        current = await assigned(database, fixture)
        _, corrected = await rows(current.adopted_version_id)
        assert corrected[0][1].author_ids == (authors[0].id, authors[3].id)
        assert corrected[1][1].author_ids == expected
        assert corrected[2][1].author_ids == (authors[2].id,)
        assert (await rows(stored.version_id))[1][0][1].author_ids == expected
        assert (await profiles.profile(authors[1].id))["author"]["question_count"] == 1
        assert (await profiles.profile(authors[3].id))["author"]["question_count"] == 3
        async with database.sessions() as session:
            for player in fixture.players:
                assert len(list(await session.scalars(select(PlayerExposureClaimRecord).where(
                    PlayerExposureClaimRecord.player_id == player.id,
                    PlayerExposureClaimRecord.state == "burnt",
                )))) == 6
            question = await session.get(LogicalQuestionRecord, corrected[0][1].question_id)
            assert question.author_ids == (authors[0].id, authors[3].id)
            assert (await links._author_summary(session, authors[3])).authorship.question_count == 3

        # Clearing explicit authors re-enables inheritance, including later theme edits.
        editor = await edit(service, fixture, assignment)
        path = "themes.0.questions.2.authors"
        editor["packet"]["themes"][0]["questions"][2]["authors"] = []
        editor["field_author_ids"][path] = []
        await save(service, fixture, assignment, editor, {path: "correction"})
        current = await assigned(database, fixture)
        _, corrected = await rows(current.adopted_version_id)
        assert corrected[2][1].inherits_theme_authors
        assert corrected[2][1].author_ids == (authors[0].id, authors[3].id)

        # Merging two coauthors must remove duplicate credit and preserve all foreign keys.
        await AdminManagementService(database).merge_authors(
            fixture.manager.id, authors[0].id, authors[3].id, confirm=True,
        )
        _, merged = await rows(current.adopted_version_id)
        assert merged[0][1].author_ids == (authors[0].id,)
        assert (await profiles.profile(authors[0].id))["author"]["question_count"] == 5
        async with database.sessions() as session:
            assert await session.get(AuthorRecord, authors[3].id) is None
    finally:
        await database.close()
