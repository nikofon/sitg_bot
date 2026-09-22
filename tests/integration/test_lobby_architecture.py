import os
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.server import ConsoleApplicationServer
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.persistent_game import ParticipantInput, PersistentGameService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameObserverRecord,
    GameParticipantRecord,
    GameRecord,
    GameResultRecord,
    GameRulesetVersionRecord,
    OutboxEventRecord,
    PacketDraftRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    PlayerNotificationAlertRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    QuestionRevisionRecord,
    RatingLedgerRecord,
    RulesetRatingLedgerRecord,
    RulesetRatingRecord,
    TelegramGameViewRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
    TournamentRegistrationAttemptRecord,
    TournamentTypeVersionRecord,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL")
        else:
            os.environ["DATABASE_URL"] = previous_url
    return url


@dataclass(frozen=True)
class TournamentFixture:
    tournament_id: UUID
    manager: PlayerRecord
    players: tuple[PlayerRecord, ...]
    inputs: tuple[ParticipantInput, ...]
    packet_id: UUID


def packet() -> Packet:
    return Packet(
        f"Architecture {secrets.token_hex(6)}",
        (
            Theme(
                "Theme",
                tuple(
                    Question(
                        text=f"Question {value}",
                        answer=f"answer {value}",
                        commentary="Explanation",
                        value=value,
                        form="ANSWER",
                        source="https://example.test",
                    )
                    for value in (10, 20, 30, 40, 50)
                ),
            ),
        ),
        lead_author="Architecture Author",
        language="ru",
    )


async def tournament_fixture(
    database: Database,
    *,
    player_count: int,
    type_key: str = "ladder",
    hybrid_matchmaking_enabled: bool = True,
    finalized: bool = True,
    started: bool = True,
    minimum_players: int = 1,
    mutable_parameters: tuple[str, ...] = ("theme_count", "maximum_players"),
) -> TournamentFixture:
    suffix = int(secrets.token_hex(4), 16)
    inputs = tuple(
        ParticipantInput(suffix + index, f"Architecture Player {index}")
        for index in range(player_count)
    )
    async with database.transaction() as session:
        type_version = await session.scalar(
            select(TournamentTypeVersionRecord).where(
                TournamentTypeVersionRecord.key == type_key,
                TournamentTypeVersionRecord.version == 1,
            )
        )
        ruleset_version = await session.scalar(
            select(GameRulesetVersionRecord).where(
                GameRulesetVersionRecord.key == "si",
                GameRulesetVersionRecord.version == 1,
            )
        )
        assert type_version is not None and ruleset_version is not None
        players = tuple(
            PlayerRecord(
                telegram_user_id=item.telegram_user_id,
                real_name=item.public_nickname,
                public_nickname=item.public_nickname,
                registration_step="complete",
                registration_completed_at=datetime.now(UTC),
                status="active",
            )
            for item in inputs
        )
        manager = PlayerRecord(
            telegram_user_id=suffix + player_count + 100,
            real_name="Architecture Manager",
            public_nickname="Architecture Manager",
            registration_step="complete",
            registration_completed_at=datetime.now(UTC),
            status="active",
        )
        session.add_all((*players, manager))
        await session.flush()
        tournament = TournamentRecord(
            name="Architecture tournament",
            slug=f"architecture-{suffix}",
            type_version_id=type_version.id,
            game_ruleset_version_id=ruleset_version.id,
            created_by_id=manager.id,
            finalized_at=datetime.now(UTC) if finalized else None,
            actual_starts_at=datetime.now(UTC) if finalized and started else None,
            participants_finalized_at=(datetime.now(UTC) if type_key == "classic" else None),
        )
        session.add(tournament)
        await session.flush()
        settings = GameSettings(
            minimum_players=minimum_players,
            theme_count=1,
            ready_delay=0,
            message_delay=0,
            game_start_to_first_theme_delay=0,
        )
        session.add_all(
            [
                TournamentPolicyVersionRecord(
                    tournament_id=tournament.id,
                    version=1,
                    default_parameters=settings.to_dict(),
                    player_mutable_parameters=list(mutable_parameters),
                    policies={
                        "rating_enabled": True,
                        "hybrid_matchmaking_enabled": hybrid_matchmaking_enabled,
                    },
                    created_by_id=manager.id,
                ),
                TournamentManagerRecord(
                    tournament_id=tournament.id,
                    player_id=manager.id,
                    granted_by_id=manager.id,
                ),
                *[
                    TournamentMembershipRecord(
                        tournament_id=tournament.id,
                        player_id=player.id,
                        enrolled_by_id=manager.id,
                    )
                    for player in players
                ],
            ]
        )
        tournament_id = tournament.id
    packets = PacketAdminService(database)
    draft_id = await packets.create_draft(
        packet(),
        source_filename="architecture.json",
        uploader_id=manager.id,
        tournament_id=tournament_id,
    )
    stored = await packets.publish(draft_id, administrator_id=manager.id)
    async with database.transaction() as session:
        assignment = await session.scalar(
            select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                TournamentPacketAssignmentRecord.packet_id == stored.logical_id,
            )
        )
        assert assignment is not None
        assignment.discoverable_by_members = True
        assignment.playable_by_members = True
    return TournamentFixture(tournament_id, manager, players, inputs, stored.logical_id)


async def test_manager_management_lists_published_packet_access(database_url: str) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)

    management = await TournamentService(database).manager_management(
        fixture.tournament_id, fixture.manager.id
    )

    assert management.packet_count == 1
    assert len(management.packets) == 1
    assert management.packets[0].packet_id == fixture.packet_id
    assert len(management.packets[0].player_access) == 1
    assert management.packets[0].player_access[0].playable is True
    await database.close()


@pytest.mark.parametrize("discoverable", (True, False))
@pytest.mark.parametrize("playable", (True, False))
@pytest.mark.parametrize("readable", (True, False))
async def test_publication_applies_current_access_defaults_per_tournament(
    database_url: str,
    discoverable: bool,
    playable: bool,
    readable: bool,
) -> None:
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        legacy = await tournament_fixture(database, player_count=1)
        tournaments = TournamentService(database)
        packets = PacketAdminService(database)
        draft_id = await packets.create_draft(
            packet(),
            source_filename="access-defaults.json",
            uploader_id=fixture.manager.id,
            tournament_id=fixture.tournament_id,
            intended_tournament_ids=(fixture.tournament_id, legacy.tournament_id),
        )
        settings = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
        await tournaments.update_policy(
            fixture.tournament_id,
            fixture.manager.id,
            default_parameters=settings.default_parameters,
            player_mutable_parameters=set(settings.player_mutable_parameters),
            policies={
                **settings.policies,
                "packets_discoverable_by_default": discoverable,
                "packets_playable_by_default": playable,
                "packets_readable_by_default": readable,
            },
        )
        stored = await packets.publish(draft_id, administrator_id=fixture.manager.id)
        for target, expected in (
            (fixture, (discoverable, playable, readable)),
            (legacy, (True, False, False)),
        ):
            management = await tournaments.manager_management(
                target.tournament_id, target.manager.id
            )
            published = next(
                item for item in management.packets if item.packet_id == stored.logical_id
            )
            assert len(published.player_access) == len(target.players)
            for access in published.player_access:
                assert (access.discoverable, access.playable, access.readable) == expected
            previous = next(
                item for item in management.packets if item.packet_id == target.packet_id
            )
            for access in previous.player_access:
                assert (access.discoverable, access.playable, access.readable) == (
                    True,
                    True,
                    False,
                )
        async with database.sessions() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == stored.logical_id,
                )
            )
            assert assignment is not None
            assert assignment.editable_by_members is False
            assert await tournaments.library_viewing_rule(
                session, assignment, fixture.players[0].id
            ) == "after-play"
    finally:
        await database.close()


