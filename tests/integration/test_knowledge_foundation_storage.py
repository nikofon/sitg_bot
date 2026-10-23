from uuid import UUID, uuid4

import pytest
from conftest import create_player
from sqlalchemy import event, select
from test_training import upload

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.domain.knowledge import FactContent, dissection_candidate_id
from sitg_bot.services.knowledge import KnowledgeService
from sitg_bot.services.knowledge_store import PostgresKnowledgeStore
from sitg_bot.services.knowledge_images import KnowledgeImageService
from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.storage.models import KnowledgeFactRecord, KnowledgeImageRecord, PacketVersionRecord

pytestmark = pytest.mark.integration
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG2kAAAAASUVORK5CYII="
)


async def test_persisted_images_are_owner_scoped_and_copies_survive_source_deletion(database):
    owner = await create_player(database, 9451, "image-owner")
    recipient = await create_player(database, 9452, "image-recipient")
    other = await create_player(database, 9453, "image-other")
    gateway = ApplicationGateway(database)
    principal = ApplicationPrincipal(player_id=owner, telegram_user_id=9451)
    request = GatewayRequest.model_validate({
        "metadata": {"channel": "mini_app", "client_name": "image-test", "client_version": "1",
                     "idempotency_key": "image-upload-retry"},
        "operation": {"action": ActionCode.KNOWLEDGE_IMAGE_UPLOAD, "source_base64": PNG_BASE64},
    })
    uploaded = await gateway.execute(principal, request)
    assert uploaded.ok, uploaded.error
    replayed = await gateway.execute(principal, request)
    assert replayed.ok and replayed.data == uploaded.data
    image_id = UUID(uploaded.data["id"])
    images = KnowledgeImageService(database)
    assert (await images.read(owner, image_id))["source_base64"] == PNG_BASE64
    with pytest.raises(LookupError):
        await images.read(recipient, image_id)
    app = KnowledgeApplication(database)
    with pytest.raises(LookupError):
        await app.mutate(other, "create", {"title": "Stolen", "description": "D",
                                           "image_ids": [image_id]})
    fact = await app.mutate(owner, "create", {
        "title": "Illustrated", "description": "D", "image_ids": [image_id],
    })
    assert fact["images"][0]["id"] == image_id and fact["links"] == []
    await app.mutate(owner, "grant", {"recipient": str(recipient)})
    copied = (await app.facts(recipient))["items"][0]
    assert copied["images"] == fact["images"] and copied["packet_name"] is None
    await app.mutate(owner, "delete", {"fact_id": fact["id"], "confirm_dissection_origin": False})
    assert (await images.read(recipient, image_id))["source_base64"] == PNG_BASE64
    # A recipient can retain/reuse an image already attached to their own copy.
    await app.mutate(recipient, "update", {"fact_id": copied["id"], "expected_version": 1,
                                            "image_ids": [image_id], "title": "Own copy"})
    with pytest.raises(LookupError):
        await images.read(other, image_id)
    await app.mutate(recipient, "update", {"fact_id": copied["id"], "expected_version": 2,
                                            "image_ids": []})
    with pytest.raises(LookupError):
        await images.read(recipient, image_id)
    async with database.sessions() as session:
        assert len(list(await session.scalars(select(KnowledgeImageRecord)))) == 1


async def test_multitag_sql_search_pagination_and_metadata_reads_are_page_batched(database):
    owner = await create_player(database, 9461, "tags-owner")
    other = await create_player(database, 9462, "tags-other")
    app = KnowledgeApplication(database)
    for index in range(5):
        await app.mutate(owner, "create", {"title": f"Fact {index}", "description": "D",
                                            "tag_names": ["a", "b"] if index < 4 else ["a"]})
    await app.mutate(other, "create", {
        "title": "Other", "description": "D", "tag_names": ["a", "b"]
    })
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        first = await app.facts(owner, tags=[" A ", "b", "B"], limit=2)
        count = len(queries)
        assert first["total"] == 4 and len(first["items"]) == 2 and first["next_cursor"]
        queries.clear()
        second = await app.facts(owner, tags=["a", "b"], cursor=first["next_cursor"], limit=2)
        assert len(queries) == count
        assert [row["title"] for row in first["items"] + second["items"]] == [
            f"Fact {index}" for index in range(4)
        ]
        assert second["next_cursor"] is None
        queries.clear()
        await app.facts(owner, tags=["a", "b"], limit=4)
        assert len(queries) == count  # No additional query per card.
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert (await app.facts(owner, tags=["unknown"]))["total"] == 0


