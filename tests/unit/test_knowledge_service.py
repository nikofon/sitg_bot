from copy import deepcopy
from uuid import UUID

import pytest

from sitg_bot.domain.knowledge import (
    FactContent,
    SourceLink,
    dissection_candidate_id,
)
from sitg_bot.services.knowledge import (
    InMemoryKnowledgeStore,
    KnowledgeService,
    dissection_candidate_from_payload,
)

OWNER = UUID(int=1)
RECIPIENT = UUID(int=2)
STRANGER = UUID(int=3)
PACKET = UUID(int=10)
QUESTION = UUID(int=11)
QUESTION_B = UUID(int=12)


def service() -> KnowledgeService:
    return KnowledgeService(InMemoryKnowledgeStore(), page_size=3)


def payload(question_id: UUID, title: str, description: str = "Extracted detail") -> dict:
    content = FactContent(
        title=title,
        description=description,
        links=(SourceLink(url=f"https://example.test/q/{question_id.int}", title="Source"),),
    )
    return {
        "packet_id": str(PACKET),
        "question_id": str(question_id),
        "question_revision": "rev-1",
        "candidate_id": dissection_candidate_id(PACKET, question_id, "rev-1", content),
        "content": {
            "title": title,
            "description": description,
            "links": [
                {
                    "url": f"https://example.test/q/{question_id.int}",
                    "title": "Source",
                    "position": 0,
                }
            ],
        },
    }


async def test_create_and_view_fact_with_tags():
    knowledge = service()
    view = await knowledge.create_fact(
        OWNER,
        title="Battle of Austerlitz",
        description="1805 battle",
        links=(SourceLink(url="https://example.test/a", title="Source"),),
        tag_names=["Napoleonic Wars", " napoleonic wars "],
    )
    assert view.content.title == "Battle of Austerlitz"
    # Tag names collapse to one canonical tag, not two.
    assert [tag.name for tag in view.tags] == ["napoleonic wars"]
    assert view.editable
    detail = await knowledge.fact_detail(OWNER, view.fact_id)
    assert detail.content_version == 1
    with pytest.raises(LookupError):
        await knowledge.fact_detail(OWNER, UUID(int=99))


async def test_sharing_creates_permanent_independently_editable_copies():
    knowledge = service()
    original = await knowledge.create_fact(
        OWNER, title="Fact", description="D", tag_names=["history"]
    )
    result = await knowledge.grant_collection_access(OWNER, RECIPIENT)
    assert result.copied_count == 1
    copied_id = (await knowledge.list_facts(RECIPIENT)).items[0].fact_id
    assert copied_id != original.fact_id
    copied = await knowledge.fact_detail(RECIPIENT, copied_id)
    assert copied.editable and copied.tags[0].name == "history"
    assert copied.tags[0].tag_id != original.tags[0].tag_id
    await knowledge.update_fact(RECIPIENT, copied_id, expected_version=1, title="Recipient edit")
    await knowledge.update_fact(
        OWNER, original.fact_id, expected_version=1, description="Owner edit"
    )
    assert (await knowledge.fact_detail(OWNER, original.fact_id)).content.title == "Fact"
    assert (await knowledge.fact_detail(RECIPIENT, copied_id)).content.description == "D"
    with pytest.raises(PermissionError):
        await knowledge.fact_detail(RECIPIENT, original.fact_id)
    with pytest.raises(PermissionError, match="permanent"):
        await knowledge.revoke_collection_access(OWNER, RECIPIENT)
    await knowledge.delete_fact(OWNER, original.fact_id)
    assert (await knowledge.fact_detail(RECIPIENT, copied_id)).content.title == "Recipient edit"


async def test_repeat_shares_add_new_facts_without_overwriting_or_resurrecting_copies():
    knowledge = service()
    await knowledge.create_fact(OWNER, title="First", description="D")
    assert (await knowledge.grant_collection_access(OWNER, RECIPIENT)).copied_count == 1
    copied_id = (await knowledge.list_facts(RECIPIENT)).items[0].fact_id
    await knowledge.delete_fact(RECIPIENT, copied_id)
    assert (await knowledge.grant_collection_access(OWNER, RECIPIENT)).copied_count == 0
    await knowledge.create_fact(OWNER, title="Second", description="D")
    assert (await knowledge.grant_collection_access(OWNER, RECIPIENT)).copied_count == 1
    assert [fact.title for fact in (await knowledge.list_facts(RECIPIENT)).items] == ["Second"]
    assert (await knowledge.list_grants(OWNER))[0].copied_count == 2
    assert (await knowledge.shared_collections(RECIPIENT))[0].owner_id == OWNER