async def test_miniapp_publication_enqueues_bound_telegram_message_update(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    packets = PacketAdminService(database)
    draft_id = await packets.create_draft(
        packet(),
        source_filename="telegram-bound.json",
        uploader_id=fixture.manager.id,
        tournament_id=fixture.tournament_id,
    )
    await packets.bind_telegram_message(
        draft_id,
        fixture.manager.id,
        chat_id=123456,
        message_id=789,
        locale="ru",
    )

    await packets.publish(
        draft_id,
        administrator_id=fixture.manager.id,
        notify_bound_telegram=True,
    )

    async with database.sessions() as session:
        event = await session.scalar(
            select(OutboxEventRecord).where(
                OutboxEventRecord.topic == "telegram.packet.draft_status",
                OutboxEventRecord.aggregate_id == draft_id,
            )
        )
        assert event is not None
        assert event.payload == {
            "chat_id": 123456,
            "message_id": 789,
            "locale": "ru",
            "status": "published",
        }
    await database.close()


async def test_packet_author_associations_reuse_identities_and_extend_tournament(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    packets = PacketAdminService(database)
    tournaments = TournamentService(database)
    before = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
    async with database.transaction() as session:
        existing = AuthorRecord(display_name="Registered Writer")
        namesake = AuthorRecord(display_name="Registered Writer")
        lead = AuthorRecord(display_name="Registered Editor")
        session.add_all([existing, namesake, lead])
        await session.flush()
    source = packet()
    source = replace(
        source,
        lead_author="Lead alias",
        themes=(
            replace(
                source.themes[0],
                author="Packet alias",
                questions=tuple(
                    replace(question, author="Packet alias" if index == 0 else "Unbound writer")
                    for index, question in enumerate(source.themes[0].questions)
                ),
            ),
        ),
    )
    draft_id = await packets.create_draft(
        source,
        source_filename="authors.json",
        uploader_id=fixture.manager.id,
        tournament_id=fixture.tournament_id,
    )
    saved = await packets.update_draft(
        draft_id,
        fixture.manager.id,
        expected_version=1,
        content=asdict(source),
        author_bindings={"Packet alias": existing.id},
        lead_author_id=lead.id,
    )
    bindings = saved["author_bindings"]
    assert bindings["Packet alias"] == str(existing.id)
    assert saved["packet"]["themes"][0]["author"] == "Packet alias"
    assert saved["packet"]["lead_author"] == "Lead alias"
    unbound_id = UUID(bindings["Unbound writer"])
    after = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
    after_ids = {author.id for author in after.authors}
    assert {author.id for author in before.authors} <= after_ids
    assert {existing.id, lead.id, unbound_id} <= after_ids
    assert namesake.id not in after_ids
    with pytest.raises(StaleWriteError):
        await packets.update_draft(
            draft_id,
            fixture.manager.id,
            expected_version=1,
            content=asdict(source),
        )
    with pytest.raises(PermissionError):
        await packets.update_draft(
            draft_id,
            fixture.players[0].id,
            expected_version=2,
            content=asdict(source),
        )
    await packets.update_draft(
        draft_id,
        fixture.manager.id,
        expected_version=2,
        content=saved["packet"],
        author_bindings={name: UUID(value) for name, value in bindings.items()},
        lead_author_id=lead.id,
    )
    stored = await packets.publish(draft_id, administrator_id=fixture.manager.id)
    async with database.sessions() as session:
        version = await session.get(PacketVersionRecord, stored.version_id)
        assert version.lead_author_id == lead.id
        theme = await session.scalar(
            select(ThemeRevisionRecord).where(
                ThemeRevisionRecord.packet_version_id == stored.version_id,
            )
        )
        assert theme.author_id == existing.id
        question_authors = set(
            await session.scalars(
                select(QuestionRevisionRecord.author_id)
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                )
                .where(PacketQuestionRecord.packet_version_id == stored.version_id)
            )
        )
        assert question_authors == {existing.id, unbound_id}
        draft = await session.get(PacketDraftRecord, draft_id)
        assert draft.author_bindings == bindings
    final = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
    assert {author.id for author in final.authors} == after_ids
    await database.close()


async def test_packet_editor_can_register_structured_author(database_url: str) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    packets = PacketAdminService(database)
    draft_id = await packets.create_draft(
        packet(),
        source_filename="new-author.json",
        uploader_id=fixture.manager.id,
        tournament_id=fixture.tournament_id,
    )
    created = await packets.create_author(
        draft_id,
        fixture.manager.id,
        first_name="Ada",
        second_name="Augusta",
        surname="Lovelace",
        telegram_link="https://t.me/ada_example",
    )
    author_id = UUID(created["author_id"])
    settings = await TournamentService(database).manager_settings(
        fixture.tournament_id,
        fixture.manager.id,
    )
    assert author_id in {author.id for author in settings.authors}
    async with database.sessions() as session:
        author = await session.get(AuthorRecord, author_id)
        assert author.display_name == "Ada Augusta Lovelace"
        assert author.telegram_username == "ada_example"
    await packets.reject(draft_id, actor_id=fixture.manager.id)
    with pytest.raises(ValueError, match="unpublished"):
        await packets.create_author(
            draft_id,
            fixture.manager.id,
            first_name="Ada",
            second_name=None,
            surname="Lovelace",
            telegram_link=None,
        )
    await database.close()


async def test_lobby_start_persists_plan_and_universal_exposure_claims(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    matchmaking = InvitationMatchmakingService(database)
    lobby = await matchmaking.create_lobby(
        fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
    )
    lobby = await matchmaking.select_packet(
        lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
    )
    assert lobby.validation_violations == ()
    await matchmaking.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
    started = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
    assert started.started and started.game is not None

    async with database.sessions() as session:
        game = await session.get(GameRecord, started.game.id)
        assert game is not None
        assert game.assignment_plan["ruleset_key"] == "si"
        assert game.assignment_plan["seed"]
        assert "assignment_plan" in GameRecord.__table__.columns
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PlayerExposureClaimRecord)
                .where(
                    PlayerExposureClaimRecord.game_id == game.id,
                    PlayerExposureClaimRecord.packet_version_id.is_not(None),
                )
            )
            == 6
        )

    games = PersistentGameService(database)
    await games.join(started.game.id, fixture.inputs[0].telegram_user_id)
    await games.advance(started.game.id)
    await games.abandon(started.game.id, reason="test cleanup")
    replay = await matchmaking.create_lobby(
        fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
    )
    replay = await matchmaking.select_packet(
        replay.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
    )
    assert replay.validation_violations[0]["code"] == "insufficient_fresh_content"
    await database.close()


async def test_tournament_catalogue_enforces_visibility_roles_and_actions(
    database_url: str,
) -> None:
    database = Database(database_url)
    private_fixture = await tournament_fixture(database, player_count=1)
    public_fixture = await tournament_fixture(database, player_count=1)
    outsider_id: UUID | None = None
    private_slug = ""
    public_slug = ""
    try:
        async with database.transaction() as session:
            private_tournament = await session.get(TournamentRecord, private_fixture.tournament_id)
            public_tournament = await session.get(TournamentRecord, public_fixture.tournament_id)
            assert private_tournament is not None and public_tournament is not None
            private_tournament.visibility = "private"
            public_tournament.visibility = "public"
            public_tournament.registration_open = True
            private_slug = private_tournament.slug
            public_slug = public_tournament.slug
            outsider = PlayerRecord(
                telegram_user_id=int(secrets.token_hex(5), 16),
                real_name="Catalogue Outsider",
                public_nickname="Catalogue Outsider",
                registration_step="complete",
                registration_completed_at=datetime.now(UTC),
                status="active",
            )
            session.add(outsider)
            await session.flush()
            outsider_id = outsider.id

        tournaments = TournamentService(database)
        manager_page = await tournaments.list_visible(
            private_fixture.manager.id,
            role="manager",
            relationship="managed",
            search=private_slug,
        )
        private_item = manager_page.items[0]
        assert private_item.available_actions == ("info", "select_manager")

        assert outsider_id is not None
        outsider_page = await tournaments.list_visible(
            outsider_id,
            role="player",
            search=private_slug,
        )
        assert outsider_page.items == ()
        public_page = await tournaments.list_visible(
            outsider_id,
            role="player",
            search=public_slug,
        )
        assert public_page.total == 1
        assert public_page.items[0].available_actions == ("info", "register")
        managed_player_page = await tournaments.list_visible(
            public_fixture.manager.id,
            role="player",
            search=public_slug,
        )
        assert managed_player_page.items == ()
        with pytest.raises(LookupError):
            await tournaments.tournament_details(
                private_fixture.tournament_id, outsider_id, role="player"
            )
    finally:
        await database.close()


