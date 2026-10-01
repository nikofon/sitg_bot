"""Subscription templates and assignment-time Ladder packet rights."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select

from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.storage.models import (
    PlayerRecord,
    TournamentMembershipRecord,
    TournamentPacketEntitlementRecord,
    TournamentSubscriptionCardRecord,
    TournamentSubscriptionRecord,
    TournamentTypeVersionRecord,
)


async def subscription_snapshot(session, tournament_id: UUID) -> dict:
    cards = (
        await session.scalars(
            select(TournamentSubscriptionCardRecord)
            .where(
                TournamentSubscriptionCardRecord.tournament_id == tournament_id,
            )
            .order_by(
                TournamentSubscriptionCardRecord.created_at, TournamentSubscriptionCardRecord.id
            )
        )
    ).all()
    instances = (
        await session.scalars(
            select(TournamentSubscriptionRecord)
            .join(
                TournamentSubscriptionCardRecord,
            )
            .where(TournamentSubscriptionCardRecord.tournament_id == tournament_id)
            .order_by(
                TournamentSubscriptionRecord.created_at,
                TournamentSubscriptionRecord.id,
            )
        )
    ).all()
    players = (
        await session.execute(
            select(
                PlayerRecord.id,
                PlayerRecord.public_nickname,
                TournamentMembershipRecord.status,
            )
            .join(
                TournamentMembershipRecord,
                TournamentMembershipRecord.player_id == PlayerRecord.id,
            )
            .where(
                TournamentMembershipRecord.tournament_id == tournament_id,
                # Keep former participants with subscriptions available for revocation.
                or_(
                    TournamentMembershipRecord.status == "active",
                    PlayerRecord.id.in_([item.player_id for item in instances]),
                ),
            )
            .order_by(PlayerRecord.public_nickname, PlayerRecord.id)
        )
    ).all()
    return {
        "cards": [
            {
                "id": str(card.id),
                "name": card.name,
                "packet_count": card.packet_count,
                "discoverable": card.discoverable,
                "readable": card.readable,
                "playable": card.playable,
            }
            for card in cards
        ],
        "players": [
            {
                "id": str(player.id),
                "name": player.public_nickname or "—",
                "active": player.status == "active",
            }
            for player in players
        ],
        "instances": [
            {
                "id": str(item.id),
                "card_id": str(item.card_id),
                "player_id": str(item.player_id),
                "remaining_packets": item.remaining_packets,
                "revoked_at": item.revoked_at,
                "assigned_at": item.created_at,
            }
            for item in instances
        ],
    }


class SubscriptionService:
    def __init__(self, tournaments):
        self.tournaments = tournaments

    async def mutate(self, tournament_id, manager_id, *, expected_version, command, values):
        from sitg_bot.services.concurrency import StaleWriteError

        if command == "create":
            name = values.get("name")
            count = values.get("packet_count")
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ValueError("Subscription card name must contain 1–200 characters")
            if count is not None and (type(count) is not int or not 1 <= count <= 2147483647):
                raise ValueError("Packet count must be a positive integer or unlimited")
            if any(
                values.get(right) is not None and type(values[right]) is not bool
                for right in ("discoverable", "readable", "playable")
            ):
                raise ValueError("Subscription rights must be boolean or default")
            values = {**values, "name": name.strip()}

        async with self.tournaments.database.transaction() as session:
            await self.tournaments._require_manager(session, tournament_id, manager_id)
            tournament = await self.tournaments.require_modifiable(session, tournament_id)
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            if type_version.key != "ladder" or tournament.status != "active":
                raise ValueError("Subscriptions require an active Ladder tournament")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            if command == "create":
                session.add(
                    TournamentSubscriptionCardRecord(
                        tournament_id=tournament_id,
                        created_by_id=manager_id,
                        **values,
                    )
                )
            elif command == "assign":
                card = await session.get(TournamentSubscriptionCardRecord, UUID(values["card_id"]))
                if card is None or card.tournament_id != tournament_id:
                    raise LookupError("Subscription card not found")
                player_id = UUID(values["player_id"])
                membership = await session.get(
                    TournamentMembershipRecord, (tournament_id, player_id)
                )
                if membership is None or membership.status != "active":
                    raise LookupError("Active tournament participant not found")
                instance = TournamentSubscriptionRecord(
                    card_id=card.id,
                    player_id=player_id,
                    remaining_packets=card.packet_count,
                    assigned_by_id=manager_id,
                    created_at=datetime.now(UTC),
                )
                session.add(instance)
                await session.flush()
                await NotificationWriter.create_for_player(
                    session,
                    recipient_player_id=player_id,
                    audience="player",
                    kind="subscription.assigned",
                    deduplication_key=f"subscription-assigned:{instance.id}",
                    payload={
                        "subscription_id": str(instance.id),
                        "tournament_id": str(tournament_id),
                        "tournament_name": tournament.name,
                        "card_name": card.name,
                        "packet_count": card.packet_count,
                    },
                )
            elif command == "revoke":
                instance = await session.scalar(
                    select(TournamentSubscriptionRecord)
                    .join(
                        TournamentSubscriptionCardRecord,
                    )
                    .where(
                        TournamentSubscriptionRecord.id == UUID(values["subscription_id"]),
                        TournamentSubscriptionCardRecord.tournament_id == tournament_id,
                    )
                )
                if instance is None:
                    raise LookupError("Subscription not found")
                instance.revoked_at = instance.revoked_at or datetime.now(UTC)
            else:
                raise ValueError("Unknown subscription command")
            tournament.settings_version += 1
            await session.flush()
            return await self.tournaments._manager_management_snapshot(
                session, tournament_id, manager_id
            )

    @staticmethod
    async def apply_to_new_assignment(session, assignment, type_key: str) -> None:
        """Caller holds the tournament lock; call only for a new logical assignment."""
        if type_key != "ladder":
            return
        rows = (
            await session.execute(
                select(
                    TournamentSubscriptionRecord,
                    TournamentSubscriptionCardRecord,
                )
                .join(TournamentSubscriptionCardRecord)
                .join(
                    TournamentMembershipRecord,
                    (
                        TournamentMembershipRecord.tournament_id
                        == TournamentSubscriptionCardRecord.tournament_id
                    )
                    & (
                        TournamentMembershipRecord.player_id
                        == TournamentSubscriptionRecord.player_id
                    ),
                )
                .where(
                    TournamentSubscriptionCardRecord.tournament_id == assignment.tournament_id,
                    TournamentMembershipRecord.status == "active",
                    TournamentSubscriptionRecord.revoked_at.is_(None),
                    or_(
                        TournamentSubscriptionRecord.remaining_packets.is_(None),
                        TournamentSubscriptionRecord.remaining_packets > 0,
                    ),
                )
                .order_by(TournamentSubscriptionRecord.created_at, TournamentSubscriptionRecord.id)
            )
        ).all()
        seen = set()
        for instance, card in rows:
            if instance.player_id in seen:
                continue
            seen.add(instance.player_id)
            entitlement = await session.get(
                TournamentPacketEntitlementRecord, (assignment.id, instance.player_id)
            )
            if entitlement is None:
                entitlement = TournamentPacketEntitlementRecord(
                    assignment_id=assignment.id,
                    player_id=instance.player_id,
                )
                session.add(entitlement)
            for source, target in (
                ("discoverable", "discoverable_override"),
                ("readable", "readable_override"),
                ("playable", "playable"),
            ):
                value = getattr(card, source)
                if value is not None:
                    setattr(entitlement, target, value)
            entitlement.granted_by_id = instance.assigned_by_id
            if instance.remaining_packets is not None:
                instance.remaining_packets -= 1
