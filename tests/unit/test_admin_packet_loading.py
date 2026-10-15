"""Admin packet projections use bounded queries and preserve every version."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, select
from sqlalchemy.dialects.postgresql import JSONB
from test_management_loading import STAMP, ManagementDatabase, seed

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.services.admin_management import AdminManagementService
from sitg_bot.storage import models as m


@pytest.fixture
def admin_database():
    database = ManagementDatabase()
    metadata = MetaData()
    for model in (
        m.LogicalPacketRecord, m.PlatformAdministratorRecord, m.PlayerAuthorLinkRecord,
        m.TournamentRegistrationRequirementRecord,
    ):
        database.tables[model] = Table(model.__tablename__, metadata, *(
            Column(c.name, JSON() if isinstance(c.type, JSONB) else c.type,
                   primary_key=c.primary_key, nullable=True)
            for c in model.__table__.columns
        ))
    metadata.create_all(database.engine)
    try:
        yield database
    finally:
        database.engine.dispose()


async def admin_seed(database, *, packets=3, content=True):
    fixture = seed(database, packets=packets, players=0, content=content)
    database.insert(m.PlatformAdministratorRecord, player_id=fixture.manager_id)
    async with database.sessions() as session:
        versions = tuple(await session.scalars(select(m.PacketVersionRecord)))
    for version in versions:
        database.insert(m.LogicalPacketRecord, id=version.packet_id)
    return fixture, sorted(versions, key=lambda version: version.id)


@pytest.mark.parametrize("packets", [1, 50, 200])
async def test_packet_catalogue_queries_do_not_grow_per_version(admin_database, packets):
    database = admin_database
    fixture, versions = await admin_seed(database, packets=packets)
    service = AdminManagementService(database)
    database.queries.clear()
    result = await service.catalogue(fixture.manager_id, "packets")
    assert len(database.queries) == 7
    assert [item["id"] for item in result["items"]] == [version.id for version in versions]
    for card in result["items"]:
        assert card["packet"]["id"] == card["packet_id"]
        assert card["themes"] == card["questions"] == 1
        assert {author["display_name"] for author in card["authors"]} == {"Author", "Coauthor"}
        assert card["tournaments"][0]["id"] == fixture.tournament_id
        assert card["assignments"][0]["packet_id"] == card["packet_id"]
    database.queries.clear()
    deferred = await service.catalogue(fixture.manager_id, "packets", include_items=False)
    assert deferred["items"] == [] and deferred["packets_deferred"]
    assert len(database.queries) == 1
    database.queries.clear()
    page = await service.packet_page(fixture.manager_id)
    assert len(database.queries) == 7
    assert page["items"] == result["items"][:25]
    assert page["next_cursor"] == (versions[24].id if packets > 25 else None)


async def test_pages_keep_multiple_versions_of_a_packet_and_all_assignment_states(admin_database):
    database = admin_database
    fixture, versions = await admin_seed(database, packets=1)
    archived_id = uuid4()
    database.insert(m.PacketVersionRecord, id=archived_id, packet_id=versions[0].packet_id,
                    version_number=2, name="Archived", state="archived", published_at=STAMP)
    database.update(m.TournamentPacketAssignmentRecord,
                    m.TournamentPacketAssignmentRecord.id == fixture.assignment_ids[0],
                    status="retired")
    service = AdminManagementService(database)
    items, cursor = [], None
    while True:
        page = await service.packet_page(fixture.manager_id, cursor=cursor, limit=1)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {item["id"] for item in items} == {versions[0].id, archived_id}
    assert len({item["packet_id"] for item in items}) == 1
    assert {item["state"] for item in items} == {"published", "archived"}
    assert all(item["assignments"][0]["status"] == "retired" for item in items)
    assert [item["questions"] for item in items if item["id"] == archived_id] == [0]


async def test_empty_pages_and_admin_revocation(admin_database):
    database = admin_database
    fixture, _ = await admin_seed(database, packets=0)
    service = AdminManagementService(database)
    assert await service.packet_page(fixture.manager_id) == {"items": [], "next_cursor": None}
    with pytest.raises(ValueError):
        await service.packet_page(fixture.manager_id, limit=26)
    database.update(m.PlatformAdministratorRecord,
                    m.PlatformAdministratorRecord.player_id == fixture.manager_id, revoked_at=STAMP)
    with pytest.raises(PermissionError):
        await service.packet_page(fixture.manager_id)
    with pytest.raises(PermissionError):
        await service.catalogue(fixture.manager_id, "packets", include_items=False)


@pytest.mark.parametrize("paged", [True, False])
async def test_gateway_dispatches_admin_packet_loading_options(paged):
    gateway = object.__new__(ApplicationGateway)
    gateway.database = object()
    gateway._require_active_principal = AsyncMock(return_value=UUID(int=2))
    gateway._reject_banned_player = AsyncMock()
    service = SimpleNamespace(catalogue=AsyncMock(return_value={}),
                              packet_page=AsyncMock(return_value={}))
    operation = (
        {"action": ActionCode.ADMIN_PACKET_LIST, "cursor": str(UUID(int=7)), "limit": 10}
        if paged else {"action": ActionCode.ADMIN_MANAGEMENT_LIST,
                       "section": "packets", "include_items": False}
    )
    request = GatewayRequest.model_validate({"metadata": {
        "channel": "mini_app", "client_name": "test", "client_version": "1",
    }, "operation": operation})
    with patch("sitg_bot.application.gateway.AdminManagementService", return_value=service):
        await gateway._dispatch(ApplicationPrincipal(telegram_user_id=42), request)
    if paged:
        service.packet_page.assert_awaited_once_with(UUID(int=2), cursor=UUID(int=7), limit=10)
    else:
        service.catalogue.assert_awaited_once_with(UUID(int=2), "packets", include_items=False)


@pytest.mark.parametrize("packets", [1, 50, 200])
async def test_embedded_tournament_packet_names_use_one_query(admin_database, packets):
    database = admin_database
    fixture, versions = await admin_seed(database, packets=packets)
    database.insert(m.PacketVersionRecord, id=uuid4(), packet_id=versions[0].packet_id,
                    version_number=2, name="Latest archived name", state="archived")
    database.queries.clear()
    result = await AdminManagementService(database).catalogue(fixture.manager_id, "tournaments")
    card = result["items"][0]
    assert len(card["packets"]) == packets
    names = {item["packet_id"]: item["name"] for item in card["packets"]}
    assert names[versions[0].packet_id] == "Latest archived name"
    packet_queries = [query for query in database.queries if "packet_versions" in query]
    assert len(packet_queries) == 1


@pytest.mark.parametrize("packets", [1, 50, 200])
async def test_embedded_author_packets_include_leads_and_coauthors_in_one_query(
    admin_database, packets,
):
    database = admin_database
    fixture, versions = await admin_seed(database, packets=packets)
    lead_only = uuid4()
    database.insert(m.AuthorRecord, id=lead_only, display_name="Lead only")
    database.update(m.PacketVersionRecord, m.PacketVersionRecord.id == versions[0].id,
                    lead_author_id=lead_only)
    database.queries.clear()
    result = await AdminManagementService(database).catalogue(fixture.manager_id, "authors")
    by_name = {card["display_name"]: card for card in result["items"]}
    for name in ("Author", "Coauthor"):
        assert by_name[name]["packet_count"] == packets
        assert len(by_name[name]["packets"]) == packets
    assert [item["id"] for item in by_name["Lead only"]["packets"]] == [versions[0].id]
    version_queries = [query for query in database.queries if "packet_versions" in query]
    assert len(version_queries) == 1
