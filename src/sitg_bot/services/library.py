from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.author_exposure import burn_author_content
from sitg_bot.services.moderation import _require_administrator
from sitg_bot.services.reliable_delivery import TransactionalOutbox
from sitg_bot.services.ruleset_content import (
    DEFAULT_CONTENT_ADAPTERS,
    PacketSelection,
    SIContentAdapter,
)
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerExposureClaimRecord,
    PlayerRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPacketEntitlementRecord,
    TournamentRecord,
)


class PacketLibraryService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.tournaments = TournamentService(database)

    async def _assignments(
        self, session: AsyncSession, player_id: UUID, *,
        lock: bool = False, version_id: UUID | None = None,
    ) -> list[tuple[TournamentPacketAssignmentRecord, PacketVersionRecord]]:
        # Only visit tournaments in which the caller has a role.
        managed = select(TournamentManagerRecord.tournament_id).where(
            TournamentManagerRecord.player_id == player_id,
            TournamentManagerRecord.revoked_at.is_(None),
        )
        joined = select(TournamentMembershipRecord.tournament_id).where(
            TournamentMembershipRecord.player_id == player_id,
            TournamentMembershipRecord.status == "active",
        )
        query = select(TournamentPacketAssignmentRecord).where(
            TournamentPacketAssignmentRecord.tournament_id.in_(managed.union(joined)),
            TournamentPacketAssignmentRecord.status == "active",
        ).order_by(TournamentPacketAssignmentRecord.id)
        if version_id is not None:
            query = query.where(TournamentPacketAssignmentRecord.packet_id.in_(
                select(PacketVersionRecord.packet_id).where(PacketVersionRecord.id == version_id)
            ))
        if lock:
            # Match manager mutation ordering: tournament before its assignments.
            await session.scalars(select(TournamentRecord).where(TournamentRecord.id.in_(
                query.with_only_columns(TournamentPacketAssignmentRecord.tournament_id)
                .order_by(None)
            )).order_by(TournamentRecord.id).with_for_update())
            query = query.with_for_update()
        result = []
        for assignment in await session.scalars(query):
            if lock:
                for model in (TournamentManagerRecord, TournamentMembershipRecord):
                    await session.get(
                        model, (assignment.tournament_id, player_id), with_for_update=True
                    )
                await session.get(
                    TournamentPacketEntitlementRecord, (assignment.id, player_id),
                    with_for_update=True,
                )
            if not await self.tournaments.has_assignment_access(
                session, assignment, player_id, "content_visible"
            ):
                continue
            version_query = select(PacketVersionRecord).where(
                PacketVersionRecord.packet_id == assignment.packet_id,
                PacketVersionRecord.state == "published",
            )
            if assignment.adopted_version_id:
                version_query = version_query.where(
                    PacketVersionRecord.id == assignment.adopted_version_id
                )
            version_query = version_query.order_by(
                PacketVersionRecord.version_number.desc()
            ).limit(1)
            if lock:
                version_query = version_query.with_for_update()
            version = await session.scalar(version_query)
            if version:
                result.append((assignment, version))
        return result

    async def list_packets(self, player_id: UUID) -> dict[str, object]:
        async with self.database.sessions() as session:
            cards = {}
            for _, version in await self._assignments(session, player_id):
                if version.id not in cards:
                    author_ids = select(ThemeRevisionRecord.author_id).where(
                        ThemeRevisionRecord.packet_version_id == version.id
                    ).union(
                        select(QuestionRevisionRecord.author_id).join(
                            PacketQuestionRecord,
                            PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                        ).where(PacketQuestionRecord.packet_version_id == version.id)
                    )
                    authors = list(await session.scalars(
                        select(AuthorRecord.display_name).where(
                            AuthorRecord.id.in_(author_ids)
                            | (AuthorRecord.id == version.lead_author_id)
                        ).order_by(AuthorRecord.display_name)
                    ))
                    lead = (
                        await session.get(AuthorRecord, version.lead_author_id)
                        if version.lead_author_id else None
                    )
                    cards[version.id] = {
                        "packet_id": str(version.packet_id), "version_id": str(version.id),
                        "name": version.name, "year": version.year,
                        "published_at": version.published_at.isoformat(),
                        "lead_author": lead.display_name if lead else "",
                        "authors": authors, "tournaments": [],
                    }
            for card in cards.values():
                visible = select(TournamentRecord).join(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.packet_id == UUID(card["packet_id"]),
                    TournamentPacketAssignmentRecord.status == "active",
                    or_(
                        TournamentRecord.id.in_(select(TournamentManagerRecord.tournament_id).where(
                            TournamentManagerRecord.player_id == player_id,
                            TournamentManagerRecord.revoked_at.is_(None),
                        )),
                        (TournamentRecord.finalized_at.is_not(None))
                        & (TournamentRecord.status != "draft")
                        & or_(
                            TournamentRecord.visibility == "public",
                            TournamentRecord.id.in_(select(TournamentMembershipRecord.tournament_id)
                                .where(TournamentMembershipRecord.player_id == player_id)),
                        ),
                    ),
                ).order_by(TournamentRecord.name, TournamentRecord.id)
                for tournament in await session.scalars(visible):
                    card["tournaments"].append({
                        "id": str(tournament.id), "name": tournament.name, "slug": tournament.slug,
                        "role": "manager" if await self.tournaments._is_manager(
                            session, tournament.id, player_id
                        ) else "player",
                    })
            return {"items": sorted(
                cards.values(), key=lambda card: (card["name"], card["version_id"])
            )}

    async def access(
        self, player_id: UUID, version_id: UUID, *,
        confirm: bool = False, download: bool = False, request_key: str,
        administrator: bool = False,
    ) -> dict[str, object]:
        async with self.database.transaction() as session:
            if administrator:
                await _require_administrator(session, player_id)
            candidates = [
                (assignment, version)
                for assignment, version in ([] if administrator else await self._assignments(
                    session, player_id, lock=True, version_id=version_id
                ))
                if version.id == version_id
            ]
            authorized = None
            for assignment, version in candidates:
                if await self.tournaments.can_read_assignment(
                    session, assignment, version, player_id
                ):
                    authorized = (assignment, version)
                    break
            if administrator:
                version = await session.get(PacketVersionRecord, version_id)
                if version is None:
                    raise LookupError("Packet version not found")
                adapter = SIContentAdapter()
            elif authorized is None:
                raise PermissionError("Packet release and read prerequisites are required")
            else:
                assignment, version = authorized
                context = await self.tournaments.context(session, assignment.tournament_id)
                adapter = DEFAULT_CONTENT_ADAPTERS.get(context.ruleset_key, context.ruleset_version)
            units = await adapter.available_play_units(
                session, [PacketSelection(version_id, 0)], []
            )
            claims = {(claim.namespace, claim.identity) for unit in units for claim in unit.claims}
            query = select(PlayerExposureClaimRecord).where(
                PlayerExposureClaimRecord.player_id == player_id,
                tuple_(
                    PlayerExposureClaimRecord.claim_namespace, PlayerExposureClaimRecord.claim_id
                ).in_(sorted(claims)),
                PlayerExposureClaimRecord.state.in_(("reserved", "burnt")),
            )
            query = query.order_by(
                PlayerExposureClaimRecord.claim_namespace, PlayerExposureClaimRecord.claim_id
            )
            existing = list(await session.scalars(query.with_for_update()))
            if not administrator and any(claim.state == "reserved" for claim in existing):
                raise PermissionError("Packet content is reserved for an undisclosed game")
            burnt = {(claim.claim_namespace, claim.claim_id) for claim in existing
                     if claim.state == "burnt"}
            fresh_count = sum(
                any((claim.namespace, claim.identity) not in burnt for claim in unit.claims)
                for unit in units
            )
            if fresh_count and not confirm:
                return {"confirmation_required": True, "fresh_unit_count": fresh_count}
            if administrator:
                await burn_author_content(session, version_id=version_id, player_ids=[player_id])
            pages = await adapter.library_pages(session, version_id)
            for namespace, claim_id in sorted(claims - burnt):
                await session.execute(insert(PlayerExposureClaimRecord).values(
                    player_id=player_id, packet_version_id=version_id,
                    claim_namespace=namespace, claim_id=claim_id,
                    state="burnt", burnt_at=datetime.now(UTC),
                ).on_conflict_do_nothing(
                    index_elements=["player_id", "claim_namespace", "claim_id"],
                    index_where=text("state IN ('reserved', 'burnt')"),
                ))
            # A concurrent game may reserve or release claims during insertion.
            current = await session.scalars(
                query.with_for_update().execution_options(populate_existing=True)
            )
            persisted_burns = {
                (claim.claim_namespace, claim.claim_id)
                for claim in current if claim.state == "burnt"
            }
            if persisted_burns != claims:
                raise PermissionError(
                    "Packet exposure changed during reading; retry after the game"
                )
            result = {"confirmation_required": False, "name": version.name, "pages": pages}
            if download:
                player = await session.get(PlayerRecord, player_id)
                if player is None or player.telegram_user_id is None:
                    raise PermissionError("Telegram account is required for downloads")
                await TransactionalOutbox.enqueue(
                    session, topic="telegram.library.document",
                    deduplication_key=f"library:{player_id}:{request_key}",
                    partition_key=f"telegram:chat:{player.telegram_user_id}",
                    payload={"recipient_telegram_user_id": player.telegram_user_id,
                             "name": version.name, "pages": pages},
                )
                return {"confirmation_required": False, "queued": True}
            return result
