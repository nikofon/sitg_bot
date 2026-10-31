"""Versioned public bare-fact reuse; private/commented results never seed a cache.

All provider calls use the caller's bounded, metered wrapper and fenced checkpoints.
No packet/question text is loaded on decision or cached-transformation paths.
"""

from dataclasses import asdict, replace
from uuid import UUID

from sqlalchemy import select

from sitg_bot.services.training_ai import (
    AnalysisValidationError,
    CachedFactsDecision,
    CachedFactsRequest,
    FactCandidate,
    validate_fact_candidates,
)
from sitg_bot.storage.models import TrainingAnalysisArtifactRecord


def candidates_from_payload(payload):
    return tuple(
        FactCandidate(
            value["candidate_id"],
            value["title"],
            value["description"],
            tuple(value["links"]),
            UUID(value["question_revision_id"]),
            tuple(value.get("tags", [])),
        )
        for value in payload["candidates"]
    )


def candidate_payload(value):
    return {
        **asdict(value),
        "question_revision_id": str(value.question_revision_id),
        "links": list(value.links),
        "tags": list(value.tags),
    }


async def public_base(service, session, fingerprint, tags, *, require_coverage=False):
    model = TrainingAnalysisArtifactRecord
    statement = select(model).where(
        model.kind == "dissection",
        model.shared.is_(True),
        model.owner_player_id.is_(None),
        model.source_fingerprint == fingerprint,
        model.status == "succeeded",
        model.model == service.settings.model,
        model.prompt_version == service.settings.prompt_version,
        model.schema_version == service.settings.schema_version,
        model.tokenizer_version == service.settings.tokenizer_version,
        model.payload["cache_version"].as_integer() == 2,
    )
    if require_coverage:
        # Filter before the bounded page: an older compatible base must not be
        # hidden by newer artifacts carrying unrelated tag palettes.
        statement = statement.where(model.payload["tag_coverage"].contains(sorted(tags)))
    rows = list(
        await session.scalars(statement.order_by(model.created_at.desc(), model.id).limit(100))
    )
    requested = set(tags)
    eligible = [
        row
        for row in rows
        if row.payload
        and row.payload.get("cache_version") == 2
        and (not require_coverage or requested <= set(row.payload.get("tag_coverage", [])))
    ]
    if not eligible:
        return None
    # Prefer an already-covering base; otherwise maximize useful existing tag coverage.
    return min(
        eligible,
        key=lambda row: (
            not requested <= set(row.payload["tag_coverage"]),
            -len(requested & set(row.payload["tag_coverage"])),
            str(row.id),
        ),
    )


async def run_cached_dissection(service, artifact_id, lease_token, instructions, descriptor):
    """Return true when finished from cached facts, false for selected-source extraction."""
    async with service.database.transaction() as session:
        artifact = await session.get(TrainingAnalysisArtifactRecord, artifact_id)
        checkpoint = dict(artifact.checkpoint or {})
        if descriptor.get("private") or checkpoint.get("cache_route") == "full":
            return False
        if "base" not in checkpoint:
            base = await public_base(
                service, session, artifact.source_fingerprint, descriptor.get("tag_names", [])
            )
            if base is None:
                return False
            checkpoint = {"base": base.payload, "cache_route": "undecided"}
    # Snapshot is request-private recovery data, not a mutable/reusable global comment cache.
    cached = validate_fact_candidates(
        candidates_from_payload(checkpoint["base"]),
        allowed_question_revision_ids={
            UUID(value) for value in descriptor["question_revision_ids"]
        },
        max_output_facts=service.settings.max_output_facts,
        allowed_tag_names=checkpoint["base"]["tag_coverage"],
    )
    tags = tuple(descriptor.get("tag_names", []))
    request = CachedFactsRequest(
        cached,
        instructions,
        descriptor["language"],
        tags,
        tuple(sorted(set(tags) - set(checkpoint["base"]["tag_coverage"]))),
    )
    if checkpoint["cache_route"] == "undecided":
        if instructions:
            decision = await service._provider_call(service.provider.decide_cached_facts, request)
            if (
                not isinstance(decision, CachedFactsDecision)
                or type(decision.requires_source) is not bool
            ):
                raise AnalysisValidationError("Provider returned invalid cached-facts decision")
            if decision.requires_source:
                await service._write_checkpoint(artifact_id, lease_token, {"cache_route": "full"})
                return False
        checkpoint["cache_route"] = "cached"
        await service._write_checkpoint(artifact_id, lease_token, checkpoint)
    if "transformed" not in checkpoint:
        if not instructions and not request.new_tag_names:
            transformed = tuple(
                replace(value, tags=tuple(tag for tag in value.tags if tag in tags))
                for value in cached
            )
        else:
            transformed = await service._provider_call(
                service.provider.transform_cached_facts, request
            )
        transformed = validate_fact_candidates(
            transformed,
            allowed_question_revision_ids={value.question_revision_id for value in cached},
            max_output_facts=service.settings.max_output_facts,
            allowed_tag_names=tags,
        )
        if not instructions and {value.candidate_id for value in transformed} != {
            value.candidate_id for value in cached
        }:
            raise AnalysisValidationError(
                "Bare retagging must preserve every cached fact unchanged"
            )
        allowed_links = {}
        for value in cached:
            allowed_links.setdefault(value.question_revision_id, set()).update(value.links)
        if any(
            set(value.links) - allowed_links.get(value.question_revision_id, set())
            for value in transformed
        ):
            raise AnalysisValidationError("Cached transformation invented source references")
        checkpoint["transformed"] = [candidate_payload(value) for value in transformed]
        checkpoint.update(chunks_done=1, chunks_total=1)
        await service._write_checkpoint(artifact_id, lease_token, checkpoint)
    await service._write_terminal(
        artifact_id,
        lease_token,
        status="succeeded",
        payload={
            "candidates": checkpoint["transformed"],
            "cache_version": 2,
            "tag_coverage": sorted(tags),
            "route": "cached_transform",
        },
    )
    return True
