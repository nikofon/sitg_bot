import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from conftest import create_player
from sqlalchemy import event, func, inspect, select
from test_training_knowledge import application

from sitg_bot.domain.knowledge import dissection_candidate_id
from sitg_bot.services.knowledge import _encode_cursor
from sitg_bot.services.knowledge_review import content_of
from sitg_bot.services.reliable_delivery import DurableJobWorker
from sitg_bot.services.training import TrainingService
from sitg_bot.services.training_ai import FactCandidate, fact_candidate_id
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.models import (
    AuthorRecord,
    KnowledgeDissectionRecord,
    KnowledgeFactRecord,
    KnowledgeImageRecord,
    KnowledgeImportHashRecord,
    KnowledgeImportRecord,
    TrainingEventRecord,
    TrainingMemberRecord,
    TrainingSessionRecord,
)

pytestmark = pytest.mark.integration


async def publish(app, owner, themes):
    draft = (await app.packets.import_upload(
        owner, json.dumps({"name": "Private packet", "themes": themes}).encode(), "packet.json",
    ))["drafts"][0]
    result = await app.packets.decide_draft(
        owner, UUID(draft["draft_id"]), expected_version=draft["version"], publish=True,
    )
    return UUID(result["version_id"])


async def test_original_field_citations_survive_cache_staging_and_approval(database):
    owner = await create_player(database, 9971, "linked-dissection")
    app = await application(database, owner)
    links = tuple(f"https://example.org/{name}" for name in ("question", "answer", "note", "a", "b"))
    version = await publish(app, owner, [{
        "name": "Theme", "authors": ["Private author"],
        "questions": [{
            "value": value, "text": "Question " + links[0], "answer": links[1],
            "commentary": links[2], "source": "\n".join(links[3:]),
        } for value in (10, 20, 30, 40, 50)],
    }])

    def extract(request):
        return tuple(
            FactCandidate(
                fact_candidate_id(identity, str(identity), "Body", links),
                str(identity), "Body", links, identity,
            )
            for chunk in request.chunks for identity in chunk.question_revision_ids
        )

    app.analysis.provider.fact_response = extract
    request = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="linked",
    )
    working = await app.reviews.detail(owner, request["import_id"])
    assert working["status"] == "working"
    for decision in ("approve", "reject"):
        with pytest.raises(ValueError, match="not ready"):
            await app.reviews.decide(
                owner, import_id=request["import_id"], decision=decision, selected_candidate_ids=[],
            )
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    assert await worker.run_once() == 1
    detail = await app.reviews.detail(owner, request["import_id"])
    assert len(detail["sections"]["new"]) == 5
    for candidate in detail["sections"]["new"]:
        assert tuple(link["url"] for link in candidate["links"]) == links
        assert {link["origin"] for link in candidate["links"]} == {"dissection"}
    ids = [value["id"] for value in detail["sections"]["new"]]
    result = await app.reviews.decide(
        owner, import_id=request["import_id"], decision="approve", selected_candidate_ids=ids,
    )
    assert result["approved_count"] == 5
    async with database.sessions() as session:
        review = await session.get(KnowledgeImportRecord, request["import_id"])
        facts = list(await session.scalars(select(KnowledgeFactRecord).where(
            KnowledgeFactRecord.owner_id == owner,
        )))
        assert len(facts) == 5
        assert {value["id"] for value in review.candidates} == set(ids)
        assert review.decision["selected_candidate_ids"] == sorted(ids)
        storage_ids = {
            dissection_candidate_id(
                UUID(value["packet_id"]), UUID(value["question_id"]),
                value["question_revision"], content_of(value),
            ) for value in review.candidates
        }
        assert {fact.applied_candidate_id for fact in facts} == storage_ids
        assert all(len(identity) == 19 for identity in storage_ids)
        hashes = {content_of(value).fingerprint() for value in review.candidates}
        assert {fact.content_fingerprint for fact in facts} == hashes
        claims = set(await session.scalars(
            select(KnowledgeImportHashRecord.content_fingerprint).where(
                KnowledgeImportHashRecord.recipient_id == owner,
            ),
        ))
        assert claims == hashes
    after = await app.reviews.detail(owner, request["import_id"])
    assert {value["id"] for value in after["sections"]["duplicates"]} == set(ids)
    assert await app.reviews.decide(
        owner, import_id=request["import_id"], decision="approve", selected_candidate_ids=ids,
    ) == result