async def test_manager_cannot_participate_even_with_legacy_active_membership(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    matchmaking = InvitationMatchmakingService(database)
    try:
        async with database.transaction() as session:
            session.add(
                TournamentMembershipRecord(
                    tournament_id=fixture.tournament_id,
                    player_id=fixture.manager.id,
                )
            )
        manager_input = ParticipantInput(
            fixture.manager.telegram_user_id,
            fixture.manager.public_nickname,
        )
        with pytest.raises(PermissionError, match="managers cannot play"):
            await matchmaking.create_lobby(
                manager_input,
                tournament_id=fixture.tournament_id,
                max_players=1,
            )
    finally:
        await database.close()


@pytest.mark.parametrize("type_key", ["ladder", "classic"])
async def test_console_lists_and_approves_pending_registrations(database_url: str, type_key: str):
    database = Database(database_url)
    fixture = await tournament_fixture(
        database, player_count=4, type_key=type_key, started=False,
        hybrid_matchmaking_enabled=type_key == "ladder",
    )
    server = ConsoleApplicationServer(database)
    connection = SimpleNamespace(session=SimpleNamespace(player_id=fixture.manager.id))
    params = {"tournament_id": str(fixture.tournament_id)}
    try:
        async with database.transaction() as session:
            for player, status in zip(
                fixture.players, ["registered", "registered", "active", "rejected"], strict=True
            ):
                membership = await session.get(
                    TournamentMembershipRecord, (fixture.tournament_id, player.id)
                )
                membership.status = status
                membership.registered_at = datetime.now(UTC)
        connection.session.player_id = fixture.players[0].id
        for action in ("tournament_registrations", "tournament_registrations_approve_all"):
            with pytest.raises(PermissionError):
                await server._dispatch(connection, action, params)
        connection.session.player_id = fixture.manager.id
        result = await server._dispatch(connection, "tournament_registrations", params)
        assert {r.player_id for r in result["registrations"]} == {p.id for p in fixture.players}
        assert all(r.display_name and r.registered_at for r in result["registrations"])
        result = await server._dispatch(connection, "tournament_registrations_approve_all", params)
        assert result == {"approved_count": 2}
        result = await server._dispatch(connection, "tournament_registrations_approve_all", params)
        assert result == {"approved_count": 0}
        async with database.sessions() as session:
            for index, player in enumerate(fixture.players):
                membership = await session.get(
                    TournamentMembershipRecord, (fixture.tournament_id, player.id)
                )
                if index < 2:
                    assert membership.status == ("active" if type_key == "ladder" else "approved")
                    assert membership.approved_by_id == fixture.manager.id
                    assert membership.approved_at is not None
                else:
                    assert membership.status == ("active" if index == 2 else "rejected")
    finally:
        await database.close()


async def test_console_manager_can_select_and_finalize_without_membership(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1, finalized=False)
    server = ConsoleApplicationServer(database)
    connection = SimpleNamespace(session=SimpleNamespace(player_id=fixture.manager.id))
    params = {"tournament_id": str(fixture.tournament_id)}
    try:
        async with database.sessions() as session:
            assert await session.get(
                TournamentMembershipRecord, (fixture.tournament_id, fixture.manager.id)
            ) is None
        for action in ("tournament_manage", "tournament_info"):
            info = await server._dispatch_console(connection, action, params)
            assert info["manager"] is True
            assert info["membership_status"] is None
            assert info["rating"] is None
            assert info["finalized_at"] is None
        finalized = await server._dispatch_console(
            connection,
            "tournament_setup_finalize",
            {**params, "expected_version": info["settings_version"]},
        )
        assert finalized["finalized_at"] is not None
        started = await server._dispatch(
            connection,
            "tournament_start",
            {**params, "expected_version": finalized["settings_version"]},
        )
        assert started["actual_starts_at"] is not None
        info = await server._dispatch(connection, "tournament_info", params)
        assert info["actual_starts_at"] == started["actual_starts_at"]
        with pytest.raises(ValueError, match="already started"):
            await server._dispatch(
                connection,
                "tournament_start",
                {**params, "expected_version": started["settings_version"]},
            )
        connection.session.player_id = fixture.players[0].id
        with pytest.raises(PermissionError, match="manager role"):
            await server._dispatch_console(connection, "tournament_manage", params)
        connection.session.player_id = UUID(int=0)
        with pytest.raises(PermissionError, match="manager role"):
            await server._dispatch_console(connection, "tournament_info", params)
    finally:
        await database.close()


async def test_unfinalized_tournament_is_manager_only_until_finalized(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1, finalized=False)
    tournaments = TournamentService(database)
    matchmaking = InvitationMatchmakingService(database)
    try:
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            assert tournament is not None
            tournament.visibility = "public"
            tournament.registration_open = True
            slug = tournament.slug

        player_page = await tournaments.list_visible(
            fixture.players[0].id, role="player", search=slug
        )
        assert player_page.items == ()
        manager_page = await tournaments.list_visible(
            fixture.manager.id,
            role="manager",
            relationship="managed",
            search=slug,
        )
        managed = manager_page.items[0]
        assert managed.finalized_at is None
        with pytest.raises(ValueError, match="tournament_stage_closed"):
            await matchmaking.create_lobby(
                fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
            )

        settings = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
        assert "ruleset_rating_weight" not in settings.policies
        assert "ruleset_rating_weight" not in {
            descriptor.name for descriptor in settings.policy_descriptors
        }
        assert {descriptor.name for descriptor in settings.setting_descriptors} >= {
            "ready_delay",
            "question_values",
        }
        finalized = await tournaments.finalize_tournament_setup(
            fixture.tournament_id,
            fixture.manager.id,
            expected_version=settings.settings_version,
        )
        assert finalized.finalized_at is not None
        assert "finalize" not in finalized.available_actions

        visible = await tournaments.list_visible(fixture.players[0].id, role="player", search=slug)
        assert fixture.tournament_id in {item.id for item in visible.items}
        item = finalized.tournament
        updated = await tournaments.update_manager_settings(
            fixture.tournament_id,
            fixture.manager.id,
            expected_version=finalized.settings_version,
            name=f"{item.name} Updated",
            slug=item.slug,
            type_key=item.type_key,
            game_ruleset_key=item.ruleset_key,
            visibility=item.visibility,
            language=item.language,
            payment_type=item.payment_type,
            pricing_plans=[],
            registration_open=item.registration_open,
            registration_starts_at=item.registration_starts_at,
            registration_ends_at=item.registration_ends_at,
            starts_at=item.starts_at,
            planned_ends_at=item.planned_ends_at,
            author_names=finalized.author_names,
            default_parameters=finalized.default_parameters,
            player_mutable_parameters=set(finalized.player_mutable_parameters),
            policies={
                **finalized.policies,
                "packets_discoverable_by_default": False,
                "packets_playable_by_default": True,
                "packets_readable_by_default": True,
            },
        )
        assert updated.tournament.name.endswith(" Updated")
        reloaded = await tournaments.manager_settings(fixture.tournament_id, fixture.manager.id)
        assert reloaded.policies["packets_discoverable_by_default"] is False
        assert reloaded.policies["packets_playable_by_default"] is True
        assert reloaded.policies["packets_readable_by_default"] is True

        with pytest.raises(PermissionError, match="administrators"):
            await tournaments.update_manager_settings(
                fixture.tournament_id,
                fixture.manager.id,
                expected_version=updated.settings_version,
                name=updated.tournament.name,
                slug=updated.tournament.slug,
                type_key=updated.tournament.type_key,
                game_ruleset_key=updated.tournament.ruleset_key,
                visibility=updated.tournament.visibility,
                language=updated.tournament.language,
                payment_type=updated.tournament.payment_type,
                pricing_plans=[],
                registration_open=updated.tournament.registration_open,
                registration_starts_at=updated.tournament.registration_starts_at,
                registration_ends_at=updated.tournament.registration_ends_at,
                starts_at=updated.tournament.starts_at,
                planned_ends_at=updated.tournament.planned_ends_at,
                author_names=updated.author_names,
                default_parameters=updated.default_parameters,
                player_mutable_parameters=set(updated.player_mutable_parameters),
                policies={**updated.policies, "ruleset_rating_weight": 0.5},
            )

        with pytest.raises(ValueError, match="cannot change after finalization"):
            await tournaments.update_manager_settings(
                fixture.tournament_id,
                fixture.manager.id,
                expected_version=updated.settings_version,
                name=updated.tournament.name,
                slug=updated.tournament.slug,
                type_key="classic",
                game_ruleset_key=updated.tournament.ruleset_key,
                visibility=updated.tournament.visibility,
                language=updated.tournament.language,
                payment_type=updated.tournament.payment_type,
                pricing_plans=[],
                registration_open=updated.tournament.registration_open,
                registration_starts_at=updated.tournament.registration_starts_at,
                registration_ends_at=updated.tournament.registration_ends_at,
                starts_at=updated.tournament.starts_at,
                planned_ends_at=updated.tournament.planned_ends_at,
                author_names=updated.author_names,
                default_parameters=updated.default_parameters,
                player_mutable_parameters=set(updated.player_mutable_parameters),
                policies=updated.policies,
            )
    finally:
        await database.close()


async def test_registering_author_notifies_matching_active_bot_user(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    tournaments = TournamentService(database)
    try:
        async with database.transaction() as session:
            player = await session.get(PlayerRecord, fixture.players[0].id)
            assert player is not None
            username = "ada_" + secrets.token_hex(8)
            player.telegram_username = username

        settings = await tournaments.register_tournament_author(
            fixture.tournament_id,
            fixture.manager.id,
            first_name="Ada",
            second_name=None,
            surname="Lovelace",
            telegram_link=f"https://t.me/{username}",
        )

        assert "Ada Lovelace" in settings.author_names
        notifications = AuthorLinkService(database)
        throttled_alert = await notifications.claim_notification_alerts(
            fixture.players[0].id, audience="player"
        )
        assert throttled_alert.audiences == ()
        page = await notifications.notification_page(
            fixture.players[0].id, audience="player", read_state="unseen", limit=100
        )
        assert any(item.kind == "author.registered" for item in page.items)
        assert (
            await notifications.notification_page(
                fixture.players[0].id, audience="player", read_state="seen", limit=5
            )
        ).items == ()
        async with database.transaction() as session:
            alert = await session.get(PlayerNotificationAlertRecord, fixture.players[0].id)
            assert alert is not None
            delivery = await session.scalar(
                select(OutboxEventRecord).where(
                    OutboxEventRecord.topic == "telegram.notification.alert",
                    OutboxEventRecord.payload["recipient_telegram_user_id"].astext
                    == str(fixture.players[0].telegram_user_id),
                )
            )
            assert delivery is not None
            assert delivery.payload["audience"] == "player"
            assert delivery.payload["same_role"] is True
            alert.last_alerted_at = datetime.now(UTC) - timedelta(seconds=16)
        assert (
            await notifications.claim_notification_alerts(fixture.players[0].id, audience="player")
        ).audiences == ("player",)
        read = await notifications.mark_notifications_read(fixture.players[0].id, audience="player")
        assert read.read_count >= 1
        assert (
            await notifications.claim_notification_alerts(fixture.players[0].id, audience="player")
        ).audiences == ()
        assert (
            len(
                (
                    await notifications.notification_page(
                        fixture.players[0].id,
                        audience="player",
                        read_state="seen",
                        limit=5,
                    )
                ).items
            )
            >= 1
        )
        async with database.sessions() as session:
            notification = await session.scalar(
                select(PlayerNotificationRecord).where(
                    PlayerNotificationRecord.recipient_player_id == fixture.players[0].id,
                    PlayerNotificationRecord.kind == "author.registered",
                )
            )
            assert notification is not None
            assert notification.payload["tournament_name"] == "Architecture tournament"
    finally:
        await database.close()


async def test_hybrid_search_uses_pinned_context_and_combined_plan(database_url: str) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=2)
    matchmaking = InvitationMatchmakingService(
        database,
        initial_rating_tolerance=1000,
        initial_reputation_tolerance=1000,
    )
    lobbies = []
    for participant in fixture.inputs:
        lobby = await matchmaking.create_lobby(
            participant, tournament_id=fixture.tournament_id, max_players=2
        )
        lobby = await matchmaking.select_packet(
            lobby.id, participant.telegram_user_id, fixture.packet_id
        )
        lobbies.append(await matchmaking.find_players(lobby.id, participant.telegram_user_id))
    merges = await matchmaking.match_searching()
    assert len(merges) == 1
    merged = await matchmaking.get(merges[0].surviving_lobby_id)
    assert len(merged.members) == 2
    assert merged.validation_violations == ()
    assert all(member.ready is False for member in merged.members)
    await database.close()


async def test_classic_rejects_hybrid_matchmaking_policy_and_search(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(
        database,
        player_count=1,
        type_key="classic",
        hybrid_matchmaking_enabled=False,
    )
    tournaments = TournamentService(database)
    async with database.sessions() as session:
        context = await tournaments.context(session, fixture.tournament_id)
    with pytest.raises(ValueError, match="does not support hybrid matchmaking"):
        await tournaments.update_policy(
            fixture.tournament_id,
            fixture.manager.id,
            default_parameters=context.settings.to_dict(),
            player_mutable_parameters=context.mutable_parameters,
            policies={"hybrid_matchmaking_enabled": True},
        )

    matchmaking = InvitationMatchmakingService(database)
    lobby = await matchmaking.create_lobby(
        fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
    )
    assert not lobby.hybrid_matchmaking_available
    with pytest.raises(ValueError, match="does not support hybrid matchmaking"):
        await matchmaking.find_players(lobby.id, fixture.inputs[0].telegram_user_id)
    await database.close()


async def test_pairwise_settlement_updates_tournament_and_ruleset_ratings(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=2)
    completed_at = datetime.now(UTC)
    async with database.transaction() as session:
        tournament = await session.get(TournamentRecord, fixture.tournament_id)
        assert tournament is not None
        policy = await session.scalar(
            select(TournamentPolicyVersionRecord).where(
                TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
            )
        )
        assert policy is not None
        policy.policies = {**policy.policies, "ruleset_rating_weight": 0.5}
        game = GameRecord(
            tournament_id=tournament.id,
            tournament_type_version_id=tournament.type_version_id,
            game_ruleset_version_id=tournament.game_ruleset_version_id,
            tournament_policy_version_id=policy.id,
            host_player_id=fixture.players[0].id,
            assignment_plan={},
            status="completed",
            phase="finished",
            completed_at=completed_at,
        )
        session.add(game)
        await session.flush()
        participants = []
        for index, player in enumerate(fixture.players, 1):
            current_player = await session.get(PlayerRecord, player.id, with_for_update=True)
            membership = await session.get(TournamentMembershipRecord, (tournament.id, player.id))
            assert membership is not None and current_player is not None
            membership.rating_sequence += 1
            current_player.game_sequence += 1
            participant = GameParticipantRecord(
                tournament_id=tournament.id,
                game_id=game.id,
                player_id=player.id,
                seat=index,
                rating_sequence=membership.rating_sequence,
                global_game_sequence=current_player.game_sequence,
                active=False,
                final_place=Decimal(index),
            )
            participants.append(participant)
            session.add(participant)
            session.add(
                GameResultRecord(
                    game_id=game.id,
                    player_id=player.id,
                    tournament_id=tournament.id,
                    tournament_policy_version_id=policy.id,
                    place=Decimal(index),
                    score=0,
                )
            )
        await session.flush()
        await PersistentGameService(database)._apply_rating_settlement(session, game, participants)
        game_id = game.id

    async with database.sessions() as session:
        tournament_deltas = tuple(
            (
                await session.execute(
                    select(RatingLedgerRecord.delta)
                    .where(RatingLedgerRecord.game_id == game_id)
                    .order_by(RatingLedgerRecord.player_id)
                )
            ).scalars()
        )
        ruleset_deltas = tuple(
            (
                await session.execute(
                    select(RulesetRatingLedgerRecord.delta)
                    .where(RulesetRatingLedgerRecord.game_id == game_id)
                    .order_by(RulesetRatingLedgerRecord.player_id)
                )
            ).scalars()
        )
        ruleset_states = tuple(
            (
                await session.execute(
                    select(RulesetRatingRecord.rating)
                    .where(
                        RulesetRatingRecord.ruleset_key == "si",
                        RulesetRatingRecord.player_id.in_(player.id for player in fixture.players),
                    )
                    .order_by(RulesetRatingRecord.player_id)
                )
            ).scalars()
        )

    assert sorted(tournament_deltas) == [Decimal("-20.0000"), Decimal("20.0000")]
    assert sorted(ruleset_deltas) == [Decimal("-10.0000"), Decimal("10.0000")]
    assert sorted(ruleset_states) == [Decimal("990.0000"), Decimal("1010.0000")]


async def test_registration_packet_metadata_and_viewing_rules(database_url: str) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=1)
    tournaments = TournamentService(database)
    packets = PacketAdminService(database)

    async with database.transaction() as session:
        invited_player = PlayerRecord(
            telegram_user_id=int(secrets.token_hex(4), 16),
            real_name="Invited registration player",
            public_nickname="Invited registration player",
            registration_step="complete",
            registration_completed_at=datetime.now(UTC),
            status="active",
        )
        session.add(invited_player)
        await session.flush()
        invited_player_id = invited_player.id
        assignment = await session.scalar(
            select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            )
        )
        assert assignment is not None
        assignment_id = assignment.id
        version_id = assignment.adopted_version_id
        assert version_id is not None

    await tournaments.update_metadata(
        fixture.tournament_id,
        fixture.manager.id,
        registration_open=True,
        language="ru",
        payment_type="one-time",
        pricing_plans=[
            {
                "name": "Students",
                "prices": [
                    {"amount": "500.00", "currency": "rub"},
                    {"amount": "5.00", "currency": "usd"},
                ],
            },
            {
                "name": "Adults",
                "prices": [{"amount": "750.00", "currency": "rub"}],
            },
        ],
    )
    await tournaments.invite(
        fixture.tournament_id, invited_player_id, invited_by_id=fixture.manager.id
    )
    await tournaments.register(fixture.tournament_id, invited_player_id)
    status = await tournaments.approve_registration(
        fixture.tournament_id, invited_player_id, manager_id=fixture.manager.id
    )
    assert status == "active"

    await tournaments.assign_packet(
        fixture.tournament_id,
        fixture.packet_id,
        fixture.manager.id,
        adopted_version_id=version_id,
        discoverable=True,
        playable=True, library_viewing_rule="after-play",
    )
    await tournaments.set_packet_entitlement(
        assignment_id,
        invited_player_id,
        fixture.manager.id,
        playable=False,
    )
    await packets.set_library_release(
        fixture.packet_id,
        fixture.manager.id,
        released=True,
        version_id=version_id,
    )

    async with database.sessions() as session:
        membership = await session.get(
            TournamentMembershipRecord, (fixture.tournament_id, invited_player_id)
        )
        tournament = await session.get(TournamentRecord, fixture.tournament_id)
        assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
        version = await session.get(PacketVersionRecord, version_id)
        assert membership is not None and membership.status == "active"
        assert tournament is not None
        assert tournament.language == "ru"
        assert tournament.payment_type == "one-time"
        pricing_plans = await tournaments.pricing_plans(session, fixture.tournament_id)
        assert [plan.name for plan in pricing_plans] == ["Students", "Adults"]
        assert {(price.amount, price.currency) for price in pricing_plans[0].prices} == {
            (Decimal("500.00"), "RUB"),
            (Decimal("5.00"), "USD"),
        }
        assert assignment is not None
        assert (
            await tournaments.library_viewing_rule(session, assignment, invited_player_id)
            == "after-play"
        )
        assert not await tournaments.has_assignment_access(
            session, assignment, invited_player_id, "playable"
        )
        assert version is not None and version.language == "ru"
        assert version.library_released_at is not None
        assert await tournaments.tournament_authors(session, fixture.tournament_id) == (
            "Architecture Author",
        )
    await database.close()


