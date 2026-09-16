import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url

from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.services.profiles import PlayerProfileService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    GamePacketVersionRecord,
    GameParticipantRecord,
    GameRecord,
    GameResultRecord,
    GameRulesetVersionRecord,
    GameThemeRecord,
    PacketQuestionRecord,
    PlayerRecord,
    QuestionRoundRecord,
    RulesetRatingLedgerRecord,
    RulesetRatingRecord,
    TournamentManagerRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
    TournamentTypeVersionRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


@dataclass(frozen=True)
class ProfileFixture:
    tournament: TournamentRecord
    manager: PlayerRecord
    members: tuple[PlayerRecord, ...]
    outsider: PlayerRecord
    type_version_id: UUID
    ruleset_version_id: UUID
    packet_version_id: UUID
    assignment_id: UUID
    theme_revision_id: UUID
    questions: tuple[tuple[UUID, int], ...]


def active_player(name: str, suffix: int, index: int) -> PlayerRecord:
    return PlayerRecord(
        telegram_user_id=suffix + index,
        real_name=f"Real {name}",
        public_nickname=f"Nickname {name}",
        telegram_username=f"nick_{index}",
        telegram_public=False,
        registration_step="complete",
        registration_completed_at=datetime.now(UTC),
        status="active",
    )

async def build_fixture(
    database: Database,
    *,
    values: tuple[int, ...] = (10, 20, 30, 40, 50),
    member_count: int = 2,
) -> ProfileFixture:
    suffix = int(secrets.token_hex(4), 16)
    async with database.transaction() as session:
        type_version = await session.scalar(
            select(TournamentTypeVersionRecord).where(
                TournamentTypeVersionRecord.key == "ladder",
                TournamentTypeVersionRecord.version == 1,
            )
        )
        ruleset_version = await session.scalar(
            select(GameRulesetVersionRecord).where(
                GameRulesetVersionRecord.key == "si",
                GameRulesetVersionRecord.version == 1,
            )
        )
        assert type_version is not None and ruleset_version is not None
        members = tuple(
            active_player(f"Member {index}", suffix, index) for index in range(member_count)
        )
        manager = active_player("Manager", suffix, 900)
        outsider = active_player("Outsider", suffix, 901)
        session.add_all((*members, manager, outsider))
        await session.flush()
        tournament = TournamentRecord(
            name="Profile tournament",
            slug=f"profile-{suffix}",
            visibility="private",
            type_version_id=type_version.id,
            game_ruleset_version_id=ruleset_version.id,
            created_by_id=manager.id,
            finalized_at=datetime.now(UTC),
        )
        session.add(tournament)
        await session.flush()
        settings = GameSettings(
            theme_count=1,
            ready_delay=0,
            message_delay=0,
            game_start_to_first_theme_delay=0,
            question_values=values,
        )
        session.add_all(
            [
                TournamentPolicyVersionRecord(
                    tournament_id=tournament.id,
                    version=1,
                    default_parameters=settings.to_dict(),
                    player_mutable_parameters=["theme_count"],
                    policies={"rating_enabled": True},
                    created_by_id=manager.id,
                ),
                TournamentManagerRecord(
                    tournament_id=tournament.id,
                    player_id=manager.id,
                    granted_by_id=manager.id,
                ),
                *[
                    TournamentMembershipRecord(
                        tournament_id=tournament.id,
                        player_id=player.id,
                        enrolled_by_id=manager.id,
                    )
                    for player in members
                ],
            ]
        )
    packet = Packet(
        f"Packet {secrets.token_hex(4)}",
        (
            Theme(
                "Secret theme name",
                tuple(
                    Question(
                        text=f"Secret question text {value}",
                        answer=f"secret answer {value}",
                        commentary="Explanation",
                        value=value,
                        form="ANSWER",
                        source="https://example.test",
                    )
                    for value in values
                ),
            ),
        ),
        lead_author="Profile Author",
        language="ru",
    )
    packets = PacketAdminService(database)
    draft_id = await packets.create_draft(
        packet,
        source_filename="profile.json",
        uploader_id=manager.id,
        tournament_id=tournament.id,
    )
    stored = await packets.publish(draft_id, administrator_id=manager.id)
    async with database.transaction() as session:
        assignment = await session.scalar(
            select(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == tournament.id,
                TournamentPacketAssignmentRecord.packet_id == stored.logical_id,
            )
        )
        assert assignment is not None and assignment.adopted_version_id is not None
        question_rows = (
            (
                await session.execute(
                    select(
                        PacketQuestionRecord.theme_revision_id,
                        PacketQuestionRecord.question_revision_id,
                        PacketQuestionRecord.value,
                    )
                    .where(
                        PacketQuestionRecord.packet_version_id == assignment.adopted_version_id
                    )
                    .order_by(PacketQuestionRecord.value)
                )
            ).all()
        )
    theme_revision_ids = {row[0] for row in question_rows}
    assert len(theme_revision_ids) == 1
    return ProfileFixture(
        tournament,
        manager,
        members,
        outsider,
        type_version.id,
        ruleset_version.id,
        assignment.adopted_version_id,
        assignment.id,
        question_rows[0][0],
        tuple((row[1], row[2]) for row in question_rows),
    )

