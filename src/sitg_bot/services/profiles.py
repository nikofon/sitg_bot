import logging
import re
from collections.abc import Iterable
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    GameParticipantRecord,
    GameRecord,
    GameResultRecord,
    GameRulesetVersionRecord,
    GameThemeRecord,
    PacketQuestionRecord,
    PlatformAdministratorRecord,
    PlayerRecord,
    QuestionRoundRecord,
    RulesetRatingLedgerRecord,
    RulesetRatingRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentRecord,
)

LOGGER = logging.getLogger(__name__)

SI_RULESET_KEY = "si"
DEFAULT_RULESET_RATING = 1000.0
PROFILE_GAME_LIMIT = 20
RATING_HISTORY_LIMIT = 20
PRIVATE_PLACES = 4
MEMBERSHIP_VISIBLE_STATUSES = frozenset({"invited", "registered", "approved", "active"})
TELEGRAM_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,64}")

PLACEMENT_KINDS = (
    "place_1", "place_1_5", "place_2", "place_2_5",
    "place_3", "place_3_5", "place_4", "below_4",
)


def placement_summary(places: Iterable[Decimal]) -> list[dict[str, object]]:
    """Bucket final places from 1 to 4 in half-place steps, and worse."""
    counts: dict[str, int] = dict.fromkeys(PLACEMENT_KINDS, 0)
    for place in places:
        if place <= PRIVATE_PLACES:
            suffix = "_5" if place != place.to_integral_value() else ""
            counts[f"place_{int(place)}{suffix}"] += 1
        else:
            counts["below_4"] += 1
    total = sum(counts.values())
    return [
        {
            "kind": kind,
            "count": count,
            "percent": round(count * 100 / total, 1) if total else 0.0,
        }
        for kind, count in counts.items()
    ]


def canonical_question_values(values: Iterable[int]) -> dict[int, int]:
    """Map a tournament's question point scale onto canonical SI values 10, 20, ..."""
    ordered = sorted(set(values))
    return {value: (index + 1) * 10 for index, value in enumerate(ordered)}


def identity_projection(player: PlayerRecord, *, privileged: bool) -> dict[str, object]:
    """Project public identity; real name and private Telegram data need privilege."""
    identity: dict[str, object] = {
        "id": player.id,
        "nickname": player.public_nickname,
        "viewer_privileged": privileged,
    }
    if privileged:
        identity.update(
            {
                "real_name": player.real_name,
                "telegram_username": player.telegram_username,
                "telegram_public": player.telegram_public,
            }
        )
    elif player.telegram_public:
        identity["telegram_username"] = player.telegram_username
    return identity


