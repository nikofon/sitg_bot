from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from sitg_bot.domain.knowledge import (
    AppliedCandidate,
    BingoScore,
    BingoScoring,
    CandidateOutcome,
    CommonKnowledgeEstimate,
    DissectionApplication,
    DissectionCandidate,
    FactContent,
    FactProvenance,
    QueryProvenance,
    SharingGrant,
    SourceLink,
    build_mentions,
    can_mutate_collection,
    can_view_collection,
    dissection_candidate_id,
    dissection_instruction_digest,
    is_base_dissection_request,
    normalize_tag_name,
    plan_candidate_application,
    validate_common_knowledge_value,
)

OWNER = UUID(int=1)
RECIPIENT = UUID(int=2)
STRANGER = UUID(int=3)
PACKET = UUID(int=10)
QUESTION = UUID(int=11)


def fact_content(
    title: str = "Battle of <Austerlitz>", description: str = "1805 battle"
) -> FactContent:
    return FactContent(
        title=title,
        description=description,
        links=(SourceLink(url="https://example.test/a", title="Source", position=0),),
    )


def test_tag_names_normalize_to_a_single_canonical_form():
    assert normalize_tag_name("  Napoleonic   Wars ") == "napoleonic wars"
    with pytest.raises(ValueError):
        normalize_tag_name("   ")
    with pytest.raises(ValueError):
        normalize_tag_name("x" * 65)


def test_source_links_require_bounded_http_urls_without_duplicates():
    with pytest.raises(ValueError):
        SourceLink(url="ftp://example.test")
    with pytest.raises(ValueError):
        SourceLink(url="https://example.test/a b")
    with pytest.raises(ValueError):
        SourceLink(url="https://" + "x" * 2000)
    content = FactContent(
        title="T",
        description="D",
        links=(
            SourceLink(url="https://example.test/b", position=1),
            SourceLink(url="https://example.test/a", position=0),
        ),
    )
    assert [link.url for link in content.links] == [
        "https://example.test/a",
        "https://example.test/b",
    ]
    with pytest.raises(ValueError):
        FactContent(
            title="T",
            description="D",
            links=(
                SourceLink(url="https://example.test/a"),
                SourceLink(url="https://example.test/a"),
            ),
        )


def test_fact_content_fingerprint_tracks_editable_text_only():
    first = fact_content()
    same = FactContent(
        title="Battle of  <Austerlitz> ",
        description=" 1805 battle ",
        links=(SourceLink(url="https://example.test/a", title="Source", position=0),),
    )
    assert first.fingerprint() == same.fingerprint()
    assert not first.changed(same)
    changed = fact_content(description="1805 battle near Brno")
    assert first.changed(changed)


def test_fact_provenance_keeps_dissection_question_attribution():
    with pytest.raises(ValueError):
        FactProvenance(origin="dissection", packet_id=PACKET)
    provenance = FactProvenance(
        origin="dissection",
        packet_id=PACKET,
        question_id=QUESTION,
        question_revision="rev-1",
        request_id=UUID(int=99),
    )
    assert provenance.origin == "dissection"


def test_legacy_grants_no_longer_authorize_source_collection_reads():
    granted_at = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        SharingGrant(
            grant_id=UUID(int=5), owner_id=OWNER, recipient_id=OWNER, granted_at=granted_at
        )
    grant = SharingGrant(
        grant_id=UUID(int=5), owner_id=OWNER, recipient_id=RECIPIENT, granted_at=granted_at
    )
    assert grant.active()
    revoked = SharingGrant(
        grant_id=UUID(int=5),
        owner_id=OWNER,
        recipient_id=RECIPIENT,
        granted_at=granted_at,
        revoked_at=granted_at,
    )
    assert not revoked.active()
    assert can_view_collection(OWNER, OWNER, ())
    assert not can_view_collection(RECIPIENT, OWNER, (grant,))
    assert not can_view_collection(RECIPIENT, OWNER, (revoked,))
    assert not can_view_collection(STRANGER, OWNER, (grant,))
    assert not can_view_collection(STRANGER, OWNER, ())
    assert can_mutate_collection(OWNER, OWNER)
    assert not can_mutate_collection(RECIPIENT, OWNER)


