import asyncio
import json
from uuid import UUID, uuid4

import pytest
from conftest import create_player
from sqlalchemy import func, select
from test_training_knowledge import application

from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.knowledge_review import DEFAULT_TAG_LABELS
from sitg_bot.services.reliable_delivery import DurableJobWorker
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.models import (
    KnowledgeImportRecord,
    PacketQuestionRecord,
    PlayerExposureClaimRecord,
    ThemeRevisionRecord,
    TrainingAnalysisRequestRecord,
    TrainingUsagePeriodRecord,
)

pytestmark = pytest.mark.integration


async def collection(app, owner, recipient):
    staged = await app.mutate(owner, "grant", {"recipient": str(recipient)})
    return staged["import_id"]


def selected(detail):
    return [
        value["id"]
        for values in detail["sections"].values()
        for value in values
        if value["selected"]
    ]


async def test_author_search_one_time_deletable_defaults(database):
    owner = await create_player(database, 9901, "author-owner")
    app = KnowledgeApplication(database)
    first = await app.tags(owner)
    assert {item["label"] for item in first["items"]} == set(DEFAULT_TAG_LABELS)
    removed = next(item for item in first["items"] if item["label"] == "Chemestry")
    await app.mutate(owner, "tag_delete", {"tag_id": removed["tag_id"]})
    app = KnowledgeApplication(database)
    await app.mutate(owner, "create", {"title": "A", "description": "B", "author": "Anne %_"})
    assert "Chemestry" not in {item["label"] for item in (await app.tags(owner))["items"]}
    assert (await app.facts(owner, author="%_"))["total"] == 1
    assert (await app.facts(owner, search="ANNE"))["total"] == 1
    fact = (await app.facts(owner))["items"][0]
    assert fact["author"] == "Anne %_" and fact["packet_name"] is None
    updated = await app.mutate(
        owner,
        "update",
        {
            "fact_id": fact["id"],
            "expected_version": 1,
            "description": "changed",
        },
    )
    assert updated["author"] == "Anne %_"
    cleared = await app.mutate(
        owner,
        "update",
        {
            "fact_id": fact["id"],
            "expected_version": 2,
            "author": "",
        },
    )
    assert cleared["author"] == ""


async def test_review_dedupe_snapshot_decisions_and_no_resurrection(database):
    owner = await create_player(database, 9902, "sender")
    recipient = await create_player(database, 9903, "recipient")
    other = await create_player(database, 9904, "outsider")
    app = KnowledgeApplication(database)
    source = []
    for title, description in (("Exact", "same"), ("  Possible Name ", "new"), ("New", "fresh")):
        source.append(
            await app.mutate(
                owner,
                "create",
                {
                    "title": title,
                    "description": description,
                    "author": "Real author",
                },
            )
        )
    for title, description in (("Exact", "same"), ("Possible Name", "different")):
        await app.mutate(
            recipient,
            "create",
            {
                "title": title,
                "description": description,
                "author": "Real author",
            },
        )
    import_id = await collection(app, owner, recipient)
    assert (await app.facts(recipient))["total"] == 2  # staged, not applied
    detail = await app.reviews.detail(recipient, import_id)
    assert {key: len(values) for key, values in detail["sections"].items()} == {
        "new": 1,
        "potential_duplicates": 1,
        "duplicates": 1,
    }
    assert detail["packet_name"] is None and detail["author"] == "sender"
    assert detail["sections"]["duplicates"][0]["selected"] is False
    with pytest.raises(LookupError):
        await app.reviews.detail(other, import_id)
    with pytest.raises(ValueError):
        await app.reviews.decide(
            recipient,
            import_id=import_id,
            decision="approve",
            selected_candidate_ids=[str(uuid4())],
        )
    await app.mutate(
        owner,
        "update",
        {
            "fact_id": source[2]["id"],
            "expected_version": 1,
            "description": "source changed",
        },
    )
    result = await app.reviews.decide(
        recipient, import_id=import_id, decision="approve", selected_candidate_ids=selected(detail)
    )
    assert result["approved_count"] == 2
    facts = (await app.facts(recipient))["items"]
    new = next(item for item in facts if item["title"] == "New")
    assert new["description"] == "fresh" and new["author"] == "Real author"
    assert (
        await app.reviews.decide(
            recipient,
            import_id=import_id,
            decision="approve",
            selected_candidate_ids=selected(detail),
        )
        == result
    )
    with pytest.raises(StaleWriteError):
        await app.reviews.decide(recipient, import_id=import_id, decision="reject")
    await app.mutate(
        recipient, "delete", {"fact_id": new["id"], "confirm_dissection_origin": False}
    )
    repeat = await collection(app, owner, recipient)
    repeat_detail = await app.reviews.detail(recipient, repeat)
    approved = await app.reviews.decide(
        recipient,
        import_id=repeat,
        decision="approve",
        selected_candidate_ids=selected(repeat_detail),
    )
    assert approved["approved_count"] == 0  # source claims survive changed source and deletion


