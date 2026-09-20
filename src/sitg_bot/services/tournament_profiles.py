"""Read-only tournament profile projections for the Mini App tournament window."""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.classic import SCHEMES, playoff_places, standings
from sitg_bot.services.classic import is_chair
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    ClassicMatchRecord,
    ClassicRoundRecord,
    ClassicStageRecord,
    GameParticipantRecord,
    GameRecord,
    GameRulesetVersionRecord,
    PlayerRecord,
    PlatformAdministratorRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentRecord,
    TournamentTypeVersionRecord,
)

LOGGER = logging.getLogger(__name__)

PROFILE_GAME_LIMIT = 20
MEMBERSHIP_VISIBLE_STATUSES = frozenset({"invited", "registered", "approved", "active"})
PARTICIPANT_STATUSES = frozenset({"approved", "active"})


class TournamentProfileService:
    """Read-only projections backing the tournament profile Mini App window."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def profile(
        self, viewer_player_id: UUID | None, tournament_id: UUID
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            tournament = await session.get(TournamentRecord, tournament_id)
            if tournament is None or not await self._visible(
                session, tournament, viewer_player_id
            ):
                raise LookupError("Tournament not found")
            type_version = await session.get(
                TournamentTypeVersionRecord, tournament.type_version_id
            )
            ruleset_version = await session.get(
                GameRulesetVersionRecord, tournament.game_ruleset_version_id
            )
            memberships = await self._membership_rows(session, tournament_id)
            classic = type_version.key == "classic" if type_version else False
            now = datetime.now(UTC)
            return {
                "type_key": type_version.key if type_version else "",
                "tournament": {
                    "id": tournament.id,
                    "name": tournament.name,
                    "slug": tournament.slug,
                },
                "general": await self._general_projection(session, tournament, memberships, now),
                "registrations": [
                    {
                        "player_id": player_id,
                        "nickname": nickname,
                        "status": status,
                        "registered_at": registered_at,
                    }
                    for player_id, nickname, status, registered_at in memberships
                ],
                "participants": (
                    [
                        {"player_id": player_id, "nickname": nickname}
                        for player_id, nickname, status, _ in memberships
                        if status in PARTICIPANT_STATUSES
                    ]
                    if classic
                    else None
                ),
                "games": await self._games_projection(session, tournament, classic),
                "leaders": await self._leaders_projection(
                    session, tournament, memberships, classic
                ),
            }

    async def _visible(
        self, session: AsyncSession, tournament: TournamentRecord, viewer_player_id: UUID | None
    ) -> bool:
        if tournament.visibility == "public":
            return True
        if viewer_player_id is None:
            return False
        administrator = await session.get(PlatformAdministratorRecord, viewer_player_id)
        if administrator is not None and administrator.revoked_at is None:
            return True
        membership = await session.get(
            TournamentMembershipRecord, (tournament.id, viewer_player_id)
        )
        if membership is not None and membership.status in MEMBERSHIP_VISIBLE_STATUSES:
            return True
        manager = await session.get(TournamentManagerRecord, (tournament.id, viewer_player_id))
        return manager is not None and manager.revoked_at is None

    @staticmethod
    async def _membership_rows(
        session: AsyncSession, tournament_id: UUID
    ) -> list[tuple[str, str, str, object]]:
        rows = (
            await session.execute(
                select(
                    PlayerRecord.id,
                    PlayerRecord.public_nickname,
                    TournamentMembershipRecord.status,
                    TournamentMembershipRecord.registered_at,
                )
                .join(
                    TournamentMembershipRecord,
                    TournamentMembershipRecord.player_id == PlayerRecord.id,
                )
                .where(TournamentMembershipRecord.tournament_id == tournament_id)
                .order_by(PlayerRecord.public_nickname, PlayerRecord.id)
            )
        ).all()
        return [
            (str(player_id), nickname, status, registered_at)
            for player_id, nickname, status, registered_at in rows
        ]

    @staticmethod
    async def _manager_rows(session: AsyncSession, tournament_id: UUID) -> list[dict]:
        rows = (
            await session.execute(
                select(PlayerRecord)
                .join(
                    TournamentManagerRecord,
                    TournamentManagerRecord.player_id == PlayerRecord.id,
                )
                .where(
                    TournamentManagerRecord.tournament_id == tournament_id,
                    TournamentManagerRecord.revoked_at.is_(None),
                )
                .order_by(PlayerRecord.public_nickname, PlayerRecord.id)
            )
        ).scalars()
        return [
            {"player_id": str(manager.id), "name": manager.public_nickname} for manager in rows
        ]

    async def _games_projection(
        self, session: AsyncSession, tournament: TournamentRecord, classic: bool
    ) -> dict[str, object]:
        if not classic:
            return {
                "kind": "ladder",
                "items": await self._recent_games(session, tournament.id),
            }
        return {
            "kind": "classic",
            "stages": await self._stage_games(session, tournament.id),
        }

    async def _recent_games(
        self, session: AsyncSession, tournament_id: UUID
    ) -> list[dict[str, object]]:
        games = list(
            (
                await session.scalars(
                    select(GameRecord)
                    .where(
                        GameRecord.tournament_id == tournament_id,
                        GameRecord.status == "finalized",
                    )
                    .order_by(GameRecord.completed_at.desc().nullslast(), GameRecord.id.desc())
                    .limit(PROFILE_GAME_LIMIT)
                )
            )
        )
        participants = await self._participant_rows(session, tuple(game.id for game in games))
        return [
            {
                "game_id": game.id,
                "played_at": game.completed_at,
                "participants": participants[game.id],
            }
            for game in games
        ]

    @staticmethod
    async def _participant_rows(
        session: AsyncSession, game_ids: tuple[UUID, ...]
    ) -> dict[UUID, list[dict[str, object]]]:
        if not game_ids:
            return {}
        rows = (
            await session.execute(
                select(GameParticipantRecord, PlayerRecord)
                .join(PlayerRecord, PlayerRecord.id == GameParticipantRecord.player_id)
                .where(GameParticipantRecord.game_id.in_(set(game_ids)))
            )
        ).all()
        grouped: dict[UUID, list[dict[str, object]]] = {game_id: [] for game_id in game_ids}
        for participant, player in rows:
            grouped.setdefault(participant.game_id, []).append(
                {
                    "player_id": str(player.id),
                    "nickname": player.public_nickname,
                    "score": participant.score,
                    "place": participant.final_place,
                }
            )
        for summaries in grouped.values():
            summaries.sort(key=lambda item: (item["place"] or 99, item["player_id"]))
        return grouped

    async def _general_projection(
        self,
        session: AsyncSession,
        tournament: TournamentRecord,
        memberships: list[tuple[str, str, str, object]],
        now: datetime,
    ) -> dict[str, object]:
        type_version = await session.get(
            TournamentTypeVersionRecord, tournament.type_version_id
        )
        ruleset_version = await session.get(
            GameRulesetVersionRecord, tournament.game_ruleset_version_id
        )
        return {
            "name": tournament.name,
            "slug": tournament.slug,
            "description": tournament.description,
            "status": tournament.status,
            "moderation_status": tournament.moderation_status,
            "visibility": tournament.visibility,
            "language": tournament.language,
            "payment_type": tournament.payment_type,
            "type": {
                "key": type_version.key,
                "name": type_version.name,
                "version": type_version.version,
            }
            if type_version
            else None,
            "ruleset": {
                "key": ruleset_version.key,
                "name": ruleset_version.name,
                "version": ruleset_version.version,
            }
            if ruleset_version
            else None,
            "starts_at": tournament.starts_at,
            "planned_ends_at": tournament.planned_ends_at,
            "actual_starts_at": tournament.actual_starts_at,
            "actual_ends_at": tournament.actual_ends_at,
            "finalized_at": tournament.finalized_at,
            "settings_version": tournament.settings_version,
            "registration": {
                "open": TournamentService._registration_is_open(tournament, now),
                "starts_at": tournament.registration_starts_at,
                "ends_at": tournament.registration_ends_at,
            },
            "managers": await self._manager_rows(session, tournament.id),
            "authors": list(
                await TournamentService(self.database).tournament_authors(
                    session, tournament.id
                )
            ),
            "registration_count": len(memberships),
            "participant_count": sum(
                status in PARTICIPANT_STATUSES for _, _, status, _ in memberships
            ),
        }

    async def _stage_games(
        self, session: AsyncSession, tournament_id: UUID
    ) -> list[dict[str, object]]:
        names = dict(
            (player_id, nickname)
            for player_id, nickname, _, _ in await self._membership_rows(
                session, tournament_id
            )
        )
        result = []
        for stage in await self._stage_rows(session, tournament_id):
            rounds = list(
                (
                    await session.scalars(
                        select(ClassicRoundRecord)
                        .where(ClassicRoundRecord.stage_id == stage.id)
                        .order_by(ClassicRoundRecord.number)
                    )
                )
            )
            matches = await self._stage_matches(session, stage.id)
            game_ids = tuple(match.game_id for match in matches if match.game_id)
            participants = await self._participant_rows(session, game_ids)
            games_by_id = {}
            if game_ids:
                games_by_id = {
                    game.id: game
                    for game in (
                        await session.scalars(
                            select(GameRecord).where(GameRecord.id.in_(set(game_ids)))
                        )
                    )
                }
            result.append(
                {
                    "kind": stage.kind,
                    "stage_type": stage.stage_type,
                    "scheme_key": stage.scheme_key,
                    "started_at": stage.started_at,
                    "completed_at": stage.completed_at,
                    "groups": sorted({match.group_number for match in matches}),
                    "rounds": [
                        {
                            "number": round_record.number,
                            "matches": [
                                self._match_projection(
                                    match, participants, games_by_id, names
                                )
                                for match in matches
                                if match.round_id == round_record.id
                            ],
                        }
                        for round_record in rounds
                    ],
                }
            )
        return result

    @staticmethod
    def _match_projection(
        match: ClassicMatchRecord,
        participants: dict[UUID, list[dict[str, object]]],
        games_by_id: dict[UUID, GameRecord],
        names: dict[str, str],
    ) -> dict[str, object]:
        game = games_by_id.get(match.game_id) if match.game_id else None
        return {
            "id": str(match.id),
            "group": match.group_number,
            "number": match.number,
            "game_id": str(match.game_id) if match.game_id else None,
            "played_at": game.completed_at if game else None,
            "participants": participants.get(match.game_id, []) if match.game_id else [],
            "players": [
                {"player_id": seat, "nickname": names.get(seat, seat)}
                for seat in match.seats or []
                if not is_chair(seat)
            ],
            "manual_results": [
                {
                    "player_id": result["seat"],
                    "nickname": names.get(result["seat"], result["seat"]),
                    "place": result["place"],
                    "score": result["score"],
                    "points": result.get("points", "0"),
                }
                for result in match.results or []
                if not is_chair(result["seat"])
            ],
        }

    async def _leaders_projection(
        self,
        session: AsyncSession,
        tournament: TournamentRecord,
        memberships: list[tuple[str, str, str, object]],
        classic: bool,
    ) -> dict[str, object]:
        if not classic:
            return {
                "kind": "ladder",
                "items": [
                    {
                        "player_id": player_id,
                        "nickname": nickname,
                        "rating": float(rating),
                    }
                    for player_id, nickname, rating in await self._ladder_ratings(
                        session, tournament.id
                    )
                ],
            }
        names = {player_id: nickname for player_id, nickname, _, _ in memberships}
        projected = []
        for stage in await self._stage_rows(session, tournament.id):
            matches = await self._stage_matches(session, stage.id)
            if stage.kind == "first" and stage.stage_type in {"groups", "quiz"}:
                ranking = standings(
                    matches, quiz=stage.stage_type == "quiz", seed=stage.random_seed
                )
                projected.append(
                    {
                        "kind": "first",
                        "stage_type": stage.stage_type,
                        "standings": [
                            {
                                "player_id": row["seat"],
                                "nickname": names.get(row["seat"], row["seat"]),
                                "points": row["points"],
                                "score": row["score"],
                            }
                            for row in ranking
                            if not is_chair(row["seat"])
                        ],
                    }
                )
                continue
            if stage.kind == "playoff" and stage.scheme_key in SCHEMES:
                rounds = {
                    round_record.id: round_record.number
                    for round_record in (
                        await session.scalars(
                            select(ClassicRoundRecord).where(
                                ClassicRoundRecord.stage_id == stage.id
                            )
                        )
                    )
                }
                seats = {
                    (rounds[match.round_id], match.number): list(match.seats or [])
                    for match in matches
                }
                results = {
                    (rounds[match.round_id], match.number): list(match.results or [])
                    for match in matches
                    if match.results
                }
                places = playoff_places(SCHEMES[stage.scheme_key], seats, results)
                projected.append(
                    {
                        "kind": "playoff",
                        "stage_type": stage.stage_type,
                        "places": [
                            {
                                "player_id": row["seat"],
                                "nickname": names.get(row["seat"], row["seat"]),
                                "place": row["place"],
                            }
                            for row in places
                            if not is_chair(row["seat"])
                        ],
                    }
                )
        return {"kind": "classic", "stages": projected}

    @staticmethod
    async def _stage_rows(session: AsyncSession, tournament_id: UUID) -> list:
        return list(
            (
                await session.scalars(
                    select(ClassicStageRecord)
                    .where(ClassicStageRecord.tournament_id == tournament_id)
                    .order_by(ClassicStageRecord.kind)
                )
            )
        )

    @staticmethod
    async def _stage_matches(session: AsyncSession, stage_id: UUID) -> list:
        return list(
            (
                await session.scalars(
                    select(ClassicMatchRecord)
                    .join(
                        ClassicRoundRecord, ClassicRoundRecord.id == ClassicMatchRecord.round_id
                    )
                    .where(ClassicRoundRecord.stage_id == stage_id)
                    .order_by(
                        ClassicRoundRecord.number,
                        ClassicMatchRecord.group_number,
                        ClassicMatchRecord.number,
                    )
                )
            )
        )

    @staticmethod
    async def _ladder_ratings(
        session: AsyncSession, tournament_id: UUID
    ) -> list[tuple[str, str, object]]:
        rows = (
            await session.execute(
                select(
                    PlayerRecord.id,
                    PlayerRecord.public_nickname,
                    TournamentMembershipRecord.rating,
                )
                .join(
                    TournamentMembershipRecord,
                    TournamentMembershipRecord.player_id == PlayerRecord.id,
                )
                .where(
                    TournamentMembershipRecord.tournament_id == tournament_id,
                    TournamentMembershipRecord.status.in_(PARTICIPANT_STATUSES),
                )
                .order_by(
                    TournamentMembershipRecord.rating.desc(),
                    PlayerRecord.public_nickname,
                    PlayerRecord.id,
                )
            )
        ).all()
        return [(str(player_id), nickname, rating) for player_id, nickname, rating in rows]