async def test_upload_only_catalogue_filter_and_split_lobby_mutations(database):
    from test_lobby_architecture import tournament_fixture

    from sitg_bot.services.concurrency import StaleWriteError

    fixture = await tournament_fixture(database, player_count=1)
    owner = fixture.manager.id
    other = await create_player(database, 9472, "filter-other")
    version_id, _ = await upload(database, owner)
    other_version, _ = await upload(database, other)
    packets = TrainingPacketService(database)
    available = (await packets.list_packets(owner))["items"]
    assert any(item["scope"] == "tournament" for item in available)
    own = (await packets.list_packets(owner, owner_uploads_only=True))["items"]
    assert [item["id"] for item in own] == [version_id]
    assert other_version not in [item["id"] for item in own]
    service = TrainingService(database)
    initial = (await service.lobby(owner, "create"))["lobby"]
    settings = (await service.lobby(owner, "save_settings", expected_version=initial["version"],
                                    settings={"reversed": True}))["lobby"]
    selected = (await service.lobby(owner, "select_packet", expected_version=settings["version"],
                                    version_id=version_id))["lobby"]
    assert selected["settings"] == settings["settings"]
    with pytest.raises(StaleWriteError):
        await service.lobby(
            owner, "save_settings", expected_version=settings["version"], settings={}
        )
    assert (await service.current(owner))["lobby"] == selected
    with pytest.raises(LookupError):
        await service.lobby(owner, "select_packet", expected_version=selected["version"],
                            version_id=other_version)
    assert (await service.current(owner))["lobby"] == selected


async def test_dissection_loads_bounded_matching_identities_and_keeps_tombstones(database):
    owner = await create_player(database, 9481, "candidate-owner")
    other = await create_player(database, 9482, "candidate-other")
    version, _ = await upload(database, owner)
    async with database.transaction() as session:
        packet = await session.get(PacketVersionRecord, version)
        packet_id = packet.packet_id
        session.add_all([
            KnowledgeFactRecord(
                owner_id=owner, title=f"Unrelated {index}", title_key=f"unrelated {index}",
                description="D",
                content_fingerprint=FactContent(f"Unrelated {index}", "D").fingerprint(),
            )
            for index in range(250)
        ])
        service = KnowledgeService(PostgresKnowledgeStore(session))
        payloads = []
        for index in range(105):
            question_id = uuid4()
            content = FactContent(f"Candidate {index}", "Extracted")
            payloads.append({
                "candidate_id": dissection_candidate_id(packet_id, question_id, "r1", content),
                "packet_id": str(packet_id), "question_id": str(question_id),
                "question_revision": "r1",
                "content": {"title": content.title, "description": content.description},
            })
        first = await service.request_dissection(owner, packet_id=packet_id, packet_revision="r1")
        applied = await service.apply_dissection_result(owner, first.request_id, payloads)
        assert len(applied.applied_fact_ids) == 105
        fact_ids = sorted(applied.applied_fact_ids)
        await service.delete_fact(owner, fact_ids[0], confirm_dissection_origin=True)
        await service.update_fact(owner, fact_ids[1], expected_version=1, description="Owner edit")
        retry = await service.request_dissection(
            owner, packet_id=packet_id, packet_revision="r1", comment="retry",
        )
        foreign = await service.request_dissection(other, packet_id=packet_id, packet_revision="r1")
        await service.apply_dissection_result(other, foreign.request_id, [payloads[0]])
        question_id = uuid4()
        content = FactContent("Only foreign", "Extracted")
        foreign_payload = {
            "candidate_id": dissection_candidate_id(packet_id, question_id, "r1", content),
            "packet_id": str(packet_id), "question_id": str(question_id),
            "question_revision": "r1",
            "content": {"title": content.title, "description": content.description},
        }
        foreign_next = await service.request_dissection(
            other, packet_id=packet_id, packet_revision="r1", comment="foreign",
        )
        await service.apply_dissection_result(other, foreign_next.request_id, [foreign_payload])

    sizes = []

    class ObservedSession:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def execute(self, statement, *args, **kwargs):
            result = await self.session.execute(statement, *args, **kwargs)
            if statement.is_select:
                frozen = result.freeze()
                sizes.append(len(frozen().all()))
                return frozen()
            return result

        async def scalars(self, *args, **kwargs):
            values = list(await self.session.scalars(*args, **kwargs))
            sizes.append(len(values))
            return values

    async with database.transaction() as session:
        service = KnowledgeService(PostgresKnowledgeStore(ObservedSession(session)))
        result = await service.apply_dissection_result(
            owner, retry.request_id, [*payloads, foreign_payload],
        )
        assert result.status == "succeeded" and len(result.applied_fact_ids) == 1
        assert sizes and max(sizes) <= 100
        assert sum(sizes) <= 106
    async with database.sessions() as session:
        rows = list(await session.scalars(select(KnowledgeFactRecord).where(
            KnowledgeFactRecord.owner_id == owner,
        )))
        assert len(rows) == 356
        by_id = {row.id: row for row in rows}
        assert by_id[fact_ids[0]].deleted_at is not None
        assert by_id[fact_ids[1]].description == "Owner edit"
        assert by_id[fact_ids[1]].content_version == 2