async def test_concurrent_reviews_recheck_current_library(database):
    owner = await create_player(database, 9905, "concurrent-sender")
    recipient = await create_player(database, 9906, "concurrent-recipient")
    app = KnowledgeApplication(database)
    await app.mutate(owner, "create", {"title": "Only once", "description": "body"})
    imports = [await collection(app, owner, recipient) for _ in range(2)]
    choices = [selected(await app.reviews.detail(recipient, identity)) for identity in imports]
    results = await asyncio.gather(
        *[
            app.reviews.decide(
                recipient, import_id=identity, decision="approve", selected_candidate_ids=ids
            )
            for identity, ids in zip(imports, choices, strict=True)
        ]
    )
    assert sum(result["approved_count"] for result in results) == 1
    assert (await app.facts(recipient))["total"] == 1


async def test_selected_themes_preview_quota_and_durable_review(database):
    owner = await create_player(database, 9907, "theme-owner")
    app = await application(database, owner, limit=1)
    payload = {
        "name": "Selected packet",
        "themes": [
            {
                "name": name,
                "authors": ["Theme author"],
                "commentary": "Theme note",
                "questions": [
                    {"value": value, "text": f"{name} secret {value}", "answer": "Answer"}
                    for value in (10, 20, 30, 40, 50)
                ],
            }
            for name in ("First", "Second")
        ],
    }
    draft = (await app.packets.import_upload(owner, json.dumps(payload).encode(), "x.json"))[
        "drafts"
    ][0]
    version = UUID(
        (
            await app.packets.decide_draft(
                owner, UUID(draft["draft_id"]), expected_version=draft["version"], publish=True
            )
        )["version_id"]
    )
    listed = await app.themes.list(owner, version)
    assert listed["quota"]["remaining"] == 1 and listed["quota"]["reset_at"]
    assert all(set(value) == {"theme_revision_id", "name"} for value in listed["themes"])
    theme = listed["themes"][0]["theme_revision_id"]
    second = listed["themes"][1]["theme_revision_id"]
    confirmation = await app.themes.preview(owner, version_id=version, theme_revision_id=theme)
    assert confirmation == {"confirmation_required": True, "fresh_unit_count": 1}
    async with database.transaction() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PlayerExposureClaimRecord)
                .where(PlayerExposureClaimRecord.player_id == owner)
            )
            == 0
        )
    preview = await app.themes.preview(
        owner, version_id=version, theme_revision_id=theme, confirm_exposure=True
    )
    async with database.transaction() as session:
        exposed = set(
            await session.scalars(
                select(PlayerExposureClaimRecord.claim_id).where(
                    PlayerExposureClaimRecord.player_id == owner,
                    PlayerExposureClaimRecord.claim_namespace == "theme",
                )
            )
        )
        selected_logical = (await session.get(ThemeRevisionRecord, theme)).theme_id
        unselected_logical = (await session.get(ThemeRevisionRecord, second)).theme_id
        assert selected_logical in exposed and unselected_logical not in exposed
    assert preview["theme"]["author"] == "Theme author"
    assert len(preview["theme"]["questions"]) == 5
    with pytest.raises(ValueError):
        await app.request_analysis(
            owner,
            version_id=version,
            kind="dissection",
            theme_revision_ids=[uuid4()],
            idempotency_key="invalid-theme",
        )
    request = await app.request_analysis(
        owner,
        version_id=version,
        kind="dissection",
        theme_revision_ids=[theme],
        confirm_exposure=True,
        idempotency_key="selected-only",
    )
    async with database.transaction() as session:
        row = await session.get(TrainingAnalysisRequestRecord, request["request_id"])
        assert row.source["quota_units"] == 1
        placements = set(
            await session.scalars(
                select(PacketQuestionRecord.question_revision_id).where(
                    PacketQuestionRecord.theme_revision_id == theme
                )
            )
        )
        assert {UUID(value) for value in row.source["question_revision_ids"]} == placements
    worker = DurableJobWorker(
        app.analysis.jobs, {"training.analysis": app.analysis_job}, kinds=("training.analysis",)
    )
    assert await worker.run_once() == 1
    assert (await app.facts(owner))["total"] == 0
    detail = await app.reviews.detail(owner, request["import_id"])
    assert detail["status"] == "pending" and detail["fact_count"] > 0
    assert all(value["author"] == "Theme author" for value in detail["sections"]["new"])
    approved = await app.reviews.decide(
        owner,
        import_id=request["import_id"],
        decision="approve",
        selected_candidate_ids=selected(detail)[:1],
    )
    assert approved["approved_count"] == 1 and (await app.facts(owner))["total"] == 1
    from sitg_bot.services.analysis_jobs import AnalysisQuotaExceededError

    with pytest.raises(AnalysisQuotaExceededError):
        await app.request_analysis(
            owner,
            version_id=version,
            kind="dissection",
            theme_revision_ids=[second],
            confirm_exposure=True,
            idempotency_key="over-budget",
        )
    async with database.transaction() as session:
        usage = await session.scalar(
            select(TrainingUsagePeriodRecord).where(
                TrainingUsagePeriodRecord.player_id == owner,
                TrainingUsagePeriodRecord.feature == "dissection",
            )
        )
        assert usage.settled == 1
        stored = await session.get(KnowledgeImportRecord, request["import_id"])
        assert stored.status == "approved"
        assert await session.scalar(select(func.count()).select_from(PlayerExposureClaimRecord)) > 0