class PlayerProfileService:
    """Read-only player profile projections computed from settled game results."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_players(
        self, *, ruleset_key: str | None = None, search: str = "",
        order: str = "name_asc", offset: int = 0, limit: int = 20
    ) -> dict[str, object]:
        if len(search) > 200 or order not in {"name_asc", "name_desc"}:
            raise ValueError("Invalid player search")
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
            raise ValueError("Invalid player page")
        async with self.database.sessions() as session:
            versions = (await session.execute(select(GameRulesetVersionRecord).order_by(
                GameRulesetVersionRecord.key, GameRulesetVersionRecord.version.desc()
            ))).scalars()
            names: dict[str, str] = {}
            for version in versions:
                names.setdefault(version.key, version.name)
        rulesets = [{"key": key, "name": name} for key, name in names.items()]
        selected = ruleset_key or (
            SI_RULESET_KEY if SI_RULESET_KEY in names else next(iter(names), None)
        )
        if ruleset_key is not None and ruleset_key not in names:
            raise ValueError("Unknown ruleset")
        game_counts = (
            select(GameResultRecord.player_id, func.count().label("games"))
            .join(GameRecord, GameRecord.id == GameResultRecord.game_id)
            .join(GameRulesetVersionRecord,
                  GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id)
            .where(GameRulesetVersionRecord.key == selected)
            .group_by(GameResultRecord.player_id)
            .having(func.count() > 1)
            .subquery()
        )
        query = select(
            PlayerRecord.id, PlayerRecord.public_nickname, game_counts.c.games,
            func.coalesce(RulesetRatingRecord.rating, DEFAULT_RULESET_RATING).label("rating"),
        ).join(game_counts, game_counts.c.player_id == PlayerRecord.id).outerjoin(
            RulesetRatingRecord, and_(
                RulesetRatingRecord.player_id == PlayerRecord.id,
                RulesetRatingRecord.ruleset_key == selected,
            ),
        ).where(
            PlayerRecord.status == "active", PlayerRecord.public_nickname.is_not(None)
        )
        if search.strip():
            query = query.where(
                func.lower(PlayerRecord.public_nickname).contains(
                    search.strip().lower(), autoescape=True
                )
            )
        name = func.lower(PlayerRecord.public_nickname)
        ordering = (name, PlayerRecord.id) if order == "name_asc" else (
            name.desc(), PlayerRecord.id.desc()
        )
        async with self.database.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            rows = (
                await session.execute(query.order_by(*ordering).offset(offset).limit(limit))
            ).all()
        return {
            "rulesets": rulesets,
            "ruleset_key": selected,
            "items": [{
                "id": str(row.id), "label": row.public_nickname,
                "rating": float(row.rating), "games": row.games,
            } for row in rows],
            "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < (total or 0) else None,
        }

    async def profile(
        self,
        viewer_player_id: UUID | None,
        player_id: UUID,
        *,
        ruleset_key: str | None = None,
        game_limit: int = PROFILE_GAME_LIMIT,
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            player = await session.get(PlayerRecord, player_id)
            if player is None or (viewer_player_id is None and player.status != "active"):
                raise LookupError("Player not found")
            privileged = viewer_player_id == player_id or await self._is_administrator(
                session, viewer_player_id
            )
            rulesets = await self._played_rulesets(session, player_id)
            available = {item["key"] for item in rulesets}
            selected = ruleset_key if ruleset_key in available else None
            if selected is None and rulesets:
                selected = rulesets[0]["key"]
            payload: dict[str, object] = {
                "player": identity_projection(player, privileged=privileged),
                "rulesets": rulesets,
                "ruleset_key": selected,
                "rating": {"value": DEFAULT_RULESET_RATING, "history": []},
                "stats": {
                    "games": 0,
                    "wins": 0,
                    "win_rate": 0.0,
                    "placements": placement_summary(()),
                },
                "si_question_stats": None,
                "games": [],
            }
            if selected is None:
                return payload
            results = await self._ruleset_results(session, player_id, selected)
            places = [result.place for result, _ in results]
            wins = sum(1 for place in places if place == Decimal(1))
            payload["rating"] = await self._rating_projection(session, selected, player_id)
            payload["stats"] = {
                "games": len(results),
                "wins": wins,
                "win_rate": round(wins * 100 / len(results), 1) if results else 0.0,
                "placements": placement_summary(places),
            }
            if selected == SI_RULESET_KEY:
                payload["si_question_stats"] = await self._si_question_stats(
                    session, player_id, selected
                )
            payload["games"] = await self._game_summaries(
                session, viewer_player_id, results[: max(1, game_limit)]
            )
            return payload

    async def game_results(
        self, viewer_player_id: UUID | None, player_id: UUID, game_id: UUID
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            game = await session.get(GameRecord, game_id)
            if game is None:
                raise LookupError("Game not found")
            if await session.get(GameResultRecord, (game_id, player_id)) is None:
                raise LookupError("Player has no result in this game")
            tournament = await session.get(TournamentRecord, game.tournament_id)
            assert tournament is not None
            visible = await self._tournament_visible(session, tournament, viewer_player_id)
            participants = (await self._participant_summaries(session, (game_id,)))[game_id]
            return {
                "game_id": game.id,
                "player_id": player_id,
                "tournament_name": tournament.name if visible else None,
                "tournament_visible": visible,
                "stage": None,
                "played_at": game.completed_at,
                "participants": participants,
                "themes": await self._theme_grids(session, game_id),
            }

    async def resolve_reference(self, reference: str) -> dict[str, object]:
        """Resolve a profile reference: a player UUID or an @-prefixed Telegram username."""
        text = reference.strip()
        if not text:
            raise ValueError("A player reference is required")
        async with self.database.sessions() as session:
            player: PlayerRecord | None = None
            if text.startswith("@"):
                username = text[1:].strip()
                if TELEGRAM_USERNAME_PATTERN.fullmatch(username) is None:
                    raise ValueError("Telegram username is invalid")
                player = await session.scalar(
                    select(PlayerRecord).where(
                        func.lower(PlayerRecord.telegram_username) == username.lower()
                    )
                )
            else:
                try:
                    player_id = UUID(text)
                except ValueError as error:
                    raise ValueError(
                        "Reference must be a player ID or an @-prefixed username"
                    ) from error
                player = await session.get(PlayerRecord, player_id)
            if player is None:
                raise LookupError("Player not found")
            return {"player_id": player.id, "nickname": player.public_nickname}

    async def _played_rulesets(
        self, session: AsyncSession, player_id: UUID
    ) -> list[dict[str, str]]:
        rows = (
            await session.execute(
                select(
                    GameRulesetVersionRecord.key,
                    GameRulesetVersionRecord.name,
                    GameRulesetVersionRecord.version,
                )
                .join(
                    GameRecord, GameRecord.game_ruleset_version_id == GameRulesetVersionRecord.id
                )
                .join(GameResultRecord, GameResultRecord.game_id == GameRecord.id)
                .where(GameResultRecord.player_id == player_id)
                .distinct()
            )
        ).all()
        latest: dict[str, tuple[int, str]] = {}
        for key, name, version in rows:
            known = latest.get(key)
            if known is None or version > known[0]:
                latest[key] = (version, name)
        return [
            {"key": key, "name": name}
            for key, (_, name) in sorted(latest.items(), key=lambda item: item[1][1].casefold())
        ]

    async def _ruleset_results(
        self, session: AsyncSession, player_id: UUID, ruleset_key: str
    ) -> list[tuple[GameResultRecord, GameRecord]]:
        return list(
            (
                await session.execute(
                    select(GameResultRecord, GameRecord)
                    .join(GameRecord, GameRecord.id == GameResultRecord.game_id)
                    .join(
                        GameRulesetVersionRecord,
                        GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
                    )
                    .where(
                        GameResultRecord.player_id == player_id,
                        GameRulesetVersionRecord.key == ruleset_key,
                    )
                    .order_by(
                        GameRecord.completed_at.desc().nullslast(),
                        GameRecord.id.desc(),
                    )
                )
            ).all()
        )

    async def _rating_projection(
        self, session: AsyncSession, ruleset_key: str, player_id: UUID
    ) -> dict[str, object]:
        rating = await session.get(RulesetRatingRecord, (ruleset_key, player_id))
        value = float(rating.rating) if rating is not None else DEFAULT_RULESET_RATING
        ledger = (
            await session.execute(
                select(
                    RulesetRatingLedgerRecord.played_at,
                    RulesetRatingLedgerRecord.rating_after,
                )
                .where(
                    RulesetRatingLedgerRecord.ruleset_key == ruleset_key,
                    RulesetRatingLedgerRecord.player_id == player_id,
                )
                .order_by(
                    RulesetRatingLedgerRecord.played_at.desc(),
                    RulesetRatingLedgerRecord.id.desc(),
                )
                .limit(RATING_HISTORY_LIMIT)
            )
        ).all()
        history = [
            {"played_at": played_at, "rating": float(rating_after)}
            for played_at, rating_after in reversed(ledger)
        ]
        return {"value": value, "history": history}

    async def _si_question_stats(
        self, session: AsyncSession, player_id: UUID, ruleset_key: str
    ) -> list[dict[str, object]]:
        # The scale of a game is every value it could ask, not only the values this
        # player attempted; otherwise unattempted values would shift the mapping.
        scale_rows = (
            await session.execute(
                select(GameRecord.id, PacketQuestionRecord.value)
                .join(
                    GameResultRecord,
                    and_(
                        GameResultRecord.game_id == GameRecord.id,
                        GameResultRecord.player_id == player_id,
                    ),
                )
                .join(
                    GameRulesetVersionRecord,
                    GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
                )
                .join(QuestionRoundRecord, QuestionRoundRecord.game_id == GameRecord.id)
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id
                    == QuestionRoundRecord.question_revision_id,
                )
                .join(
                    GameThemeRecord,
                    and_(
                        GameThemeRecord.game_id == GameRecord.id,
                        GameThemeRecord.theme_revision_id
                        == PacketQuestionRecord.theme_revision_id,
                        GameThemeRecord.source_packet_version_id
                        == PacketQuestionRecord.packet_version_id,
                    ),
                )
                .where(GameRulesetVersionRecord.key == ruleset_key)
            )
        ).all()
        scales: dict[UUID, set[int]] = {}
        for game_id, value in scale_rows:
            scales.setdefault(game_id, set()).add(value)
        mappings = {
            game_id: canonical_question_values(values) for game_id, values in scales.items()
        }
        rows = (
            await session.execute(
                select(
                    GameRecord.id,
                    PacketQuestionRecord.value,
                    AnswerAttemptRecord.final_correct,
                )
                .join(
                    GameResultRecord,
                    and_(
                        GameResultRecord.game_id == GameRecord.id,
                        GameResultRecord.player_id == player_id,
                    ),
                )
                .join(
                    GameRulesetVersionRecord,
                    GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
                )
                .join(
                    GameParticipantRecord,
                    and_(
                        GameParticipantRecord.game_id == GameRecord.id,
                        GameParticipantRecord.player_id == player_id,
                    ),
                )
                .join(QuestionRoundRecord, QuestionRoundRecord.game_id == GameRecord.id)
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id
                    == QuestionRoundRecord.question_revision_id,
                )
                .join(
                    GameThemeRecord,
                    and_(
                        GameThemeRecord.game_id == GameRecord.id,
                        GameThemeRecord.theme_revision_id
                        == PacketQuestionRecord.theme_revision_id,
                        GameThemeRecord.source_packet_version_id
                        == PacketQuestionRecord.packet_version_id,
                    ),
                )
                .join(
                    AnswerAttemptRecord,
                    and_(
                        AnswerAttemptRecord.round_id == QuestionRoundRecord.id,
                        AnswerAttemptRecord.participant_id == GameParticipantRecord.id,
                    ),
                )
                .where(
                    GameRulesetVersionRecord.key == ruleset_key,
                    QuestionRoundRecord.status != "invalidated",
                )
            )
        ).all()
        counts: dict[int, dict[str, object]] = {}
        for game_id, value, correct in rows:
            canonical = mappings[game_id].get(value)
            if canonical is None:
                LOGGER.warning(
                    "Question value %s is outside the game scale game=%s", value, game_id
                )
                continue
            bucket = counts.setdefault(
                canonical, {"value": canonical, "correct": 0, "incorrect": 0}
            )
            key = "correct" if correct else "incorrect"
            bucket[key] = int(bucket[key]) + 1
        return [counts[value] for value in sorted(counts)]

    async def _game_summaries(
        self,
        session: AsyncSession,
        viewer_player_id: UUID | None,
        results: list[tuple[GameResultRecord, GameRecord]],
    ) -> list[dict[str, object]]:
        games = [game for _, game in results]
        if not games:
            return []
        tournaments = {
            tournament.id: tournament
            for tournament in (
                await session.execute(
                    select(TournamentRecord).where(
                        TournamentRecord.id.in_({game.tournament_id for game in games})
                    )
                )
            ).scalars()
        }
        participants = await self._participant_summaries(
            session, tuple(game.id for game in games)
        )
        viewer_admin = await self._is_administrator(session, viewer_player_id)
        accessible = await self._accessible_tournaments(
            session, {game.tournament_id for game in games}, viewer_player_id
        )
        summaries = []
        for game in games:
            tournament = tournaments.get(game.tournament_id)
            if tournament is None:
                LOGGER.warning("Game references a missing tournament game=%s", game.id)
                continue
            visible = (
                tournament.visibility == "public"
                or viewer_admin
                or tournament.id in accessible
            )
            summaries.append(
                {
                    "game_id": game.id,
                    "tournament_id": tournament.id,
                    "tournament_name": tournament.name if visible else None,
                    "tournament_visible": visible,
                    "stage": None,
                    "played_at": game.completed_at,
                    "participants": participants[game.id],
                }
            )
        return summaries

    async def _participant_summaries(
        self, session: AsyncSession, game_ids: tuple[UUID, ...]
    ) -> dict[UUID, list[dict[str, object]]]:
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
                    "participant_id": participant.id,
                    "player_id": player.id,
                    "nickname": player.public_nickname,
                    "score": participant.score,
                    "place": participant.final_place,
                }
            )
        for summaries in grouped.values():
            summaries.sort(
                key=lambda item: (item["place"] or Decimal(99), item["participant_id"])
            )
        return grouped

    async def _theme_grids(
        self, session: AsyncSession, game_id: UUID
    ) -> list[dict[str, object]]:
        theme_positions = {
            theme_revision_id: position
            for theme_revision_id, position in (
                await session.execute(
                    select(
                        GameThemeRecord.theme_revision_id, GameThemeRecord.position
                    ).where(GameThemeRecord.game_id == game_id)
                )
            ).all()
        }
        question_rows = (
            await session.execute(
                select(
                    QuestionRoundRecord.id,
                    PacketQuestionRecord.theme_revision_id,
                    PacketQuestionRecord.value,
                )
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id
                    == QuestionRoundRecord.question_revision_id,
                )
                .join(
                    GameThemeRecord,
                    and_(
                        GameThemeRecord.game_id == QuestionRoundRecord.game_id,
                        GameThemeRecord.theme_revision_id
                        == PacketQuestionRecord.theme_revision_id,
                        GameThemeRecord.source_packet_version_id
                        == PacketQuestionRecord.packet_version_id,
                    ),
                )
                .where(
                    QuestionRoundRecord.game_id == game_id,
                    QuestionRoundRecord.status != "invalidated",
                )
            )
        ).all()
        attempt_rows = (
            await session.execute(
                select(
                    AnswerAttemptRecord.round_id,
                    AnswerAttemptRecord.participant_id,
                    AnswerAttemptRecord.final_correct,
                )
                .join(
                    GameParticipantRecord,
                    GameParticipantRecord.id == AnswerAttemptRecord.participant_id,
                )
                .where(GameParticipantRecord.game_id == game_id)
            )
        ).all()
        answers: dict[UUID, dict[UUID, str]] = {}
        for round_id, participant_id, final_correct in attempt_rows:
            answers.setdefault(round_id, {})[participant_id] = (
                "correct" if final_correct else "incorrect"
            )
        grouped: dict[int, list[tuple[int, UUID]]] = {}
        for round_id, theme_revision_id, value in question_rows:
            position = theme_positions.get(theme_revision_id)
            if position is None:
                continue
            grouped.setdefault(position, []).append((value, round_id))
        themes = []
        for position in sorted(grouped):
            questions = [
                {"value": value, "answers": answers.get(round_id, {})}
                for value, round_id in sorted(grouped[position])
            ]
            themes.append({"index": position, "questions": questions})
        return themes

    async def _tournament_visible(
        self, session: AsyncSession, tournament: TournamentRecord, viewer_player_id: UUID | None
    ) -> bool:
        if tournament.visibility == "public":
            return True
        if await self._is_administrator(session, viewer_player_id):
            return True
        return tournament.id in await self._accessible_tournaments(
            session, {tournament.id}, viewer_player_id
        )

    @staticmethod
    async def _accessible_tournaments(
        session: AsyncSession, tournament_ids: set[UUID], viewer_player_id: UUID | None
    ) -> set[UUID]:
        if not tournament_ids or viewer_player_id is None:
            return set()
        memberships = set(
            (
                await session.execute(
                    select(TournamentMembershipRecord.tournament_id).where(
                        TournamentMembershipRecord.tournament_id.in_(tournament_ids),
                        TournamentMembershipRecord.player_id == viewer_player_id,
                        TournamentMembershipRecord.status.in_(MEMBERSHIP_VISIBLE_STATUSES),
                    )
                )
            ).scalars()
        )
        managers = set(
            (
                await session.execute(
                    select(TournamentManagerRecord.tournament_id).where(
                        TournamentManagerRecord.tournament_id.in_(tournament_ids),
                        TournamentManagerRecord.player_id == viewer_player_id,
                        TournamentManagerRecord.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
        return memberships | managers

    @staticmethod
    async def _is_administrator(session: AsyncSession, player_id: UUID | None) -> bool:
        if player_id is None:
            return False
        administrator = await session.get(PlatformAdministratorRecord, player_id)
        return administrator is not None and administrator.revoked_at is None