async def record_game(
    database: Database,
    fixture: ProfileFixture,
    *,
    results: list[tuple[PlayerRecord, Decimal, Decimal]],
    attempts: dict[UUID, dict[int, bool]],
) -> UUID:
    """Insert the rows a finished SI game leaves behind, without running the game."""
    now = datetime.now(UTC)
    async with database.transaction() as session:
        policy = await session.scalar(
            select(TournamentPolicyVersionRecord)
            .where(TournamentPolicyVersionRecord.tournament_id == fixture.tournament.id)
            .order_by(TournamentPolicyVersionRecord.version.desc())
        )
        assert policy is not None
        game = GameRecord(
            tournament_id=fixture.tournament.id,
            tournament_type_version_id=fixture.type_version_id,
            game_ruleset_version_id=fixture.ruleset_version_id,
            tournament_policy_version_id=policy.id,
            host_player_id=fixture.manager.id,
            status="finalized",
            phase="finished",
            completed_at=now,
            finalized_at=now,
            assignment_plan={},
        )
        session.add(game)
        await session.flush()
        session.add_all(
            [
                GamePacketVersionRecord(
                    game_id=game.id,
                    packet_version_id=fixture.packet_version_id,
                    assignment_id=fixture.assignment_id,
                    selection_order=1,
                ),
                GameThemeRecord(
                    game_id=game.id,
                    theme_revision_id=fixture.theme_revision_id,
                    source_packet_version_id=fixture.packet_version_id,
                    position=1,
                ),
            ]
        )
        participants: dict[UUID, GameParticipantRecord] = {}
        for seat, (player, place, score) in enumerate(results, start=1):
            participant = GameParticipantRecord(
                tournament_id=fixture.tournament.id,
                game_id=game.id,
                player_id=player.id,
                seat=seat,
                rating_sequence=seat,
                global_game_sequence=seat,
                score=score,
                final_place=place,
                active=False,
            )
            session.add(participant)
            participants[player.id] = participant
        await session.flush()
        for sequence, (question_revision_id, value) in enumerate(fixture.questions, start=1):
            round_record = QuestionRoundRecord(
                game_id=game.id,
                question_revision_id=question_revision_id,
                sequence=sequence,
                status="completed",
                started_at=now,
                completed_at=now,
            )
            session.add(round_record)
            await session.flush()
            for player_id, answers in attempts.items():
                if value not in answers:
                    continue
                correct = answers[value]
                session.add(
                    AnswerAttemptRecord(
                        round_id=round_record.id,
                        participant_id=participants[player_id].id,
                        attempt_number=1,
                        submitted_answer="answer",
                        timed_out=False,
                        original_correct=correct,
                        final_correct=correct,
                    )
                )
        for player, place, score in results:
            session.add(
                GameResultRecord(
                    game_id=game.id,
                    player_id=player.id,
                    tournament_id=fixture.tournament.id,
                    tournament_policy_version_id=policy.id,
                    place=place,
                    score=score,
                    settled_at=now,
                )
            )
        session.add(
            RulesetRatingRecord(
                ruleset_key="si",
                player_id=results[0][0].id,
                rating=Decimal("1050"),
            )
        )
        session.add(
            RulesetRatingLedgerRecord(
                ruleset_key="si",
                tournament_id=fixture.tournament.id,
                game_id=game.id,
                player_id=results[0][0].id,
                rating_before=Decimal("1000"),
                delta=Decimal("50"),
                rating_after=Decimal("1050"),
                confidence_before=Decimal("0.5"),
                confidence_after=Decimal("0.55"),
                k_factor=Decimal("25"),
                tournament_weight=Decimal("1"),
                rating_model="time_weighted",
                reason="pairwise_elo",
                played_at=now,
            )
        )
    return game.id

