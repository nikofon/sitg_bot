"""Exercise management queries on disposable SQLite data, without PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, Table, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql import visitors

from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage import models as m

MODELS = (
    m.PlayerRecord, m.TournamentRecord, m.TournamentTypeVersionRecord,
    m.GameRulesetVersionRecord, m.TournamentPolicyVersionRecord,
    m.TournamentManagerRecord, m.TournamentMembershipRecord,
    m.TournamentPricingPlanRecord, m.TournamentPricingPlanPriceRecord,
    m.TournamentAuthorRecord, m.AuthorRecord, m.TournamentPacketAssignmentRecord,
    m.PacketVersionRecord, m.TournamentPacketEntitlementRecord,
    m.ThemeRevisionRecord, m.ThemeRevisionCoauthorRecord, m.QuestionRevisionRecord,
    m.QuestionRevisionCoauthorRecord, m.PacketQuestionRecord,
    m.TournamentSubscriptionCardRecord, m.TournamentSubscriptionRecord,
    m.ClassicStageRecord, m.ClassicRoundRecord, m.ClassicMatchRecord,
)
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


class AsyncSessionFacade:
    def __init__(self, session):
        self.session = session

    async def execute(self, query):
        return self.session.execute(query)

    async def scalar(self, query):
        return self.session.scalar(query)

    async def scalars(self, query):
        return self.session.scalars(query)

    async def get(self, model, identity):
        return self.session.get(model, identity)


class ManagementDatabase:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        metadata = MetaData()
        self.tables = {
            model: Table(model.__tablename__, metadata, *(
                Column(c.name, JSON() if isinstance(c.type, JSONB) else c.type,
                       primary_key=c.primary_key, nullable=True)
                for c in model.__table__.columns
            )) for model in MODELS
        }
        metadata.create_all(self.engine)
        self.queries = []

        @event.listens_for(self.engine, "before_cursor_execute")
        def count(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                self.queries.append(statement)

    def insert(self, model, **values):
        row = {c.name: None for c in model.__table__.columns}
        row.update({name: STAMP for name in ("created_at", "updated_at") if name in row})
        row.update(values)
        with self.engine.begin() as connection:
            connection.execute(self.tables[model].insert(), row)

    def update(self, model, where, **values):
        where = visitors.replacement_traverse(where, {}, lambda element: (
            self.tables[model].c[element.name]
            if getattr(element, "table", None) is model.__table__ else None
        ))
        with self.engine.begin() as connection:
            connection.execute(self.tables[model].update().where(where).values(**values))

    @asynccontextmanager
    async def sessions(self):
        with Session(self.engine) as session:
            @event.listens_for(session, "loaded_as_persistent")
            def restore_timezone(session, record):
                # Match PostgreSQL's aware timestamps when SQLite returns naive values.
                for column in record.__table__.columns:
                    value = getattr(record, column.name)
                    if isinstance(column.type, DateTime) and value and value.tzinfo is None:
                        set_committed_value(record, column.name, value.replace(tzinfo=UTC))

            yield AsyncSessionFacade(session)


@pytest.fixture
def management_database():
    database = ManagementDatabase()
    try:
        yield database
    finally:
        database.engine.dispose()


def seed(database, *, packets=1, players=3, type_key="ladder", content=False):
    tournament_id, manager_id, type_id, ruleset_id = (uuid4() for _ in range(4))
    player_ids = [uuid4() for _ in range(players)]
    database.insert(m.TournamentTypeVersionRecord, id=type_id, key=type_key,
                    version=1, name=type_key, rules={}, active=True)
    database.insert(m.GameRulesetVersionRecord, id=ruleset_id, key="si", version=1)
    database.insert(m.TournamentPolicyVersionRecord, id=uuid4(), tournament_id=tournament_id,
                    version=1, policies={}, default_parameters={}, player_mutable_parameters=[])
    database.insert(m.TournamentRecord, id=tournament_id, name="Cup", slug=str(tournament_id),
                    status="active", moderation_status="normal", type_version_id=type_id,
                    game_ruleset_version_id=ruleset_id, visibility="private", language="en",
                    payment_type="free", registration_open=False, settings_version=1,
                    ignore_late_registrations=False)
    for index, player_id in enumerate([manager_id, *player_ids]):
        database.insert(m.PlayerRecord, id=player_id, status="active",
                        public_nickname=f"Player {index:03}")
    database.insert(m.TournamentManagerRecord, tournament_id=tournament_id, player_id=manager_id)
    for player_id in player_ids:
        database.insert(m.TournamentMembershipRecord, tournament_id=tournament_id,
                        player_id=player_id, status="active", registered_at=STAMP,
                        registration_rejection_reasons=[])
    assignments = []
    author_id, coauthor_id = uuid4(), uuid4()
    if content:
        database.insert(m.AuthorRecord, id=author_id, display_name="Author")
        database.insert(m.AuthorRecord, id=coauthor_id, display_name="Coauthor")
    for index in range(packets):
        assignment_id, packet_id, version_id = (uuid4() for _ in range(3))
        database.insert(m.PacketVersionRecord, id=version_id, packet_id=packet_id,
                        version_number=1, name=f"Packet {index:03}", state="published",
                        published_at=STAMP, year=2026,
                        lead_author_id=author_id if content else None)
        if content:
            theme, question = uuid4(), uuid4()
            database.insert(m.ThemeRevisionRecord, id=theme, packet_version_id=version_id,
                            author_id=author_id)
            database.insert(m.ThemeRevisionCoauthorRecord, content_id=theme, author_id=coauthor_id)
            database.insert(m.QuestionRevisionRecord, id=question, author_id=coauthor_id)
            database.insert(m.PacketQuestionRecord, packet_version_id=version_id,
                            theme_revision_id=theme, question_revision_id=question, position=1)
        database.insert(m.TournamentPacketAssignmentRecord, id=assignment_id,
                        tournament_id=tournament_id, packet_id=packet_id,
                        adopted_version_id=version_id, status="active",
                        playable_by_members=True, discoverable_by_members=True,
                        content_visible_by_members=False, library_viewing_rule="after-play",
                        created_at=STAMP + timedelta(seconds=index))
        assignments.append(assignment_id)
    return SimpleNamespace(tournament_id=tournament_id, manager_id=manager_id,
                           player_ids=player_ids, assignment_ids=assignments)


@pytest.mark.parametrize("packets", [1, 50, 200])
@pytest.mark.parametrize("players", [1, 20])
async def test_management_query_counts_are_bounded(management_database, packets, players):
    database = management_database
    fixture = seed(database, packets=packets, players=players, content=True)
    service = TournamentService(database)
    database.queries.clear()
    view = await service.manager_management(fixture.tournament_id, fixture.manager_id,
                                            include_player_access=False)
    assert len(view.packets) == packets
    assert view.participant_count == players
    assert all(packet.player_access is None for packet in view.packets)
    assert len(database.queries) == 13
    assert not any("tournament_packet_entitlements" in query for query in database.queries)

    database.queries.clear()
    access = await service.manager_packet_access(
        fixture.tournament_id, fixture.assignment_ids[0], fixture.manager_id
    )
    assert len(access["player_access"]) == players
    assert all(row.playable and row.discoverable and not row.readable
               for row in access["player_access"])
    assert len(database.queries) == 7

    database.queries.clear()
    legacy = await service.manager_management(fixture.tournament_id, fixture.manager_id)
    assert all(len(packet.player_access) == players for packet in legacy.packets)
    assert len(database.queries) == 16


@pytest.mark.parametrize("packets,players", [(0, 3), (3, 0), (0, 0)])
async def test_empty_management_lists(management_database, packets, players):
    fixture = seed(management_database, packets=packets, players=players)
    service = TournamentService(management_database)
    view = await service.manager_management(fixture.tournament_id, fixture.manager_id)
    assert view.packet_count == packets
    assert view.participant_count == players
    assert all(packet.player_access == () for packet in view.packets)


@pytest.mark.parametrize("type_key", ["ladder", "classic"])
@pytest.mark.parametrize("moderation", ["normal", "halted", "abolished"])
@pytest.mark.parametrize("manager_revoked", [False, True])
async def test_batched_access_matches_individual_checks(
    management_database, type_key, moderation, manager_revoked,
):
    database = management_database
    fixture = seed(database, packets=2, players=4, type_key=type_key)
    database.update(m.TournamentRecord, m.TournamentRecord.id == fixture.tournament_id,
                    moderation_status=moderation)
    database.insert(m.TournamentManagerRecord, tournament_id=fixture.tournament_id,
                    player_id=fixture.player_ids[2], revoked_at=STAMP if manager_revoked else None)
    database.update(m.TournamentMembershipRecord,
                    m.TournamentMembershipRecord.player_id == fixture.player_ids[3],
                    status="approved")
    for assignment_id in fixture.assignment_ids:
        for index, player_id in enumerate(fixture.player_ids[:2]):
            database.insert(m.TournamentPacketEntitlementRecord,
                            assignment_id=assignment_id, player_id=player_id,
                            playable=False, discoverable=True, content_visible=True,
                            discoverable_override=False, readable_override=False,
                            revoked_at=STAMP if index else None)
    async with database.sessions() as session:
        tournament = await session.get(m.TournamentRecord, fixture.tournament_id)
        assignments = [await session.get(m.TournamentPacketAssignmentRecord, key)
                       for key in fixture.assignment_ids]
        batched = await TournamentService._management_player_access(
            session, tournament, assignments, type_key
        )
        for assignment in assignments:
            assert len(batched[assignment.id]) == 3
            for row in batched[assignment.id]:
                for field, right in (("playable", "playable"), ("discoverable", "discoverable"),
                                     ("readable", "content_visible")):
                    assert getattr(row, field) == await TournamentService.has_assignment_access(
                        session, assignment, row.player_id, right
                    )


@pytest.mark.parametrize("scenario", [
    "open", "not_started", "completed", "played", "game", "expired", "override_off",
    "future", "override_on", "discoverable_off",
])
async def test_classic_batched_round_access(management_database, scenario):
    database = management_database
    fixture = seed(database, type_key="classic")
    assignment_id = fixture.assignment_ids[0]
    stage_id, round_id = uuid4(), uuid4()
    database.insert(m.ClassicStageRecord, id=stage_id, tournament_id=fixture.tournament_id,
                    started_at=None if scenario == "not_started" else STAMP,
                    completed_at=STAMP if scenario == "completed" else None)
    database.insert(m.ClassicRoundRecord, id=round_id, stage_id=stage_id,
                    assignment_id=assignment_id, playable=False if scenario == "override_off"
                    else True if scenario == "override_on" else None,
                    discoverable=False if scenario == "discoverable_off"
                    else True if scenario == "override_on" else None,
                    start_deadline=STAMP if scenario == "expired"
                    else datetime.now(UTC) + timedelta(days=1) if scenario == "future" else None)
    if scenario == "override_on":
        database.update(m.TournamentPacketAssignmentRecord,
                        m.TournamentPacketAssignmentRecord.id == assignment_id,
                        playable_by_members=False, discoverable_by_members=False)
    database.insert(m.ClassicMatchRecord, id=uuid4(), round_id=round_id,
                    seats=[str(fixture.player_ids[0]), "chair:1"],
                    results=[] if scenario == "played" else None,
                    game_id=uuid4() if scenario == "game" else None)
    service = TournamentService(database)
    database.queries.clear()
    access = await service.manager_packet_access(
        fixture.tournament_id, assignment_id, fixture.manager_id
    )
    assert len(database.queries) == 8
    seated, unseated = access["player_access"][:2]
    assert seated.playable is (scenario in {"open", "future", "override_on", "discoverable_off"})
    assert seated.discoverable is (scenario not in {"not_started", "discoverable_off"})
    assert not unseated.playable and not unseated.discoverable
    async with database.sessions() as session:
        assignment = await session.get(m.TournamentPacketAssignmentRecord, assignment_id)
        for right in ("playable", "discoverable"):
            assert getattr(seated, right) == await service.has_assignment_access(
                session, assignment, seated.player_id, right
            )


@pytest.mark.parametrize("defaults,grant,playable,override,revoked,expected", [
    (True, False, False, False, False, (False, False, False)),
    (True, False, False, False, True, (True, True, True)),
    (False, True, None, None, False, (False, True, True)),
    (False, False, True, True, False, (True, True, True)),
    (False, True, True, None, True, (False, False, False)),
])
async def test_entitlement_overrides_and_revocation(
    management_database, defaults, grant, playable, override, revoked, expected,
):
    database = management_database
    fixture = seed(database, players=1)
    database.update(m.TournamentPacketAssignmentRecord,
                    m.TournamentPacketAssignmentRecord.id == fixture.assignment_ids[0],
                    playable_by_members=defaults, discoverable_by_members=defaults,
                    content_visible_by_members=defaults)
    database.insert(m.TournamentPacketEntitlementRecord,
                    assignment_id=fixture.assignment_ids[0], player_id=fixture.player_ids[0],
                    playable=playable, discoverable=grant, content_visible=grant,
                    discoverable_override=override, readable_override=override,
                    revoked_at=STAMP if revoked else None)
    access = await TournamentService(database).manager_packet_access(
        fixture.tournament_id, fixture.assignment_ids[0], fixture.manager_id
    )
    row = access["player_access"][0]
    assert (row.playable, row.discoverable, row.readable) == expected


async def test_access_rechecks_manager_and_assignment(management_database):
    database = management_database
    fixture = seed(database)
    other = seed(database)
    service = TournamentService(database)
    with pytest.raises(LookupError):
        await service.manager_packet_access(fixture.tournament_id, other.assignment_ids[0],
                                           fixture.manager_id)
    database.update(m.TournamentPacketAssignmentRecord,
                    m.TournamentPacketAssignmentRecord.id == fixture.assignment_ids[0],
                    status="retired")
    with pytest.raises(LookupError):
        await service.manager_packet_access(fixture.tournament_id, fixture.assignment_ids[0],
                                           fixture.manager_id)
    database.update(m.TournamentManagerRecord,
                    m.TournamentManagerRecord.player_id == fixture.manager_id, revoked_at=STAMP)
    with pytest.raises(PermissionError):
        await service.manager_packet_access(fixture.tournament_id, fixture.assignment_ids[0],
                                           fixture.manager_id)


async def test_metadata_versions_and_content_coauthors(management_database):
    database = management_database
    fixture = seed(database, packets=4)
    async with database.sessions() as session:
        assignments = [await session.get(m.TournamentPacketAssignmentRecord, key)
                       for key in fixture.assignment_ids]
    pinned, fallback, missing, unpublished = assignments
    lead, author, coauthor = uuid4(), uuid4(), uuid4()
    for author_id, name in ((lead, "Lead"), (author, "Author"), (coauthor, "Coauthor")):
        database.insert(m.AuthorRecord, id=author_id, display_name=name)
    database.update(m.PacketVersionRecord, m.PacketVersionRecord.id == pinned.adopted_version_id,
                    state="archived", lead_author_id=lead)
    for number, state in ((2, "published"), (3, "archived")):
        database.insert(m.PacketVersionRecord, id=uuid4(), packet_id=fallback.packet_id,
                        version_number=number, state=state, name=f"Version {number}")
    database.update(m.TournamentPacketAssignmentRecord,
                    m.TournamentPacketAssignmentRecord.id == fallback.id, adopted_version_id=None)
    database.update(m.TournamentPacketAssignmentRecord,
                    m.TournamentPacketAssignmentRecord.id == missing.id,
                    adopted_version_id=uuid4())
    database.update(m.TournamentPacketAssignmentRecord,
                    m.TournamentPacketAssignmentRecord.id == unpublished.id,
                    adopted_version_id=None)
    database.update(m.PacketVersionRecord,
                    m.PacketVersionRecord.id == unpublished.adopted_version_id, state="archived")
    theme, question = uuid4(), uuid4()
    database.insert(m.ThemeRevisionRecord, id=theme,
                    packet_version_id=pinned.adopted_version_id, author_id=author)
    database.insert(m.ThemeRevisionCoauthorRecord, content_id=theme, author_id=coauthor)
    database.insert(m.QuestionRevisionRecord, id=question, author_id=coauthor)
    database.insert(m.QuestionRevisionCoauthorRecord, content_id=question, author_id=author)
    database.insert(m.PacketQuestionRecord, packet_version_id=pinned.adopted_version_id,
                    theme_revision_id=theme, question_revision_id=question, position=1)
    view = await TournamentService(database).manager_management(
        fixture.tournament_id, fixture.manager_id, include_player_access=False
    )
    assert [p.assignment_id for p in view.packets] == fixture.assignment_ids
    first, second, third, fourth = view.packets
    assert first.packet_version_id == pinned.adopted_version_id
    assert first.lead_author == "Lead"
    assert first.authors == ("Author", "Coauthor")
    assert view.tournament.authors == ("Author", "Coauthor", "Lead")
    assert second.version == 2
    assert third.packet_version_id is None and third.name == str(missing.packet_id)
    assert fourth.packet_version_id is None and fourth.name == str(unpublished.packet_id)