async def test_invalid_application_rolls_back_facts_claims_and_decision(database):
    owner = await create_player(database, 9977, "atomic-dissection")
    app = await application(database, owner)
    version = await publish(app, owner, [{
        "name": "Theme", "questions": [{
            "value": value, "text": "Question", "answer": "Answer",
        } for value in (10, 20, 30, 40, 50)],
    }])
    request = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="atomic",
    )
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    assert await worker.run_once() == 1
    async with database.transaction() as session:
        review = await session.get(KnowledgeImportRecord, request["import_id"])
        original = review.candidates
        review.candidates = [
            *original[:-1], {**original[-1], "packet_id": str(uuid4())},
        ]
        ids = [value["id"] for value in review.candidates]
        dissection = await session.get(KnowledgeDissectionRecord, request["dissection_id"])
        status = dissection.status
    with pytest.raises(ValueError, match="could not be applied"):
        await app.reviews.decide(
            owner, import_id=request["import_id"], decision="approve", selected_candidate_ids=ids,
        )
    async with database.transaction() as session:
        review = await session.get(KnowledgeImportRecord, request["import_id"])
        assert review.status == "pending" and review.decision is None
        assert {value["id"] for value in review.candidates} == set(ids)
        dissection = await session.get(KnowledgeDissectionRecord, request["dissection_id"])
        assert dissection.status == status and dissection.application is None
        assert await session.scalar(select(func.count()).select_from(KnowledgeFactRecord)
                                    .where(KnowledgeFactRecord.owner_id == owner)) == 0
        assert await session.scalar(select(func.count()).select_from(KnowledgeImportHashRecord)
                                    .where(KnowledgeImportHashRecord.recipient_id == owner)) == 0
        review.candidates = original
    result = await app.reviews.decide(
        owner, import_id=request["import_id"], decision="approve", selected_candidate_ids=ids,
    )
    assert result["approved_count"] == 5
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeFactRecord)
                                    .where(KnowledgeFactRecord.owner_id == owner)) == 5
        assert await session.scalar(select(func.count()).select_from(KnowledgeImportHashRecord)
                                    .where(KnowledgeImportHashRecord.recipient_id == owner)) == 5


