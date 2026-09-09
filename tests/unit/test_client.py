from typing import Any

import pytest

from sitg_bot.client import InteractiveConsole
from sitg_bot.domain.game_settings import GameSettings


class RecordingClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.info = {
            "id": "tournament-1",
            "name": "Test tournament",
            "slug": "test-tournament",
            "status": "active",
            "visibility": "private",
            "starts_at": None,
            "planned_ends_at": None,
            "type": "ladder",
            "type_version": 1,
            "game_ruleset": "si",
            "game_ruleset_version": 1,
            "policy_version": 1,
            "default_parameters": GameSettings().to_dict(),
            "player_mutable_parameters": [],
            "policies": {
                "rating_enabled": True,
                "hybrid_matchmaking_enabled": True,
            },
            "type_supports_hybrid_matchmaking": True,
            "hybrid_matchmaking_enabled": True,
            "membership_status": "active",
            "rating": 1000,
            "rating_sequence": 0,
            "manager": True,
        }

    async def request(self, action: str, **params: Any) -> Any:
        self.requests.append((action, params))
        if action in {"tournaments_mine", "tournaments_public"}:
            return {
                "ongoing": [
                    {
                        "id": "tournament-1",
                        "name": "Test tournament",
                        "slug": "test-tournament",
                        "status": "active",
                        "visibility": "public",
                        "starts_at": "2026-08-20T12:00:00+00:00",
                        "planned_ends_at": None,
                        "type_key": "ladder",
                        "type_version": 1,
                        "ruleset_key": "si",
                        "ruleset_version": 1,
                        "joinable": True,
                        "membership_status": ("active" if action == "tournaments_mine" else None),
                    }
                ],
                "future": [],
                "past": [],
            }
        if action == "tournament_info":
            return self.info
        if action == "tournament_create":
            return {"id": "created-tournament"}
        if action == "packets":
            return []
        if action == "lobby_create":
            return {
                "id": "lobby-1",
                "tournament_id": params["tournament_id"],
                "status": "assembling",
                "invitation_code": "invite",
                "max_players": params["max_players"],
                "searching": False,
                "hybrid_matchmaking_available": True,
                "other_searching_lobby_count": 0,
                "other_searching_player_count": 0,
                "selected_packets": [],
                "settings": GameSettings().to_dict(),
                "validation_violations": [],
                "members": [],
            }
        if action == "lobby_role":
            return {
                "id": params["lobby_id"],
                "tournament_id": "tournament-1",
                "status": "assembling",
                "invitation_code": "invite",
                "max_players": 4,
                "searching": False,
                "hybrid_matchmaking_available": True,
                "other_searching_lobby_count": 0,
                "other_searching_player_count": 0,
                "selected_packets": [],
                "settings": GameSettings().to_dict(),
                "validation_violations": [],
                "members": [],
            }
        if action == "manager_appeal_tickets":
            return [
                {
                    "id": "appeal-1",
                    "question_sequence": 4,
                    "question_text": "Question text",
                    "official_answer": "Official answer",
                    "kind": "accept_incorrect",
                    "submitted_answer": "Appealed answer",
                    "commentary": "Please accept",
                    "created_at": "2026-08-27T10:00:00+00:00",
                    "expires_at": "2026-08-28T10:00:00+00:00",
                }
            ]
        if action == "manager_appeal_decide":
            return {
                "closed": True,
                "appeal_id": params["appeal_id"],
                "approved": params["approve"],
                "source": "manager",
            }
        if action == "game_observe_list":
            return []
        if action == "game_observe":
            return {
                "joined": True,
                "confirmation_required": False,
                "fresh_content_count": 0,
                "events": [],
                "snapshot": {
                    "id": params["game_id"],
                    "tournament_id": "tournament-1",
                    "game_ruleset": "si",
                    "game_ruleset_version": 1,
                    "status": "active",
                    "phase": "intermission",
                    "version": 2,
                    "participants": [],
                    "observers": [],
                    "question": None,
                    "appeal": None,
                    "paused": False,
                },
            }
        if action.startswith("game_"):
            appeal = None
            if action in {"game_appeal", "game_appeal_vote", "game_appeal_escalate"}:
                appeal = {
                    "id": "appeal-1",
                    "round_id": "round-1",
                    "appellant_participant_id": "participant-1",
                    "target_attempt_id": "attempt-1",
                    "target_player_id": 101,
                    "submitted_answer": "Answer",
                    "kind": "accept_incorrect",
                    "status": "voting",
                    "voting_rule": "majority",
                    "electorate_size": 2,
                    "approvals": 0,
                    "rejections": 0,
                    "vote_deadline": "2026-08-27T10:01:00+00:00",
                    "escalation_enabled": True,
                    "escalation_deadline": None,
                    "commentary_deadline": None,
                    "ticket_expires_at": None,
                }
            return {
                "accepted": True,
                "events": [],
                "snapshot": {
                    "id": params["game_id"],
                    "tournament_id": "tournament-1",
                    "game_ruleset": "si",
                    "game_ruleset_version": 1,
                    "status": "active",
                    "phase": "intermission",
                    "version": 2,
                    "participants": [],
                    "question": None,
                    "appeal": appeal,
                    "paused": appeal is not None,
                },
            }
        return {"updated": True}


