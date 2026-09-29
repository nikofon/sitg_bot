"""Stable rejection codes for actions against a projected game state."""


def game_action_reason(view: dict, command: str) -> str:
    if view.get("dismissed"):
        return "left"
    status = view.get("status")
    if status not in {"active", "lobby"} and command not in {"quit", "report", "reputation"}:
        return "finished"
    player = next((p for p in view.get("participants", ()) if p.get("self")), None)
    if player is None and command != "observe_leave":
        return "observer"
    if command == "join":
        return (
            "already_joined" if player and player.get("joined") and player.get("active")
            else "reconnect_expired"
        )
    if player and (not player.get("active") or not player.get("joined")):
        return "not_joined"
    if status == "lobby":
        return "not_started"
    appeal = view.get("appeal") or {}
    if command in {"pause", "resume"} and appeal.get("status") in {
        "voting", "awaiting_escalation", "awaiting_commentary",
    }:
        return "appeal_pending"
    if view.get("paused") and command != "resume":
        return "paused"
    question = view.get("question") or {}
    if command in {"buzz", "answer"}:
        if view.get("phase") != "question" or not question:
            return "no_question"
        buzzer = question.get("accepted_buzzer_id")
        if command == "answer":
            return "not_your_answer"
        if buzzer is not None:
            return "already_buzzed" if buzzer == view.get("viewer_id") else "another_answering"
        if view.get("viewer_id") in question.get("attempted_player_ids", ()):
            return "already_attempted"
        if view.get("viewer_id") not in question.get("eligible_player_ids", ()):
            return "not_eligible"
        if not question.get("revealed_token_count"):
            return "not_revealed"
    if command == "pause":
        return (
            "pause_disabled" if not view.get("pausing_allowed", True)
            else "pause_between_questions"
        )
    if command == "resume":
        return "not_paused"
    if command == "appeal":
        if appeal.get("status") in {"voting", "awaiting_escalation", "awaiting_commentary"}:
            return "appeal_pending"
        return (
            "appeal_between_questions" if view.get("phase") != "intermission"
            else "no_appeal_targets"
        )
    if command in {"vote", "escalate", "commentary"}:
        return {
            "vote": "no_vote", "escalate": "no_escalation", "commentary": "no_commentary",
        }[command]
    if command in {"report", "reputation"}:
        return "results_required"
    if command == "themes":
        return "themes_unavailable"
    return "state_changed"
