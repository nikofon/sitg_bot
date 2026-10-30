from uuid import uuid4

import pytest

from sitg_bot.domain.knowledge import FactContent
from sitg_bot.services.knowledge import InMemoryKnowledgeStore, KnowledgeService
from sitg_bot.services.knowledge_review import DEFAULT_TAG_LABELS, category, title_key


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


def test_dedupe_uses_editable_content_and_normalized_name():
    candidate = {"content": {"title": " Some   NAME ", "description": "Body", "author": "Writer"}}
    digest = FactContent("Some NAME", "Body", author="Writer").fingerprint()
    assert category(candidate, set(), set()) == "new"
    assert category(candidate, set(), {title_key("some name")}) == "potential_duplicates"
    assert category(candidate, {digest}, {title_key("some name")}) == "duplicates"
    assert "Chemestry" in DEFAULT_TAG_LABELS and "Chemistry" not in DEFAULT_TAG_LABELS
    assert len(DEFAULT_TAG_LABELS) == 28