async def test_stale_edits_conflict_and_content_changes_invalidate_statistics():
    knowledge = service()
    view = await knowledge.create_fact(OWNER, title="Fact", description="D")
    with pytest.raises(ValueError):
        await knowledge.update_fact(OWNER, view.fact_id, expected_version=7, description="New")
    updated = await knowledge.update_fact(
        OWNER, view.fact_id, expected_version=1, description="New description"
    )
    assert updated.content_version == 2
    assert updated.statistic_status == "stale"
    # Tag-only edits keep the content version and statistic state.
    tagged = await knowledge.update_fact(
        OWNER, view.fact_id, expected_version=2, tag_names=["history"]
    )
    assert tagged.content_version == 2
    assert tagged.statistic_status == "stale"
    assert [tag.name for tag in tagged.tags] == ["history"]


async def test_dissection_delete_and_merge_require_explicit_confirmation():
    knowledge = service()
    manual = await knowledge.create_fact(OWNER, title="Manual", description="D")
    request = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1"
    )
    applied = await knowledge.apply_dissection_result(
        OWNER, request.request_id, [payload(QUESTION, "Extracted")]
    )
    imported_id = next(iter(applied.applied_fact_ids))
    with pytest.raises(ValueError):
        await knowledge.delete_fact(OWNER, imported_id)
    await knowledge.delete_fact(OWNER, imported_id, confirm_dissection_origin=True)
    with pytest.raises(ValueError):
        await knowledge.merge_facts(
            OWNER,
            manual.fact_id,
            manual.fact_id,
            expected_target_version=manual.content_version,
        )
    request_b = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-2"
    )
    applied_b = await knowledge.apply_dissection_result(
        OWNER, request_b.request_id, [payload(QUESTION_B, "Mergable")]
    )
    source_id = next(iter(applied_b.applied_fact_ids))
    merged = await knowledge.merge_facts(
        OWNER,
        source_id,
        manual.fact_id,
        expected_target_version=manual.content_version,
    )
    assert merged.content_version == manual.content_version + 1
    with pytest.raises(LookupError):
        await knowledge.fact_detail(OWNER, source_id)
    # Merging away a manually maintained fact needs explicit confirmation too.
    manual_b = await knowledge.create_fact(OWNER, title="Manual B", description="D")
    with pytest.raises(ValueError):
        await knowledge.merge_facts(
            OWNER,
            manual_b.fact_id,
            manual.fact_id,
            expected_target_version=merged.content_version,
        )


async def test_listing_filters_sorts_and_paginates_with_stable_cursors():
    knowledge = service()
    titles = ["Alpha", "Beta", "Cup", "Delta", "Echo"]
    for title in titles:
        await knowledge.create_fact(
            OWNER,
            title=title,
            description=f"About {title}",
            tag_names=["history"] if title in {"Beta", "Delta"} else ["war"],
        )
    page = await knowledge.list_facts(OWNER)
    assert [item.title for item in page.items] == ["Alpha", "Beta", "Cup"]
    assert page.total == 5
    assert page.next_cursor is not None
    second = await knowledge.list_facts(OWNER, cursor=page.next_cursor)
    assert [item.title for item in second.items] == ["Delta", "Echo"]
    assert second.total == 5
    assert second.next_cursor is None
    reverse = await knowledge.list_facts(OWNER, order="title_desc")
    assert [item.title for item in reverse.items] == ["Echo", "Delta", "Cup"]
    reverse_second = await knowledge.list_facts(
        OWNER, order="title_desc", cursor=reverse.next_cursor
    )
    assert [item.title for item in reverse_second.items] == ["Beta", "Alpha"]
    search = await knowledge.list_facts(OWNER, search="about beta")
    assert [item.title for item in search.items] == ["Beta"]
    tagged = await knowledge.list_facts(OWNER, tag="HISTORY")
    assert {item.title for item in tagged.items} == {"Beta", "Delta"}
    with pytest.raises(ValueError):
        await knowledge.list_facts(OWNER, order="rating_desc")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await knowledge.list_facts(OWNER, cursor="not-a-cursor")


async def test_tags_are_owner_scoped_and_deleting_removes_associations():
    knowledge = service()
    await knowledge.create_tag(OWNER, "history")
    with pytest.raises(ValueError):
        await knowledge.create_tag(OWNER, " HISTORY ")
    # Tag names are scoped per owner: another trainee may own the same name.
    other_tag = await knowledge.create_tag(RECIPIENT, "history")
    assert other_tag.name == "history"
    # Strangers cannot list another collection's tags.
    with pytest.raises(PermissionError):
        await knowledge.list_tags(STRANGER, owner_id=OWNER)
    view = await knowledge.create_fact(OWNER, title="F", description="D", tag_names=["history"])
    tag_id = (await knowledge.list_tags(OWNER))[0].tag_id
    await knowledge.delete_tag(OWNER, tag_id)
    assert await knowledge.list_tags(OWNER) == ()
    detail = await knowledge.fact_detail(OWNER, view.fact_id)
    assert detail.tags == ()
    with pytest.raises(LookupError):
        await knowledge.delete_tag(OWNER, tag_id)


