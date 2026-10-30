from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sitg_bot.services.analysis_cache import candidate_payload, run_cached_dissection
from sitg_bot.services.training_ai import (
    AnalysisValidationError,
    CachedFactsDecision,
    FactCandidate,
    FakeAnalysisProvider,
    ProviderSettings,
    fact_candidate_id,
)


class RoutingHarness:
    def __init__(self, monkeypatch, *, tags=("history",)):
        self.settings = ProviderSettings("fake", "fixture")
        self.provider = FakeAnalysisProvider()
        self.question_id = uuid4()
        self.candidate = FactCandidate(
            fact_candidate_id(self.question_id, "Known", "Cached fact", ()),
            "Known",
            "Cached fact",
            (),
            self.question_id,
            tags,
        )
        self.payload = {
            "candidates": [candidate_payload(self.candidate)],
            "cache_version": 2,
            "tag_coverage": list(tags),
        }
        self.artifact = SimpleNamespace(checkpoint={}, source_fingerprint="exact-selected-source")
        self.database = SimpleNamespace(transaction=self.transaction)
        self.terminal = None
        self.descriptor = {
            "private": False,
            "question_revision_ids": [str(self.question_id)],
            "language": "en",
            "tag_names": list(tags),
        }

        async def public_base(*args, **kwargs):
            return SimpleNamespace(payload=self.payload)

        monkeypatch.setattr("sitg_bot.services.analysis_cache.public_base", public_base)

    @asynccontextmanager
    async def transaction(self):
        async def get(*args):
            return self.artifact

        yield SimpleNamespace(get=get)

    async def _provider_call(self, operation, request):
        return await operation(request)

    async def _write_checkpoint(self, artifact_id, lease_token, checkpoint):
        self.artifact.checkpoint = dict(checkpoint)

    async def _write_terminal(self, artifact_id, lease_token, **kwargs):
        self.terminal = kwargs

    async def run(self, commentary=""):
        return await run_cached_dissection(self, uuid4(), uuid4(), commentary, self.descriptor)


async def test_compatible_bare_facts_make_zero_calls_and_subset_tags(monkeypatch):
    harness = RoutingHarness(monkeypatch, tags=("history", "art"))
    harness.descriptor["tag_names"] = ["art"]
    assert await harness.run()
    assert not harness.provider.extract_calls
    assert not harness.provider.decision_calls
    assert not harness.provider.transform_calls
    assert harness.terminal["payload"]["candidates"][0]["tags"] == ["art"]
    assert harness.payload["candidates"][0]["tags"] == ["history", "art"]  # immutable base


async def test_new_tags_use_cached_payload_not_packet_source(monkeypatch):
    harness = RoutingHarness(monkeypatch)
    harness.descriptor["tag_names"] = ["history", "art"]
    assert await harness.run()
    assert not harness.provider.extract_calls and not harness.provider.decision_calls
    (request,) = harness.provider.transform_calls
    assert request.new_tag_names == ("art",)
    assert request.candidates == (harness.candidate,)
    assert not hasattr(request, "chunks")  # no raw packet payload


async def test_commentary_uses_structured_llm_decision_and_cached_transform(monkeypatch):
    harness = RoutingHarness(monkeypatch)
    commentary = "  Arbitrary user request: do not interpret dates before 1500 A.C.\n"
    assert await harness.run(commentary)
    assert len(harness.provider.decision_calls) == len(harness.provider.transform_calls) == 1
    assert not harness.provider.extract_calls
    assert harness.provider.decision_calls[0].instructions == commentary
    # Persisted decision/result survive restart without repeating provider calls.
    assert await harness.run(commentary)
    assert len(harness.provider.decision_calls) == len(harness.provider.transform_calls) == 1


async def test_full_read_only_when_provider_requires_it_and_never_private_cache(monkeypatch):
    harness = RoutingHarness(monkeypatch)
    harness.provider.cached_decision = CachedFactsDecision(True)
    assert not await harness.run("needs full source")
    assert len(harness.provider.decision_calls) == 1 and not harness.provider.transform_calls
    assert harness.artifact.checkpoint == {"cache_route": "full"}
    assert not await harness.run("needs full source")
    assert len(harness.provider.decision_calls) == 1
    harness = RoutingHarness(monkeypatch)
    harness.descriptor["private"] = True
    assert not await harness.run()
    assert not harness.provider.transform_calls and not harness.provider.decision_calls


async def test_bare_retagging_cannot_drop_cached_facts(monkeypatch):
    harness = RoutingHarness(monkeypatch)
    harness.descriptor["tag_names"] = ["art"]
    harness.provider.cached_response = lambda request: ()
    with pytest.raises(AnalysisValidationError, match="preserve every cached fact"):
        await harness.run()


async def test_invalid_decision_and_invented_provenance_rejected(monkeypatch):
    harness = RoutingHarness(monkeypatch)
    harness.provider.cached_decision = CachedFactsDecision("false")
    with pytest.raises(AnalysisValidationError, match="decision"):
        await harness.run("comment")
    harness = RoutingHarness(monkeypatch)
    harness.descriptor["tag_names"] = ["art"]
    foreign = uuid4()
    harness.provider.cached_response = lambda request: (
        FactCandidate(
            fact_candidate_id(foreign, "X", "Y", ()),
            "X",
            "Y",
            (),
            foreign,
        ),
    )
    with pytest.raises(AnalysisValidationError, match="unknown question"):
        await harness.run()