async def test_registration_requirements_use_play_and_exposure_history(
    database_url: str,
) -> None:
    database = Database(database_url)
    source = await tournament_fixture(database, player_count=1)
    destination = await tournament_fixture(database, player_count=1)
    matchmaking = InvitationMatchmakingService(database)
    games = PersistentGameService(database)
    tournaments = TournamentService(database)

    lobby = await matchmaking.create_lobby(
        source.inputs[0], tournament_id=source.tournament_id, max_players=1
    )
    await matchmaking.select_packet(lobby.id, source.inputs[0].telegram_user_id, source.packet_id)
    await matchmaking.set_ready(lobby.id, source.inputs[0].telegram_user_id)
    started = await matchmaking.start(lobby.id, source.inputs[0].telegram_user_id)
    assert started.game is not None
    await games.join(started.game.id, source.inputs[0].telegram_user_id)
    await games.advance(started.game.id)
    await games.abandon(started.game.id, reason="registration requirement test")

    await tournaments.update_metadata(
        destination.tournament_id,
        destination.manager.id,
        registration_open=True,
    )
    await tournaments.invite(
        destination.tournament_id,
        source.players[0].id,
        invited_by_id=destination.manager.id,
    )
    await tournaments.add_registration_requirement(
        destination.tournament_id,
        destination.manager.id,
        kind="has-played-tournament",
        target_id=source.tournament_id,
    )
    unseen_requirement_id = await tournaments.add_registration_requirement(
        destination.tournament_id,
        destination.manager.id,
        kind="has-not-seen-packet",
        target_id=source.packet_id,
    )

    declined_for_packet = await tournaments.register(
        destination.tournament_id, source.players[0].id
    )
    assert not declined_for_packet.accepted
    assert declined_for_packet.status == "rejected"
    assert "has already been seen" in declined_for_packet.reasons[0]

    await tournaments.remove_registration_requirement(
        destination.tournament_id,
        unseen_requirement_id,
        manager_id=destination.manager.id,
    )
    exclusion_requirement_id = await tournaments.add_registration_requirement(
        destination.tournament_id,
        destination.manager.id,
        kind="has-not-played-tournament",
        target_id=source.tournament_id,
    )
    declined_for_play = await tournaments.register(destination.tournament_id, source.players[0].id)
    assert not declined_for_play.accepted
    assert "is not allowed" in declined_for_play.reasons[0]

    await tournaments.remove_registration_requirement(
        destination.tournament_id,
        exclusion_requirement_id,
        manager_id=destination.manager.id,
    )
    accepted = await tournaments.register(destination.tournament_id, source.players[0].id)
    assert accepted.accepted and accepted.status == "registered"

    async with database.sessions() as session:
        membership = await session.get(
            TournamentMembershipRecord,
            (destination.tournament_id, source.players[0].id),
        )
        attempt_count = await session.scalar(
            select(func.count())
            .select_from(TournamentRegistrationAttemptRecord)
            .where(
                TournamentRegistrationAttemptRecord.tournament_id == destination.tournament_id,
                TournamentRegistrationAttemptRecord.player_id == source.players[0].id,
            )
        )
        assert membership is not None
        assert membership.registration_rejection_reasons == []
        assert attempt_count == 3
    await database.close()


