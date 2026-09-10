from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.game_rulesets import ExposureClaim, PlayUnit
from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PlayerExposureClaimRecord,
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
        return [
            {"title": theme.name, "author": theme.author,
             "questions": [
                 {**asdict(question), "accepted_answers": list(question.accepted_answers)}
                 for question in theme.questions
             ]}
            for theme in stored.packet.themes
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
        themes = list(
            (
                await session.execute(
                    select(ThemeRevisionRecord)
                    .where(ThemeRevisionRecord.packet_version_id.in_(packet_order))
                    .order_by(
                        ThemeRevisionRecord.packet_version_id,
                        ThemeRevisionRecord.position,
                    )
                )
            ).scalars()
        )
        blocked: set[tuple[str, UUID]] = set()
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
                        )
                    )
                ).tuples()
            )
        units: list[PlayUnit] = []
        for theme in themes:
            questions = list(
                (
                    await session.execute(
                        select(PacketQuestionRecord, QuestionRevisionRecord.question_id)
                        .join(
                            QuestionRevisionRecord,
                            QuestionRevisionRecord.id == PacketQuestionRecord.question_revision_id,
                        )
                        .where(
                            PacketQuestionRecord.packet_version_id == theme.packet_version_id,
                            PacketQuestionRecord.theme_revision_id == theme.id,
                        )
                        .order_by(PacketQuestionRecord.position)
                    )
                ).all()
            )
            claims = (ExposureClaim("theme", theme.theme_id),) + tuple(
                ExposureClaim("question", question_id) for _, question_id in questions
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
                        placement.question_revision_id for placement, _ in questions
                    ),
                    claims=claims,
                    metadata={"question_values": [placement.value for placement, _ in questions]},
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
