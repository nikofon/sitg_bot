"""Real transaction coverage; runs only in the fixture's disposable PostgreSQL DB."""

import asyncio
from uuid import UUID

import pytest
from conftest import create_player
from sqlalchemy import select

from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.services.training_packets import TrainingPacketService
from sitg_bot.storage.models import KnowledgeImportHashRecord

pytestmark = pytest.mark.integration


async def fact(app, owner, title="Fact"):
    return await app.mutate(owner, "create", {"title": title, "description": "Original"})


async def test_link_lifecycle_independent_recipients_and_permanent_copies(database):
    owner = await create_player(database, 9101, "sender")
    first = await create_player(database, 9102, "first")
    second = await create_player(database, 9103, "second")
    app = KnowledgeApplication(database)
    source = await fact(app, owner)
    link = await app.mutate(owner, "link_create", {})
    assert link["active"] and link["url"] == f"/knowledge#import={link['token']}"
    assert len(link["token"]) == 43
    assert (await app.sharing(owner))["links"] == [link]
    assert (await app.sharing(first))["links"] == []
    for recipient in (first, second):
        result = await app.mutate(recipient, "import", {"token": link["token"]})
        assert result["imported_count"] == 1 and result["skipped_count"] == 0
        assert result["recipient_id"] == recipient
    repeat = await app.mutate(first, "import", {"token": link["token"]})
    assert repeat["imported_count"] == 0 and repeat["skipped_count"] == 1
    copied = (await app.facts(first))["items"][0]["id"]
    assert copied != source["id"]
    with pytest.raises(LookupError):
        await app.mutate(first, "link_deactivate", {"link_id": link["id"]})
    inactive = await app.mutate(owner, "link_deactivate", {"link_id": link["id"]})
    assert not inactive["active"] and inactive["deactivated_at"]
    assert await app.mutate(owner, "link_deactivate", {"link_id": link["id"]}) == inactive
    with pytest.raises(LookupError):
        await app.mutate(first, "import", {"token": link["token"]})
    edited = await app.mutate(
        first,
        "update",
        {
            "fact_id": copied,
            "expected_version": 1,
            "title": "Permanent recipient edit",
        },
    )
    assert edited["editable"]
    assert (await app.detail(owner, source["id"]))["title"] == "Fact"


async def test_concurrent_imports_hash_dedup_across_links_and_senders(database):
    owner = await create_player(database, 9111, "sender")
    other = await create_player(database, 9112, "other")
    recipient = await create_player(database, 9113, "recipient")
    app = KnowledgeApplication(database)
    await fact(app, owner)
    await fact(app, owner)  # Two source identities, identical canonical content.
    await fact(app, other)
    links = [await app.mutate(sender, "link_create", {}) for sender in (owner, owner, other)]
    results = await asyncio.gather(
        *(app.mutate(recipient, "import", {"token": link["token"]}) for link in links)
    )
    assert sum(result["imported_count"] for result in results) == 1
    assert (await app.facts(recipient))["total"] == 1
    async with database.sessions() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(KnowledgeImportHashRecord).where(
                            KnowledgeImportHashRecord.recipient_id == recipient
                        )
                    )
                )
            )
            == 1
        )
    copied = (await app.facts(recipient))["items"][0]["id"]
    await app.mutate(
        recipient,
        "update",
        {
            "fact_id": copied,
            "expected_version": 1,
            "description": "User changed content",
        },
    )
    for link in links:
        result = await app.mutate(recipient, "import", {"token": link["token"]})
        assert result["imported_count"] == 0
    await app.mutate(recipient, "delete", {"fact_id": copied, "confirm_dissection_origin": False})
    assert (await app.mutate(recipient, "import", {"token": links[-1]["token"]}))[
        "imported_count"
    ] == 0
    assert (await app.facts(recipient))["total"] == 0


