from datetime import timedelta
from uuid import uuid4

import pytest

from sitg_bot.services.training_ai import (
    ANALYSIS_BEST_BUZZ,
    ANALYSIS_COMMON_KNOWLEDGE,
    ANALYSIS_DISSECTION,
    EMPTY_INSTRUCTION_DIGEST,
    AnalysisValidationError,
    BestBuzzSource,
    BuzzBoundary,
    BuzzRequest,
    CommonKnowledgeEstimate,
    CommonKnowledgeRequest,
    CommonKnowledgeSource,
    DissectionSource,
    ExtractionRequest,
    FactCandidate,
    FakeAnalysisProvider,
    ProviderOutcomeUncertain,
    ProviderSettings,
    QuestionText,
    SourceTooLarge,
    fact_candidate_id,
    instruction_digest,
    packet_chunks,
    source_descriptor,
    source_fingerprint_of,
    source_from_descriptor,
    source_kind,
    text_fingerprint,
    validate_buzz_boundaries,
    validate_common_knowledge,
    validate_fact_candidates,
)


def candidate(title: str, description: str, revision_id=None, links=("https://a.test/1",)):
    revision_id = revision_id or uuid4()
    return FactCandidate(
        fact_candidate_id(revision_id, title, description, links),
        title,
        description,
        links,
        revision_id,
    )


def test_packet_chunks_pack_questions_within_the_bound() -> None:
    questions = tuple(
        QuestionText(uuid4(), f"Question number {index} with plenty of text")
        for index in range(5)
    )
    chunks = packet_chunks(questions, max_chunk_characters=100)
    assert len(chunks) >= 2
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    flat = [identifier for chunk in chunks for identifier in chunk.question_revision_ids]
    assert flat == [question.question_revision_id for question in questions]
    for chunk in chunks:
        for identifier in chunk.question_revision_ids:
            assert f"[{identifier}]" in chunk.text


def test_packet_chunks_reject_oversized_single_questions() -> None:
    with pytest.raises(SourceTooLarge):
        packet_chunks((QuestionText(uuid4(), "x" * 200),), max_chunk_characters=100)


def test_fingerprints_are_stable_and_instruction_digests_preserve_instructions() -> None:
    assert text_fingerprint("a", "b") == text_fingerprint("a", "b")
    assert text_fingerprint("a", "b") != text_fingerprint("b", "a")
    assert instruction_digest("  spaced  ") != instruction_digest("spaced")
    assert instruction_digest("") == EMPTY_INSTRUCTION_DIGEST


def test_source_kind_descriptor_and_fingerprint_round_trip() -> None:
    dissection = DissectionSource(uuid4(), (uuid4(), uuid4()), language="ru")
    assert source_kind(dissection) == ANALYSIS_DISSECTION
    assert source_from_descriptor(source_descriptor(dissection)) == dissection
    assert source_fingerprint_of(dissection) == source_fingerprint_of(
        source_from_descriptor(source_descriptor(dissection))
    )

    buzz = BestBuzzSource(uuid4(), (uuid4(),), question_token_target_chars=12)
    assert source_kind(buzz) == ANALYSIS_BEST_BUZZ
    assert source_from_descriptor(source_descriptor(buzz)) == buzz
    assert source_fingerprint_of(buzz) != source_fingerprint_of(
        BestBuzzSource(buzz.packet_revision_id, buzz.question_revision_ids)
    )

    knowledge = CommonKnowledgeSource("fact text", population="historians")
    assert source_kind(knowledge) == ANALYSIS_COMMON_KNOWLEDGE
    assert source_from_descriptor(source_descriptor(knowledge)) == knowledge


def test_provider_settings_validation_and_redaction() -> None:
    settings = ProviderSettings(provider="fake", model="m1")
    assert "api_key" not in settings.redacted()
    assert settings.redacted()["provider"] == "fake"
    with pytest.raises(ValueError):
        ProviderSettings(provider="", model="m1")
    with pytest.raises(ValueError):
        ProviderSettings(provider="fake", model="m1", timeout=timedelta(0))
    with pytest.raises(ValueError):
        ProviderSettings(provider="fake", model="m1", max_chunk_characters=10)
    with pytest.raises(ValueError):
        ProviderSettings(provider="fake", model="m1", max_output_facts=0)


