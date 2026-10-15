"""Queries shared by packet attribution, author catalogues, and access checks."""

from sqlalchemy import select

from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PacketVersionRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
)


def authorship_rows(model):
    coauthor = model.__mapper__.relationships["coauthors"].mapper.class_
    first = getattr(model, model._author_column)
    return select(model.id.label("content_id"), first.label("author_id")).where(
        first.is_not(None)
    ).union(select(coauthor.content_id, coauthor.author_id)).subquery()


def packet_author_ids(version_id):
    themes = authorship_rows(ThemeRevisionRecord)
    questions = authorship_rows(QuestionRevisionRecord)
    return select(themes.c.author_id).join(
        ThemeRevisionRecord, ThemeRevisionRecord.id == themes.c.content_id,
    ).where(ThemeRevisionRecord.packet_version_id == version_id).union(
        select(questions.c.author_id).join(
            PacketQuestionRecord,
            PacketQuestionRecord.question_revision_id == questions.c.content_id,
        ).where(PacketQuestionRecord.packet_version_id == version_id)
    )


def packet_author_rows(version_ids, *, include_leads=True):
    """Unique version/author pairs, optionally including packet leads."""
    themes = authorship_rows(ThemeRevisionRecord)
    questions = authorship_rows(QuestionRevisionRecord)
    rows = select(
        ThemeRevisionRecord.packet_version_id, themes.c.author_id,
    ).join(
        themes, ThemeRevisionRecord.id == themes.c.content_id,
    ).where(ThemeRevisionRecord.packet_version_id.in_(version_ids))
    others = [
        select(PacketQuestionRecord.packet_version_id, questions.c.author_id).join(
            questions, PacketQuestionRecord.question_revision_id == questions.c.content_id,
        ).where(PacketQuestionRecord.packet_version_id.in_(version_ids)),
    ]
    if include_leads:
        others.append(select(PacketVersionRecord.id, PacketVersionRecord.lead_author_id).where(
            PacketVersionRecord.id.in_(version_ids),
            PacketVersionRecord.lead_author_id.is_not(None),
        ))
    return rows.union(*others)
