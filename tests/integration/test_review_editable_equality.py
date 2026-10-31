from dataclasses import asdict

import pytest
from conftest import create_player
from sqlalchemy import select
from test_review_regressions import publish
from test_training_knowledge import application

from sitg_bot.domain.knowledge import FactContent, SourceLink
from sitg_bot.services.reliable_delivery import DurableJobWorker
from sitg_bot.services.training_ai import FactCandidate, fact_candidate_id
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.models import KnowledgeFactRecord, KnowledgeImportHashRecord

pytestmark = pytest.mark.integration


def linked_content(origins):
    return FactContent("Linked fact", "Body", tuple(
        SourceLink(f"https://example.org/{index}", position=index, origin=origin)
        for index, origin in enumerate(origins)
    ))


async def create_linked(app, owner, content):
    async with app.database.transaction() as session:
        return await app.service(session).create_fact(
            owner, title=content.title, description=content.description,
            author=content.author, links=content.links, image_ids=content.image_ids,
        )


@pytest.mark.parametrize(("incoming_origins", "existing_origins"), [
    (("manual", "manual"), ("dissection", "dissection")),
    (("dissection", "dissection"), ("manual", "manual")),
    (("manual", "dissection"), ("dissection", "manual")),
])
@pytest.mark.parametrize("state", ["live", "late", "deleted"])
async def test_collection_dedupes_editable_content_without_rewriting_provenance(
    database, incoming_origins, existing_origins, state,
):
    sender = await create_player(database, 9984, "equality-sender")
    recipient = await create_player(database, 9985, "equality-recipient")
    app = KnowledgeApplication(database)
    incoming, existing = linked_content(incoming_origins), linked_content(existing_origins)
    source = await create_linked(app, sender, incoming)
    stored = None
    if state != "late":
        stored = await create_linked(app, recipient, existing)
        if state == "deleted":
            await app.mutate(recipient, "delete", {
                "fact_id": stored.fact_id, "confirm_dissection_origin": False,
            })
    staged = await app.mutate(sender, "grant", {"recipient": str(recipient)})
    detail = await app.reviews.detail(recipient, staged["import_id"])
    bucket = "duplicates" if state == "live" else "new"
    assert len(detail["sections"][bucket]) == 1
    candidate = detail["sections"][bucket][0]
    assert candidate["selected"] is (state != "live")
    assert candidate["links"] == [asdict(link) for link in incoming.links]
    if state == "late":
        stored = await create_linked(app, recipient, existing)
    result = await app.reviews.decide(
        recipient, import_id=staged["import_id"], decision="approve",
        selected_candidate_ids=[candidate["id"]],
    )
    assert result["approved_count"] == 0 and result["skipped_count"] == 1
    assert (await app.facts(recipient))["total"] == (0 if state == "deleted" else 1)
    async with database.sessions() as session:
        for fact, content in ((source, incoming), (stored, existing)):
            record = await session.get(KnowledgeFactRecord, fact.fact_id)
            assert record.links == [asdict(link) for link in content.links]
            assert record.content_fingerprint == content.fingerprint()
    assert await app.reviews.decide(
        recipient, import_id=staged["import_id"], decision="approve",
        selected_candidate_ids=[candidate["id"]],
    ) == result


