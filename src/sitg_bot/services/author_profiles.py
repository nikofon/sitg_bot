"""Player-facing author catalogue and aggregate, spoiler-free SI performance."""

from uuid import UUID

from sqlalchemy import and_, func, select

from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    AppealRecord,
    AuthorRecord,
    GameRecord,
    GameRulesetVersionRecord,
    GameThemeRecord,
    LogicalQuestionRecord,
    PacketQuestionRecord,
    PlayerQuestionStateRecord,
    QuestionRevisionRecord,
    QuestionRoundRecord,
    TournamentAuthorRecord,
)


def _author_cards():
    questions = select(
        LogicalQuestionRecord.statistical_author_id.label("author_id"),
        func.count().label("count"),
    ).group_by(LogicalQuestionRecord.statistical_author_id).subquery()
    tournaments = select(
        TournamentAuthorRecord.author_id, func.count().label("count"),
    ).group_by(TournamentAuthorRecord.author_id).subquery()
    return select(
        AuthorRecord.id, AuthorRecord.display_name,
        func.coalesce(tournaments.c.count, 0).label("tournament_count"),
        func.coalesce(questions.c.count, 0).label("question_count"),
    ).outerjoin(tournaments, tournaments.c.author_id == AuthorRecord.id).outerjoin(
        questions, questions.c.author_id == AuthorRecord.id,
    )


def _rates(counts: dict) -> dict:
    def percentage(numerator: str, denominator: str) -> float | None:
        return (
            round(counts[numerator] * 100 / counts[denominator], 1)
            if counts[denominator] else None
        )

    return {
        **counts,
        "buzz_rate": percentage("buzzes", "exposures"),
        "accuracy": percentage("correct", "attempts"),
        "solved_rate": percentage("solved", "presentations"),
    }


class AuthorProfileService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def catalogue(self) -> dict:
        async with self.database.sessions() as session:
            rows = await session.execute(_author_cards().order_by(
                func.lower(AuthorRecord.display_name), AuthorRecord.id,
            ))
            return {"kind": "authors", "state": "ready", "items": [
                dict(row) for row in rows.mappings()
            ]}

    async def profile(self, author_id: UUID) -> dict:
        async with self.database.sessions() as session:
            author = (await session.execute(
                _author_cards().where(AuthorRecord.id == author_id)
            )).mappings().one_or_none()
            if author is None:
                raise LookupError("Author not found")

            # Pin the value to the played packet/theme revision. The same question
            # revision may occur in several corrected packet versions.
            rounds = select(
                QuestionRoundRecord.id, PacketQuestionRecord.value,
            ).join(GameRecord, GameRecord.id == QuestionRoundRecord.game_id).join(
                GameRulesetVersionRecord,
                GameRulesetVersionRecord.id == GameRecord.game_ruleset_version_id,
            ).join(
                QuestionRevisionRecord,
                QuestionRevisionRecord.id == QuestionRoundRecord.question_revision_id,
            ).join(
                LogicalQuestionRecord,
                LogicalQuestionRecord.id == QuestionRevisionRecord.question_id,
            ).join(
                PacketQuestionRecord,
                PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
            ).join(GameThemeRecord, and_(
                GameThemeRecord.game_id == GameRecord.id,
                GameThemeRecord.theme_revision_id == PacketQuestionRecord.theme_revision_id,
                GameThemeRecord.source_packet_version_id == PacketQuestionRecord.packet_version_id,
            )).where(
                LogicalQuestionRecord.statistical_author_id == author_id,
                GameRulesetVersionRecord.key == "si",
                GameRecord.status == "finalized",
                QuestionRoundRecord.status == "completed",
                QuestionRoundRecord.started_at.is_not(None),
                ~select(AppealRecord.id).where(
                    AppealRecord.game_id == GameRecord.id,
                    AppealRecord.status.not_in(("accepted", "rejected")),
                ).exists(),
            ).subquery()

            # State rows are created only for players eligible at presentation (or
            # reconnection). `eligible` later becomes false after answering/leaving.
            exposures = select(
                PlayerQuestionStateRecord.round_id,
                func.count().label("exposures"),
                func.count().filter(
                    PlayerQuestionStateRecord.accepted_buzz_order.is_not(None)
                ).label("buzzes"),
            ).join(rounds, rounds.c.id == PlayerQuestionStateRecord.round_id).group_by(
                PlayerQuestionStateRecord.round_id,
            ).subquery()
            attempts = select(
                AnswerAttemptRecord.round_id,
                func.count().label("attempts"),
                func.count().filter(AnswerAttemptRecord.final_correct).label("correct"),
                func.count().filter(AnswerAttemptRecord.timed_out).label("timeouts"),
            ).join(rounds, rounds.c.id == AnswerAttemptRecord.round_id).group_by(
                AnswerAttemptRecord.round_id,
            ).subquery()
            columns = {
                "exposures": exposures.c.exposures, "buzzes": exposures.c.buzzes,
                "attempts": attempts.c.attempts, "correct": attempts.c.correct,
                "timeouts": attempts.c.timeouts,
            }
            rows = (await session.execute(select(
                rounds.c.value,
                func.count().label("presentations"),
                func.count().filter(attempts.c.correct > 0).label("solved"),
                *(func.coalesce(func.sum(column), 0).label(name)
                  for name, column in columns.items()),
            ).outerjoin(exposures, exposures.c.round_id == rounds.c.id).outerjoin(
                attempts, attempts.c.round_id == rounds.c.id,
            ).group_by(rounds.c.value).order_by(rounds.c.value))).mappings().all()
            totals = dict.fromkeys(("presentations", "solved", *columns), 0)
            values = []
            for row in rows:
                counts = {key: int(row[key]) for key in totals}
                for key, value in counts.items():
                    totals[key] += value
                values.append({"value": row["value"], **_rates(counts)})
            return {
                "kind": "author_profile", "state": "ready", "author": dict(author),
                "statistics": _rates(totals), "by_value": values,
            }