def test_validate_fact_candidates_checks_provenance_links_and_ids() -> None:
    revision_id = uuid4()
    allowed = {revision_id}
    valid = candidate("Title", "Description", revision_id=revision_id)
    assert validate_fact_candidates(
        (valid,), allowed_question_revision_ids=allowed, max_output_facts=10
    )

    unknown = candidate("Title", "Description")
    with pytest.raises(AnalysisValidationError, match="unknown question"):
        validate_fact_candidates(
            (unknown,), allowed_question_revision_ids=allowed, max_output_facts=10
        )

    bad_link = candidate("T", "D", revision_id=revision_id, links=("ftp://host/x",))
    with pytest.raises(AnalysisValidationError, match="http"):
        validate_fact_candidates(
            (bad_link,), allowed_question_revision_ids=allowed, max_output_facts=10
        )

    tampered = FactCandidate("not-deterministic", "Title", "Description", (), revision_id)
    with pytest.raises(AnalysisValidationError, match="deterministic"):
        validate_fact_candidates(
            (tampered,), allowed_question_revision_ids=allowed, max_output_facts=10
        )

    duplicated = (candidate("T", "D", revision_id=revision_id),) * 2
    with pytest.raises(AnalysisValidationError, match="Duplicate"):
        validate_fact_candidates(
            duplicated, allowed_question_revision_ids=allowed, max_output_facts=10
        )

    many = tuple(candidate(f"T{i}", "D", revision_id=revision_id) for i in range(3))
    with pytest.raises(AnalysisValidationError, match="output bound"):
        validate_fact_candidates(many, allowed_question_revision_ids=allowed, max_output_facts=2)


def test_validate_buzz_boundaries_require_coverage_digests_and_range() -> None:
    revision_id = uuid4()
    digest = text_fingerprint("question text")
    boundary = BuzzBoundary(revision_id, digest, 2)
    assert validate_buzz_boundaries(
        (boundary,), token_counts={revision_id: 5}, text_digests={revision_id: digest}
    )

    with pytest.raises(AnalysisValidationError, match="digest"):
        validate_buzz_boundaries(
            (boundary,), token_counts={revision_id: 5}, text_digests={revision_id: "other"}
        )
    with pytest.raises(AnalysisValidationError, match="outside"):
        validate_buzz_boundaries(
            (BuzzBoundary(revision_id, digest, 6),),
            token_counts={revision_id: 5},
            text_digests={revision_id: digest},
        )
    with pytest.raises(AnalysisValidationError, match="missing"):
        validate_buzz_boundaries(
            (), token_counts={revision_id: 5}, text_digests={revision_id: digest}
        )


def test_validate_common_knowledge_bounds() -> None:
    assert validate_common_knowledge(0) == 0
    assert validate_common_knowledge(100) == 100
    with pytest.raises(AnalysisValidationError):
        validate_common_knowledge(101)
    with pytest.raises(AnalysisValidationError):
        validate_common_knowledge(-1)


@pytest.mark.asyncio
async def test_fake_provider_defaults_calls_and_failures() -> None:
    provider = FakeAnalysisProvider(delay=0.01)
    revision_id = uuid4()
    chunks = packet_chunks((QuestionText(revision_id, "Text"),), max_chunk_characters=200)

    facts = await provider.extract_facts(ExtractionRequest(chunks, "", "en"))
    assert facts and facts[0].question_revision_id == revision_id

    boundaries = await provider.best_buzz_boundaries(BuzzRequest(chunks, "en"))
    assert boundaries[0].text_digest == text_fingerprint("Text")

    estimate = await provider.estimate_common_knowledge(
        CommonKnowledgeRequest("fact", "en", "general")
    )
    assert estimate == CommonKnowledgeEstimate(50)
    assert len(provider.provider_request_ids) == 3
    assert provider.max_in_flight == 1

    provider.extract_failures.append(
        ProviderOutcomeUncertain("boom", provider_request_id="req-1")
    )
    with pytest.raises(ProviderOutcomeUncertain) as details:
        await provider.extract_facts(ExtractionRequest(chunks, "", "en"))
    assert details.value.provider_request_id == "req-1"
    assert len(provider.provider_request_ids) == 4