async def test_observers_confirm_burning_replay_events_and_stop_when_playing(
    database_url: str,
) -> None:
    database = Database(database_url)
    fixture = await tournament_fixture(database, player_count=3)
    matchmaking = InvitationMatchmakingService(database)
    games = PersistentGameService(database)
    tournaments = TournamentService(database)

    async with database.sessions() as session:
        context = await tournaments.context(session, fixture.tournament_id)
    await tournaments.update_policy(
        fixture.tournament_id,
        fixture.manager.id,
        default_parameters=context.settings.to_dict(),
        player_mutable_parameters=context.mutable_parameters,
        policies={**context.policies, "observing": "unlimited"},
    )

    lobby = await matchmaking.create_lobby(
        fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
    )
    await matchmaking.select_packet(lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id)
    await matchmaking.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
    started = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
    assert started.game is not None
    observed_game_id = started.game.id
    await games.join(observed_game_id, fixture.inputs[0].telegram_user_id)

    warning = await games.observe(observed_game_id, fixture.inputs[1].telegram_user_id)
    assert not warning.joined
    assert warning.confirmation_required
    assert warning.fresh_content_count == 6
    assert warning.snapshot is None

    first_observer = await games.observe(
        observed_game_id,
        fixture.inputs[1].telegram_user_id,
        confirm_fresh=True,
    )
    assert first_observer.joined
    assert first_observer.snapshot is not None
    assert first_observer.events[0]["kind"] == "game_created"
    await games.observe(
        observed_game_id,
        fixture.inputs[2].telegram_user_id,
        confirm_fresh=True,
    )

    async with database.sessions() as session:
        reserved_counts = {
            player.id: int(
                await session.scalar(
                    select(func.count())
                    .select_from(PlayerExposureClaimRecord)
                    .where(
                        PlayerExposureClaimRecord.game_id == observed_game_id,
                        PlayerExposureClaimRecord.player_id == player.id,
                        PlayerExposureClaimRecord.state == "reserved",
                    )
                )
                or 0
            )
            for player in fixture.players[1:]
        }
    assert reserved_counts == {fixture.players[1].id: 6, fixture.players[2].id: 6}

    packets = PacketAdminService(database)
    draft_id = await packets.create_draft(
        packet(),
        source_filename="observer-own-game.json",
        uploader_id=fixture.manager.id,
        tournament_id=fixture.tournament_id,
    )
    own_packet = await packets.publish(draft_id, administrator_id=fixture.manager.id)
    async with database.transaction() as session:
        own_assignment = await session.scalar(
            select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == own_packet.logical_id,
            )
        )
        assert own_assignment is not None
        own_assignment.discoverable_by_members = True
        own_assignment.playable_by_members = True

    own_lobby = await matchmaking.create_lobby(
        fixture.inputs[2], tournament_id=fixture.tournament_id, max_players=1
    )
    await matchmaking.select_packet(
        own_lobby.id, fixture.inputs[2].telegram_user_id, own_packet.logical_id
    )
    await matchmaking.set_ready(own_lobby.id, fixture.inputs[2].telegram_user_id)
    own_started = await matchmaking.start(own_lobby.id, fixture.inputs[2].telegram_user_id)
    assert own_started.game is not None
    await games.join(own_started.game.id, fixture.inputs[2].telegram_user_id)

    async with database.sessions() as session:
        stopped_observer = await session.scalar(
            select(GameObserverRecord).where(
                GameObserverRecord.game_id == observed_game_id,
                GameObserverRecord.player_id == fixture.players[2].id,
            )
        )
        released_count = await session.scalar(
            select(func.count())
            .select_from(PlayerExposureClaimRecord)
            .where(
                PlayerExposureClaimRecord.game_id == observed_game_id,
                PlayerExposureClaimRecord.player_id == fixture.players[2].id,
                PlayerExposureClaimRecord.state == "released",
            )
        )
        assert stopped_observer is not None and not stopped_observer.active
        assert stopped_observer.left_at is not None
        assert released_count == 6

    with pytest.raises(PermissionError, match="cannot observe while playing"):
        await games.observe(observed_game_id, fixture.inputs[2].telegram_user_id)

    await games.advance(observed_game_id)
    async with database.sessions() as session:
        burnt_count = await session.scalar(
            select(func.count())
            .select_from(PlayerExposureClaimRecord)
            .where(
                PlayerExposureClaimRecord.game_id == observed_game_id,
                PlayerExposureClaimRecord.player_id == fixture.players[1].id,
                PlayerExposureClaimRecord.state == "burnt",
            )
        )
    assert burnt_count == 6

    await games.stop_observing(observed_game_id, fixture.inputs[1].telegram_user_id)
    replay = await games.observe(observed_game_id, fixture.inputs[1].telegram_user_id)
    assert replay.joined and not replay.confirmation_required
    assert any(event["kind"] == "themes_announced" for event in replay.events)
    assert replay.snapshot is not None
    version_before = replay.snapshot.version
    event_count_before = len(await games.events(observed_game_id))
    private_score = await games.request_observer_score(
        observed_game_id, fixture.inputs[1].telegram_user_id
    )
    assert private_score.events[0]["payload"]["private"] is True
    assert private_score.snapshot.version == version_before
    assert len(await games.events(observed_game_id)) == event_count_before

    await games.abandon(observed_game_id, reason="observer integration cleanup")
    await games.abandon(own_started.game.id, reason="observer integration cleanup")
    await database.close()


async def test_telegram_lobby_navigation_invitations_and_notices(database_url: str) -> None:
    from sitg_bot.services.navigation import TelegramNavigationService

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        service = InvitationMatchmakingService(database)
        navigation = TelegramNavigationService(database)
        creator = fixture.inputs[0].telegram_user_id
        guest = fixture.inputs[1].telegram_user_id
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        nav = await navigation.set_context(creator, "lobby")
        assert nav.context == "lobby" and "lobby.search" in nav.allowed_actions
        nav = await navigation.set_context(
            creator, "lobby_other", expected_version=nav.navigation_version
        )
        assert (await navigation.snapshot(creator)).context == "lobby_other"
        nav = await navigation.set_context(
            creator, "lobby", expected_version=nav.navigation_version
        )
        nav = await navigation.set_context(
            creator, "tournament", expected_version=nav.navigation_version
        )
        assert nav.active_lobby.id == lobby.id and "lobby.reopen" in nav.allowed_actions
        await navigation.set_context(creator, "lobby", expected_version=nav.navigation_version)
        with pytest.raises(StaleWriteError):
            await navigation.set_context(
                creator, "tournament", expected_version=nav.navigation_version
            )
        lobby = await service.join(lobby.invitation_code, fixture.inputs[1])
        guest_nav = await navigation.set_context(guest, "lobby")
        assert "lobby.start" not in guest_nav.allowed_actions
        assert "lobby.ready" in guest_nav.allowed_actions
        username = "invite_" + secrets.token_hex(5)
        async with database.transaction() as session:
            player = await session.get(PlayerRecord, fixture.players[2].id)
            player.telegram_username = username
        await service.invite(
            lobby.id, guest, "@" + username.upper(), expected_version=lobby.version
        )
        with pytest.raises(LookupError, match="Registered Telegram"):
            await service.invite(
                lobby.id, guest, "@missing_" + secrets.token_hex(4), expected_version=lobby.version
            )
        with pytest.raises(PermissionError):
            await service.invite(
                lobby.id,
                fixture.inputs[2].telegram_user_id,
                "@" + username,
                expected_version=lobby.version,
            )
        lobby = await service.select_packet(
            lobby.id, creator, fixture.packet_id, expected_version=lobby.version
        )
        lobby = await service.set_ready(
            lobby.id, creator, ready=True, expected_version=lobby.version
        )
        lobby = await service.set_settings(
            lobby.id, creator, {"theme_count": 2}, expected_version=lobby.version
        )
        assert not any(member.ready for member in lobby.members)
        with pytest.raises(PermissionError):
            await service.set_settings(
                lobby.id, guest, {"theme_count": 1}, expected_version=lobby.version
            )
        async with database.sessions() as session:
            notices = list(
                await session.scalars(
                    select(OutboxEventRecord).where(
                        OutboxEventRecord.aggregate_id == lobby.id,
                        OutboxEventRecord.topic == "telegram.lobby.notice",
                    )
                )
            )
        invites = [item for item in notices if item.payload["kind"] == "invitation"]
        assert len(invites) == 1 and not invites[0].payload["allow_observer"]
        settings = [item for item in notices if item.payload["kind"] == "settings_changed"]
        assert len(settings) == 2
        assert all(item.payload["changes"] == {"theme_count": 2} for item in settings)
        assert len([item for item in notices if item.payload["kind"] == "packet_selected"]) == 2
    finally:
        await database.close()


