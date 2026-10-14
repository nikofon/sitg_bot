from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Protocol
from uuid import UUID
from weakref import WeakKeyDictionary

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

LAYOUT_CACHE_LIMIT = 1024
PLAYER_CACHE_LIMIT = 8192


@dataclass
class _ContentCache:
    layouts: OrderedDict = field(default_factory=OrderedDict)
    counts: OrderedDict = field(default_factory=OrderedDict)
    players: OrderedDict = field(default_factory=OrderedDict)
    layout_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class _Layout:
    units: tuple[PlayUnit, ...]
    identities: frozenset[tuple[str, UUID]]


_CACHES: WeakKeyDictionary = WeakKeyDictionary()


def _cache(session: AsyncSession) -> _ContentCache:
    # The engine, rather than a URL or session, isolates separate databases and tests.
    bind = session.get_bind()
    if bind not in _CACHES:
        _CACHES[bind] = _ContentCache()
    return _CACHES[bind]


def _remember(cache: OrderedDict, key, value, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


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
        cache = _cache(session)
        identity = (self.key, self.version, packet_version_id)
        if identity in cache.counts:
            cache.counts.move_to_end(identity)
            return cache.counts[identity]
        async with cache.layout_lock:
            if identity in cache.counts:
                return cache.counts[identity]
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ThemeRevisionRecord)
                    .where(ThemeRevisionRecord.packet_version_id == packet_version_id)
                )
                or 0
            )
            _remember(cache.counts, identity, count, LAYOUT_CACHE_LIMIT)
        return count

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
        # Resolve blocks using only version metadata, before any content or exposure read.
        blocked_versions = set()
        if player_ids:
            blocked_versions = set((await session.execute(
                select(PacketVersionRecord.id)
                .join(PlayerPacketBlockRecord,
                      PlayerPacketBlockRecord.packet_id == PacketVersionRecord.packet_id)
                .where(PacketVersionRecord.id.in_(packet_order),
                       PlayerPacketBlockRecord.player_id.in_(player_ids))
            )).scalars())
        packet_order = {key: value for key, value in packet_order.items()
                        if key not in blocked_versions}
        if not packet_order:
            return []
        cache = _cache(session)
        # Coalesce cold layout loads. Exposure is always read from the caller's transaction.
        layouts = {version_id: cache.layouts[(self.key, self.version, version_id)]
                   for version_id in packet_order
                   if (self.key, self.version, version_id) in cache.layouts}
        # Warm gameplay reads must not wait for unrelated background layout loads.
        if len(layouts) != len(packet_order):
            async with cache.layout_lock:
                missing = [version_id for version_id in packet_order
                           if (self.key, self.version, version_id) not in cache.layouts]
                loaded = await self._load_layouts(session, missing) if missing else {}
                layouts = {version_id: cache.layouts[(self.key, self.version, version_id)]
                           for version_id in packet_order if version_id not in loaded}
                for version_id, layout in loaded.items():
                    identity = (self.key, self.version, version_id)
                    _remember(cache.layouts, identity, layout, LAYOUT_CACHE_LIMIT)
                    _remember(cache.counts, identity, len(layout.units), LAYOUT_CACHE_LIMIT)
                layouts.update(loaded)
        for version_id in layouts:
            identity = (self.key, self.version, version_id)
            if identity in cache.layouts:
                cache.layouts.move_to_end(identity)
        claims = set().union(*(layout.identities for layout in layouts.values()))
        exposure: dict[UUID, set[tuple[str, UUID]]] = {player: set() for player in player_ids}
        if player_ids and claims:
            rows = (await session.execute(
                select(PlayerExposureClaimRecord.player_id,
                       PlayerExposureClaimRecord.claim_namespace,
                       PlayerExposureClaimRecord.claim_id)
                .where(
                    PlayerExposureClaimRecord.player_id.in_(player_ids),
                    PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
                    or_(*[
                        (PlayerExposureClaimRecord.claim_namespace == namespace)
                        & PlayerExposureClaimRecord.claim_id.in_(
                            [identity for kind, identity in claims if kind == namespace]
                        )
                        for namespace in ("theme", "question")
                    ]),
                )
            )).all()
            for player, namespace, identity in rows:
                exposure[player].add((namespace, identity))
        available: list[PlayUnit] = []
        for version_id, layout in layouts.items():
            units, identities = layout.units, layout.identities
            common = {unit.revision_id for unit in units}
            for player in dict.fromkeys(player_ids):
                inputs = frozenset(exposure[player] & identities)
                identity = (self.key, self.version, version_id, player)
                previous = cache.players.get(identity)
                if previous is None or previous[0] != inputs:
                    fresh = self._calculate_available(units, inputs)
                    _remember(cache.players, identity, (inputs, fresh), PLAYER_CACHE_LIMIT)
                else:
                    fresh = previous[1]
                    cache.players.move_to_end(identity)
                common.intersection_update(fresh)
            available.extend(replace(unit, packet_order=packet_order[version_id],
                                     metadata={"question_values": list(
                                         unit.metadata["question_values"]
                                     )})
                             for unit in units if unit.revision_id in common)
        return sorted(available, key=lambda unit: (unit.packet_version_id, unit.position))

    @staticmethod
    def _calculate_available(units, inputs) -> frozenset[UUID]:
        return frozenset(unit.revision_id for unit in units
                         if not any((claim.namespace, claim.identity) in inputs
                                    for claim in unit.claims))

    async def _load_layouts(
        self, session: AsyncSession, version_ids: Sequence[UUID]
    ) -> dict[UUID, _Layout]:
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
                .where(ThemeRevisionRecord.packet_version_id.in_(version_ids))
                .order_by(
                    ThemeRevisionRecord.packet_version_id,
                    ThemeRevisionRecord.position,
                )
            )
        ).all()
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
                    version_ids
                ))
                .order_by(PacketQuestionRecord.theme_revision_id, PacketQuestionRecord.position)
            )
        ).all()
        questions_by_theme: dict[UUID, list[tuple[UUID, int, UUID]]] = {}
        for theme_id, revision_id, value, question_id in question_rows:
            questions_by_theme.setdefault(theme_id, []).append((revision_id, value, question_id))
        layouts: dict[UUID, list[PlayUnit]] = {version_id: [] for version_id in version_ids}
        for theme in theme_rows:
            questions = questions_by_theme.get(theme.id, [])
            claims = (ExposureClaim("theme", theme.theme_id),) + tuple(
                ExposureClaim("question", question_id) for _, _, question_id in questions
            )
            layouts[theme.packet_version_id].append(
                PlayUnit(
                    kind="si_theme",
                    logical_id=theme.theme_id,
                    revision_id=theme.id,
                    packet_version_id=theme.packet_version_id,
                    packet_order=1,
                    position=theme.position,
                    question_revision_ids=tuple(
                        revision_id for revision_id, _, _ in questions
                    ),
                    claims=claims,
                    metadata={"question_values": tuple(value for _, value, _ in questions)},
                )
            )
        return {version_id: _Layout(tuple(units), frozenset(
            (claim.namespace, claim.identity) for unit in units for claim in unit.claims
        )) for version_id, units in layouts.items()}


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
