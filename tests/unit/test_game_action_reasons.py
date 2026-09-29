import pytest

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.domain.game_action_reasons import game_action_reason


@pytest.mark.parametrize(("command", "changes", "reason"), [
    ("buzz", {"paused": True}, "paused"),
    ("buzz", {"phase": "intermission"}, "no_question"),
    ("buzz", {"question": {"accepted_buzzer_id": "other"}}, "another_answering"),
    ("buzz", {"question": {"accepted_buzzer_id": "me"}}, "already_buzzed"),
    ("buzz", {"question": {"attempted_player_ids": ["me"]}}, "already_attempted"),
    ("buzz", {"question": {
        "eligible_player_ids": ["me"], "revealed_token_count": 0,
    }}, "not_revealed"),
    ("answer", {}, "not_your_answer"),
    ("pause", {"pausing_allowed": False}, "pause_disabled"),
    ("pause", {}, "pause_between_questions"),
    ("resume", {}, "not_paused"),
    ("appeal", {}, "appeal_between_questions"),
    ("appeal", {"phase": "intermission"}, "no_appeal_targets"),
    ("buzz", {"participants": []}, "observer"),
    ("buzz", {"status": "finalized"}, "finished"),
])
def test_unavailable_actions_have_localized_context(command, changes, reason):
    view = {
        "status": "active", "phase": "question", "viewer_id": "me",
        "participants": [{"self": True, "active": True, "joined": True}],
        "question": {"revealed_token_count": 1, "eligible_player_ids": ["me"]},
        **changes,
    }
    assert game_action_reason(view, command) == reason
    for locale in ("en", "ru"):
        assert LocalizationService().text("game.denied." + reason, locale)
