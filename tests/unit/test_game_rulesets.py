from decimal import Decimal
from uuid import UUID

import pytest

from sitg_bot.domain.game_rulesets import (
    DEFAULT_RULESETS,
    ExposureClaim,
    PlayUnit,
    SIGameRuleset,
)
from sitg_bot.domain.game_settings import GameSettings
from sitg_bot.domain.packet import Packet, Question, Theme


def test_si_validates_configured_values_and_scores_fractional_penalties() -> None:
    ruleset = SIGameRuleset()
    settings = GameSettings(question_values=(100, 200, 300), minus_multiplier=0.25)
    packet = Packet(
        "Custom SI",
        (
            Theme(
                "Theme",
                tuple(Question(str(value), "answer", "", value) for value in (100, 200, 300)),
            ),
        ),
    )

    assert ruleset.validate_content(packet, settings) == ()
    assert ruleset.score_answer(300, True, settings) == Decimal("300")
    assert ruleset.score_answer(300, False, settings) == Decimal("-75.00")


def test_si_rejected_answers_take_precedence_over_accepted_answers() -> None:
    ruleset = SIGameRuleset()
    accepted = Question("Text", "Answer", "", 10, accepted_answers=("Alt",)).all_answers
    rejected = Question("Text", "Answer", "", 10).rejected_answers

    assert ruleset.judge_answer("Answer", accepted)
    assert ruleset.judge_answer("alt", accepted)
    assert ruleset.judge_answer("Answer", accepted, rejected_answers=rejected)  # empty list
    assert not ruleset.judge_answer(
        "Answer", accepted, rejected_answers=Question(
            "Text", "Answer", "", 10, rejected_answers=("Answer",)
        ).all_rejected_answers
    )
    assert ruleset.judge_answer(
        "Other", accepted, rejected_answers=("Answer",)
    ) is False


def test_ruleset_registry_rejects_unknown_versions() -> None:
    assert DEFAULT_RULESETS.get("si", 1).key == "si"

    try:
        DEFAULT_RULESETS.get("si", 2)
    except LookupError as error:
        assert "si version 2" in str(error)
    else:
        raise AssertionError("Unknown ruleset version was accepted")


@pytest.mark.parametrize(
    ("accepted", "submitted", "expected"),
    [
        ("Центр Помпиду", "центл Памптду", True),
        ("Центр Помпиду", "Це\u0301нтр Помпиду\u0301", True),
        ("Центр Помпиду", "центр Памптд", True),
        ("Центр Помпиду", "центры Помпиду", True),
        ("Центр Помпиду", "цинтл Памптдю", False),
        ("Центр Помпиду", "Лувр", False),
        ("Paris", "Parix", True),
        ("Paris", "Pxrix", False),
        ("Rome", "Roma", False),
        ("12345", "12346", False),
        ("Apollo 11", "Apollo 12", False),
        ("Apollo 11", "Apolo 11", True),
        ("Café", "CAFE", True),
        ("Pushkin", "Пу\u0301шкин", True),
        ("Chaykovskiy", "Чайковский", True),
        ("Borshch", "Борщ", True),
        ("Yozh", "Ёж", True),
        ("Obyekt", "Объект", True),
        ("Rus", "Русь", True),
        ("Пушкин", "Pushkin", False),
        ("Answer", "", False),
        ("Answer", "!!!", False),
        ("abcdefghijklmnopqrst", "abcdXXXXijklmnopqrst", False),
    ],
)
def test_si_answer_matching(accepted: str, submitted: str, expected: bool) -> None:
    assert SIGameRuleset().judge_answer(submitted, (accepted,)) is expected


def test_si_fuzzy_matching_includes_alternatives_and_optional_words() -> None:
    question = Question("Text", "Museum", "", 10, accepted_answers=("(Центр) Помпиду",))
    assert SIGameRuleset().judge_answer("Пампиду", question.all_answers)


@pytest.mark.parametrize(
    ("submitted", "accepted", "rejected", "expected"),
    [
        ("центл Памптду", "Центр Помпиду", "ЦЕНТЛ ПАМПТДУ!", False),
        ("центл Памптду", "Центр Помпиду", "центл Памптда", True),
        ("Це\u0301нтр Помпиду", "Центр Помпиду", "Центр Помпиду", False),
        ("CAFE", "Café", "Café", False),
        ("Пушкин", "Pushkin", "Пу\u0301шкин", False),
        ("Пушкин", "Pushkin", "Pushkin", True),
    ],
)
def test_si_rejected_matching_stays_exact(
    submitted: str, accepted: str, rejected: str, expected: bool,
) -> None:
    assert SIGameRuleset().judge_answer(
        submitted, (accepted,), rejected_answers=(rejected,),
    ) is expected


def test_si_assignment_plan_is_seeded_and_preserves_canonical_claims() -> None:
    ruleset = SIGameRuleset()
    unit = PlayUnit(
        kind="si_theme",
        logical_id=UUID(int=1),
        revision_id=UUID(int=2),
        packet_version_id=UUID(int=3),
        packet_order=1,
        position=1,
        question_revision_ids=(UUID(int=4),),
        claims=(
            ExposureClaim("theme", UUID(int=1)),
            ExposureClaim("question", UUID(int=5)),
        ),
        metadata={"question_values": [10, 20, 30, 40, 50]},
    )
    plan = ruleset.prepare_assignment(
        player_count=1,
        packet_version_ids=(UUID(int=3),),
        available_play_units=(unit,),
        parameters=GameSettings(theme_count=1),
        seed="auditable-seed",
    )

    assert plan.play_units == (unit,)
    assert plan.claims == unit.claims
    assert plan.to_dict()["seed"] == "auditable-seed"
