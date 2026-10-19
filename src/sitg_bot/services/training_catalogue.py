"""Batched, content-free metadata for the authorized training packet page."""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import aliased

from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter
from sitg_bot.storage.authorship import packet_author_rows
from sitg_bot.storage.models import (
    AuthorRecord,
    KnowledgeDissectionRecord,
    LogicalPacketRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    ThemeRevisionRecord,
    TournamentPacketAssignmentRecord,
    TournamentRecord,
    TrainingAnalysisRequestRecord,
    TrainingBestBuzzCompletionRecord,
)


async def enrich_packet_cards(session, player_id, items, readable_assignments):
    """Every query is scoped to the authorized page, never one query per card."""
    if not items:
        return items
    ids = [item["id"] for item in items]
    authors = defaultdict(list)
    pairs = packet_author_rows(ids).subquery()
    for version_id, author_id, name in await session.execute(
        select(pairs.c.packet_version_id, AuthorRecord.id, AuthorRecord.display_name)
        .join(AuthorRecord, AuthorRecord.id == pairs.c.author_id)
        .order_by(AuthorRecord.display_name, AuthorRecord.id)
    ):
        authors[version_id].append({"id": author_id, "name": name})

    tournaments = defaultdict(list)
    assignment = aliased(TournamentPacketAssignmentRecord)
    for version_id, tournament_id, name in await session.execute(
        select(PacketVersionRecord.id, TournamentRecord.id, TournamentRecord.name)
        .select_from(PacketVersionRecord)
        .join(LogicalPacketRecord)
        .join(assignment, assignment.packet_id == LogicalPacketRecord.id)
        .join(TournamentRecord, TournamentRecord.id == assignment.tournament_id)
        .where(PacketVersionRecord.id.in_(ids), assignment.id.in_(readable_assignments))
        .distinct()
        .order_by(TournamentRecord.name, TournamentRecord.id)
    ):
        tournaments[version_id].append({"id": tournament_id, "name": name})

    themes = defaultdict(set)
    for version_id, theme_id in await session.execute(
        select(ThemeRevisionRecord.packet_version_id, ThemeRevisionRecord.id).where(
            ThemeRevisionRecord.packet_version_id.in_(ids)
        )
    ):
        themes[version_id].add(theme_id)
    fresh = defaultdict(set)
    for unit in await SIContentAdapter().available_play_units(
        session,
        [PacketSelection(version_id, i) for i, version_id in enumerate(ids, 1)],
        [player_id],
    ):
        fresh[unit.packet_version_id].add(unit.revision_id)

    question_themes = defaultdict(dict)
    for version_id, question_id, theme_id in await session.execute(
        select(
            PacketQuestionRecord.packet_version_id,
            PacketQuestionRecord.question_revision_id,
            PacketQuestionRecord.theme_revision_id,
        ).where(PacketQuestionRecord.packet_version_id.in_(ids))
    ):
        question_themes[str(version_id)][str(question_id)] = theme_id

    covered = defaultdict(set)
    dissected = set()
    unknown = set()
    # Applied library requests, not merely generated cache artifacts, are the
    # authority for per-user library dissection state. Never read other owners.
    for revision, status, source, saved_themes, request_status in await session.execute(
        select(
            KnowledgeDissectionRecord.packet_revision,
            KnowledgeDissectionRecord.status,
            TrainingAnalysisRequestRecord.source,
            KnowledgeDissectionRecord.theme_revision_ids,
            TrainingAnalysisRequestRecord.status,
        )
        .outerjoin(
            TrainingAnalysisRequestRecord,
            TrainingAnalysisRequestRecord.request_id
            == KnowledgeDissectionRecord.analysis_request_id,
        )
        .where(
            KnowledgeDissectionRecord.owner_id == player_id,
            KnowledgeDissectionRecord.packet_revision.in_([str(value) for value in ids]),
            KnowledgeDissectionRecord.status.in_(("succeeded", "partially_succeeded")),
            KnowledgeDissectionRecord.application.is_not(None),
        )
    ):
        dissected.add(revision)
        if status == "succeeded" and saved_themes is not None:
            covered[revision].update(UUID(value) for value in saved_themes)
            continue
        questions = source.get("question_revision_ids") if source else None
        mapping = question_themes[revision]
        if (
            status != "succeeded"
            or request_status != "succeeded"
            or not questions
            or any(q not in mapping for q in questions)
        ):
            unknown.add(revision)
        else:
            covered[revision].update(mapping[q] for q in questions)

    best_buzz = set(
        await session.scalars(
            select(TrainingBestBuzzCompletionRecord.packet_version_id).where(
                TrainingBestBuzzCompletionRecord.owner_id == player_id,
                TrainingBestBuzzCompletionRecord.packet_version_id.in_(ids),
            )
        )
    )
    for item in items:
        identity = item["id"]
        revision = str(identity)
        count = len(covered[revision])
        if revision in unknown and covered[revision] != themes[identity]:
            count = None
        item.update(
            authors=authors[identity],
            tournaments=tournaments[identity],
            total_theme_count=len(themes[identity]),
            fresh_theme_count=len(fresh[identity]),
            dissected_theme_count=count,
            dissected=revision in dissected,
            best_buzz_calculated=identity in best_buzz,
        )
    return items


def packet_order(order):
    """Stable scalar sort key; UUID is always the tie-breaker. Unknown years sort as 0."""
    from sqlalchemy import func

    model = PacketVersionRecord
    if order == "id_asc":
        return model.id, False
    if order in ("name_asc", "name_desc"):
        return func.lower(model.name), order.endswith("desc")
    if order in ("year_asc", "year_desc"):
        return func.coalesce(model.year, 0), order.endswith("desc")
    raise ValueError("Unsupported training packet order")


def packet_cursor(cursor, order, signature):
    import base64
    import json

    try:
        if order == "id_asc":
            return UUID(cursor), UUID(cursor)
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if payload["order"] != order or payload["filter"] != signature:
            raise ValueError
        value = payload["value"]
        if order.startswith("year"):
            if not isinstance(value, int):
                raise ValueError
        elif not isinstance(value, str):
            raise ValueError
        return value, UUID(payload["id"])
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ValueError("Invalid training packet cursor") from error


def encode_packet_cursor(item, value, order, signature):
    import base64
    import json

    if order == "id_asc":
        return str(item["id"])
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "id": str(item["id"]),
                    "value": value,
                    "order": order,
                    "filter": signature,
                },
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