@pytest.mark.parametrize("legacy_claim", [False, True])
async def test_review_claims_survive_edits_and_keep_historical_retry_hashes(database, legacy_claim):
    sender = await create_player(database, 9986, "claim-sender")
    recipient = await create_player(database, 9987, "claim-recipient")
    app = KnowledgeApplication(database)
    content = linked_content(("dissection", "manual"))
    await create_linked(app, sender, content)
    if legacy_claim:
        async with database.transaction() as session:
            session.add(KnowledgeImportHashRecord(
                recipient_id=recipient, content_fingerprint=content.fingerprint(),
            ))
    first = await app.mutate(sender, "grant", {"recipient": str(recipient)})
    detail = await app.reviews.detail(recipient, first["import_id"])
    result = await app.reviews.decide(
        recipient, import_id=first["import_id"], decision="approve",
        selected_candidate_ids=[value["id"] for value in detail["sections"]["new"]],
    )
    assert result["approved_count"] == (0 if legacy_claim else 1)
    if not legacy_claim:
        fact = (await app.facts(recipient))["items"][0]
        await app.mutate(recipient, "update", {
            "fact_id": fact["id"], "expected_version": 1, "description": "Edited body",
        })
    other_sender = await create_player(database, 9988, "claim-other-sender")
    await create_linked(app, other_sender, linked_content(("manual", "dissection")))
    staged = await app.mutate(other_sender, "grant", {"recipient": str(recipient)})
    detail = await app.reviews.detail(recipient, staged["import_id"])
    result = await app.reviews.decide(
        recipient, import_id=staged["import_id"], decision="approve",
        selected_candidate_ids=[
            value["id"] for section in detail["sections"].values() for value in section
        ],
    )
    assert result["approved_count"] == 0 and result["skipped_count"] == 1
    async with database.sessions() as session:
        claims = set(await session.scalars(
            select(KnowledgeImportHashRecord.content_fingerprint)
            .where(KnowledgeImportHashRecord.recipient_id == recipient)
        ))
        assert content.fingerprint() in claims
        assert linked_content(("manual", "manual")).fingerprint() in claims


@pytest.mark.parametrize("state", ["live", "late", "batch"])
async def test_dissection_classification_rechecks_and_batch_claims_use_editable_equality(
    database, state,
):
    owner = await create_player(database, 9989, "dissection-equality")
    app = await application(database, owner)
    urls = tuple(link.url for link in linked_content(("manual", "manual")).links)
    version = await publish(app, owner, [{
        "name": "Theme", "questions": [{
            "value": value, "text": "Question " + " ".join(urls), "answer": "Answer",
        } for value in (10, 20, 30, 40, 50)],
    }])

    def extract(request):
        return tuple(
            FactCandidate(
                fact_candidate_id(identity, "Linked fact", "Body", urls),
                "Linked fact", "Body", urls, identity,
            )
            for chunk in request.chunks for identity in chunk.question_revision_ids
        )

    app.analysis.provider.fact_response = extract
    request = await app.request_analysis(
        owner, version_id=version, kind="dissection", confirm_exposure=True,
        idempotency_key="editable-equality",
    )
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",),
    )
    assert await worker.run_once() == 1
    if state == "live":
        await create_linked(app, owner, linked_content(("manual", "dissection")))
    detail = await app.reviews.detail(owner, request["import_id"])
    bucket = "duplicates" if state == "live" else "new"
    assert len(detail["sections"][bucket]) == 5
    assert all(value["selected"] is (state != "live") for value in detail["sections"][bucket])
    if state == "late":
        await create_linked(app, owner, linked_content(("manual", "manual")))
    ids = [value["id"] for value in detail["sections"][bucket]]
    result = await app.reviews.decide(
        owner, import_id=request["import_id"], decision="approve", selected_candidate_ids=ids,
    )
    expected = 1 if state == "batch" else 0
    assert result["approved_count"] == expected and result["skipped_count"] == 5 - expected
    facts = (await app.facts(owner))["items"]
    assert len(facts) == 1
    origins = {"live": ["manual", "dissection"], "late": ["manual", "manual"],
               "batch": ["dissection", "dissection"]}[state]
    assert [link["origin"] for link in facts[0]["links"]] == origins
    if state == "batch":
        async with database.sessions() as session:
            fact = await session.get(KnowledgeFactRecord, facts[0]["id"])
            assert fact.applied_candidate_id
            assert fact.content_fingerprint == linked_content(origins).fingerprint()
    after = await app.reviews.detail(owner, request["import_id"])
    assert len(after["sections"]["duplicates"]) == 5
