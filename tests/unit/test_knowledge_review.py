from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from sitg_bot.domain.knowledge import FactContent, SourceLink, dissection_candidate_id
from sitg_bot.services.knowledge import InMemoryKnowledgeStore, KnowledgeService
from sitg_bot.services.knowledge_review import (
    DEFAULT_TAG_LABELS,
    category,
    content_of,
    editable_fingerprint,
    title_key,
)
from sitg_bot.services.training_ai import fact_candidate_id


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
    review_id = fact_candidate_id(
        question, content.title, content.description, (content.links[0].url,),
    )
    storage_id = dissection_candidate_id(packet, question, "rev", content)
    candidate = {
        "id": review_id,
        "packet_id": str(packet), "question_id": str(question), "question_revision": "rev",
        "content": asdict(content),
    }
    result = await service.apply_dissection_result(owner, request.request_id, [candidate])
    assert len(result.applied_fact_ids) == 1
    fact = await service.store.get_fact(next(iter(result.applied_fact_ids)))
    assert fact.content == content_of(candidate) == content
    assert fact.applied_candidate_id == storage_id
    assert len(storage_id) == 19 and candidate["id"] == review_id
    assert category(candidate, {editable_fingerprint(fact.content)}, set()) == "duplicates"
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


@pytest.mark.parametrize("field", ["title", "description", "author", "links", "image_ids"])
def test_review_equality_ignores_only_internal_link_origins(field):
    content = FactContent(
        "Linked", "Body", (
            SourceLink("https://example.org/a", "First", 0, "manual"),
            SourceLink("https://example.org/b", "Second", 1, "dissection"),
        ), (uuid4(),), author="Writer",
    )
    other = replace(content, links=tuple(
        replace(link, origin="dissection" if link.origin == "manual" else "manual")
        for link in content.links
    ))
    original_hash = content.fingerprint()
    assert other.fingerprint() != original_hash
    assert editable_fingerprint(content) == editable_fingerprint(other)
    candidate = {"content": {**asdict(other), "image_ids": [str(other.image_ids[0])]}}
    assert category(candidate, {editable_fingerprint(content)}, set()) == "duplicates"
    assert content.fingerprint() == original_hash
    assert [link.origin for link in content.links] == ["manual", "dissection"]
    changes = {
        "title": "Different", "description": "Different", "author": "Different",
        "links": (replace(content.links[0], title="Different"), content.links[1]),
        "image_ids": (uuid4(),),
    }
    assert editable_fingerprint(replace(other, **{field: changes[field]})) != editable_fingerprint(
        content,
    )


def test_dedupe_uses_editable_content_and_normalized_name():
    candidate = {"content": {"title": " Some   NAME ", "description": "Body", "author": "Writer"}}
    digest = FactContent("Some NAME", "Body", author="Writer").fingerprint()
    assert category(candidate, set(), set()) == "new"
    assert category(candidate, set(), {title_key("some name")}) == "potential_duplicates"
    assert category(candidate, {digest}, {title_key("some name")}) == "duplicates"
    assert "Chemestry" in DEFAULT_TAG_LABELS and "Chemistry" not in DEFAULT_TAG_LABELS
    assert len(DEFAULT_TAG_LABELS) == 28
