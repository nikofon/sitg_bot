from uuid import UUID

from sqlalchemy import select

from sitg_bot.services.classic import ClassicService
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.ruleset_content import DEFAULT_CONTENT_ADAPTERS, PacketSelection
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ClassicRoundRecord,
    PacketVersionRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentRecord,
)


class PacketAvailabilityService:
    """Reconcile new play opportunities into durable, per-assignment notices."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.tournaments = TournamentService(database)

    async def reconcile(self) -> None:
        async with self.database.sessions() as session:
            tournament_ids = list(
                await session.scalars(
                    select(TournamentRecord.id)
                    .where(
                        TournamentRecord.status == "active",
                        TournamentRecord.moderation_status == "normal",
                        TournamentRecord.finalized_at.is_not(None),
                        TournamentRecord.actual_starts_at.is_not(None),
                    )
                    .order_by(TournamentRecord.id)
                )
            )
        for tournament_id in tournament_ids:
            async with self.database.transaction() as session:
                # Serialize recipients and rule edits with other tournament mutations.
                context = await self.tournaments.context(session, tournament_id, lock=True)
                if not context.assembly_open:
                    continue
                tournament = await session.get(TournamentRecord, tournament_id)
                if tournament.moderation_status != "normal":
                    continue
                players = list(
                    await session.scalars(
                        select(PlayerRecord)
                        .join(
                            TournamentMembershipRecord,
                            TournamentMembershipRecord.player_id == PlayerRecord.id,
                        )
                        .where(
                            TournamentMembershipRecord.tournament_id == tournament_id,
                            TournamentMembershipRecord.status == "active",
                            PlayerRecord.status == "active",
                        )
                        .order_by(PlayerRecord.id)
                    )
                )
                assignments = list(
                    await session.scalars(
                        select(TournamentPacketAssignmentRecord)
                        .where(
                            TournamentPacketAssignmentRecord.tournament_id == tournament_id,
                            TournamentPacketAssignmentRecord.status == "active",
                        )
                        .order_by(TournamentPacketAssignmentRecord.id)
                    )
                )
                adapter = DEFAULT_CONTENT_ADAPTERS.get(context.ruleset_key, context.ruleset_version)
                for assignment in assignments:
                    version = await session.scalar(
                        select(PacketVersionRecord)
                        .where(
                            PacketVersionRecord.packet_id == assignment.packet_id,
                            PacketVersionRecord.state == "published",
                            *(
                                [PacketVersionRecord.id == assignment.adopted_version_id]
                                if assignment.adopted_version_id
                                else []
                            ),
                        )
                        .order_by(PacketVersionRecord.version_number.desc())
                        .limit(1)
                    )
                    if version is None:
                        continue
                    for player in players:
                        key = f"packet-available:{assignment.id}:{player.id}"
                        if await session.scalar(
                            select(PlayerNotificationRecord.id).where(
                                PlayerNotificationRecord.deduplication_key == key,
                            )
                        ):
                            continue
                        if await self.tournaments._is_manager(session, tournament_id, player.id):
                            continue
                        if not await self.tournaments.has_assignment_access(
                            session, assignment, player.id, "playable"
                        ) or not await self.tournaments.has_assignment_access(
                            session, assignment, player.id, "discoverable"
                        ):
                            continue
                        units = await adapter.available_play_units(
                            session,
                            [PacketSelection(version.id, 1)],
                            [player.id],
                        )
                        values = context.settings.to_dict().get("question_values")
                        if not any(
                            list(unit.metadata.get("question_values", ())) == values
                            for unit in units
                        ):
                            continue
                        payload = {
                            "tournament_id": str(tournament_id),
                            "tournament_name": tournament.name,
                            "packet_id": str(assignment.packet_id),
                            "packet_name": version.name,
                        }
                        if context.type_key == "classic":
                            try:
                                match = await ClassicService.prescribed_match(
                                    session,
                                    tournament_id,
                                    assignment.id,
                                    [player.id],
                                    exact=False,
                                )
                            except ValueError:
                                continue
                            round_record = await session.get(ClassicRoundRecord, match.round_id)
                            opponents = []
                            for seat in match.seats:
                                if seat == str(player.id):
                                    continue
                                if seat.startswith("chair:"):
                                    opponents.append({"name": "", "chair": True})
                                else:
                                    opponent = await session.get(PlayerRecord, UUID(seat))
                                    opponents.append(
                                        {"name": opponent.public_nickname or "—", "chair": False}
                                    )
                            payload.update(
                                {
                                    "opponents": opponents,
                                    "match_id": str(match.id),
                                    "start_deadline": round_record.start_deadline.isoformat()
                                    if round_record.start_deadline
                                    else None,
                                }
                            )
                        await NotificationWriter.create_for_player(
                            session,
                            recipient_player_id=player.id,
                            audience="player",
                            kind="packet.available",
                            deduplication_key=key,
                            payload=payload,
                        )
