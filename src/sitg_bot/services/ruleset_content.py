from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.game_rulesets import ExposureClaim, PlayUnit
from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    PlayerPacketBlockRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
)
from sitg_bot.storage.packets import PostgresPacketRepository


@dataclass(frozen=True, slots=True)
class PacketSelection:
    packet_version_id: UUID
    selection_order: int


class RulesetContentAdapter(Protocol):
    key: str
    version: int

    async def library_pages(
        self, session: AsyncSession, packet_version_id: UUID
    ) -> list[dict[str, object]]: ...

    async def play_unit_count(self, session: AsyncSession, packet_version_id: UUID) -> int: ...

    async def available_play_units(
        self,
        session: AsyncSession,
        selections: Sequence[PacketSelection],
        player_ids: Sequence[UUID],
    ) -> list[PlayUnit]: ...


@dataclass(frozen=True, slots=True)
class SIContentAdapter:
    key: str = "si"
    version: int = 1

    async def library_pages(
        self, session: AsyncSession, packet_version_id: UUID
    ) -> list[dict[str, object]]:
        stored = await PostgresPacketRepository().get(session, packet_version_id)
        if stored is None:
            raise LookupError("Packet version not found")
        theme_rows = (
            await session.execute(
                select(ThemeRevisionRecord)
                .where(ThemeRevisionRecord.packet_version_id == packet_version_id)
                .order_by(ThemeRevisionRecord.position)
            )
        ).scalars().all()
        question_rows = (
            await session.execute(
                select(
                    PacketQuestionRecord.theme_revision_id,
                    PacketQuestionRecord.question_revision_id,
                    QuestionRevisionRecord,
                )
                .join(
                    QuestionRevisionRecord,
                    QuestionRevisionRecord.id == PacketQuestionRecord.question_revision_id,
                )
                .where(PacketQuestionRecord.packet_version_id == packet_version_id)
                .order_by(
                    PacketQuestionRecord.theme_revision_id, PacketQuestionRecord.position
                )
            )
        ).all()
        questions_by_theme: dict[UUID, list[tuple[UUID, tuple[UUID, ...]]]] = {}
        for theme_revision_id, question_revision_id, revision in question_rows:
            questions_by_theme.setdefault(theme_revision_id, []).append(
                (question_revision_id, revision.author_ids)
            )
        return [
            {
                "title": theme.name,
                "author": theme.author,
                "authors": list(theme.authors),
                "commentary": theme.commentary,
                "author_id": str(row.author_ids[0]) if row.author_ids else None,
                "author_ids": [str(value) for value in row.author_ids],
                "questions": [
                    {
                        **asdict(question),
                        "authors": list(question.authors or theme.authors),
                        "author": ", ".join(question.authors or theme.authors),
                        "inherits_theme_authors": not question.authors,
                        "accepted_answers": list(question.accepted_answers),
                        "rejected_answers": list(question.rejected_answers),
                        "id": str(question_revision_id),
                        "author_id": str(question_author_ids[0]) if question_author_ids else None,
                        "author_ids": [str(value) for value in question_author_ids],
                    }
                    for question, (question_revision_id, question_author_ids) in zip(
                        theme.questions,
                        questions_by_theme.get(row.id, ()),
                        strict=True,
                    )
                ],
            }
            for row, theme in zip(
                theme_rows, stored.packet.themes, strict=True
            )
        ]

    async def play_unit_count(self, session: AsyncSession, packet_version_id: UUID) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ThemeRevisionRecord)
                .where(ThemeRevisionRecord.packet_version_id == packet_version_id)
            )
            or 0
        )

    async def available_play_units(
        self,
        session: AsyncSession,
        selections: Sequence[PacketSelection],
        player_ids: Sequence[UUID],
    ) -> list[PlayUnit]:
        if not selections:
            return []
        packet_order = {
            selection.packet_version_id: selection.selection_order for selection in selections
        }
        theme_rows = (
            await session.execute(
                select(
                    ThemeRevisionRecord.id,
                    ThemeRevisionRecord.theme_id,
                    ThemeRevisionRecord.packet_version_id,
                    ThemeRevisionRecord.position,
                    PacketVersionRecord.packet_id,
                )
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == ThemeRevisionRecord.packet_version_id,
                )
                .where(ThemeRevisionRecord.packet_version_id.in_(packet_order))
                .order_by(
                    ThemeRevisionRecord.packet_version_id,
                    ThemeRevisionRecord.position,
                )
            )
        ).all()
        if not theme_rows:
            return []
        blocked: set[tuple[str, UUID]] = set()
        blocked_packets: set[UUID] = set()
        if player_ids:
            blocked = set(
                (
                    await session.execute(
                        select(
                            PlayerExposureClaimRecord.claim_namespace,
                            PlayerExposureClaimRecord.claim_id,
                        ).where(
                            PlayerExposureClaimRecord.player_id.in_(player_ids),
                            PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
                            # Match logical identities across corrections, not claim provenance.
                            or_(
                                (PlayerExposureClaimRecord.claim_namespace == "theme")
                                & PlayerExposureClaimRecord.claim_id.in_(
                                    select(ThemeRevisionRecord.theme_id).where(
                                        ThemeRevisionRecord.packet_version_id.in_(packet_order)
                                    )
                                ),
                                (PlayerExposureClaimRecord.claim_namespace == "question")
                                & PlayerExposureClaimRecord.claim_id.in_(
                                    select(QuestionRevisionRecord.question_id)
                                    .join(
                                        PacketQuestionRecord,
                                        PacketQuestionRecord.question_revision_id
                                        == QuestionRevisionRecord.id,
                                    )
                                    .where(PacketQuestionRecord.packet_version_id.in_(packet_order))
                                ),
                            ),
                        )
                    )
                ).tuples()
            )
            # A player-declared block treats every theme and question of the
            # packet as burnt without persisting exposure claims.
            blocked_packets = set(
                (
                    await session.execute(
                        select(PlayerPacketBlockRecord.packet_id).where(
                            PlayerPacketBlockRecord.player_id.in_(player_ids),
                            PlayerPacketBlockRecord.packet_id.in_(
                                {theme.packet_id for theme in theme_rows}
                            ),
                        )
                    )
                ).scalars()
            )
        themes = [
            theme for theme in theme_rows
            if theme.packet_id not in blocked_packets and ("theme", theme.theme_id) not in blocked
        ]
        if not themes:
            return []
        question_rows = (
            await session.execute(
                select(
                    PacketQuestionRecord.theme_revision_id,
                    PacketQuestionRecord.question_revision_id,
                    PacketQuestionRecord.value,
                    QuestionRevisionRecord.question_id,
                )
                .join(
                    QuestionRevisionRecord,
                    QuestionRevisionRecord.id == PacketQuestionRecord.question_revision_id,
                )
                .where(PacketQuestionRecord.packet_version_id.in_(
                    {theme.packet_version_id for theme in themes}
                ))
                .order_by(PacketQuestionRecord.theme_revision_id, PacketQuestionRecord.position)
            )
        ).all()
        questions_by_theme: dict[UUID, list[tuple[UUID, int, UUID]]] = {}
        for theme_id, revision_id, value, question_id in question_rows:
            questions_by_theme.setdefault(theme_id, []).append((revision_id, value, question_id))
        units: list[PlayUnit] = []
        for theme in themes:
            questions = questions_by_theme.get(theme.id, [])
            claims = (ExposureClaim("theme", theme.theme_id),) + tuple(
                ExposureClaim("question", question_id) for _, _, question_id in questions
            )
            if any((claim.namespace, claim.identity) in blocked for claim in claims):
                continue
            units.append(
                PlayUnit(
                    kind="si_theme",
                    logical_id=theme.theme_id,
                    revision_id=theme.id,
                    packet_version_id=theme.packet_version_id,
                    packet_order=packet_order[theme.packet_version_id],
                    position=theme.position,
                    question_revision_ids=tuple(
                        revision_id for revision_id, _, _ in questions
                    ),
                    claims=claims,
                    metadata={"question_values": [value for _, value, _ in questions]},
                )
            )
        return units


class RulesetContentRegistry:
    def __init__(self, adapters: Iterable[RulesetContentAdapter] = ()) -> None:
        self._adapters: dict[tuple[str, int], RulesetContentAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: RulesetContentAdapter) -> None:
        identity = (adapter.key, adapter.version)
        if identity in self._adapters:
            raise ValueError(
                f"Ruleset content adapter {adapter.key} version {adapter.version} is registered"
            )
        self._adapters[identity] = adapter

    def get(self, key: str, version: int) -> RulesetContentAdapter:
        try:
            return self._adapters[(key, version)]
        except KeyError as error:
            raise LookupError(f"Unsupported ruleset content: {key} version {version}") from error


DEFAULT_CONTENT_ADAPTERS = RulesetContentRegistry((SIContentAdapter(),))
