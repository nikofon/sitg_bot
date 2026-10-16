from uuid import UUID

import pytest
from pydantic import ValidationError

from sitg_bot.application.contracts import (
    ActionCode,
    TrainingActOperation,
    TrainingStartOperation,
)
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.domain.training import (
    TrainingSettings,
    best_buzz_cursor,
    normal_game_modifiers,
    text_digest,
)


def test_reverse_changes_word_order_and_preserves_source():
    source = "One  two\nthree <four>"
    settings = TrainingSettings(reversed=True, token_target_chars=200)
    assert settings.tokens(source) == ("<four> three two One",)
    assert source == "One  two\nthree <four>"


@pytest.mark.parametrize(
    "values",
    [
        {"best_buzz": True, "reversed": True},
        {"best_buzz": True, "hidden_after": 3},
        {"hidden_after": 10},
        {"hidden_after": 0},
        {"token_delay": float("nan")},
        {"reversed": "false"},
        {"token_target_chars": True},
        {"version": True},
    ],
)
def test_invalid_modifier_combinations_and_settings_are_rejected(values):
    with pytest.raises(ValueError):
        TrainingSettings(**values)


def test_hidden_and_reverse_combine_and_duration_is_configurable():
    settings = TrainingSettings(reversed=True, hidden_after=2.5)
    assert TrainingSettings.from_mapping(settings.to_dict()) == settings


def test_best_buzz_maps_source_characters_to_packed_tokens():
    source = "One  two\nthree four five"
    revision = UUID(int=1)
    analysis = {
        "schema_version": 1,
        "tokenizer_version": "announcement-v1",
        "question_revision_id": str(revision),
        "text_digest": text_digest(source),
        "source_end": 8,
    }
    assert best_buzz_cursor(source, revision_id=revision, analysis=analysis, target_chars=14) == 1
    assert best_buzz_cursor(source, revision_id=revision, analysis=analysis, target_chars=3) == 2
    for invalid in (
        {"source_end": 7},
        {"source_end": 100},
        {"source_end": True},
        {"text_digest": "other"},
        {"question_revision_id": str(UUID(int=2))},
        {"tokenizer_version": "provider-tokens"},
    ):
        with pytest.raises(ValueError):
            best_buzz_cursor(
                source, revision_id=revision, analysis={**analysis, **invalid}, target_chars=14
            )


def test_normal_game_modifiers_are_disabled_by_default():
    normal_game_modifiers(())
    with pytest.raises(ValueError):
        normal_game_modifiers(("reversed",))


def test_unknown_settings_are_validation_errors():
    with pytest.raises(ValueError, match="Unknown training setting"):
        TrainingSettings.from_mapping({"player_id": "someone-else"})


def test_training_mutations_require_idempotency_and_do_not_trust_actor_or_analysis():
    for action in (
        ActionCode.TRAINING_START,
        ActionCode.TRAINING_ACT,
        ActionCode.TRAINING_UPLOAD,
        ActionCode.TRAINING_DRAFT_DECIDE,
    ):
        assert ACTION_POLICIES[action].idempotency_required
        assert ACTION_POLICIES[action].authentication_required
    for extra in (
        {"player_id": str(UUID(int=2))},
        {"best_buzz": {"source_end": 1}},
        {"confirm": "false"},
    ):
        with pytest.raises(ValidationError):
            TrainingStartOperation.model_validate(
                {
                    "action": ActionCode.TRAINING_START,
                    "version_id": UUID(int=1),
                    **extra,
                }
            )
    with pytest.raises(ValidationError):
        TrainingActOperation.model_validate(
            {
                "action": ActionCode.TRAINING_ACT,
                "session_id": UUID(int=1),
                "expected_version": 1,
                "question_revision_id": UUID(int=2),
                "command": "invent",
            }
        )
