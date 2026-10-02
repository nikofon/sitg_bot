"""Administrator-only catalogue and audited tournament/author actions."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, update

from sitg_bot.services.author_exposure import burn_author_content
from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.moderation import _require_administrator, _resolve_player
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    GameParticipantRecord,
    GameRecord,
    GameRulesetVersionRecord,
    LogicalPacketRecord,
    LogicalQuestionRecord,
    PacketDraftRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerAuthorLinkRecord,
    PlayerAuthorLinkRequestRecord,
    PlayerBanRecord,
    PlayerRecord,
    PlayerReportRecord,
    QuestionRevisionRecord,
    RulesetRatingLedgerRecord,
    RulesetRatingRecord,
    ThemeRecord,
    ThemeRevisionRecord,
    TournamentAuthorRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentPricingPlanPriceRecord,
    TournamentPricingPlanRecord,
    TournamentRecord,
    TournamentRegistrationRequirementRecord,
    TournamentTypeVersionRecord,
)


def fields(record) -> dict:
    """Full metadata is confined to the administrator projection."""
    return {column.key: getattr(record, column.key) for column in record.__table__.columns}


class AdminManagementService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def catalogue(self, administrator_id: UUID, section: str) -> dict:
        if section not in {
            "tournaments", "authors", "players", "packets", "link_requests", "ongoing_games"
        }:
            raise ValueError("Unknown management section")
        async with self.database.sessions() as session:
            await _require_administrator(session, administrator_id)
            items = []
            if section == "link_requests":
                requests = await session.scalars(
                    select(PlayerAuthorLinkRequestRecord).order_by(
                        PlayerAuthorLinkRequestRecord.created_at.desc(),
                        PlayerAuthorLinkRequestRecord.id.desc(),
                    )
                )
                for record in requests:
                    player = await session.get(PlayerRecord, record.player_id)
                    author = await session.get(AuthorRecord, record.author_id)
                    if player is None or author is None:
                        continue
                    items.append({
                        "id": record.id,
                        "name": author.display_name,
                        "status": record.status,
                        "request_note": record.request_note,
                        "decision_note": record.decision_note,
                        "created_at": record.created_at,
                        "decided_at": record.decided_at,
                        "cancelled_at": record.cancelled_at,
                        "player": await self._player(session, player),
                        "author": {
                            **fields(author),
                            "questions": await session.scalar(
                                select(func.count(func.distinct(QuestionRevisionRecord.question_id))).where(
                                    QuestionRevisionRecord.author_id == author.id
                                )
                            ),
                            "themes": await session.scalar(
                                select(func.count(func.distinct(ThemeRevisionRecord.theme_id))).where(
                                    ThemeRevisionRecord.author_id == author.id
                                )
                            ),
                        },
                    })
                return {
                    "kind": "admin_management",
                    "state": "ready",
                    "section": section,
                    "items": items,
                }
            if section == "ongoing_games":
                for game in await session.scalars(
                    select(GameRecord)
                    .where(GameRecord.status.in_(("lobby", "active")))
                    .order_by(GameRecord.created_at.desc(), GameRecord.id.desc())
                ):
                    tournament = await session.get(TournamentRecord, game.tournament_id)
                    policy = await session.get(
                        TournamentPolicyVersionRecord, game.tournament_policy_version_id
                    )
                    host = await session.get(PlayerRecord, game.host_player_id)
                    type_version = await session.get(
                        TournamentTypeVersionRecord, game.tournament_type_version_id
                    )
                    ruleset_version = await session.get(
                        GameRulesetVersionRecord, game.game_ruleset_version_id
                    )
                    if (
                        tournament is None
                        or policy is None
                        or host is None
                        or type_version is None
                        or ruleset_version is None
                    ):
                        continue
                    card = fields(game)
                    card["name"] = tournament.name
                    card["tournament"] = {"id": tournament.id, "name": tournament.name}
                    card["host"] = {
                        "id": host.id,
                        "public_nickname": host.public_nickname,
                    }
                    card["participants"] = [
                        {
                            "id": player.id,
                            "public_nickname": player.public_nickname,
                            "seat": participant.seat,
                            "score": participant.score,
                            "ready": participant.ready,
                            "joined": participant.joined,
                            "active": participant.active,
                            "is_chair": participant.is_chair,
                            "abandoned_at": participant.abandoned_at,
                        }
                        for participant, player in await session.execute(
                            select(GameParticipantRecord, PlayerRecord)
                            .join(
                                PlayerRecord,
                                PlayerRecord.id == GameParticipantRecord.player_id,
                            )
                            .where(GameParticipantRecord.game_id == game.id)
                            .order_by(GameParticipantRecord.seat)
                        )
                    ]
                    card["participant_count"] = len(card["participants"])
                    card["settings"] = fields(policy)
                    card["type"] = type_version.key
                    card["ruleset"] = ruleset_version.key
                    items.append(card)
                return {
                    "kind": "admin_management",
                    "state": "ready",
                    "section": section,
                    "items": items,
                }
            model = {
                "tournaments": TournamentRecord,
                "authors": AuthorRecord,
                "players": PlayerRecord,
                "packets": PacketVersionRecord,
            }[section]
            for record in await session.scalars(select(model).order_by(model.id)):
                card = fields(record)
                if section == "players":
                    card = await self._player(session, record)
                elif section == "tournaments":
                    card["managers"] = [
                        fields(p)
                        for p in await session.scalars(
                            select(PlayerRecord)
                            .join(
                                TournamentManagerRecord,
                                TournamentManagerRecord.player_id == PlayerRecord.id,
                            )
                            .where(
                                TournamentManagerRecord.tournament_id == record.id,
                                TournamentManagerRecord.revoked_at.is_(None),
                            )
                        )
                    ]
                    card["participants"] = await session.scalar(
                        select(func.count())
                        .select_from(TournamentMembershipRecord)
                        .where(
                            TournamentMembershipRecord.tournament_id == record.id,
                            TournamentMembershipRecord.status == "active",
                        )
                    )
                    card["packets"] = [
                        fields(a)
                        for a in await session.scalars(
                            select(TournamentPacketAssignmentRecord).where(
                                TournamentPacketAssignmentRecord.tournament_id == record.id
                            )
                        )
                    ]
                    for assignment in card["packets"]:
                        version = await session.scalar(
                            select(PacketVersionRecord)
                            .where(PacketVersionRecord.packet_id == assignment["packet_id"])
                            .order_by(PacketVersionRecord.version_number.desc())
                            .limit(1)
                        )
                        assignment["name"] = version.name if version else ""
                    policy = await session.scalar(
                        select(TournamentPolicyVersionRecord)
                        .where(TournamentPolicyVersionRecord.tournament_id == record.id)
                        .order_by(TournamentPolicyVersionRecord.version.desc())
                        .limit(1)
                    )
                    card["settings"] = fields(policy) if policy else {}
                    card["authors"] = [
                        fields(a)
                        for a in await session.scalars(
                            select(AuthorRecord)
                            .join(
                                TournamentAuthorRecord,
                                TournamentAuthorRecord.author_id == AuthorRecord.id,
                            )
                            .where(TournamentAuthorRecord.tournament_id == record.id)
                        )
                    ]
                    card["requirements"] = [
                        fields(r)
                        for r in await session.scalars(
                            select(TournamentRegistrationRequirementRecord).where(
                                TournamentRegistrationRequirementRecord.tournament_id == record.id
                            )
                        )
                    ]
                    card["pricing_plans"] = []
                    for plan in await session.scalars(
                        select(TournamentPricingPlanRecord).where(
                            TournamentPricingPlanRecord.tournament_id == record.id
                        )
                    ):
                        card["pricing_plans"].append(
                            {
                                **fields(plan),
                                "prices": [
                                    fields(price)
                                    for price in await session.scalars(
                                        select(TournamentPricingPlanPriceRecord).where(
                                            TournamentPricingPlanPriceRecord.pricing_plan_id
                                            == plan.id
                                        )
                                    )
                                ],
                            }
                        )
                    card["type"] = (
                        await session.get(TournamentTypeVersionRecord, record.type_version_id)
                    ).key
                    card["ruleset"] = (
                        await session.get(GameRulesetVersionRecord, record.game_ruleset_version_id)
                    ).key
                elif section == "packets":
                    packet = await session.get(LogicalPacketRecord, record.packet_id)
                    card["packet"] = fields(packet)
                    card["tournaments"] = [
                        fields(t)
                        for t in await session.scalars(
                            select(TournamentRecord)
                            .join(TournamentPacketAssignmentRecord)
                            .where(TournamentPacketAssignmentRecord.packet_id == record.packet_id)
                        )
                    ]
                    card["assignments"] = [
                        fields(a)
                        for a in await session.scalars(
                            select(TournamentPacketAssignmentRecord).where(
                                TournamentPacketAssignmentRecord.packet_id == record.packet_id
                            )
                        )
                    ]
                    card["themes"] = await session.scalar(
                        select(func.count())
                        .select_from(ThemeRevisionRecord)
                        .where(ThemeRevisionRecord.packet_version_id == record.id)
                    )
                    card["questions"] = await session.scalar(
                        select(func.count())
                        .select_from(PacketQuestionRecord)
                        .where(PacketQuestionRecord.packet_version_id == record.id)
                    )
                    author_ids = (
                        select(ThemeRevisionRecord.author_id)
                        .where(ThemeRevisionRecord.packet_version_id == record.id)
                        .union(
                            select(QuestionRevisionRecord.author_id)
                            .join(
                                PacketQuestionRecord,
                                PacketQuestionRecord.question_revision_id
                                == QuestionRevisionRecord.id,
                            )
                            .where(PacketQuestionRecord.packet_version_id == record.id)
                        )
                    )
                    card["authors"] = [
                        fields(a)
                        for a in await session.scalars(
                            select(AuthorRecord).where(
                                AuthorRecord.id.in_(author_ids)
                                | (AuthorRecord.id == record.lead_author_id)
                            )
                        )
                    ]
                else:
                    versions = (
                        select(ThemeRevisionRecord.packet_version_id)
                        .where(ThemeRevisionRecord.author_id == record.id)
                        .union(
                            select(PacketQuestionRecord.packet_version_id)
                            .join(
                                QuestionRevisionRecord,
                                QuestionRevisionRecord.id
                                == PacketQuestionRecord.question_revision_id,
                            )
                            .where(QuestionRevisionRecord.author_id == record.id),
                            select(PacketVersionRecord.id).where(
                                PacketVersionRecord.lead_author_id == record.id
                            ),
                        )
                    )
                    packets = list(
                        await session.scalars(
                            select(PacketVersionRecord).where(PacketVersionRecord.id.in_(versions))
                        )
                    )
                    card["packets"] = [fields(p) for p in packets]
                    card["packet_count"] = len({p.packet_id for p in packets})
                    card["questions"] = await session.scalar(
                        select(func.count(func.distinct(QuestionRevisionRecord.question_id))).where(
                            QuestionRevisionRecord.author_id == record.id
                        )
                    )
                    card["themes"] = await session.scalar(
                        select(func.count(func.distinct(ThemeRevisionRecord.theme_id))).where(
                            ThemeRevisionRecord.author_id == record.id
                        )
                    )
                    associated = select(TournamentPacketAssignmentRecord.tournament_id).where(
                        TournamentPacketAssignmentRecord.packet_id.in_(
                            {p.packet_id for p in packets}
                        )
                    )
                    direct = select(TournamentAuthorRecord.tournament_id).where(
                        TournamentAuthorRecord.author_id == record.id
                    )
                    card["tournaments"] = [
                        fields(t)
                        for t in await session.scalars(
                            select(TournamentRecord).where(
                                TournamentRecord.id.in_(associated.union(direct))
                            )
                        )
                    ]
                    card["players"] = [
                        await self._player(session, p)
                        for p in await session.scalars(
                            select(PlayerRecord)
                            .join(
                                PlayerAuthorLinkRecord,
                                PlayerAuthorLinkRecord.player_id == PlayerRecord.id,
                            )
                            .where(PlayerAuthorLinkRecord.author_id == record.id)
                        )
                    ]
                items.append(card)
            return {
                "kind": "admin_management",
                "state": "ready",
                "section": section,
                "items": items,
            }

    @staticmethod
    async def _player(session, player) -> dict:
        card = fields(player)
        ban = await session.get(PlayerBanRecord, player.id)
        admin = await session.get(PlatformAdministratorRecord, player.id)
        card["ban"] = fields(ban) if ban and ban.lifted_at is None else None
        card["administrator"] = bool(admin and admin.revoked_at is None)
        card["tournaments"] = []
        for membership, tournament in await session.execute(
            select(TournamentMembershipRecord, TournamentRecord).join(
                TournamentRecord, TournamentRecord.id == TournamentMembershipRecord.tournament_id
            ).where(TournamentMembershipRecord.player_id == player.id)
        ):
            card["tournaments"].append({
                **fields(membership), "id": tournament.id, "name": tournament.name,
            })
        card["authors"] = [fields(author) for author in await session.scalars(
            select(AuthorRecord).join(PlayerAuthorLinkRecord,
                PlayerAuthorLinkRecord.author_id == AuthorRecord.id).where(
                PlayerAuthorLinkRecord.player_id == player.id))]
        card["rulesets"] = [
            fields(r)
            for r in await session.scalars(
                select(RulesetRatingRecord).where(RulesetRatingRecord.player_id == player.id)
            )
        ]
        card["games_played"] = await session.scalar(
            select(func.count())
            .select_from(GameParticipantRecord)
            .join(GameRecord, GameRecord.id == GameParticipantRecord.game_id)
            .where(
                GameParticipantRecord.player_id == player.id,
                GameRecord.status.in_(("completed", "finalized")),
            )
        )
        card["reports"] = [
            {"kind": kind, "count": count}
            for kind, count in (
                await session.execute(
                    select(PlayerReportRecord.kind, func.count())
                    .where(PlayerReportRecord.reported_player_id == player.id)
                    .group_by(PlayerReportRecord.kind)
                )
            )
        ]
        return card

    async def moderate_tournament(
        self,
        administrator_id: UUID,
        tournament_id: UUID,
        *,
        command: str,
        expected_version: int,
        confirm: bool,
    ) -> dict:
        if not confirm or command not in {"halt", "resume", "abolish"}:
            raise ValueError("Explicit confirmation is required")
        async with self.database.transaction() as session:
            await _require_administrator(session, administrator_id)
            tournament = await session.get(TournamentRecord, tournament_id, with_for_update=True)
            if tournament is None:
                raise LookupError("Tournament not found")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament changed")
            if tournament.moderation_status == "abolished":
                raise ValueError("Abolition is permanent")
            if command == "halt" and not (
                tournament.moderation_status == "normal"
                and tournament.status == "active"
                and tournament.actual_starts_at is not None
                and tournament.actual_ends_at is None
            ):
                raise ValueError("Only ongoing tournaments can be halted")
            if command == "resume" and tournament.moderation_status != "halted":
                raise ValueError("Only halted tournaments can be resumed")
            now = datetime.now(UTC)
            tournament.moderation_status = {
                "halt": "halted",
                "resume": "normal",
                "abolish": "abolished",
            }[command]
            tournament.moderated_by_id = administrator_id
            tournament.moderated_at = now
            tournament.settings_version += 1
            if command == "abolish":
                tournament.status = "completed"
                tournament.actual_ends_at = now
                tournament.registration_open = False
                tournament.registration_open_override = False
                entries = list(
                    await session.scalars(
                        select(RulesetRatingLedgerRecord)
                        .where(
                            RulesetRatingLedgerRecord.tournament_id == tournament_id,
                            RulesetRatingLedgerRecord.reason == "pairwise_elo",
                        )
                        .order_by(RulesetRatingLedgerRecord.player_id, RulesetRatingLedgerRecord.id)
                    )
                )
                for entry in entries:
                    rating = await session.get(
                        RulesetRatingRecord,
                        (entry.ruleset_key, entry.player_id),
                        with_for_update=True,
                    )
                    before = Decimal(rating.rating)
                    delta = -Decimal(entry.delta)
                    rating.rating = before + delta
                    session.add(
                        RulesetRatingLedgerRecord(
                            ruleset_key=entry.ruleset_key,
                            tournament_id=tournament_id,
                            game_id=entry.game_id,
                            player_id=entry.player_id,
                            rating_before=before,
                            delta=delta,
                            rating_after=before + delta,
                            confidence_before=entry.confidence_after,
                            confidence_after=entry.confidence_after,
                            k_factor=entry.k_factor,
                            tournament_weight=entry.tournament_weight,
                            rating_model=entry.rating_model,
                            reason="admin_correction",
                            played_at=now,
                        )
                    )
            return fields(tournament)

    async def set_tournament_rating_weight(
        self,
        administrator_id: UUID,
        tournament_id: UUID,
        *,
        weight: float,
        expected_version: int,
    ) -> dict:
        decimal_weight = Decimal(str(weight))
        if not 0.1 <= decimal_weight <= 1:
            raise ValueError("Rating weight must be between 0.1 and 1")
        async with self.database.transaction() as session:
            await _require_administrator(session, administrator_id)
            tournament = await session.get(TournamentRecord, tournament_id, with_for_update=True)
            if tournament is None:
                raise LookupError("Tournament not found")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament changed")
            if tournament.moderation_status != "normal":
                raise PermissionError("Tournament is halted or abolished")
            policy = await session.scalar(
                select(TournamentPolicyVersionRecord)
                .where(TournamentPolicyVersionRecord.tournament_id == tournament_id)
                .order_by(TournamentPolicyVersionRecord.version.desc())
                .limit(1)
            )
            if policy is None:
                raise LookupError("Tournament settings not found")
            session.add(
                TournamentPolicyVersionRecord(
                    tournament_id=tournament_id,
                    version=policy.version + 1,
                    default_parameters=policy.default_parameters,
                    player_mutable_parameters=policy.player_mutable_parameters,
                    policies={
                        **policy.policies,
                        "ruleset_rating_weight": weight,
                    },
                    created_by_id=administrator_id,
                )
            )
            tournament.settings_version += 1
            await session.flush()
            return fields(tournament)

    async def link_author(self, administrator_id: UUID, author_id: UUID, target: str) -> dict:
        # Use the same pair lock and approval records as player-requested links.
        async with self.database.transaction() as session:
            await _require_administrator(session, administrator_id)
            if not target.startswith("@"):
                UUID(target)
            player = await _resolve_player(session, target.removeprefix("@"))
            author = await session.get(AuthorRecord, author_id)
            if player is None or player.status != "active" or author is None:
                raise LookupError("Active player or author not found")
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        AuthorLinkService._pair_lock_key(player.id, author_id)
                    )
                )
            )
            if await session.get(PlayerAuthorLinkRecord, (player.id, author_id)):
                return {"linked": True}
            request = await session.scalar(
                select(PlayerAuthorLinkRequestRecord)
                .where(
                    PlayerAuthorLinkRequestRecord.player_id == player.id,
                    PlayerAuthorLinkRequestRecord.author_id == author_id,
                    PlayerAuthorLinkRequestRecord.status == "pending",
                )
                .with_for_update()
            )
            if request is None:
                request = PlayerAuthorLinkRequestRecord(player_id=player.id, author_id=author_id)
                session.add(request)
            now = datetime.now(UTC)
            request.status = "approved"
            request.decided_by_id = administrator_id
            request.decided_at = now
            request.decision_note = "Linked through administrator management"
            await session.flush()
            session.add(
                PlayerAuthorLinkRecord(
                    player_id=player.id,
                    author_id=author_id,
                    approved_request_id=request.id,
                    approved_by_id=administrator_id,
                    approved_at=now,
                )
            )
            await burn_author_content(session, author_id=author_id)
            return {"linked": True}

    async def merge_authors(
        self,
        administrator_id: UUID,
        author_id: UUID,
        merge_author_id: UUID,
        *,
        confirm: bool,
    ) -> dict:
        """Join two author identities into one.

        The second author is deleted; their statistics, attributions, links, and
        requests transfer to the first author. Rows that would collide with the
        surviving identity (duplicate links, tournament authorships, and pending
        requests that can no longer be approved) are dropped in favour of the
        surviving author's row.
        """
        if author_id == merge_author_id:
            raise ValueError("An author cannot be joined with itself")
        if not confirm:
            raise ValueError("Author merge requires confirmation")
        async with self.database.transaction() as session:
            await _require_administrator(session, administrator_id)
            primary = await session.get(AuthorRecord, author_id, with_for_update=True)
            secondary = await session.get(AuthorRecord, merge_author_id, with_for_update=True)
            if primary is None or secondary is None:
                raise LookupError("Author not found")
            summary = {
                "questions": await session.scalar(
                    select(func.count()).select_from(QuestionRevisionRecord).where(
                        QuestionRevisionRecord.author_id == merge_author_id
                    )
                ),
                "themes": await session.scalar(
                    select(func.count()).select_from(ThemeRevisionRecord).where(
                        ThemeRevisionRecord.author_id == merge_author_id
                    )
                ),
                "packets": await session.scalar(
                    select(func.count()).select_from(LogicalPacketRecord).where(
                        LogicalPacketRecord.statistical_author_id == merge_author_id
                    )
                ),
                "linked_players": await session.scalar(
                    select(func.count()).select_from(PlayerAuthorLinkRecord).where(
                        PlayerAuthorLinkRecord.author_id == merge_author_id
                    )
                ),
                "tournaments": await session.scalar(
                    select(func.count()).select_from(TournamentAuthorRecord).where(
                        TournamentAuthorRecord.author_id == merge_author_id
                    )
                ),
            }
            await session.execute(
                update(PacketDraftRecord)
                .where(PacketDraftRecord.lead_author_id == merge_author_id)
                .values(lead_author_id=author_id)
            )
            await session.execute(
                update(PacketVersionRecord)
                .where(PacketVersionRecord.lead_author_id == merge_author_id)
                .values(lead_author_id=author_id)
            )
            await session.execute(
                update(LogicalPacketRecord)
                .where(LogicalPacketRecord.statistical_author_id == merge_author_id)
                .values(statistical_author_id=author_id)
            )
            await session.execute(
                update(ThemeRecord)
                .where(ThemeRecord.statistical_author_id == merge_author_id)
                .values(statistical_author_id=author_id)
            )
            await session.execute(
                update(ThemeRevisionRecord)
                .where(ThemeRevisionRecord.author_id == merge_author_id)
                .values(author_id=author_id)
            )
            await session.execute(
                update(LogicalQuestionRecord)
                .where(LogicalQuestionRecord.statistical_author_id == merge_author_id)
                .values(statistical_author_id=author_id)
            )
            await session.execute(
                update(QuestionRevisionRecord)
                .where(QuestionRevisionRecord.author_id == merge_author_id)
                .values(author_id=author_id)
            )
            # Draft Telegram author bindings map display names to author IDs.
            secondary_text = str(merge_author_id)
            for draft in await session.scalars(
                select(PacketDraftRecord).where(
                    PacketDraftRecord.author_bindings != {}  # type: ignore[comparison-overlap]
                )
            ):
                if secondary_text not in set(draft.author_bindings.values()):
                    continue
                draft.author_bindings = {
                    name: str(author_id) if value == secondary_text else value
                    for name, value in draft.author_bindings.items()
                }
            # Pending requests that could never be approved after the merge are
            # dropped; every other request is re-pointed at the surviving author.
            blocked_players = select(PlayerAuthorLinkRequestRecord.player_id).where(
                PlayerAuthorLinkRequestRecord.status == "pending",
                PlayerAuthorLinkRequestRecord.author_id == author_id,
            ).union(
                select(PlayerAuthorLinkRecord.player_id).where(
                    PlayerAuthorLinkRecord.author_id == author_id
                )
            )
            await session.execute(
                delete(PlayerAuthorLinkRequestRecord).where(
                    PlayerAuthorLinkRequestRecord.author_id == merge_author_id,
                    PlayerAuthorLinkRequestRecord.status == "pending",
                    PlayerAuthorLinkRequestRecord.player_id.in_(blocked_players),
                )
            )
            await session.execute(
                update(PlayerAuthorLinkRequestRecord)
                .where(PlayerAuthorLinkRequestRecord.author_id == merge_author_id)
                .values(author_id=author_id)
            )
            await session.execute(
                delete(PlayerAuthorLinkRecord).where(
                    PlayerAuthorLinkRecord.author_id == merge_author_id,
                    PlayerAuthorLinkRecord.player_id.in_(
                        select(PlayerAuthorLinkRecord.player_id).where(
                            PlayerAuthorLinkRecord.author_id == author_id
                        )
                    ),
                )
            )
            await session.execute(
                update(PlayerAuthorLinkRecord)
                .where(PlayerAuthorLinkRecord.author_id == merge_author_id)
                .values(author_id=author_id)
            )
            await session.execute(
                delete(TournamentAuthorRecord).where(
                    TournamentAuthorRecord.author_id == merge_author_id,
                    TournamentAuthorRecord.tournament_id.in_(
                        select(TournamentAuthorRecord.tournament_id).where(
                            TournamentAuthorRecord.author_id == author_id
                        )
                    ),
                )
            )
            await session.execute(
                update(TournamentAuthorRecord)
                .where(TournamentAuthorRecord.author_id == merge_author_id)
                .values(author_id=author_id)
            )
            # Players inherited from the joined author must be burned on the
            # surviving author's content, including the transferred attribution.
            await burn_author_content(session, author_id=author_id)
            await session.delete(secondary)
            await session.flush()
            return {
                "merged": True,
                "author_id": str(author_id),
                "merged_author_id": str(merge_author_id),
                "display_name": primary.display_name,
                **summary,
            }
