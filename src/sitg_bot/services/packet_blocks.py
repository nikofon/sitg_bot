"""Player packet blocks: per-player burnt-like treatment and tournament listings."""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.packet_catalogue import background_packet_batch, matches_packet
from sitg_bot.services.ruleset_content import (
    DEFAULT_CONTENT_ADAPTERS,
    PacketSelection,
)
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.authorship import packet_author_ids
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    LogicalPacketRecord,
    PacketVersionRecord,
    PlayerPacketBlockRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentRecord,
)


class PacketBlockService:
    """Lists tournament packets for players and manages their packet blocks.

    A block is stored per player and logical packet, so blocking a packet
    assigned to several tournaments blocks it everywhere for that player.
    Fresh-content computation treats blocked packets as fully burnt, while
    library viewing keeps burning content normally.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.tournaments = TournamentService(database)
        self.content_adapters = DEFAULT_CONTENT_ADAPTERS

    async def list_tournament_packets(
        self, player_id: UUID, tournament_id: UUID, *, include_items: bool = True,
        limit: int | None = None, cursor: UUID | None = None,
        packet_id: UUID | None = None, filters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if limit is not None and packet_id is None:
            async with background_packet_batch():
                return await self._packet_page(
                    player_id, tournament_id, include_items=include_items, limit=limit,
                    cursor=cursor, filters=filters,
                )
        return await self._packet_page(
            player_id, tournament_id, include_items=include_items, limit=limit,
            cursor=cursor, packet_id=packet_id, filters=filters,
        )

    async def _packet_page(
        self, player_id: UUID, tournament_id: UUID, *, include_items: bool,
        limit: int | None, cursor: UUID | None, filters: dict[str, object] | None,
        packet_id: UUID | None = None,
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            tournament = await session.get(TournamentRecord, tournament_id)
            membership = await session.get(
                TournamentMembershipRecord, (tournament_id, player_id)
            )
            manager = await session.get(TournamentManagerRecord, (tournament_id, player_id))
            if (
                tournament is None
                or membership is None
                or membership.status != "active"
                or tournament.finalized_at is None
                or (manager is not None and manager.revoked_at is None)
            ):
                raise PermissionError("Active participant membership is required")
            if not include_items:
                return {"tournament_id": str(tournament_id), "tournament_name": tournament.name,
                        "items": [], "next_cursor": None}
            context = await self.tournaments.context(session, tournament_id)
            adapter = self.content_adapters.get(context.ruleset_key, context.ruleset_version)
            blocked = set(
                await session.scalars(
                    select(PlayerPacketBlockRecord.packet_id).where(
                        PlayerPacketBlockRecord.player_id == player_id
                    )
                )
            )
            assignments = list(
                await session.scalars(
                    select(TournamentPacketAssignmentRecord)
                    .where(
                        TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                        TournamentPacketAssignmentRecord.status == "active",
                        *([TournamentPacketAssignmentRecord.id > cursor] if cursor else []),
                        *([TournamentPacketAssignmentRecord.packet_id == packet_id]
                          if packet_id else []),
                    )
                    .order_by(TournamentPacketAssignmentRecord.id)
                )
            )
            items: list[dict[str, object]] = []
            candidates = []
            next_cursor = None
            for assignment in assignments:
                if not await self.tournaments.has_assignment_access(
                    session, assignment, player_id, "discoverable"
                ):
                    continue
                version = await self._assignment_version(session, assignment)
                if version is None:
                    continue
                authors = await self._authors(session, version)
                if not matches_packet(
                    version, [str(author["display_name"]) for author in authors], filters or {},
                ):
                    continue
                if limit is not None and len(candidates) == limit:
                    next_cursor = str(candidates[-1][0].id)
                    break
                candidates.append((assignment, version, authors))
            available = await adapter.available_play_units(
                session, [PacketSelection(version.id, 1) for _, version, _ in candidates],
                [player_id],
            )
            fresh_by_version = Counter(unit.packet_version_id for unit in available)
            for assignment, version, authors in candidates:
                total = await adapter.play_unit_count(session, version.id)
                items.append(
                    {
                        "packet_id": str(assignment.packet_id),
                        "version_id": str(version.id),
                        "name": version.name,
                        "year": version.year,
                        "published_at": version.published_at,
                        "lead_author": next(
                            (
                                author["display_name"]
                                for author in authors
                                if author["id"] == version.lead_author_id
                            ),
                            None,
                        ),
                        "authors": tuple(
                            author["display_name"]
                            for author in authors
                            if author["id"] != version.lead_author_id
                        ),
                        "total_play_unit_count": total,
                        "fresh_play_unit_count": fresh_by_version[version.id],
                        "playable": await self.tournaments.has_assignment_access(
                            session, assignment, player_id, "playable"
                        ),
                        "blocked": assignment.packet_id in blocked,
                        "library_viewable": await self.tournaments.can_read_assignment(
                            session, assignment, version, player_id
                        ),
                    }
                )
            return {
                "tournament_id": str(tournament_id),
                "tournament_name": tournament.name,
                "items": items,
                "next_cursor": next_cursor,
            }

    async def set_blocked(
        self, player_id: UUID, packet_id: UUID, *, blocked: bool
    ) -> dict[str, object]:
        async with self.database.transaction() as session:
            packet = await session.get(LogicalPacketRecord, packet_id)
            if packet is None or packet.retired_at is not None:
                raise LookupError("Packet not found")
            record = await session.get(PlayerPacketBlockRecord, (player_id, packet_id))
            if blocked:
                assignments = list(
                    await session.scalars(
                        select(TournamentPacketAssignmentRecord).where(
                            TournamentPacketAssignmentRecord.packet_id == packet_id,
                            TournamentPacketAssignmentRecord.status == "active",
                        )
                    )
                )
                accessible = False
                for assignment in assignments:
                    if await self.tournaments.has_assignment_access(
                        session, assignment, player_id, "discoverable"
                    ):
                        accessible = True
                        break
                if not accessible:
                    raise PermissionError(
                        "Packet access through an active tournament is required"
                    )
                if record is None:
                    session.add(
                        PlayerPacketBlockRecord(player_id=player_id, packet_id=packet_id)
                    )
            elif record is not None:
                await session.delete(record)
            return {"packet_id": str(packet_id), "blocked": blocked}

    @staticmethod
    async def _assignment_version(
        session: AsyncSession, assignment: TournamentPacketAssignmentRecord
    ) -> PacketVersionRecord | None:
        if assignment.adopted_version_id is not None:
            return await session.get(PacketVersionRecord, assignment.adopted_version_id)
        return await session.scalar(
            select(PacketVersionRecord)
            .where(
                PacketVersionRecord.packet_id == assignment.packet_id,
                PacketVersionRecord.state == "published",
            )
            .order_by(PacketVersionRecord.version_number.desc())
            .limit(1)
        )

    @staticmethod
    async def _authors(
        session: AsyncSession, version: PacketVersionRecord
    ) -> list[dict[str, object]]:
        author_ids = packet_author_ids(version.id)
        rows = list(
            await session.scalars(
                select(AuthorRecord).where(
                    or_(AuthorRecord.id.in_(author_ids), AuthorRecord.id == version.lead_author_id)
                ).order_by(AuthorRecord.display_name, AuthorRecord.id)
            )
        )
        return [{"id": author.id, "display_name": author.display_name} for author in rows]