async def test_profile_scopes_ruleset_privacy_and_game_cards(database_url: str) -> None:
    database = Database(database_url)
    fixture = await build_fixture(database)
    first, second = fixture.members
    game_id = await record_game(
        database,
        fixture,
        results=[
            (first, Decimal(1), Decimal(60)),
            (second, Decimal(2), Decimal(10)),
        ],
        attempts={
            first.id: {10: True, 20: True, 30: False},
            second.id: {40: False},
        },
    )
    service = PlayerProfileService(database)

    own = await service.profile(first.id, first.id, ruleset_key="si")
    assert own["player"]["real_name"] == first.real_name
    assert own["player"]["telegram_username"] == first.telegram_username
    assert own["ruleset_key"] == "si"
    assert [item["key"] for item in own["rulesets"]] == ["si"]
    assert own["rating"]["value"] == 1050.0
    assert len(own["rating"]["history"]) == 1
    assert own["stats"]["games"] == 1
    assert own["stats"]["wins"] == 1
    assert own["stats"]["win_rate"] == 100.0
    placements = {item["kind"]: item["count"] for item in own["stats"]["placements"]}
    assert placements["place_1"] == 1
    assert placements["place_1_5"] == 0
    assert placements["place_2_5"] == 0
    assert placements["place_3_5"] == 0
    assert own["si_question_stats"] == [
        {"value": 10, "correct": 1, "incorrect": 0},
        {"value": 20, "correct": 1, "incorrect": 0},
        {"value": 30, "correct": 0, "incorrect": 1},
    ]
    card = own["games"][0]
    assert card["game_id"] == game_id
    assert card["tournament_name"] == fixture.tournament.name
    assert [item["place"] for item in card["participants"]] == [Decimal(1), Decimal(2)]
    assert {item["nickname"] for item in card["participants"]} == {
        first.public_nickname,
        second.public_nickname,
    }

    outsider_view = await service.profile(fixture.outsider.id, first.id, ruleset_key="si")
    assert "real_name" not in outsider_view["player"]
    assert "telegram_username" not in outsider_view["player"]
    assert outsider_view["player"]["nickname"] == first.public_nickname
    assert outsider_view["games"][0]["tournament_name"] is None
    assert outsider_view["games"][0]["tournament_visible"] is False

    anonymous = await service.profile(None, first.id, ruleset_key="si")
    assert "real_name" not in anonymous["player"]
    assert "telegram_username" not in anonymous["player"]
    assert anonymous["games"][0]["tournament_name"] is None
    game = await service.game_results(None, first.id, game_id)
    assert game["tournament_name"] is None
    assert "question_text" not in str(game)

    await database.close()


async def test_profile_normalizes_custom_question_point_scale(database_url: str) -> None:
    database = Database(database_url)
    fixture = await build_fixture(database, values=(1, 2, 3, 4, 5), member_count=1)
    first = fixture.members[0]
    await record_game(
        database,
        fixture,
        results=[(first, Decimal(1), Decimal(20))],
        attempts={first.id: {1: True, 5: False}},
    )
    service = PlayerProfileService(database)

    profile = await service.profile(fixture.outsider.id, first.id, ruleset_key="si")
    assert profile["si_question_stats"] == [
        {"value": 10, "correct": 1, "incorrect": 0},
        {"value": 50, "correct": 0, "incorrect": 1},
    ]

    await database.close()


async def test_game_results_hide_private_content_and_tournament_name(database_url: str) -> None:
    database = Database(database_url)
    fixture = await build_fixture(database)
    first, second = fixture.members
    game_id = await record_game(
        database,
        fixture,
        results=[
            (first, Decimal(1), Decimal(60)),
            (second, Decimal(2), Decimal(10)),
        ],
        attempts={
            first.id: {10: True, 20: True},
            second.id: {30: False},
        },
    )
    service = PlayerProfileService(database)

    member_view = await service.game_results(second.id, first.id, game_id)
    assert member_view["tournament_name"] == fixture.tournament.name
    assert member_view["tournament_visible"] is True
    assert member_view["stage"] is None
    themes = member_view["themes"]
    assert len(themes) == 1
    questions = themes[0]["questions"]
    assert [question["value"] for question in questions] == [10, 20, 30, 40, 50]
    participant_by_player = {
        item["player_id"]: item["participant_id"] for item in member_view["participants"]
    }
    first_id = participant_by_player[first.id]
    second_id = participant_by_player[second.id]
    assert questions[0]["answers"] == {first_id: "correct"}
    assert questions[1]["answers"] == {first_id: "correct"}
    assert questions[2]["answers"] == {second_id: "incorrect"}
    assert questions[3]["answers"] == {}
    assert questions[4]["answers"] == {}
    # The grid must never expose theme names or question content.
    serialized = repr(member_view)
    assert "Secret theme name" not in serialized
    assert "Secret question text" not in serialized

    outsider_view = await service.game_results(fixture.outsider.id, first.id, game_id)
    assert outsider_view["tournament_name"] is None
    assert outsider_view["tournament_visible"] is False

    with pytest.raises(LookupError):
        await service.game_results(
            fixture.outsider.id, fixture.outsider.id, game_id
        )

    await database.close()