def test_mentions_dedupe_across_aliases_and_pages_and_never_fake_completeness():
    retrieval_at = datetime(2026, 10, 1, tzinfo=UTC)
    title_query = QueryProvenance(
        query="battle of austerlitz",
        pages_fetched=2,
        question_ids=frozenset({"q1", "q2"}),
        capped=False,
    )
    alias_query = QueryProvenance(
        query="battle of the three emperors",
        pages_fetched=3,
        question_ids=frozenset({"q2", "q3"}),
        capped=False,
    )
    mentions = build_mentions(
        [title_query, alias_query], retrieval_at=retrieval_at, corpus_size=120_000
    )
    assert mentions.distinct_count == 3
    assert mentions.complete_count() == 3
    assert mentions.question_ids == frozenset({"q1", "q2", "q3"})
    assert mentions.corpus_size == 120_000

    capped_query = QueryProvenance(
        query="battle of austerlitz",
        pages_fetched=5,
        question_ids=frozenset({"q1", "q2", "q3"}),
        capped=True,
    )
    partial = build_mentions([capped_query], retrieval_at=retrieval_at)
    assert partial.capped
    assert partial.complete_count() is None
    assert partial.distinct_count == 3


def test_common_knowledge_estimates_validate_range_and_metadata():
    with pytest.raises(ValueError):
        validate_common_knowledge_value(101)
    with pytest.raises(ValueError):
        validate_common_knowledge_value(True)
    with pytest.raises(ValueError):
        validate_common_knowledge_value("42")
    assert validate_common_knowledge_value(0) == 0
    estimated = CommonKnowledgeEstimate(
        status="available",
        value=42,
        model="model-x",
        prompt_version="p1",
        schema_version="s1",
        population="adults",
        estimated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert estimated.value == 42
    with pytest.raises(ValueError):
        CommonKnowledgeEstimate(status="available", value=42)
    with pytest.raises(ValueError):
        CommonKnowledgeEstimate(status="pending", value=10)
    unavailable = CommonKnowledgeEstimate(status="unavailable")
    assert unavailable.value is None


def test_bingo_scores_are_pending_calibration_and_never_fake_zero():
    with pytest.raises(ValueError):
        BingoScore(status="unknown", value=0)
    mentions = build_mentions(
        [QueryProvenance("q", 1, frozenset({"a"}), False)],
        retrieval_at=datetime(2026, 10, 1, tzinfo=UTC),
        corpus_size=10,
        corpus_as_of=date(2026, 9, 30),
    )
    available = CommonKnowledgeEstimate(
        status="available", value=30, estimated_at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    scoring = BingoScoring()
    score = scoring.score(mentions, available)
    assert score.status == "pending_calibration"
    assert score.value is None
    assert scoring.score(mentions, CommonKnowledgeEstimate(status="denied")).status == "denied"
    assert scoring.score(mentions, CommonKnowledgeEstimate(status="failed")).status == "failed"
    assert (
        scoring.score(mentions, CommonKnowledgeEstimate(status="pending")).status
        == "insufficient_data"
    )
    zero_knowledge = CommonKnowledgeEstimate(
        status="available", value=0, estimated_at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    empty_mentions = build_mentions([], retrieval_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert scoring.score(empty_mentions, zero_knowledge).status == "insufficient_data"
    calibrated = BingoScoring(
        version="bingo-calibrated-v1",
        formula=lambda mention_count, common: min(
            100, mention_count.distinct_count * 10 + (100 - (common.value or 0))
        ),
    )
    scored = calibrated.score(mentions, available)
    assert scored.status == "scored"
    assert scored.value == 80
    with pytest.raises(ValueError):
        BingoScoring(formula="not-callable")


def test_dissection_candidates_use_deterministic_identity():
    content = fact_content()
    candidate_id = dissection_candidate_id(PACKET, QUESTION, "rev-1", content)
    assert candidate_id == dissection_candidate_id(PACKET, QUESTION, "rev-1", content)
    assert candidate_id != dissection_candidate_id(PACKET, QUESTION, "rev-2", content)
    DissectionCandidate(
        candidate_id=candidate_id,
        content=content,
        packet_id=PACKET,
        question_id=QUESTION,
        question_revision="rev-1",
    )
    with pytest.raises(ValueError):
        DissectionCandidate(
            candidate_id="dc_forged",
            content=content,
            packet_id=PACKET,
            question_id=QUESTION,
            question_revision="rev-1",
        )
    with pytest.raises(ValueError):
        DissectionCandidate(
            candidate_id=dissection_candidate_id(PACKET, QUESTION, "", content),
            content=content,
            packet_id=PACKET,
            question_id=QUESTION,
            question_revision="",
        )


def test_instruction_identity_separates_base_from_comment_revisions():
    assert not is_base_dissection_request("   ", "")
    assert is_base_dissection_request("", "")
    assert not is_base_dissection_request("focus on navy", "")
    assert dissection_instruction_digest("same") != dissection_instruction_digest(
        "same", "comment"
    )
    assert dissection_instruction_digest("same", "one") != dissection_instruction_digest(
        "same", "two"
    )


def test_candidate_application_plan_preserves_manual_edits_and_deduplicates():
    content = fact_content()
    new_id = dissection_candidate_id(PACKET, QUESTION, "rev-1", content)
    edited_id = dissection_candidate_id(PACKET, UUID(int=12), "rev-1", fact_content("Edited"))
    repeated_id = dissection_candidate_id(PACKET, UUID(int=13), "rev-1", fact_content("Repeat"))
    candidates = (
        DissectionCandidate(new_id, content, PACKET, QUESTION, "rev-1"),
        DissectionCandidate(
            edited_id, fact_content("Edited"), PACKET, UUID(int=12), "rev-1"
        ),
        DissectionCandidate(
            repeated_id, fact_content("Repeat"), PACKET, UUID(int=13), "rev-1"
        ),
        DissectionCandidate(
            repeated_id, fact_content("Repeat"), PACKET, UUID(int=13), "rev-1"
        ),
    )
    applied = {
        edited_id: AppliedCandidate(
            candidate_id=edited_id,
            fact_id=UUID(int=71),
            applied_content_version=1,
            current_content_version=2,
        ),
        repeated_id: AppliedCandidate(
            candidate_id=repeated_id,
            fact_id=UUID(int=72),
            applied_content_version=1,
            current_content_version=1,
        ),
    }
    outcomes = plan_candidate_application(
        candidates, applied, excluded=[new_id]
    )
    assert outcomes == (
        CandidateOutcome(new_id, "excluded"),
        CandidateOutcome(edited_id, "manual_edit_preserved", UUID(int=71)),
        CandidateOutcome(repeated_id, "duplicate", UUID(int=72)),
        CandidateOutcome(repeated_id, "duplicate"),
    )


def test_dissection_application_tracks_partial_work_for_retry():
    request_id = UUID(int=90)
    application = DissectionApplication(
        request_id=request_id,
        packet_id=PACKET,
        instruction_digest="abc",
        base=True,
        outcomes=(
            CandidateOutcome("dc_new", "applied", UUID(int=81)),
            CandidateOutcome("dc_bad", "invalid"),
            CandidateOutcome("dc_old", "duplicate", UUID(int=82)),
        ),
        applied_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert application.status == "partially_succeeded"
    assert application.applied_fact_ids == frozenset({UUID(int=81)})
    assert application.failed_candidate_ids == frozenset({"dc_bad"})
    clean = DissectionApplication(
        request_id=request_id,
        packet_id=PACKET,
        instruction_digest="abc",
        base=False,
        outcomes=(CandidateOutcome("dc_new", "applied", UUID(int=81)),),
        applied_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert clean.status == "succeeded"
    assert clean.applied_fact_ids == frozenset({UUID(int=81)})
