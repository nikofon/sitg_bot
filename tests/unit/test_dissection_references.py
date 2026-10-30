from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sitg_bot.services.analysis_jobs import AnalysisService
from sitg_bot.services.training_ai import (
    ProviderSettings,
    QuestionText,
    fact_candidate_id,
    source_reference_urls,
)
from sitg_bot.services.training_knowledge import dissection_text


async def test_original_fields_bound_cached_citations_without_json_escape_loss():
    identity = uuid4()
    links = tuple(f"https://example.org/{name}" for name in ("question", "answer", "note", "a", "b"))
    original = SimpleNamespace(
        text="Question " + links[0], answer=links[1], commentary=links[2],
        source="\n".join(links[3:]),
    )
    question = QuestionText(
        identity, dissection_text(original), reference_urls=source_reference_urls(original),
    )
    output_links = (*links, "https://invented.example/untrusted")
    worker = SimpleNamespace(settings=ProviderSettings("fake", "fixture"), _write_terminal=AsyncMock())
    await AnalysisService._finalize_packet_scan(
        worker, uuid4(), uuid4(), "dissection", (question,), [{
            "candidate_id": fact_candidate_id(identity, "Fact", "Body", output_links),
            "title": "Fact", "description": "Body", "links": list(output_links),
            "question_revision_id": str(identity),
        }], {"tag_names": []},
    )
    result = worker._write_terminal.await_args.kwargs
    assert result["status"] == "succeeded"
    candidate, = result["payload"]["candidates"]
    assert candidate["links"] == list(links)
    assert candidate["candidate_id"] == fact_candidate_id(identity, "Fact", "Body", links)