async def test_lobby_packet_discovery_is_per_viewer_and_failed_selection_is_broadcast(
    database_url: str,
) -> None:
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        service = InvitationMatchmakingService(database)
        creator = fixture.inputs[0].telegram_user_id
        guest = fixture.inputs[1].telegram_user_id
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        lobby = await service.join(lobby.invitation_code, fixture.inputs[1])
        # Explicit access overrides are used by the production packet management API.
        from sitg_bot.storage.models import TournamentPacketEntitlementRecord

        async with database.transaction() as session:
            assignment = await session.scalar(
                select(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
                )
            )
            assignment.discoverable_by_members = False
            assignment.playable_by_members = False
            session.add_all(
                [
                    TournamentPacketEntitlementRecord(
                        assignment_id=assignment.id,
                        player_id=fixture.players[0].id,
                        discoverable=True,
                        playable=False,
                    ),
                    TournamentPacketEntitlementRecord(
                        assignment_id=assignment.id,
                        player_id=fixture.players[1].id,
                        discoverable=False,
                        playable=False,
                    ),
                ]
            )
        assert any(
            item.packet_id == fixture.packet_id
            for item in await service.suggest_packets(lobby.id, telegram_user_id=creator)
        )
        assert not await service.suggest_packets(lobby.id, telegram_user_id=guest)
        with pytest.raises(PermissionError):
            await service.select_packet(
                lobby.id, creator, fixture.packet_id, expected_version=lobby.version
            )
        async with database.sessions() as session:
            notices = list(
                await session.scalars(
                    select(OutboxEventRecord).where(
                        OutboxEventRecord.aggregate_id == lobby.id,
                        OutboxEventRecord.topic == "telegram.lobby.notice",
                    )
                )
            )
        notices = [item for item in notices if item.payload["kind"] == "packet_rejected"]
        assert len(notices) == 2
        assert all(item.payload["kind"] == "packet_rejected" for item in notices)
        assert (await service.get(lobby.id)).version == lobby.version
    finally:
        await database.close()


async def test_lobby_packet_cards_report_metadata_freshness_and_all_player_access(
    database_url: str,
) -> None:
    from sitg_bot.storage.models import TournamentPacketEntitlementRecord

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        service = InvitationMatchmakingService(database)
        async with database.transaction() as session:
            policy = await session.scalar(select(TournamentPolicyVersionRecord).where(
                TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
            ))
            policy.policies = {**policy.policies, "observing": "unlimited"}
            version = await session.scalar(select(PacketVersionRecord).where(
                PacketVersionRecord.packet_id == fixture.packet_id
            ))
            version.year = 2020
            version.published_at = datetime(2025, 1, 1, tzinfo=UTC)
            theme_author = AuthorRecord(display_name="Theme Author")
            question_author = AuthorRecord(display_name="Question Author")
            session.add_all([theme_author, question_author])
            await session.flush()
            theme = await session.scalar(select(ThemeRevisionRecord).where(
                ThemeRevisionRecord.packet_version_id == version.id
            ))
            theme.author_id = theme_author.id
            question = await session.scalar(select(QuestionRevisionRecord).join(
                PacketQuestionRecord,
                PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
            ).where(PacketQuestionRecord.packet_version_id == version.id).limit(1))
            question.author_id = question_author.id
            assignment = await session.scalar(select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                TournamentPacketAssignmentRecord.packet_id == fixture.packet_id,
            ))
            session.add(TournamentPacketEntitlementRecord(
                assignment_id=assignment.id, player_id=fixture.players[2].id,
                discoverable=True, playable=False,
            ))
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            game = GameRecord(
                tournament_id=fixture.tournament_id,
                tournament_type_version_id=tournament.type_version_id,
                game_ruleset_version_id=tournament.game_ruleset_version_id,
                tournament_policy_version_id=policy.id,
                host_player_id=fixture.players[2].id,
            )
            session.add(game)
            await session.flush()
            session.add(PlayerExposureClaimRecord(
                game_id=game.id, player_id=fixture.players[2].id,
                packet_version_id=version.id, claim_namespace="theme",
                claim_id=theme.theme_id, state="burnt",
            ))
            version_id, theme_id, game_id, assignment_id = (
                version.id, theme.theme_id, game.id, assignment.id
            )
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        lobby = await service.join(lobby.invitation_code, fixture.inputs[1])
        lobby = await service.join(
            lobby.invitation_code, fixture.inputs[2], role="observer", confirm_fresh=True
        )
        suggestion, = await service.suggest_packets(
            lobby.id, telegram_user_id=fixture.inputs[0].telegram_user_id
        )
        assert suggestion.year == 2020
        assert suggestion.published_at == datetime(2025, 1, 1, tzinfo=UTC)
        assert suggestion.lead_author == "Architecture Author"
        assert set(suggestion.authors) == {
            "Architecture Author", "Theme Author", "Question Author"
        }
        # An observer's access and exposure do not affect players' packet indicators.
        assert suggestion.playable_for_all is True
        assert suggestion.fresh_play_unit_count == suggestion.total_play_unit_count == 1
        lobby = await service.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id,
            expected_version=lobby.version,
        )
        assert lobby.selected_packets[0] == suggestion
        async with database.transaction() as session:
            session.add(TournamentPacketEntitlementRecord(
                assignment_id=assignment_id, player_id=fixture.players[1].id,
                discoverable=True, playable=False,
            ))
        suggestion, = await service.suggest_packets(lobby.id)
        assert suggestion.playable_for_all is False
        assert suggestion.fresh_play_unit_count == 1
        assert (await service.get(lobby.id)).selected_packets[0].playable_for_all is False
        async with database.transaction() as session:
            session.add(PlayerExposureClaimRecord(
                game_id=game_id, player_id=fixture.players[1].id,
                packet_version_id=version_id, claim_namespace="theme",
                claim_id=theme_id, state="burnt",
            ))
        suggestion, = await service.suggest_packets(lobby.id)
        assert suggestion.fresh_play_unit_count == 0
        assert (await service.get(lobby.id)).selected_packets[0].fresh_play_unit_count == 0
        lobby = await service.leave(
            lobby.id, fixture.inputs[1].telegram_user_id, expected_version=lobby.version
        )
        assert lobby.selected_packets[0].playable_for_all is True
        assert lobby.selected_packets[0].fresh_play_unit_count == 1
    finally:
        await database.close()


async def test_lobby_gateway_creation_join_and_payload_are_idempotent(database_url: str) -> None:
    from sitg_bot.application.contracts import ApplicationPrincipal, GatewayRequest
    from sitg_bot.application.gateway import ApplicationGateway
    from sitg_bot.services.launch_references import LaunchReferenceService

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        references = LaunchReferenceService(database, signing_key="test-key-" * 8, bot_id=123)
        gateway = ApplicationGateway(database, launch_references=references)
        owner = ApplicationPrincipal(fixture.players[0].id, fixture.inputs[0].telegram_user_id)
        guest = ApplicationPrincipal(fixture.players[1].id, fixture.inputs[1].telegram_user_id)

        def request(action, **fields):
            return GatewayRequest.model_validate(
                {
                    "metadata": {
                        "channel": "telegram_bot",
                        "client_name": "lobby-test",
                        "client_version": "1",
                        "idempotency_key": secrets.token_hex(16),
                    },
                    "operation": {"action": action, **fields},
                }
            )

        await gateway.navigation.select_tournament(
            owner.telegram_user_id,
            mode="player",
            tournament_id=fixture.tournament_id,
            expected_version=0,
        )
        create = request("lobbies.create.v1", tournament_id=str(fixture.tournament_id))
        created = await gateway.execute(owner, create)
        assert created.ok, created.error
        replay = await gateway.execute(owner, create)
        assert replay.ok and replay.data == created.data
        lobby_id = created.data["lobby"]["id"]
        nav = await gateway.navigation.snapshot(owner.telegram_user_id)
        assert nav.context == "lobby" and str(nav.active_lobby.id) == lobby_id
        outsider = await gateway.execute(guest, request("lobbies.info.v1", lobby_id=lobby_id))
        assert not outsider.ok and outsider.error.code.value == "forbidden"
        join = request("lobbies.join.v1", invitation_code=created.data["lobby"]["invitation_code"])
        joined = await gateway.execute(guest, join)
        assert joined.ok, joined.error
        assert (await gateway.execute(guest, join)).data == joined.data
        assert (await gateway.navigation.snapshot(guest.telegram_user_id)).context == "lobby"
        info = await gateway.execute(owner, request("lobbies.info.v1", lobby_id=lobby_id))
        assert info.ok, info.error
        assert info.data["tournament_name"] == "Architecture tournament"
        assert info.data["mutable_parameters"] == ["theme_count"]
        assert info.data["settings"]["theme_count"] == 1
        assert info.data["setting_descriptors"]
        assert len(info.data["members"]) == 2
        assert "cancel" in info.data["available_actions"]
        assert "leave" not in info.data["available_actions"]
        forbidden = await gateway.execute(
            guest,
            request(
                "lobbies.settings.update.v1",
                lobby_id=lobby_id,
                expected_version=info.data["version"],
                changes={"theme_count": 2},
            ),
        )
        assert not forbidden.ok and forbidden.error.code.value == "forbidden"
    finally:
        await database.close()


