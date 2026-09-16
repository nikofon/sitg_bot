from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.storage.models import (
    AuthorRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
)


@dataclass(frozen=True, slots=True)
class StoredPacket:
    version_id: UUID
    logical_id: UUID
    version_number: int
    packet: Packet


class PostgresPacketRepository:
    async def get(
        self, session: AsyncSession, version_id: UUID | None = None
    ) -> StoredPacket | None:
        version_query = select(PacketVersionRecord)
        if version_id is None:
            version_query = version_query.where(PacketVersionRecord.state == "published").order_by(
                PacketVersionRecord.published_at.desc()
            ).limit(1)
        else:
            version_query = version_query.where(PacketVersionRecord.id == version_id)
        version = await session.scalar(version_query)
        if version is None:
            return None

        theme_rows = (
            await session.execute(
                select(ThemeRevisionRecord, AuthorRecord.display_name)
                .outerjoin(AuthorRecord, AuthorRecord.id == ThemeRevisionRecord.author_id)
                .where(ThemeRevisionRecord.packet_version_id == version.id)
                .order_by(ThemeRevisionRecord.position)
            )
        ).all()
        themes: list[Theme] = []
        for theme_record, theme_author in theme_rows:
            question_rows = (
                await session.execute(
                    select(
                        PacketQuestionRecord,
                        QuestionRevisionRecord,
                        AuthorRecord.display_name,
                    )
                    .join(
                        QuestionRevisionRecord,
                        QuestionRevisionRecord.id == PacketQuestionRecord.question_revision_id,
                    )
                    .outerjoin(AuthorRecord, AuthorRecord.id == QuestionRevisionRecord.author_id)
                    .where(
                        PacketQuestionRecord.packet_version_id == version.id,
                        PacketQuestionRecord.theme_revision_id == theme_record.id,
                    )
                    .order_by(PacketQuestionRecord.position)
                )
            ).all()
            questions = tuple(
                Question(
                    text=revision.text,
                    answer=revision.answer,
                    commentary=revision.commentary,
                    value=placement.value,
                    accepted_answers=tuple(revision.accepted_answers),
                    form=revision.form,
                    source=revision.source,
                    author=question_author or theme_author or "",
                )
                for placement, revision, question_author in question_rows
            )
            themes.append(
                Theme(
                    name=theme_record.name,
                    questions=questions,
                    author=theme_author or "",
                    commentary=theme_record.commentary,
                )
            )
        return StoredPacket(
            version_id=version.id,
            logical_id=version.packet_id,
            version_number=version.version_number,
            packet=Packet(
                version.name,
                tuple(themes),
                year=version.year,
                lead_author=(
                    await session.scalar(
                        select(AuthorRecord.display_name).where(
                            AuthorRecord.id == version.lead_author_id
                        )
                    )
                    if version.lead_author_id is not None
                    else ""
                ),
                language=version.language,
            ),
        )
