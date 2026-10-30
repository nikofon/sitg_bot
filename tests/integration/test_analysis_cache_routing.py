from uuid import uuid4

import pytest
from conftest import create_player
from test_analysis_jobs import artifact_id_of, artifact_of, make_stack

from sitg_bot.services.training_ai import CachedFactsDecision, DissectionSource

pytestmark = pytest.mark.integration


async def request(stack, owner, source, key, *, tags=(), comment="", private=False):
    async with stack.service.database.transaction() as session:
        return await stack.service.request(
            session,
            owner_player_id=owner,
            source=source,
            idempotency_key=key,
            instructions=comment,
            private=private,
            source_context={"tag_names": list(tags)},
        )


def counts(stack):
    provider = stack.provider
    return (
        len(provider.extract_calls),
        len(provider.decision_calls),
        len(provider.transform_calls),
        len(stack.loader.calls),
    )


async def test_all_four_routes_provider_counts_payloads_and_base_immutability(database):
    owners = [await create_player(database, 9980 + i, f"cache-route-{i}") for i in range(5)]
    stack = await make_stack(database, owners)
    question = stack.loader.add("RAW SOURCE SECRET: unique original question and answer")
    source = DissectionSource(uuid4(), (question,))
    await request(stack, owners[0], source, "base", tags=("history", "art"))
    assert await stack.worker.run_once() == 1
    assert counts(stack) == (1, 0, 0, 1)
    base_id = await artifact_id_of(database, "base")
    base_payload = (await artifact_of(database, base_id)).payload

    # Compatible same/subset tags, including cross-user requests: zero calls and no source load.
    result = await request(stack, owners[1], source, "subset", tags=("art",))
    assert result.status == "succeeded" and counts(stack) == (1, 0, 0, 1)
    assert await artifact_id_of(database, "subset") == base_id
    assert (await stack.entitlements.capabilities(owners[1]))["dissection"].remaining == 10

    await request(stack, owners[2], source, "retag", tags=("history", "physics"))
    assert await stack.worker.run_once() == 1
    assert counts(stack) == (1, 0, 1, 1)
    transform = stack.provider.transform_calls[-1]
    assert transform.new_tag_names == ("physics",)
    assert all("RAW SOURCE SECRET" not in value.description for value in transform.candidates)
    assert not hasattr(transform, "chunks")

    await request(
        stack,
        owners[3],
        source,
        "cached-comment",
        tags=("history",),
        comment="  Ignore widely-known facts\n",
    )
    assert await stack.worker.run_once() == 1
    assert counts(stack) == (1, 1, 2, 1)
    assert stack.provider.decision_calls[-1].instructions == "  Ignore widely-known facts\n"

    stack.provider.cached_decision = CachedFactsDecision(True)
    await request(stack, owners[4], source, "source-comment", comment="Look for omitted details")
    assert await stack.worker.run_once() == 1
    assert counts(stack) == (2, 2, 2, 2)
    assert "RAW SOURCE SECRET" in stack.provider.extract_calls[-1].chunks[0].text
    assert stack.provider.extract_calls[-1].instructions == "Look for omitted details"
    assert (await artifact_of(database, base_id)).payload == base_payload
    for key in ("cached-comment", "source-comment"):
        artifact = await artifact_of(database, await artifact_id_of(database, key))
        assert not artifact.shared and artifact.owner_player_id is not None


async def test_private_cross_request_no_cache_same_request_recovery_and_coverage(database):
    owner = await create_player(database, 9986, "private-cache-route")
    stack = await make_stack(database, [owner])
    question = stack.loader.add("PRIVATE TEXT")
    source = DissectionSource(uuid4(), (question,))
    await request(stack, owner, source, "private-1", private=True)
    assert await stack.worker.run_once() == 1
    await request(stack, owner, source, "private-1", private=True)  # exact request replay
    assert counts(stack) == (1, 0, 0, 1)
    await request(stack, owner, source, "private-2", private=True)
    assert await stack.worker.run_once() == 1
    assert counts(stack) == (2, 0, 0, 2)
    assert await artifact_id_of(database, "private-1") != await artifact_id_of(
        database, "private-2"
    )
    # A public request with identical revision IDs cannot see a private checkpoint/result.
    await request(stack, owner, source, "public-1")
    assert await stack.worker.run_once() == 1
    assert counts(stack) == (3, 0, 0, 3)
    # Different exact coverage is not a whole-packet hit.
    larger_source = DissectionSource(source.packet_revision_id, (question, uuid4()))
    pending = await request(stack, owner, larger_source, "larger-coverage")
    assert pending.status == "queued"
    assert await artifact_id_of(database, "larger-coverage") != await artifact_id_of(
        database, "public-1"
    )