async def test_recipient_lists_copies_without_source_collection_access():
    knowledge = service()
    await knowledge.create_fact(OWNER, title="Shared fact", description="D")
    await knowledge.create_fact(STRANGER, title="Private fact", description="D")
    await knowledge.grant_collection_access(OWNER, RECIPIENT)
    page = await knowledge.list_facts(RECIPIENT)
    assert [item.title for item in page.items] == ["Shared fact"]
    with pytest.raises(PermissionError):
        await knowledge.list_facts(RECIPIENT, owner_id=STRANGER)


@pytest.mark.parametrize("target_start", [15, 17])
async def test_merge_tag_union_remains_editable_or_rejects_without_mutation(target_start):
    knowledge = service()
    source = await knowledge.create_fact(
        OWNER, title="Source", description="Source fact",
        tag_names=[f"tag-{index}" for index in range(17)],
    )
    target = await knowledge.create_fact(
        OWNER, title="Target", description="Target fact",
        tag_names=[f"tag-{index}" for index in range(target_start, target_start + 17)],
    )
    before = deepcopy((
        knowledge.store.facts[source.fact_id], knowledge.store.facts[target.fact_id]
    ))
    if target_start == 17:
        with pytest.raises(ValueError, match="at most 32 tags"):
            await knowledge.merge_facts(
                OWNER, source.fact_id, target.fact_id,
                expected_target_version=target.content_version, confirm_source_delete=True,
            )
        assert (
            knowledge.store.facts[source.fact_id], knowledge.store.facts[target.fact_id]
        ) == before
        return
    merged = await knowledge.merge_facts(
        OWNER, source.fact_id, target.fact_id,
        expected_target_version=target.content_version, confirm_source_delete=True,
    )
    assert len(merged.tags) == 32
    saved = await knowledge.update_fact(
        OWNER, merged.fact_id, expected_version=merged.content_version,
        tag_names=[tag.name for tag in merged.tags],
    )
    assert saved.tags == merged.tags


async def test_old_dissection_identity_does_not_depend_on_the_history_window():
    class BoundedHistory(InMemoryKnowledgeStore):
        async def requests_for_owner(self, owner_id, *, packet_id=None):
            return (await super().requests_for_owner(owner_id, packet_id=packet_id))[-100:]

    knowledge = KnowledgeService(BoundedHistory())
    first = await knowledge.request_dissection(OWNER, packet_id=PACKET, packet_revision="rev-1")
    applied = await knowledge.apply_dissection_result(
        OWNER, first.request_id, [payload(QUESTION, "Old applied fact")],
    )
    fact_id = next(iter(applied.applied_fact_ids))
    await knowledge.delete_fact(OWNER, fact_id, confirm_dissection_origin=True)
    for index in range(101):
        await knowledge.request_dissection(
            OWNER, packet_id=PACKET, packet_revision="rev-1", comment=f"Revision {index}",
        )
    assert first.request_id not in {
        row.id for row in await knowledge.store.requests_for_owner(OWNER, packet_id=PACKET)
    }
    retry = await knowledge.request_dissection(OWNER, packet_id=PACKET, packet_revision="rev-1")
    assert retry == applied
    assert len(knowledge.store.requests) == 102
    assert knowledge.store.facts[fact_id].deleted_at is not None


async def test_dissection_requests_are_idempotent_and_comments_create_revisions():
    knowledge = service()
    base = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1"
    )
    again = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1"
    )
    assert base.request_id == again.request_id
    assert base.base and base.status == "pending"
    commented = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1", comment="more naval history"
    )
    assert commented.request_id != base.request_id
    assert not commented.base and commented.comment == "more naval history"
    with pytest.raises(ValueError):
        await knowledge.request_dissection(
            OWNER, packet_id=PACKET, packet_revision="rev-1", instructions="x" * 4001
        )
    with pytest.raises(ValueError):
        await knowledge.request_dissection(OWNER, packet_id=PACKET, packet_revision="  ")
    # Another trainee may request their own dissection of the same packet.
    foreign = await knowledge.request_dissection(
        RECIPIENT, packet_id=PACKET, packet_revision="rev-1"
    )
    assert foreign.request_id != base.request_id
    with pytest.raises(LookupError):
        await knowledge.dissection_request(OWNER, foreign.request_id)