@pytest.fixture
def console() -> tuple[InteractiveConsole, RecordingClient]:
    client = RecordingClient()
    interactive = InteractiveConsole(client)  # type: ignore[arg-type]
    interactive.logged_in = True
    return interactive, client


async def test_tournament_selection_scopes_packet_and_lobby_commands(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console

    await interactive.execute("tournament use tournament-1")
    await interactive.execute("packets")
    await interactive.execute("lobby create 6")

    assert ("packets", {"tournament_id": "tournament-1"}) in client.requests
    assert (
        "lobby_create",
        {"tournament_id": "tournament-1", "max_players": 6},
    ) in client.requests


async def test_tournament_settings_and_mutability_update_complete_policy(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console
    interactive.current_tournament_id = "tournament-1"

    await interactive.execute('tournament setting question_values "[100, 200, 300]"')
    update = next(
        params for action, params in client.requests if action == "tournament_policy_update"
    )
    assert update["default_parameters"]["question_values"] == [100, 200, 300]
    assert update["policies"] == {
        "rating_enabled": True,
        "hybrid_matchmaking_enabled": True,
    }

    client.requests.clear()
    await interactive.execute("tournament mutable theme_count on")
    update = next(
        params for action, params in client.requests if action == "tournament_policy_update"
    )
    assert update["player_mutable_parameters"] == ["theme_count"]


async def test_tournament_creation_and_packet_entitlements_are_exposed(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console

    await interactive.execute(
        'tournament create secret summer-cup "Summer Cup" --type classic --ruleset si '
        "--visibility public --starts-at 2026-09-01T10:00:00+03:00 "
        "--planned-ends-at 2026-09-02T18:00:00+03:00"
    )
    assert interactive.current_tournament_id == "created-tournament"
    assert client.requests[-1] == (
        "tournament_create",
        {
            "token": "secret",
            "slug": "summer-cup",
            "name": "Summer Cup",
            "type": "classic",
            "game_ruleset": "si",
            "visibility": "public",
            "starts_at": "2026-09-01T10:00:00+03:00",
            "planned_ends_at": "2026-09-02T18:00:00+03:00",
        },
    )

    await interactive.execute("packet release packet-1 on version-1")
    assert client.requests[-1] == (
        "packet_library_release_set",
        {
            "packet_id": "packet-1",
            "released": True,
            "packet_version_id": "version-1",
        },
    )

    interactive.current_tournament_id = "tournament-1"
    await interactive.execute(
        "tournament packet entitlement assignment-1 player-1 content-visible on"
    )
    assert client.requests[-1] == (
        "tournament_packet_entitlement_set",
        {
            "assignment_id": "assignment-1",
            "player_id": "player-1",
            "rights": {"content_visible": True},
        },
    )

    await interactive.execute(
        'tournament requirement add has-not-seen-packet packet-1 "Packet already seen"'
    )
    assert client.requests[-1] == (
        "tournament_registration_requirement_add",
        {
            "tournament_id": "tournament-1",
            "kind": "has-not-seen-packet",
            "target_id": "packet-1",
            "failure_message": "Packet already seen",
        },
    )

    await interactive.execute(
        "tournament pricing set one-time "
        '\'[{"name":"Students","prices":[{"amount":10,"currency":"USD"}]}]\''
    )
    assert client.requests[-1] == (
        "tournament_metadata_update",
        {
            "tournament_id": "tournament-1",
            "payment_type": "one-time",
            "pricing_plans": [
                {
                    "name": "Students",
                    "prices": [{"amount": 10, "currency": "USD"}],
                }
            ],
        },
    )


async def test_tournament_discovery_and_membership_listing_use_separate_actions(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console

    await interactive.execute("tournament list public")
    await interactive.execute("tournament list mine")

    assert client.requests[-2:] == [
        ("tournaments_public", {}),
        ("tournaments_mine", {}),
    ]


@pytest.mark.parametrize(
    "command",
    (
        "tournaments",
        "tournament discover",
        "tournament list",
        "tournament info tournament-1",
        "packets tournament-1",
        "lobby packet packet-1",
        "lobby themes 8",
        "lobby settings",
        "lobby ready on",
        "lobby find stop",
        "blacklist list",
        "exit",
    ),
)
async def test_overlapping_command_aliases_are_not_available(
    console: tuple[InteractiveConsole, RecordingClient], command: str
) -> None:
    interactive, _ = console
    interactive.current_tournament_id = "tournament-1"
    interactive.current_lobby_id = "lobby-1"

    with pytest.raises(ValueError):
        await interactive.execute(command)


async def test_unknown_lobby_operation_is_rejected(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, _ = console
    interactive.current_lobby_id = "lobby-1"

    with pytest.raises(ValueError, match="Unknown lobby operation"):
        await interactive.execute("lobby unsupported-operation")


async def test_observer_commands_expose_confirmation_and_listing(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console
    interactive.current_tournament_id = "tournament-1"
    interactive.current_lobby_id = "lobby-1"

    await interactive.execute("lobby role observer confirm")
    await interactive.execute("game observe list")
    await interactive.execute("game observe game-2 confirm")

    assert client.requests[-3:] == [
        (
            "lobby_role",
            {
                "lobby_id": "lobby-1",
                "role": "observer",
                "confirm_fresh": True,
            },
        ),
        ("game_observe_list", {"tournament_id": "tournament-1"}),
        ("game_observe", {"game_id": "game-2", "confirm_fresh": True}),
    ]


async def test_game_appeal_commands_use_current_appeal_context(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console
    interactive.current_game_id = "game-1"

    await interactive.execute("game appeal attempt-1")
    await interactive.execute("game appeal vote approve")
    await interactive.execute("game appeal escalate on")
    await interactive.execute('game appeal comment "Judge commentary"')

    assert client.requests[-4:] == [
        ("game_appeal", {"game_id": "game-1", "target_attempt_id": "attempt-1"}),
        (
            "game_appeal_vote",
            {"game_id": "game-1", "appeal_id": "appeal-1", "approve": True},
        ),
        (
            "game_appeal_escalate",
            {"game_id": "game-1", "appeal_id": "appeal-1", "escalate": True},
        ),
        (
            "game_appeal_commentary",
            {
                "game_id": "game-1",
                "appeal_id": "appeal-1",
                "commentary": "Judge commentary",
            },
        ),
    ]


async def test_end_game_trust_commands_are_exposed(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console
    interactive.current_game_id = "game-1"

    await interactive.execute("game reputation downvote 202")
    await interactive.execute('game report cheating 202 "Suspicious timing"')

    assert client.requests[-2:] == [
        (
            "game_reputation_vote",
            {"game_id": "game-1", "value": -1, "telegram_user_id": 202},
        ),
        (
            "game_report",
            {
                "game_id": "game-1",
                "kind": "cheating",
                "telegram_user_id": 202,
                "details": "Suspicious timing",
            },
        ),
    ]


async def test_admin_suspicion_review_and_clear_commands_are_exposed(
    console: tuple[InteractiveConsole, RecordingClient],
) -> None:
    interactive, client = console

    await interactive.execute("admin suspicion review 00000000-0000-0000-0000-000000000001 5")
    await interactive.execute(
        'admin suspicion clear 00000000-0000-0000-0000-000000000001 "Reviewed manually"'
    )

    assert client.requests[-2:] == [
        (
            "admin_suspicion_review",
            {
                "player_id": "00000000-0000-0000-0000-000000000001",
                "limit": 5,
            },
        ),
        (
            "admin_suspicion_clear",
            {
                "player_id": "00000000-0000-0000-0000-000000000001",
                "note": "Reviewed manually",
            },
        ),
    ]


async def test_manager_appeal_ticket_commands_are_tournament_scoped_and_anonymous(
    console: tuple[InteractiveConsole, RecordingClient], capsys: pytest.CaptureFixture[str]
) -> None:
    interactive, client = console
    interactive.current_tournament_id = "tournament-1"

    await interactive.execute("appeals")
    output = capsys.readouterr().out
    assert "Anonymous appeal tickets" in output
    assert "Question text" in output
    assert "player" not in output.casefold()
    assert client.requests[-1] == (
        "manager_appeal_tickets",
        {"tournament_id": "tournament-1"},
    )

    await interactive.execute("appeals decide appeal-1 reject")
    assert client.requests[-1] == (
        "manager_appeal_decide",
        {"appeal_id": "appeal-1", "approve": False},
    )
