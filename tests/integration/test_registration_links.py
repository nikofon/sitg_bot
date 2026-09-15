import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_lobby_architecture import database_url as _database_url
from test_lobby_architecture import tournament_fixture

from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PregameLobbyRecord,
    TournamentMembershipRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
    TournamentRegistrationAttemptRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url


async def open_registration(database, fixture, *, auto=False):
    async with database.transaction() as session:
        tournament = await session.get(TournamentRecord, fixture.tournament_id)
        tournament.registration_open = True
        policy = await session.scalar(select(TournamentPolicyVersionRecord).where(
            TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id,
        ))
        policy.policies = {**policy.policies, "auto_approve_registrations": auto}


@pytest.mark.parametrize("type_key, auto, status", [
    ("ladder", False, "registered"), ("ladder", True, "active"),
    ("classic", True, "approved"),
])
async def test_private_shared_invitation_and_auto_approval(database_url, type_key, auto, status):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1, type_key=type_key)
        outsider = (await tournament_fixture(database, player_count=1)).players[0]
        service = TournamentService(database)
        await open_registration(database, fixture, auto=auto)
        link = await service.registration_link(fixture.tournament_id, fixture.manager.id)
        assert link == await service.registration_link(fixture.tournament_id, fixture.players[0].id)
        assert link["visibility"] == "private"
        with pytest.raises(PermissionError):
            await service.registration_link(fixture.tournament_id, outsider.id)
        with pytest.raises(PermissionError, match="invitation"):
            await service.register(fixture.tournament_id, outsider.id)
        preview = await service.registration_invitation(link["reference"], outsider.id)
        assert preview["registration_open"] is True
        assert preview["membership_status"] is None
        decision = await service.register(
            fixture.tournament_id, outsider.id, invitation_reference=link["reference"],
        )
        assert decision.accepted and decision.status == status
        async with database.sessions() as session:
            membership = await session.get(
                TournamentMembershipRecord, (fixture.tournament_id, outsider.id),
            )
            assert (membership.approved_at is not None) is auto
            assert membership.approved_by_id is None
            assert (membership.participation_confirmed_at is not None) is (status == "active")
        with pytest.raises(ValueError, match="already"):
            await service.register(
                fixture.tournament_id, outsider.id, invitation_reference=link["reference"],
            )
        with pytest.raises(PermissionError, match="managers"):
            await service.register(
                fixture.tournament_id, fixture.manager.id, invitation_reference=link["reference"],
            )
    finally:
        await database.close()


async def test_window_requirements_and_cross_tournament_links_are_rechecked(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        outsider = other.players[0]
        service = TournamentService(database)
        await open_registration(database, fixture, auto=True)
        link = await service.registration_link(fixture.tournament_id, fixture.manager.id)
        other_link = await service.registration_link(other.tournament_id, other.manager.id)
        with pytest.raises(PermissionError, match="another tournament"):
            await service.register(
                fixture.tournament_id, outsider.id, invitation_reference=other_link["reference"],
            )
        await service.add_registration_requirement(
            fixture.tournament_id, fixture.manager.id, kind="has-played-tournament",
            target_id=other.tournament_id, failure_message="Play the qualifying tournament first.",
        )
        decision = await service.register(
            fixture.tournament_id, outsider.id, invitation_reference=link["reference"],
        )
        assert not decision.accepted and decision.status == "rejected"
        async with database.sessions() as session:
            attempt = await session.scalar(select(TournamentRegistrationAttemptRecord).where(
                TournamentRegistrationAttemptRecord.tournament_id == fixture.tournament_id,
                TournamentRegistrationAttemptRecord.player_id == outsider.id,
            ))
            assert attempt.accepted is False
            assert attempt.failure_reasons == ["Play the qualifying tournament first."]
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.registration_ends_at = datetime.now(UTC) - timedelta(seconds=1)
        assert not (await service.registration_invitation(link["reference"], outsider.id))[
            "registration_open"
        ]
        with pytest.raises(ValueError, match="closed"):
            await service.register(
                fixture.tournament_id, outsider.id, invitation_reference=link["reference"],
            )
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.registration_open_override = True
        assert (await service.registration_invitation(link["reference"], outsider.id))[
            "registration_open"
        ]
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament_id)
            tournament.finalized_at = None
        with pytest.raises(LookupError):
            await service.registration_invitation(link["reference"], outsider.id)
    finally:
        await database.close()


async def test_lobby_invitation_registers_nonparticipant_and_serializes_duplicates(database_url):
    database = Database(database_url)
    try:
        fixture = await tournament_fixture(database, player_count=1)
        other = await tournament_fixture(database, player_count=1)
        service = TournamentService(database)
        await open_registration(database, fixture, auto=True)
        lobbies = InvitationMatchmakingService(database)
        lobby = await lobbies.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        reference = f"join_{lobby.invitation_code}"
        with pytest.raises(PermissionError, match="membership"):
            await lobbies.join(lobby.invitation_code, other.inputs[0])
        preview = await service.registration_invitation(reference, other.players[0].id)
        assert preview["membership_status"] is None
        decisions = await asyncio.gather(*(
            service.register(
                fixture.tournament_id, other.players[0].id, invitation_reference=reference,
            ) for _ in range(2)
        ), return_exceptions=True)
        assert sum(isinstance(result, ValueError) for result in decisions) == 1
        assert sum(getattr(result, "status", None) == "active" for result in decisions) == 1
        joined = await lobbies.join(lobby.invitation_code, other.inputs[0])
        assert len(joined.members) == 2
        async with database.transaction() as session:
            record = await session.scalar(select(PregameLobbyRecord).where(
                PregameLobbyRecord.invitation_code == lobby.invitation_code,
            ))
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(LookupError, match="Invitation not found"):
            await service.registration_invitation(reference, other.players[0].id)
    finally:
        await database.close()
