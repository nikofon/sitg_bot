from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from test_lobby_architecture import database_url as database_url
from test_player_profiles import build_fixture, record_game

from sitg_bot.application.contracts import ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.services.author_profiles import AuthorProfileService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    AuthorRecord,
    GameParticipantRecord,
    GameRecord,
    LogicalQuestionRecord,
    PacketDraftRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlayerQuestionStateRecord,
    QuestionRevisionRecord,
    QuestionRoundRecord,
    ThemeRevisionRecord,
    TournamentAuthorRecord,
)

pytestmark = pytest.mark.integration


async def test_author_counts_and_final_performance_do_not_expose_private_data(database_url):
    database = Database(database_url)
    try:
        fixture = await build_fixture(database)
        first, second = fixture.members
        game_id = await record_game(database, fixture, results=[
            (first, Decimal(1), Decimal(10)), (second, Decimal(2), Decimal(0)),
        ], attempts={first.id: {10: True, 20: False}})
        async with database.transaction() as session:
            author = AuthorRecord(display_name="Public author", telegram_username="private_author")
            empty = AuthorRecord(display_name="Unplayed author")
            session.add_all([author, empty])
            await session.flush()
            session.add(TournamentAuthorRecord(
                author_id=author.id, tournament_id=fixture.tournament.id,
            ))
            question_ids = select(QuestionRevisionRecord.question_id).where(
                QuestionRevisionRecord.id.in_([question[0] for question in fixture.questions])
            )
            await session.execute(update(LogicalQuestionRecord).where(
                LogicalQuestionRecord.id.in_(question_ids),
            ).values(statistical_author_id=author.id))
            rounds = list(await session.scalars(select(QuestionRoundRecord).where(
                QuestionRoundRecord.game_id == game_id,
            ).order_by(QuestionRoundRecord.sequence)))
            participants = list(await session.scalars(select(GameParticipantRecord).where(
                GameParticipantRecord.game_id == game_id,
            )))
            for round_record in rounds:
                for participant in participants:
                    buzzed = round_record.sequence == 1 or (
                        participant.player_id == first.id and round_record.sequence == 2
                    )
                    session.add(PlayerQuestionStateRecord(
                        round_id=round_record.id, participant_id=participant.id,
                        eligible=not buzzed, attempted=buzzed,
                        accepted_buzz_order=participant.seat if buzzed else None,
                        buzzed_at=datetime.now(UTC) if buzzed else None,
                    ))
            session.add(AnswerAttemptRecord(
                round_id=rounds[0].id,
                participant_id=next(p.id for p in participants if p.player_id == second.id),
                attempt_number=2, submitted_answer="Wrong private answer",
                original_correct=False, final_correct=False,
            ))
            # The final verdict, not the automatic judgment, must drive accuracy.
            await session.execute(update(AnswerAttemptRecord).where(
                AnswerAttemptRecord.round_id == rounds[0].id,
            ).values(original_correct=False))
            await session.execute(update(AnswerAttemptRecord).where(
                AnswerAttemptRecord.round_id == rounds[1].id,
            ).values(timed_out=True, submitted_answer=None))
            rounds[-1].status = "invalidated"

            # A correction reuses a played question revision in another packet
            # version with a different value. It must not multiply game facts.
            version = await session.get(PacketVersionRecord, fixture.packet_version_id)
            theme = await session.get(ThemeRevisionRecord, fixture.theme_revision_id)
            draft = PacketDraftRecord(
                status="published", source_filename="correction.json", source_checksum="test",
                content={}, creation_tournament_id=fixture.tournament.id,
            )
            session.add(draft)
            await session.flush()
            corrected = PacketVersionRecord(
                packet_id=version.packet_id, version_number=2, name="Secret corrected packet",
                source_draft_id=draft.id,
            )
            session.add(corrected)
            await session.flush()
            corrected_theme = ThemeRevisionRecord(
                packet_version_id=corrected.id, theme_id=theme.theme_id,
                name="Secret corrected theme", position=1, revision_number=2,
            )
            session.add(corrected_theme)
            await session.flush()
            session.add(PacketQuestionRecord(
                packet_version_id=corrected.id, theme_revision_id=corrected_theme.id,
                question_revision_id=fixture.questions[0][0], position=1, value=100,
            ))
            # A second revision of the same logical question is still one question.
            original = await session.get(QuestionRevisionRecord, fixture.questions[0][0])
            session.add(QuestionRevisionRecord(
                question_id=original.question_id, revision_number=2,
                text="Secret correction", answer="Private answer",
            ))

        service = AuthorProfileService(database)
        catalogue = await service.catalogue()
        card = next(item for item in catalogue["items"] if item["id"] == author.id)
        assert card == {"id": author.id, "display_name": "Public author",
                        "tournament_count": 1, "question_count": 5}
        profile = await service.profile(author.id)
        assert profile["author"] == card
        assert profile["statistics"] == {
            "presentations": 4, "solved": 1, "exposures": 8, "buzzes": 3,
            "attempts": 3, "correct": 1, "timeouts": 1,
            "buzz_rate": 37.5, "accuracy": 33.3, "solved_rate": 25.0,
        }
        assert [row["value"] for row in profile["by_value"]] == [10, 20, 30, 40]
        assert [row["accuracy"] for row in profile["by_value"]] == [50.0, 0.0, None, None]
        assert "Secret" not in str(profile)
        assert "private_author" not in str(profile)
        assert str(first.id) not in str(profile)

        # Ordinary registered players can use both operations without administrator grants.
        gateway = ApplicationGateway(database)
        for operation in (
            {"action": "authors.catalogue.v1"},
            {"action": "authors.profile.v1", "author_id": str(author.id)},
        ):
            request = GatewayRequest.model_validate({
                "metadata": {"channel": "mini_app", "client_name": "test",
                             "client_version": "1.0.0"},
                "operation": operation,
            })
            result = await gateway.execute(ApplicationPrincipal(
                player_id=fixture.outsider.id, telegram_user_id=fixture.outsider.telegram_user_id,
            ), request)
            assert result.ok, result.error
            assert "private_author" not in str(result.data)
            unauthenticated = await gateway.execute(ApplicationPrincipal(), request)
            assert unauthenticated.error.code == "authentication_required"

        empty_profile = await service.profile(empty.id)
        assert empty_profile["statistics"]["accuracy"] is None
        assert empty_profile["by_value"] == []
        with pytest.raises(LookupError, match="Author not found"):
            await service.profile(uuid4())

        # Historical facts move with current statistical credit, including retired questions.
        async with database.transaction() as session:
            await session.execute(update(LogicalQuestionRecord).where(
                LogicalQuestionRecord.statistical_author_id == author.id,
            ).values(statistical_author_id=empty.id, retired_at=datetime.now(UTC)))
        assert (await service.profile(author.id))["statistics"]["presentations"] == 0
        assert (await service.profile(empty.id))["statistics"] == profile["statistics"]

        async with database.transaction() as session:
            await session.execute(update(GameRecord).where(GameRecord.id == game_id).values(
                status="completed", finalized_at=None,
            ))
        assert (await service.profile(empty.id))["statistics"]["presentations"] == 0
    finally:
        await database.close()
