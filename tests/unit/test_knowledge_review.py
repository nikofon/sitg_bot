from dataclasses import asdict
from uuid import uuid4

import pytest

from sitg_bot.domain.knowledge import FactContent, SourceLink
from sitg_bot.services.knowledge import InMemoryKnowledgeStore, KnowledgeService
from sitg_bot.services.knowledge_review import DEFAULT_TAG_LABELS, category, content_of, title_key


async def test_author_independent_edit_search_and_validation():
    owner = uuid4()
    service = KnowledgeService(InMemoryKnowledgeStore())
    fact = await service.create_fact(owner, title="First", description="Body", author="A. Writer")
    assert (await service.list_facts(owner, author="writer")).total == 1
    assert (await service.list_facts(owner, search="WRITER")).total == 1
    edited = await service.update_fact(owner, fact.fact_id, expected_version=1, description="New")
    assert edited.content.author == "A. Writer"
    cleared = await service.update_fact(owner, fact.fact_id, expected_version=2, author="")
    assert cleared.content.author == "" and cleared.content_version == 3
    with pytest.raises(ValueError):
        FactContent("a", "b", author="x" * 301)


@pytest.mark.parametrize("origin", ["manual", "dissection"])
async def test_linked_candidates_preserve_content_and_identity_through_application(origin):
    owner, packet, question = uuid4(), uuid4(), uuid4()
    service = KnowledgeService(InMemoryKnowledgeStore())
    request = await service.request_dissection(owner, packet_id=packet, packet_revision="rev")
    content = FactContent(
        "Linked fact", "Body", (SourceLink("https://example.org/a", origin=origin),),
        author="Writer",
    )
    candidate = {
        "candidate_id": "retained-identity",
        "packet_id": str(packet), "question_id": str(question), "question_revision": "rev",
        "content": asdict(content),
    }
    result = await service.apply_dissection_result(owner, request.request_id, [candidate])
    assert len(result.applied_fact_ids) == 1
    fact = await service.store.get_fact(next(iter(result.applied_fact_ids)))
    assert fact.content == content_of(candidate) == content
    assert fact.applied_candidate_id == "retained-identity"
    assert category(candidate, {fact.content.fingerprint()}, set()) == "duplicates"
    assert await service.apply_dissection_result(owner, request.request_id, [candidate]) == result


@pytest.mark.parametrize("field", ["title", "description", "author"])
async def test_unicode_search_matches_lowercased_content(field):
    owner = uuid4()
    service = KnowledgeService(InMemoryKnowledgeStore())
    values = {"title": "Title", "description": "Body", "author": "Writer", field: "Weiß %_"}
    await service.create_fact(owner, **values)
    for needle in ("Weiß", "WEIß", "%_"):
        assert (await service.list_facts(owner, search=needle)).total == 1
        if field == "author":
            assert (await service.list_facts(owner, author=needle)).total == 1
    assert (await service.list_facts(owner, search="absent")).total == 0


def test_dedupe_uses_editable_content_and_normalized_name():
    candidate = {"content": {"title": " Some   NAME ", "description": "Body", "author": "Writer"}}
    digest = FactContent("Some NAME", "Body", author="Writer").fingerprint()
    assert category(candidate, set(), set()) == "new"
    assert category(candidate, set(), {title_key("some name")}) == "potential_duplicates"
    assert category(candidate, {digest}, {title_key("some name")}) == "duplicates"
    assert "Chemestry" in DEFAULT_TAG_LABELS and "Chemistry" not in DEFAULT_TAG_LABELS
    assert len(DEFAULT_TAG_LABELS) == 28
