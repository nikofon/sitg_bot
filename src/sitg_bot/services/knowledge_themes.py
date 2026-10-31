"""Non-disclosing theme listing and confirmed, selected-theme preview."""

from sqlalchemy import select

from sitg_bot.services.training_access import require_trainee
from sitg_bot.services.training_packets import DisclosureRequired
from sitg_bot.storage.models import (
    AuthorRecord,
    PacketDraftRecord,
    PacketVersionRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
)


async def source_authors(session, version_id, theme_ids, question_ids):
    """Trusted pinned attribution, never supplied by the model."""
    themes = list(
        await session.scalars(
            select(ThemeRevisionRecord)
            .where(ThemeRevisionRecord.packet_version_id == version_id)
            .order_by(ThemeRevisionRecord.position)
        )
    )
    questions = list(
        await session.scalars(
            select(QuestionRevisionRecord).where(QuestionRevisionRecord.id.in_(question_ids))
        )
    )
    version = await session.get(PacketVersionRecord, version_id)
    draft = (
        await session.get(PacketDraftRecord, version.source_draft_id)
        if version.source_draft_id
        else None
    )
    theme_names, question_names = {}, {}
    ids = {identity for item in [*themes, *questions] for identity in item.author_ids}
    names = (
        dict(
            (
                await session.execute(
                    select(AuthorRecord.id, AuthorRecord.display_name).where(
                        AuthorRecord.id.in_(ids)
                    )
                )
            ).all()
        )
        if ids
        else {}
    )
    for theme in themes:
        theme_names[theme.id] = "; ".join(names[identity] for identity in theme.author_ids)
    for question in questions:
        question_names[question.id] = "; ".join(names[identity] for identity in question.author_ids)
    # Private authors intentionally do not enter the global author catalogue.
    if draft is not None and draft.scope == "training":
        from sitg_bot.storage.models import PacketQuestionRecord

        placements = (
            await session.execute(
                select(
                    PacketQuestionRecord.question_revision_id,
                    PacketQuestionRecord.theme_revision_id,
                )
                .where(PacketQuestionRecord.packet_version_id == version_id)
                .order_by(PacketQuestionRecord.position)
            )
        ).all()
        for theme, original in zip(themes, draft.content.get("themes", []), strict=True):
            theme_names[theme.id] = "; ".join(original.get("authors", []))
            theme_questions = [
                identity for identity, theme_id in placements if theme_id == theme.id
            ]
            for identity, value in zip(theme_questions, original.get("questions", []), strict=True):
                question_names[identity] = "; ".join(
                    value.get("authors") or original.get("authors", [])
                )
    # Explicit inheritance or omitted author attribution uses the theme, not the sender.
    from sitg_bot.storage.models import PacketQuestionRecord

    placements = (
        await session.execute(
            select(
                PacketQuestionRecord.question_revision_id,
                PacketQuestionRecord.theme_revision_id,
            ).where(
                PacketQuestionRecord.packet_version_id == version_id,
                PacketQuestionRecord.question_revision_id.in_(question_ids),
            )
        )
    ).all()
    for identity, theme_id in placements:
        if not question_names.get(identity):
            question_names[identity] = theme_names.get(theme_id, "")
    return theme_names, question_names


class KnowledgeThemes:
    def __init__(self, app):
        self.app = app

    async def list(self, actor, version_id):
        async with self.app.database.transaction() as session:
            await require_trainee(session, actor)
            version, _ = await self.app.packets.authorize(session, actor, version_id)
            themes = (
                await session.execute(
                    select(ThemeRevisionRecord.id, ThemeRevisionRecord.name)
                    .where(ThemeRevisionRecord.packet_version_id == version_id)
                    .order_by(ThemeRevisionRecord.position)
                )
            ).all()
        quota = (await self.app.entitlements.capabilities(actor))["dissection"]
        return {
            "version_id": version_id,
            "packet_name": version.name,
            "themes": [{"theme_revision_id": identity, "name": name} for identity, name in themes],
            "quota": {
                "enabled": quota.enabled,
                "remaining": quota.remaining,
                "reset_at": quota.reset_at,
            },
        }

    async def preview(self, actor, *, version_id, theme_revision_id, confirm_exposure=False):
        try:
            async with self.app.database.transaction() as session:
                source = await self.app.packets.disclose(
                    session,
                    actor,
                    version_id,
                    confirm=confirm_exposure,
                    theme_revision_ids=[theme_revision_id],
                )
                theme = await session.get(ThemeRevisionRecord, theme_revision_id)
                names, _ = await source_authors(
                    session,
                    version_id,
                    [theme_revision_id],
                    [question.revision_id for question in source.questions],
                )
                return {
                    "confirmation_required": False,
                    "theme": {
                        "theme_revision_id": theme_revision_id,
                        "name": theme.name,
                        "author": names.get(theme_revision_id, ""),
                        "commentary": theme.commentary,
                        "questions": [
                            {
                                "revision_id": question.revision_id,
                                "value": question.value,
                                "text": question.text,
                                "answer": question.answer,
                                "accepted_answers": list(question.accepted_answers),
                                "rejected_answers": list(question.rejected_answers),
                                "commentary": question.commentary,
                            }
                            for question in source.questions
                        ],
                    },
                }
        except DisclosureRequired as error:
            return {"confirmation_required": True, "fresh_unit_count": error.fresh_unit_count}