async def test_import_existing_content_and_invalid_tokens_are_non_destructive(database):
    owner = await create_player(database, 9121, "sender")
    recipient = await create_player(database, 9122, "recipient")
    app = KnowledgeApplication(database)
    await fact(app, owner)
    await fact(app, recipient)
    link = await app.mutate(owner, "link_create", {})
    result = await app.mutate(recipient, "import", {"token": link["token"]})
    assert result["imported_count"] == 0 and result["skipped_count"] == 1
    with pytest.raises(ValueError):
        await app.mutate(owner, "import", {"token": link["token"]})
    with pytest.raises(LookupError):
        await app.mutate(recipient, "import", {"token": "A" * 43})
    with pytest.raises(ValueError):
        await app.mutate(recipient, "import", {"token": "not-a-token"})
    assert (await app.facts(recipient))["total"] == 1


async def test_colour_persistence_copy_and_owner_boundaries(database):
    owner = await create_player(database, 9131, "sender")
    recipient = await create_player(database, 9132, "recipient")
    app = KnowledgeApplication(database)
    tag = await app.mutate(owner, "tag_create", {"name": " History ", "colour": "#Aa33Ff"})
    assert tag.name == "history" and tag.colour == "#aa33ff"
    created = await app.mutate(
        owner,
        "create",
        {
            "title": "Tag fact",
            "description": "Example",
            "tag_names": ["history"],
        },
    )
    assert (await app.detail(owner, created["id"]))["tags"][0]["colour"] == "#aa33ff"
    await app.mutate(owner, "tag_rename", {"tag_id": tag.tag_id, "name": "events"})
    assert (await app.detail(owner, created["id"]))["tags"][0]["colour"] == "#aa33ff"
    with pytest.raises(LookupError):
        await app.mutate(
            recipient,
            "tag_rename",
            {
                "tag_id": tag.tag_id,
                "name": "other",
                "colour": "#ffffff",
            },
        )
    link = await app.mutate(owner, "link_create", {})
    await app.mutate(recipient, "import", {"token": link["token"]})
    copied = (await app.facts(recipient))["items"][0]["id"]
    assert (await app.detail(recipient, copied))["tags"][0]["colour"] == "#aa33ff"
    with pytest.raises(ValueError):
        await app.mutate(owner, "tag_create", {"name": "unsafe", "colour": "red;display:none"})


async def test_packet_metadata_private_visibility_and_no_analysis_execution(database):
    from test_training import upload

    owner = await create_player(database, 9141, "sender")
    other = await create_player(database, 9142, "other")
    version_id, _ = await upload(database, owner)
    packets = TrainingPacketService(database)
    page = await packets.list_packets(owner, name="PRIVATE", order="name_asc", limit=1)
    assert page["next_cursor"] is None
    card = page["items"][0]
    assert card["id"] == version_id
    assert card["total_theme_count"] == card["fresh_theme_count"] == 1
    assert card["dissected_theme_count"] == 0 and not card["dissected"]
    assert not card["best_buzz_calculated"]
    assert card["authors"] == card["tournaments"] == []
    assert (await packets.list_packets(other))["items"] == []
    async with database.transaction() as session:
        await packets.disclose(session, owner, version_id, confirm=True)
    card = (await packets.list_packets(owner))["items"][0]
    assert card["fresh_theme_count"] == 0


