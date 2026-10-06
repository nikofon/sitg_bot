"""Queries shared by packet attribution, author catalogues, and access checks."""

from sqlalchemy import select

from sitg_bot.storage.models import (
    PacketQuestionRecord,
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