async def test_lobby_notices_follow_membership_across_navigation_modes(database_url: str) -> None:
    from sitg_bot.services.navigation import TelegramNavigationService

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        service = InvitationMatchmakingService(database)
        navigation = TelegramNavigationService(database)
        owner, guest, observer = (item.telegram_user_id for item in fixture.inputs)
        async with database.transaction() as session:
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord).where(
                    TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id
                )
            )
            policy.policies = {**policy.policies, "observing": "unlimited"}
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        nav = await navigation.set_context(owner, "lobby")
        await navigation.set_context(owner, "tournament", expected_version=nav.navigation_version)
        lobby = await service.join(lobby.invitation_code, fixture.inputs[1])
        nav = await navigation.set_context(guest, "lobby")
        await navigation.set_mode(guest, "manager", expected_version=nav.navigation_version)
        lobby = await service.join(
            lobby.invitation_code, fixture.inputs[2], role="observer", confirm_fresh=True
        )
        lobby = await service.select_packet(
            lobby.id, owner, fixture.packet_id, expected_version=lobby.version
        )
        name = lobby.selected_packets[0].name
        lobby = await service.remove_packet(
            lobby.id, owner, fixture.packet_id, expected_version=lobby.version
        )
        lobby = await service.set_settings(
            lobby.id, owner, {"theme_count": 2}, expected_version=lobby.version
        )
        lobby = await service.leave(lobby.id, observer, expected_version=lobby.version)
        lobby = await service.leave(lobby.id, guest, expected_version=lobby.version)
        await service.set_settings(
            lobby.id, owner, {"theme_count": 1}, expected_version=lobby.version
        )
        async with database.sessions() as session:
            events = list(
                await session.scalars(
                    select(OutboxEventRecord).where(
                        OutboxEventRecord.aggregate_id == lobby.id,
                        OutboxEventRecord.topic == "telegram.lobby.notice",
                    )
                )
            )

        def recipients(kind, **values):
            return {
                event.payload["recipient_telegram_user_id"]
                for event in events
                if event.payload["kind"] == kind
                and all(event.payload.get(k) == v for k, v in values.items())
            }

        assert recipients("player_joined", role="observer") == {owner, guest, observer}
        assert recipients("packet_selected") == {owner, guest, observer}
        assert recipients("packet_removed", packet_name=name) == {owner, guest, observer}
        assert recipients("settings_changed", changes={"theme_count": 2}) == {
            owner,
            guest,
            observer,
        }
        assert recipients("player_left", role="observer") == {owner, guest}
        assert recipients("player_left", role="player") == {owner}
        assert recipients("settings_changed", changes={"theme_count": 1}) == {owner}
    finally:
        await database.close()


async def test_unconsumed_domain_events_do_not_block_ordered_telegram_delivery(
    database_url: str,
) -> None:
    from sitg_bot.services.reliable_delivery import TransactionalOutbox

    database = Database(database_url)
    suffix = secrets.token_hex(10)
    # Use a unique subscription to keep this test independent of other fixtures' pending deliveries.
    open_topic, notice_topic = f"test.lobby.open.{suffix}", f"test.lobby.notice.{suffix}"
    try:
        async with database.transaction() as session:
            for index, topic in enumerate(("lobby.event", open_topic, "game.event", notice_topic)):
                await TransactionalOutbox.enqueue(
                    session,
                    topic=topic,
                    deduplication_key=f"{suffix}:{index}",
                    partition_key=f"telegram:chat:{suffix}",
                    aggregate_sequence=index,
                    payload={"index": index},
                )
        outbox = TransactionalOutbox(database)
        first = await outbox.claim(topics=(open_topic, notice_topic))
        assert len(first) == 1 and first[0].topic == open_topic
        assert not await outbox.claim(topics=(open_topic, notice_topic))
        await outbox.acknowledge(first[0].event_id, first[0].lease_token)
        second = await outbox.claim(topics=(open_topic, notice_topic))
        assert len(second) == 1 and second[0].topic == notice_topic
        await outbox.acknowledge(second[0].event_id, second[0].lease_token)
    finally:
        await database.close()


async def test_readiness_reports_specific_conditions_and_allows_unready(database_url: str) -> None:
    from sitg_bot.application.contracts import ActionCode
    from sitg_bot.application.gateway import ApplicationGateway
    from sitg_bot.bot.i18n import LocalizationService
    from sitg_bot.services.matchmaking import LobbyReadinessError

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        service = InvitationMatchmakingService(database)
        owner = fixture.inputs[0].telegram_user_id
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        with pytest.raises(LobbyReadinessError) as failure:
            await service.set_ready(lobby.id, owner, expected_version=lobby.version)
        assert failure.value.reason == "packet_required"
        lobby = await service.select_packet(
            lobby.id, owner, fixture.packet_id, expected_version=lobby.version
        )
        lobby = await service.set_settings(
            lobby.id, owner, {"theme_count": 2}, expected_version=lobby.version
        )
        with pytest.raises(LobbyReadinessError) as failure:
            await service.set_ready(lobby.id, owner, expected_version=lobby.version)
        assert failure.value.reason == "insufficient_fresh_content"
        error = ApplicationGateway._error(failure.value, action=ActionCode.LOBBY_READY_UPDATE)
        text = LocalizationService().text(error.message_key, "ru", **error.details)
        assert "доступно 1, требуется 2" in text
        lobby = await service.set_ready(
            lobby.id, owner, ready=False, expected_version=lobby.version
        )
        assert not lobby.members[0].ready
    finally:
        await database.close()


async def test_gateway_start_reaches_console_players_and_all_can_join(database_url: str) -> None:
    from sitg_bot.application.contracts import ApplicationPrincipal, GatewayRequest
    from sitg_bot.server import PlayerSession

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=4)
        server = ConsoleApplicationServer(database)
        lobby = await server.matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id
        )
        connections = [
            SimpleNamespace(
                session=PlayerSession(
                    player.id, player.telegram_user_id, player.public_nickname, False
                ),
                lobby_ids=set(), game_ids=set(), send=AsyncMock(), adapter_session=None,
            )
            for player in fixture.players[1:]
        ]
        server._connections = connections
        for connection in connections:
            await server._dispatch_console(
                connection, "lobby_join", {"target": lobby.invitation_code}
            )
        await server.matchmaking.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        for player in fixture.inputs:
            lobby = await server.matchmaking.set_ready(lobby.id, player.telegram_user_id)
        await server._sync_console_updates()
        for connection in connections:
            connection.send.reset_mock()

        principal = ApplicationPrincipal(fixture.players[0].id, fixture.inputs[0].telegram_user_id)

        async def request(action, **fields):
            result = await server.application_gateway.execute(
                principal,
                GatewayRequest.model_validate({
                    "metadata": {
                        "channel": "telegram_bot", "client_name": "mixed-lobby-test",
                        "client_version": "1", "idempotency_key": secrets.token_hex(16),
                    },
                    "operation": {"action": action, **fields},
                }),
            )
            assert result.ok, result.error
            return result.data

        started = await request(
            "lobbies.start.v1", lobby_id=str(lobby.id), expected_version=lobby.version
        )
        game_id = UUID(started["game"]["id"])
        await server._sync_console_updates()
        for connection in connections:
            assert game_id in connection.game_ids
            messages = [call.args[0] for call in connection.send.await_args_list]
            game_message = next(m for m in messages if m["event"] == "game_changed")
            assert game_message["game_id"] == game_id
            assert game_message["snapshot"].status == "lobby"
            assert any(
                m["event"] == "lobby_changed" and m["snapshot"].status == "started"
                for m in messages
            )

        nav = await server.application_gateway.navigation.snapshot(principal.telegram_user_id)
        assert nav.context == "game" and nav.active_game.id == game_id
        await request("games.act.v1", game_id=str(game_id), command="join")
        await server._sync_console_updates()
        assert connections[0].send.await_args.args[0]["snapshot"].participants[0].joined
        for connection in connections:
            transition = await server._dispatch_console(
                connection, "game_join", {"game_id": str(game_id)}
            )
            assert transition.accepted
        assert transition.snapshot.status == "active"
        assert all(p.joined for p in transition.snapshot.participants)
        transition = await server.games.progress_due(game_id)
        assert any(event["kind"] == "themes_announced" for event in transition.events)
    finally:
        await database.close()


