from uuid import UUID

import pytest
from conftest import create_player
from sqlalchemy import event, select
from test_training import upload

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.services.knowledge_images import KnowledgeImageService
from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.storage.models import KnowledgeImageRecord

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
    from sitg_bot.services.concurrency import StaleWriteError

    owner = await create_player(database, 9471, "filter-owner")
    other = await create_player(database, 9472, "filter-other")
    version_id, _ = await upload(database, owner)
    other_version, _ = await upload(database, other)
    packets = TrainingPacketService(database)
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