async def test_dissection_application_is_idempotent_and_records_provenance():
    knowledge = service()
    request = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1"
    )
    payloads = [payload(QUESTION, "Extracted fact", "From question one")]
    result = await knowledge.apply_dissection_result(
        OWNER, request.request_id, payloads
    )
    assert result.status == "succeeded"
    assert len(result.applied_fact_ids) == 1
    fact_id = next(iter(result.applied_fact_ids))
    detail = await knowledge.fact_detail(OWNER, fact_id)
    assert detail.provenance.origin == "dissection"
    assert detail.provenance.packet_id == PACKET
    assert detail.provenance.question_id == QUESTION
    assert detail.provenance.question_revision == "rev-1"
    assert detail.provenance.request_id == request.request_id
    # Retrying the same request never duplicates facts.
    retried = await knowledge.apply_dissection_result(
        OWNER, request.request_id, payloads
    )
    assert retried.request_id == request.request_id
    assert retried.applied_fact_ids == result.applied_fact_ids
    page = await knowledge.list_facts(OWNER)
    assert page.total == 1


async def test_manual_edits_survive_reapplication_from_a_new_request():
    knowledge = service()
    first = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1"
    )
    applied = await knowledge.apply_dissection_result(
        OWNER, first.request_id, [payload(QUESTION, "Extracted", "Detail")]
    )
    fact_id = next(iter(applied.applied_fact_ids))
    await knowledge.update_fact(
        OWNER, fact_id, expected_version=1, description="Manually improved"
    )
    revision = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1", comment="retry same revision"
    )
    reapplied = await knowledge.apply_dissection_result(
        OWNER, revision.request_id, [payload(QUESTION, "Extracted", "Detail")]
    )
    assert reapplied.status == "succeeded"
    assert not reapplied.applied_fact_ids
    detail = await knowledge.fact_detail(OWNER, fact_id)
    assert detail.content.description == "Manually improved"
    assert detail.content_version == 2
    page = await knowledge.list_facts(OWNER)
    assert page.total == 1


async def test_exclusions_and_invalid_payloads_produce_partial_results():
    knowledge = service()
    request = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1"
    )
    excluded_payload = payload(QUESTION, "Declined fact")
    broken = {"packet_id": str(PACKET), "question_id": "not-a-uuid"}
    wrong_packet = payload(QUESTION, "Other packet fact")
    wrong_packet["packet_id"] = str(UUID(int=77))
    result = await knowledge.apply_dissection_result(
        OWNER,
        request.request_id,
        [excluded_payload, broken, wrong_packet],
        excluded=[excluded_payload["candidate_id"]],
    )
    assert result.status == "partially_succeeded"
    assert result.applied_fact_ids == frozenset()
    assert len(result.failed_candidate_ids) == 2
    assert "<unavailable>" in result.failed_candidate_ids
    # A retry of the failed work with a fixed payload succeeds without
    # reapplying anything the earlier run already applied.
    fixed_request = await knowledge.request_dissection(
        OWNER, packet_id=PACKET, packet_revision="rev-1", comment="retry"
    )
    fixed = await knowledge.apply_dissection_result(
        OWNER, fixed_request.request_id, [payload(QUESTION, "Fixed fact")]
    )
    assert fixed.status == "succeeded"
    assert len(fixed.applied_fact_ids) == 1


async def test_candidate_payloads_validate_provenance_and_links():
    good = payload(QUESTION, "Valid")
    candidate = dissection_candidate_from_payload(good)
    assert candidate.question_revision == "rev-1"
    with_bad_url = {
        "packet_id": str(PACKET),
        "question_id": str(QUESTION),
        "question_revision": "rev-1",
        "content": {
            "title": "Invalid",
            "description": "D",
            "links": [{"url": "javascript:alert(1)"}],
        },
    }
    with pytest.raises(ValueError):
        dissection_candidate_from_payload(with_bad_url)
    with pytest.raises(ValueError):
        dissection_candidate_from_payload({"packet_id": str(PACKET)})


async def test_statistic_snapshots_are_immutable_history():
    knowledge = service()
    view = await knowledge.create_fact(OWNER, title="Fact", description="D")
    first = await knowledge.record_statistics(OWNER, view.fact_id)
    await knowledge.update_fact(OWNER, view.fact_id, expected_version=1, description="D2")
    with pytest.raises(PermissionError):
        await knowledge.record_statistics(RECIPIENT, view.fact_id)
    second = await knowledge.record_statistics(OWNER, view.fact_id)
    history = await knowledge.statistic_snapshots(OWNER, view.fact_id)
    assert [snapshot.snapshot_id for snapshot in history] == [
        first.snapshot_id,
        second.snapshot_id,
    ]
    assert [snapshot.content_version for snapshot in history] == [1, 2]
    detail = await knowledge.fact_detail(OWNER, view.fact_id)
    assert detail.statistic_status == "fresh"