async def test_manual_readiness_notifies_others_but_bulk_reset_does_not(database_url: str) -> None:
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=5)
        server = ConsoleApplicationServer(database)
        service = server.matchmaking
        owner = fixture.inputs[0].telegram_user_id
        lobby = await service.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        for player in fixture.inputs[1:4]:
            await service.join(lobby.invitation_code, player)
        async with database.sessions() as session:
            context = await service.tournaments.context(session, fixture.tournament_id)
        await service.tournaments.update_policy(
            fixture.tournament_id, fixture.manager.id,
            default_parameters=context.settings.to_dict(),
            player_mutable_parameters=context.mutable_parameters,
            policies={**context.policies, "observing": "unlimited"},
        )
        await service.join(
            lobby.invitation_code, fixture.inputs[4], role="observer", confirm_fresh=True
        )
        await service.select_packet(lobby.id, owner, fixture.packet_id)
        connections = [
            SimpleNamespace(
                session=SimpleNamespace(player_id=p.id, telegram_user_id=p.telegram_user_id),
                lobby_ids={lobby.id}, game_ids=set(), send=AsyncMock(),
            )
            for p in fixture.players
        ]
        server._connections = connections
        await server._sync_console_updates()
        for connection in connections:
            connection.send.reset_mock()
        for ready in (True, True, False):
            await service.set_ready(lobby.id, owner, ready=ready)
            await server._sync_console_updates()
        await service.set_settings(lobby.id, owner, {"theme_count": 2})
        await server._sync_console_updates()
        for index, connection in enumerate(connections):
            notices = [call.args[0] for call in connection.send.await_args_list
                       if call.args[0]["event"] == "lobby_readiness_changed"]
            assert len(notices) == (0 if index == 0 else 2)
            if notices:
                assert [n["ready_count"] for n in notices] == [1, 0]
                assert all(n["player_count"] == 4 for n in notices)
        async with database.sessions() as session:
            notices = list(await session.scalars(select(OutboxEventRecord).where(
                OutboxEventRecord.aggregate_id == lobby.id,
                OutboxEventRecord.topic == "telegram.lobby.notice",
            )))
        readiness = [n.payload for n in notices if n.payload["kind"] == "readiness_changed"]
        assert len(readiness) == 8
        assert all(n["recipient_telegram_user_id"] != owner for n in readiness)
        assert all(n["player_name"] == fixture.players[0].public_nickname for n in readiness)
        assert {n["ready_count"] for n in readiness} == {0, 1}
        assert all(n["player_count"] == 4 for n in readiness)
        assert any(n.payload["kind"] == "settings_changed" for n in notices)
    finally:
        await database.close()


async def test_ongoing_overview_lists_lobbies_and_observable_games(
    database_url: str,
) -> None:
    from sitg_bot.services.telegram_game import TelegramGameService

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=3)
        matchmaking = InvitationMatchmakingService(database)
        games = PersistentGameService(database)
        tournaments = TournamentService(database)
        telegram_games = TelegramGameService(database)

        async with database.sessions() as session:
            context = await tournaments.context(session, fixture.tournament_id)
        await tournaments.update_policy(
            fixture.tournament_id,
            fixture.manager.id,
            default_parameters=context.settings.to_dict(),
            player_mutable_parameters=context.mutable_parameters,
            policies={**context.policies, "observing": "unlimited"},
        )

        lobby = await matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
        )
        await matchmaking.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        await matchmaking.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        started = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert started.game is not None
        game_id = started.game.id
        await games.join(game_id, fixture.inputs[0].telegram_user_id)

        open_lobby = await matchmaking.create_lobby(
            fixture.inputs[2], tournament_id=fixture.tournament_id, max_players=2
        )
        lobby_cards = await matchmaking.ongoing_lobbies(fixture.players[1].id)
        assert [card["id"] for card in lobby_cards] == [str(open_lobby.id)]
        lobby_card = lobby_cards[0]
        assert lobby_card["tournament_name"] == "Architecture tournament"
        assert lobby_card["invitation_code"] == open_lobby.invitation_code
        assert lobby_card["is_member"] is False
        assert any(
            member["display_name"] == fixture.players[2].public_nickname
            for member in lobby_card["members"]
        )

        game_cards = await games.ongoing_games(fixture.players[1].id)
        assert [str(card["id"]) for card in game_cards] == [str(game_id)]
        game_card = game_cards[0]
        assert game_card["tournament_name"] == "Architecture tournament"
        assert game_card["can_observe"] is True
        assert game_card["confirmation_required"] is True
        assert game_card["observing"] is False
        assert fixture.players[0].public_nickname in game_card["participants"]
        assert await games.ongoing_games(fixture.players[0].id) == ()

        warning = await telegram_games.observe(
            fixture.inputs[1].telegram_user_id, game_id, confirm_fresh=False
        )
        assert warning["game_id"] == str(game_id)
        assert warning["joined"] is False
        assert warning["confirmation_required"] is True
        assert warning["fresh_content_count"] > 0

        joined = await telegram_games.observe(
            fixture.inputs[1].telegram_user_id, game_id, confirm_fresh=True
        )
        assert joined["joined"] is True
        assert joined["confirmation_required"] is False

        assert (await games.ongoing_games(fixture.players[1].id))[0]["observing"] is True
        async with database.sessions() as session:
            cursor = await session.get(TelegramGameViewRecord, (game_id, fixture.players[1].id))
            assert cursor is not None
            assert cursor.dismissed_at is None
            assert cursor.flow_sequence == 0
            replay_event = await session.scalar(
                select(OutboxEventRecord).where(
                    OutboxEventRecord.topic == "game.event",
                    OutboxEventRecord.deduplication_key.like(
                        f"game:{game_id}:observe:{fixture.inputs[1].telegram_user_id}:%"
                    ),
                )
            )
            assert replay_event is not None
            assert replay_event.payload["recipient_telegram_user_id"] == (
                fixture.inputs[1].telegram_user_id
            )
            assert replay_event.payload["game_id"] == str(game_id)

        again = await telegram_games.observe(
            fixture.inputs[1].telegram_user_id, game_id, confirm_fresh=True
        )
        assert again["joined"] is True
        async with database.sessions() as session:
            replay_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventRecord)
                .where(
                    OutboxEventRecord.topic == "game.event",
                    OutboxEventRecord.deduplication_key.like(
                        f"game:{game_id}:observe:{fixture.inputs[1].telegram_user_id}:%"
                    ),
                )
            )
        assert replay_count == 1
    finally:
        await database.close()


async def test_managers_bypass_observing_restrictions_in_managed_tournaments(
    database_url: str,
) -> None:
    from sitg_bot.services.telegram_game import TelegramGameService

    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=2)
        matchmaking = InvitationMatchmakingService(database)
        games = PersistentGameService(database)
        telegram_games = TelegramGameService(database)

        # The fixture policy leaves observing forbidden.
        lobby = await matchmaking.create_lobby(
            fixture.inputs[0], tournament_id=fixture.tournament_id, max_players=1
        )
        await matchmaking.select_packet(
            lobby.id, fixture.inputs[0].telegram_user_id, fixture.packet_id
        )
        await matchmaking.set_ready(lobby.id, fixture.inputs[0].telegram_user_id)
        started = await matchmaking.start(lobby.id, fixture.inputs[0].telegram_user_id)
        assert started.game is not None
        game_id = started.game.id
        await games.join(game_id, fixture.inputs[0].telegram_user_id)

        open_lobby = await matchmaking.create_lobby(
            fixture.inputs[1], tournament_id=fixture.tournament_id, max_players=2
        )

        # Members are still blocked by the forbidden observing policy.
        member_cards = await games.ongoing_games(fixture.players[1].id)
        assert [str(card["id"]) for card in member_cards] == [str(game_id)]
        assert member_cards[0]["can_observe"] is False
        assert member_cards[0]["managed"] is False
        with pytest.raises(PermissionError, match="forbidden"):
            await games.observe(game_id, fixture.inputs[1].telegram_user_id)

        # The manager sees lobbies and games from the managed tournament.
        lobby_cards = await matchmaking.ongoing_lobbies(fixture.manager.id)
        assert [card["id"] for card in lobby_cards] == [str(open_lobby.id)]
        assert lobby_cards[0]["viewer_manages"] is True
        assert lobby_cards[0]["is_member"] is False

        manager_cards = await games.ongoing_games(fixture.manager.id)
        assert [str(card["id"]) for card in manager_cards] == [str(game_id)]
        assert manager_cards[0]["managed"] is True
        assert manager_cards[0]["can_observe"] is True
        # The manager authored the packet, so nothing fresh is burned.
        assert manager_cards[0]["fresh_content_count"] == 0
        assert manager_cards[0]["confirmation_required"] is False

        joined = await telegram_games.observe(
            fixture.manager.telegram_user_id, game_id, confirm_fresh=False
        )
        assert joined["joined"] is True
        assert joined["confirmation_required"] is False
        async with database.sessions() as session:
            observer = await session.scalar(
                select(GameObserverRecord).where(
                    GameObserverRecord.game_id == game_id,
                    GameObserverRecord.player_id == fixture.manager.id,
                )
            )
            assert observer is not None and observer.active

        # An unaffiliated player sees nothing and cannot observe.
        async with database.transaction() as session:
            outsider = PlayerRecord(
                telegram_user_id=int(secrets.token_hex(4), 16),
                real_name="Ongoing outsider",
                public_nickname="Ongoing outsider",
                registration_step="complete",
                registration_completed_at=datetime.now(UTC),
                status="active",
            )
            session.add(outsider)
            await session.flush()
            outsider_id = outsider.id
            outsider_telegram_id = outsider.telegram_user_id
        assert await games.ongoing_games(outsider_id) == ()
        assert await matchmaking.ongoing_lobbies(outsider_id) == ()
        with pytest.raises(PermissionError, match="membership"):
            await games.observe(game_id, outsider_telegram_id)
    finally:
        await database.close()