async def test_private_authors_are_projected_and_searchable_only_for_the_owner(database):
    import json

    from sitg_bot.storage.models import AuthorRecord

    owner = await create_player(database, 9143, "private-writer-owner")
    other = await create_player(database, 9144, "private-writer-other")
    packets = TrainingPacketService(database)
    versions = []
    for index in range(2):
        content = {
            "name": f"Private attribution {index}", "lead_author": "Alice",
            "themes": [{"name": "Theme", "authors": ["Alice", "Theme Writer"], "questions": [
                {"value": value, "text": "Private question", "answer": "Paris",
                 "authors": ["Question Writer"]}
                for value in (10, 20, 30, 40, 50)
            ]}],
        }
        uploaded = await packets.import_upload(owner, json.dumps(content).encode(), "private.json")
        draft = uploaded["drafts"][0]
        assert draft["errors"] == []
        published = await packets.decide_draft(
            owner, UUID(draft["draft_id"]), expected_version=draft["version"], publish=True,
        )
        versions.append(UUID(published["version_id"]))
    first = await packets.list_packets(owner, author=" aLiCe ", order="name_asc", limit=1)
    second = await packets.list_packets(
        owner, author="alice", order="name_asc", limit=1, cursor=first["next_cursor"],
    )
    assert [page["items"][0]["id"] for page in (first, second)] == versions
    assert second["next_cursor"] is None
    assert first["items"][0]["authors"] == [
        {"id": None, "name": name} for name in ("Alice", "Question Writer", "Theme Writer")
    ]
    for author in ("Alice", "Theme Writer", "Question Writer"):
        assert len((await packets.list_packets(owner, author=author))["items"]) == 2
        assert (await packets.list_packets(other, author=author))["items"] == []
    async with database.sessions() as session:
        assert list(await session.scalars(select(AuthorRecord))) == []
    await packets.retire(owner, versions[0])
    assert [item["id"] for item in (await packets.list_packets(owner, author="Alice"))["items"]] == [
        versions[1],
    ]


async def test_calculation_and_dissection_metadata_survive_analysis_cache_cleanup(database):
    from sqlalchemy import delete
    from test_training import upload
    from test_training_knowledge import application

    from sitg_bot.services.reliable_delivery import DurableJobWorker
    from sitg_bot.storage.models import TrainingAnalysisRequestRecord

    owner = await create_player(database, 9151, "analyst")
    app = await application(database, owner)
    version, _ = await upload(database, owner)
    worker = DurableJobWorker(
        app.analysis.jobs,
        {"training.analysis": app.analysis_job},
        kinds=("training.analysis",),
        batch_size=1,
    )
    for kind in ("dissection", "best_buzz"):
        await app.request_analysis(
            owner, version_id=version, kind=kind, idempotency_key=kind, confirm_exposure=True
        )
        await worker.run_once()
    card = (await app.packets.list_packets(owner))["items"][0]
    assert card["dissected_theme_count"] == 1 and card["best_buzz_calculated"]
    async with database.transaction() as session:
        await session.execute(
            delete(TrainingAnalysisRequestRecord).where(
                TrainingAnalysisRequestRecord.owner_player_id == owner
            )
        )
    after = (await app.packets.list_packets(owner))["items"][0]
    assert after["dissected_theme_count"] == 1 and after["best_buzz_calculated"]


async def test_gateway_creation_key_is_idempotent_without_storing_token_in_audit(database):
    from sitg_bot.application.contracts import ApplicationPrincipal, GatewayRequest
    from sitg_bot.application.gateway import ApplicationGateway
    from sitg_bot.storage.models import ApplicationIdempotencyRecord, KnowledgeShareLinkRecord

    owner = await create_player(database, 9161, "gateway-link")
    gateway = ApplicationGateway(database)
    request = GatewayRequest.model_validate(
        {
            "metadata": {
                "channel": "mini_app",
                "client_name": "test",
                "client_version": "1.0.0",
                "idempotency_key": "create-once",
            },
            "operation": {"action": "knowledge.link.create.v1"},
        }
    )
    principal = ApplicationPrincipal(player_id=owner, telegram_user_id=9161)
    first = await gateway.execute(principal, request)
    second = await gateway.execute(principal, request)
    assert first.ok and second.ok and first.data["id"] == second.data["id"]
    async with database.sessions() as session:
        assert len(list(await session.scalars(select(KnowledgeShareLinkRecord)))) == 1
        record = await session.scalar(select(ApplicationIdempotencyRecord))
        assert record.response_payload == {"completed": True}