async def test_all_imports_remain_reachable_with_tied_timestamps_and_owner_scoping(database):
    owner = await create_player(database, 9978, "paged-imports")
    other = await create_player(database, 9979, "other-paged-imports")
    app = KnowledgeApplication(database)
    created_at = datetime.now(UTC)
    async with database.transaction() as session:
        session.add_all([
            KnowledgeImportRecord(
                id=UUID(int=index), owner_id=owner, creator_id=other, type="collection",
                status="pending" if index == 1 else "approved", candidates=[],
                created_at=created_at,
            ) for index in range(1, 102)
        ])
        session.add(KnowledgeImportRecord(
            id=UUID(int=999), owner_id=other, creator_id=owner, type="collection",
            status="pending", candidates=[], created_at=created_at,
        ))
    first = await app.reviews.list(owner)
    assert len(first["items"]) == 100 and first["next_cursor"]
    assert [value["id"] for value in first["items"]] == [
        UUID(int=index) for index in range(101, 1, -1)
    ]
    second = await app.reviews.list(owner, cursor=first["next_cursor"])
    assert [value["id"] for value in second["items"]] == [UUID(int=1)]
    assert second["next_cursor"] is None and second["items"][0]["status"] == "pending"
    assert (await app.reviews.detail(owner, UUID(int=1)))["status"] == "pending"
    decided = await app.reviews.decide(owner, import_id=UUID(int=1), decision="reject")
    assert decided["status"] == "rejected"
    seen, cursor = [], None
    while True:
        page = await app.reviews.list(owner, cursor=cursor, limit=17)
        assert len(page["items"]) <= 17
        seen.extend(value["id"] for value in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [value["id"] for value in first["items"]] + [UUID(int=1)]
    assert [value["id"] for value in (await app.reviews.list(other))["items"]] == [UUID(int=999)]
    for limit in (0, 101):
        with pytest.raises(ValueError, match="page size"):
            await app.reviews.list(owner, limit=limit)
    for cursor in ("invalid", _encode_cursor(("2026-01-01T00:00:00", str(owner)))):
        with pytest.raises(ValueError):
            await app.reviews.list(owner, cursor=cursor)


async def test_review_images_load_metadata_without_binary_data(database):
    owner = await create_player(database, 9972, "image-review-sender")
    recipient = await create_player(database, 9973, "image-review-recipient")
    app = KnowledgeApplication(database)
    image_id = uuid4()
    async with database.transaction() as session:
        session.add(KnowledgeImageRecord(
            id=image_id, owner_id=owner, content_type="image/png", data=b"x" * (2 * 1024 * 1024),
        ))
    await app.mutate(owner, "create", {
        "title": "Illustrated", "description": "Body", "image_ids": [image_id],
    })
    staged = await app.mutate(owner, "grant", {"recipient": str(recipient)})
    loaded = []

    def observe(image, context):
        loaded.append("data" in inspect(image).unloaded)

    event.listen(KnowledgeImageRecord, "load", observe)
    try:
        detail = await app.reviews.detail(recipient, staged["import_id"])
    finally:
        event.remove(KnowledgeImageRecord, "load", observe)
    assert loaded == [True]
    assert detail["sections"]["new"][0]["images"] == [{
        "id": image_id, "url": f"/api/miniapp/knowledge/images/{image_id}", "content_type": "image/png",
    }]


@pytest.mark.parametrize("field", ["title", "description", "author"])
async def test_unicode_search_uses_matching_database_normalization(database, field):
    owner = await create_player(database, 9974, "unicode-search")
    app = KnowledgeApplication(database)
    values = {"title": "Title", "description": "Body", "author": "Writer", field: "Weiß %_"}
    fact = await app.mutate(owner, "create", values)
    for needle in ("Weiß", "WEIß", "%_"):
        found = await app.facts(owner, search=needle)
        assert [value["id"] for value in found["items"]] == [fact["id"]]
        if field == "author":
            found = await app.facts(owner, author=needle)
            assert [value["id"] for value in found["items"]] == [fact["id"]]
    assert (await app.facts(owner, search="absent"))["total"] == 0


async def test_private_training_attribution_in_announcements_completion_and_recovery(database):
    owner = await create_player(database, 9975, "private-training-authors")
    other = await create_player(database, 9976, "private-training-outsider")
    app = KnowledgeApplication(database)
    async with database.sessions() as session:
        baseline = await session.scalar(select(func.count()).select_from(AuthorRecord))
    version = await publish(app, owner, [{
        "name": "Theme", "authors": ["Private theme author"],
        "questions": [{
            "value": value, "text": "Question", "answer": "Answer",
            "authors": ["Private question author"] if value == 10 else [],
        } for value in (10, 20, 30, 40, 50)],
    }])
    service = TrainingService(database)
    with pytest.raises((LookupError, PermissionError)):
        await service.start(other, version, confirm=True)
    state = (await service.start(owner, version, confirm=True, now=datetime.now(UTC)))["session"]
    run = UUID(state["id"])
    for _ in range(150):
        async with database.sessions() as session:
            record = await session.get(TrainingSessionRecord, run)
            if record.status == "completed":
                break
            deadline = record.deadline
        await TrainingService(database).tick(run, now=deadline)
    else:
        raise AssertionError("Training did not complete")
    async with database.transaction() as session:
        events = list(await session.scalars(select(TrainingEventRecord).where(
            TrainingEventRecord.session_id == run,
        ).order_by(TrainingEventRecord.sequence)))
        assert next(row for row in events if row.kind == "theme_started").parameters[
            "author"
        ] == "Private theme author"
        completions = [row for row in events if row.kind == "question_completed"]
        assert [row.parameters["author"] for row in completions] == [
            "Private question author", *(["Private theme author"] * 4),
        ]
        record = await session.get(TrainingSessionRecord, run)
        member = await session.get(TrainingMemberRecord, (run, owner))
        await service._recover_presentation(session, record, member)
        await session.flush()
        recovered = await session.scalar(select(TrainingEventRecord).where(
            TrainingEventRecord.session_id == run,
        ).order_by(TrainingEventRecord.sequence.desc()).limit(1))
        assert recovered.kind == "question_completed"
        assert recovered.parameters["author"] == "Private theme author"
        assert await session.scalar(select(func.count()).select_from(AuthorRecord)) == baseline
