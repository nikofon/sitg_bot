"""Persist authorship exposure independently of games and attribution corrections."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerAuthorLinkRecord,
    PlayerExposureClaimRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
)


async def burn_author_content(
    session: AsyncSession,
    *,
    version_id: UUID | None = None,
    author_id: UUID | None = None,
) -> None:
    await session.flush()
    links_query = select(PlayerAuthorLinkRecord)
    if author_id is not None:
        links_query = links_query.where(PlayerAuthorLinkRecord.author_id == author_id)
    links: dict[UUID, set[UUID]] = {}
    for link in await session.scalars(links_query):
        links.setdefault(link.author_id, set()).add(link.player_id)
    if not links:
        return
    query = select(ThemeRevisionRecord, PacketVersionRecord.lead_author_id).join(
        PacketVersionRecord, PacketVersionRecord.id == ThemeRevisionRecord.packet_version_id
    )
    if version_id is not None:
        query = query.where(PacketVersionRecord.id == version_id)
    for theme, lead_id in (await session.execute(query)).all():
        questions = (
            await session.scalars(
                select(QuestionRevisionRecord)
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                )
                .where(PacketQuestionRecord.theme_revision_id == theme.id)
            )
        ).all()
        authors = {lead_id, theme.author_id, *(question.author_id for question in questions)}
        players = {player for author in authors for player in links.get(author, ())}
        claims = {("theme", theme.theme_id), *(("question", q.question_id) for q in questions)}
        for player_id in sorted(players):
            for namespace, claim_id in sorted(claims):
                statement = insert(PlayerExposureClaimRecord).values(
                    player_id=player_id,
                    packet_version_id=theme.packet_version_id,
                    claim_namespace=namespace,
                    claim_id=claim_id,
                    state="burnt",
                    burnt_at=datetime.now(UTC),
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["player_id", "claim_namespace", "claim_id"],
                        index_where=text("state IN ('reserved', 'burnt')"),
                        set_={
                            "state": "burnt",
                            "burnt_at": func.coalesce(
                                PlayerExposureClaimRecord.burnt_at, statement.excluded.burnt_at
                            ),
                        },
                    )
                )
