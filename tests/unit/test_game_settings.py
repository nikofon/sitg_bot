import pytest

from sitg_bot.domain.game_settings import GameSettings, announcement_tokens


def test_announcement_tokens_group_short_words_and_isolate_long_words() -> None:
    assert announcement_tokens("a few extraordinarilylong words fit", 10) == (
        "a few",
        "extraordinarilylong",
        "words fit",
    )


def test_game_settings_support_instantaneous_reveal_and_validated_updates() -> None:
    settings = GameSettings().updated(
        {
            "question_token_delay": 0,
            "question_cost_announcement_delay": 1.25,
            "buzz_timer_countdown_delay": 0.5,
            "between_questions_delay": 2,
            "theme_author_to_commentary_delay": 1.5,
            "theme_commentary_to_question_delay": 2.5,
            "pausing_allowed": False,
        }
    )
    assert settings.question_token_delay == 0
    assert settings.question_cost_announcement_delay == 1.25
    assert settings.buzz_timer_countdown_delta.total_seconds() == 0.5
    assert settings.between_questions_delta.total_seconds() == 2
    assert settings.theme_author_to_commentary_delta.total_seconds() == 1.5
    assert settings.theme_commentary_to_question_delta.total_seconds() == 2.5
    assert settings.pausing_allowed is False
    with pytest.raises(ValueError, match="Unknown game setting"):
        settings.updated({"not_a_setting": 1})


def test_theme_commentary_delays_default_and_reject_invalid_values() -> None:
    defaults = GameSettings()
    assert defaults.theme_author_to_commentary_delay == 3.0
    assert defaults.theme_commentary_to_question_delay == 3.0
    with pytest.raises(ValueError, match="finite non-negative"):
        GameSettings().updated({"theme_author_to_commentary_delay": -1})
    with pytest.raises(ValueError, match="must be a number"):
        GameSettings().updated({"theme_commentary_to_question_delay": True})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"buzz_timeout": -0.1}, "finite non-negative"),
        ({"answer_timeout": float("inf")}, "finite non-negative"),
        ({"ready_delay": True}, "must be a number"),
        ({"question_token_target_chars": 0}, "between 1 and 200"),
        ({"question_token_target_chars": True}, "must be an integer"),
        ({"pausing_allowed": 1}, "true or false"),
    ],
)
def test_game_settings_reject_invalid_values(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GameSettings().updated(changes)


def test_game_settings_restore_known_values_from_persisted_mapping() -> None:
    settings = GameSettings.from_mapping(
        {"buzz_timeout": 4.5, "pausing_allowed": False, "future_setting": "ignored"}
    )

    assert settings.buzz_timeout == 4.5
    assert settings.buzz_delta.total_seconds() == 4.5
    assert settings.pausing_allowed is False
    assert GameSettings.from_mapping(None) == GameSettings()


@pytest.mark.parametrize(
    ("text", "target", "expected"),
    [
        ("", 5, ()),
        ("one two three", 7, ("one two", "three")),
        ("lengthy tail", 7, ("lengthy", "tail")),
    ],
)
def test_announcement_tokens_cover_empty_and_boundary_inputs(
    text: str, target: int, expected: tuple[str, ...]
) -> None:
    assert announcement_tokens(text, target) == expected
