"""Private tester resolution and the administrator-approved player link workflow."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.author_exposure import burn_author_content, lock_author_exposure
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PacketVersionRecord,
    PacketVersionTesterRecord,
    PlayerRecord,
    PlayerTesterLinkRecord,
    PlayerTesterLinkRequestRecord,
    TesterRecord,
    TournamentPacketAssignmentRecord,
)


def tester_summary(tester: TesterRecord) -> dict:
    return {"tester_id": str(tester.id), "display_name": tester.display_name}


async def resolve_testers(
    session: AsyncSession,
    names: tuple[str, ...],
    bindings: dict[str, UUID],
) -> list[TesterRecord]:
    from sitg_bot.services.packets import PacketAdminService

    if set(bindings) - {str(i) for i in range(len(names))}:
        raise ValueError("Tester association must refer to a tester in the packet")
    resolved = []
    # Serialize default name registration; explicit new profiles preserve namesakes.
    for name in sorted(set(names)):
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    AuthorLinkService._pair_lock_key(
                        UUID(int=0),
                        UUID(
                            bytes=hashlib.sha256(
                                " ".join(sorted(name.casefold().split())).encode()
                            ).digest()[:16]
                        ),
                    )
                )
            )
        )
    for i, name in enumerate(names):
        if str(i) in bindings:
            tester = await session.get(TesterRecord, bindings[str(i)])
            if tester is None:
                raise LookupError("Associated tester not found")
        else:
            collapsed = func.regexp_replace(func.lower(TesterRecord.display_name), r"\s+", " ", "g")
            tester = await session.scalar(
                select(TesterRecord)
                .where(collapsed.in_(PacketAdminService._name_match_forms(name)))
                .order_by(func.lower(TesterRecord.display_name), TesterRecord.id)
                .limit(1)
            )
            if tester is None:
                tester = TesterRecord(display_name=name)
                session.add(tester)
                await session.flush()
        resolved.append(tester)
    return resolved


def store_testers(session: AsyncSession, version_id: UUID, testers) -> None:
    for position, tester_id in enumerate(dict.fromkeys(t.id for t in testers)):
        session.add(
            PacketVersionTesterRecord(
                packet_version_id=version_id,
                tester_id=tester_id,
                position=position,
            )
        )


async def version_testers(session: AsyncSession, version_id: UUID) -> list[TesterRecord]:
    return list(
        await session.scalars(
            select(TesterRecord)
            .join(
                PacketVersionTesterRecord,
                PacketVersionTesterRecord.tester_id == TesterRecord.id,
            )
            .where(PacketVersionTesterRecord.packet_version_id == version_id)
            .order_by(PacketVersionTesterRecord.position)
        )
    )


class TesterService:
    def __init__(self, database: Database):
        self.database = database

    async def search(
        self,
        actor_id: UUID,
        query: str = "",
        *,
        draft_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict:
        if not 1 <= limit <= 100 or len(query) > 300:
            raise ValueError("Invalid tester search bounds")
        if draft_id is not None:
            from sitg_bot.services.packets import PacketAdminService

            await PacketAdminService(self.database)._authorized_draft(
                draft_id, actor_id, content_access=True
            )
        elif not query.strip():
            raise ValueError("A tester name is required; no public tester directory exists")
        name, identity = AuthorLinkService._decode_name_cursor(cursor)
        async with self.database.transaction() as session:
            await self._player(session, actor_id)
            lowered = func.lower(TesterRecord.display_name)
            statement = select(TesterRecord)
            if query.strip():
                statement = statement.where(
                    lowered.like(
                        f"%{AuthorLinkService._escape_like(query.strip().lower())}%", escape="\\"
                    )
                )
            if identity is not None:
                statement = statement.where(
                    (lowered > name) | ((lowered == name) & (TesterRecord.id > identity))
                )
            rows = list(
                await session.scalars(statement.order_by(lowered, TesterRecord.id).limit(limit + 1))
            )
            visible = rows[:limit]
            return {
                "items": [
                    {**tester_summary(t), "packets": await self._packets(session, t.id, actor_id)}
                    for t in visible
                ],
                "next_cursor": AuthorLinkService._encode_name_cursor(visible[-1])
                if len(rows) > limit
                else None,
            }

    async def mine(self, actor_id: UUID) -> dict:
        async with self.database.transaction() as session:
            await self._player(session, actor_id)
            testers = await session.scalars(
                select(TesterRecord)
                .join(
                    PlayerTesterLinkRecord,
                    PlayerTesterLinkRecord.tester_id == TesterRecord.id,
                )
                .where(PlayerTesterLinkRecord.player_id == actor_id)
                .order_by(func.lower(TesterRecord.display_name), TesterRecord.id)
            )
            return {
                "items": [
                    {**tester_summary(t), "packets": await self._packets(session, t.id, actor_id)}
                    for t in testers
                ]
            }

    @staticmethod
    async def _packets(
        session: AsyncSession,
        tester_id: UUID,
        actor_id: UUID,
    ) -> list[dict[str, str]]:
        from sitg_bot.services.tournaments import TournamentService

        packets = (
            await session.execute(
                select(PacketVersionRecord, TournamentPacketAssignmentRecord)
                .join(
                    TournamentPacketAssignmentRecord,
                    TournamentPacketAssignmentRecord.packet_id == PacketVersionRecord.packet_id,
                )
                .where(
                    PacketVersionRecord.packet_id.in_(
                        select(PacketVersionRecord.packet_id)
                        .join(
                            PacketVersionTesterRecord,
                            PacketVersionTesterRecord.packet_version_id == PacketVersionRecord.id,
                        )
                        .where(PacketVersionTesterRecord.tester_id == tester_id)
                        .correlate(None)
                    ),
                    PacketVersionRecord.state == "published",
                    TournamentPacketAssignmentRecord.status == "active",
                )
                .order_by(PacketVersionRecord.version_number.desc())
            )
        ).all()
        seen = set()
        tested = []
        for version, assignment in packets:
            if version.packet_id in seen:
                continue
            adopted = assignment.adopted_version_id or await session.scalar(
                select(PacketVersionRecord.id)
                .where(
                    PacketVersionRecord.packet_id == version.packet_id,
                    PacketVersionRecord.state == "published",
                )
                .order_by(PacketVersionRecord.version_number.desc())
                .limit(1)
            )
            if version.id != adopted or not await TournamentService.has_assignment_access(
                session, assignment, actor_id, "discoverable"
            ):
                continue
            tested.append({"packet_id": str(version.packet_id), "name": version.name})
            seen.add(version.packet_id)
        return tested

    @staticmethod
    async def _player(session, player_id):
        player = await session.get(PlayerRecord, player_id)
        if player is None or player.status != "active" or player.public_nickname is None:
            raise LookupError("Active player not found")
        return player

    async def create_request(self, player_id: UUID, tester_id: UUID, *, note=None) -> dict:
        note = AuthorLinkService._note(note, label="Request note")
        async with self.database.transaction() as session:
            # Identity merges serialize with request creation before acquiring the pair lock.
            await lock_author_exposure(session)
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        AuthorLinkService._pair_lock_key(player_id, tester_id)
                    )
                )
            )
            player = await self._player(session, player_id)
            tester = await session.get(TesterRecord, tester_id)
            if tester is None:
                raise LookupError("Tester not found")
            if await session.get(PlayerTesterLinkRecord, (player_id, tester_id)):
                raise ValueError("Player is already linked to this tester")
            request = await session.scalar(
                select(PlayerTesterLinkRequestRecord)
                .where(
                    PlayerTesterLinkRequestRecord.player_id == player_id,
                    PlayerTesterLinkRequestRecord.tester_id == tester_id,
                    PlayerTesterLinkRequestRecord.status == "pending",
                )
                .with_for_update()
            )
            if request is None:
                request = PlayerTesterLinkRequestRecord(
                    player_id=player_id, tester_id=tester_id, request_note=note, status="pending"
                )
                session.add(request)
                await session.flush()
                await NotificationWriter.create_for_administrators(
                    session,
                    kind="tester_link.request_created",
                    deduplication_key=f"tester-link-request:{request.id}:created",
                    payload={
                        "request_id": str(request.id),
                        "player_id": str(player_id),
                        "player_nickname": player.public_nickname,
                        "tester_id": str(tester_id),
                        "tester_name": tester.display_name,
                    },
                )
            return await self._snapshot(session, request)

    async def cancel_request(self, request_id: UUID, player_id: UUID) -> dict:
        async with self.database.transaction() as session:
            request = await session.get(
                PlayerTesterLinkRequestRecord, request_id, with_for_update=True
            )
            if request is None or request.player_id != player_id:
                raise LookupError("Tester link request not found")
            if request.status == "cancelled":
                return await self._snapshot(session, request)
            if request.status != "pending":
                raise ValueError("Only a pending tester link request can be cancelled")
            request.status = "cancelled"
            request.cancelled_at = datetime.now(UTC)
            await session.flush()
            return await self._snapshot(session, request)

    async def requests(self, actor_id: UUID, *, admin=False, cursor=None, limit=20) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid tester request limit")
        created, identity = AuthorLinkService._decode_time_cursor(cursor)
        record = PlayerTesterLinkRequestRecord
        async with self.database.transaction() as session:
            if admin:
                await AuthorLinkService._require_administrator(session, actor_id)
            else:
                await self._player(session, actor_id)
            query = select(record).where(
                record.status == "pending" if admin else record.player_id == actor_id
            )
            if identity:
                query = query.where(
                    (record.created_at > created)
                    | ((record.created_at == created) & (record.id > identity))
                )
            rows = list(
                await session.scalars(query.order_by(record.created_at, record.id).limit(limit + 1))
            )
            return {
                "items": [await self._snapshot(session, r) for r in rows[:limit]],
                "next_cursor": AuthorLinkService._encode_time_cursor(rows[limit - 1])
                if len(rows) > limit
                else None,
            }

    async def decide_request(
        self, request_id: UUID, actor_id: UUID, *, approve: bool, note=None
    ) -> dict:
        note = AuthorLinkService._note(note, label="Decision note")
        status = "approved" if approve else "rejected"
        async with self.database.transaction() as session:
            await lock_author_exposure(session)
            await AuthorLinkService._require_administrator(session, actor_id)
            request = await session.get(
                PlayerTesterLinkRequestRecord, request_id, with_for_update=True
            )
            if request is None:
                raise LookupError("Tester link request not found")
            if request.status == status:
                return await self._snapshot(session, request)
            if request.status != "pending":
                raise ValueError("Tester link request is already closed")
            now = datetime.now(UTC)
            if approve:
                session.add(
                    PlayerTesterLinkRecord(
                        player_id=request.player_id,
                        tester_id=request.tester_id,
                        approved_request_id=request.id,
                        approved_by_id=actor_id,
                        approved_at=now,
                    )
                )
                await burn_author_content(session, tester_id=request.tester_id)
            request.status = status
            request.decided_by_id = actor_id
            request.decision_note = note
            request.decided_at = now
            await NotificationWriter.create_for_player(
                session,
                recipient_player_id=request.player_id,
                audience="player",
                kind="tester_link.request_decided",
                deduplication_key=f"tester-link-request:{request.id}:decided",
                payload={
                    "request_id": str(request.id),
                    "tester_id": str(request.tester_id),
                    "status": status,
                },
            )
            await session.flush()
            return await self._snapshot(session, request)

    async def _snapshot(self, session, request) -> dict:
        player = await session.get(PlayerRecord, request.player_id)
        tester = await session.get(TesterRecord, request.tester_id)
        return {
            "request_id": str(request.id),
            "player_id": str(request.player_id),
            "player_nickname": player.public_nickname,
            "tester": tester_summary(tester),
            **{
                field: getattr(request, field)
                for field in (
                    "status",
                    "request_note",
                    "decision_note",
                    "decided_by_id",
                    "created_at",
                    "decided_at",
                    "cancelled_at",
                )
            },
        }
