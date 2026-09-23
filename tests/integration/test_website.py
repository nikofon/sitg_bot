import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import pytest
from aiohttp.test_utils import make_mocked_request
from test_lobby_architecture import database_url as _database_url
from test_player_profiles import active_player, build_fixture, record_game

from sitg_bot.application.contracts import ActionCode
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import (
    MiniAppAuthenticationError,
    MiniAppAuthService,
    MiniAppCsrfError,
)
from sitg_bot.services.profiles import PlayerProfileService
from sitg_bot.services.tournaments import TournamentService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    GameRecord,
    GameRulesetVersionRecord,
    PlayerRecord,
    RulesetRatingRecord,
    TournamentRecord,
)

pytestmark = pytest.mark.integration
database_url = _database_url
BOT_TOKEN = "123456:website-test-secret"
ORIGIN = "https://website.example.test"


def login_data(telegram_user_id: int) -> dict[str, str]:
    values = {"id": str(telegram_user_id), "auth_date": str(int(datetime.now(UTC).timestamp()))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    values["hash"] = hmac.new(
        hashlib.sha256(BOT_TOKEN.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    return values


def auth_service(database: Database) -> MiniAppAuthService:
    return MiniAppAuthService(
        database, bot_token=BOT_TOKEN, session_signing_key="w" * 32,
        allowed_origins={ORIGIN, "https://mini.example.test"},
    )


async def test_website_session_survives_restart_and_enforces_csrf_origin_and_logout(
    database_url: str,
) -> None:
    database = Database(database_url)
    try:
        telegram_id = int(secrets.token_hex(6), 16)
        auth = auth_service(database)
        credentials = await auth.create_website_session(login_data(telegram_id), origin=ORIGIN)
        restored = await auth_service(database).resume_website_session(
            credentials.session_token, origin=ORIGIN
        )
        viewer = await auth.website_viewer(credentials.session_token, origin=ORIGIN)
        assert viewer["is_admin"] is False and viewer["is_manager"] is False
        assert viewer["registration_status"] == "registration"
        context = await auth.authorize_request(
            credentials.session_token, origin=ORIGIN, method="POST",
            action=ActionCode.TOURNAMENT_REGISTER, csrf_token=restored.csrf_token,
            idempotency_key="website-test-request", content_type="application/json",
        )
        assert context.principal.telegram_user_id == telegram_id
        assert str(context.principal.player_id) == viewer["player_id"]
        refreshed = await auth.refresh_session(
            credentials.session_token, origin=ORIGIN, csrf_token=restored.csrf_token,
            idempotency_key="website-test-refresh", content_type="application/json",
        )
        assert refreshed.csrf_token == restored.csrf_token
        with pytest.raises(MiniAppCsrfError):
            await auth.authorize_request(
                credentials.session_token, origin=ORIGIN, method="POST",
                action=ActionCode.TOURNAMENT_REGISTER,
                idempotency_key="website-test-request", content_type="application/json",
            )
        with pytest.raises(MiniAppAuthenticationError):
            await auth.resume_website_session(credentials.session_token, origin="https://evil.test")
        with pytest.raises(MiniAppAuthenticationError):
            await auth.resume_website_session(
                credentials.session_token, origin="https://mini.example.test"
            )
        with pytest.raises(MiniAppCsrfError):
            await auth.logout_website_session(
                credentials.session_token, origin=ORIGIN, csrf_token="wrong-token",
                idempotency_key="website-test-logout", content_type="application/json",
            )
        await auth.logout_website_session(
            credentials.session_token, origin=ORIGIN, csrf_token=restored.csrf_token,
            idempotency_key="website-test-logout", content_type="application/json",
        )
        with pytest.raises(MiniAppAuthenticationError):
            await auth.resume_website_session(credentials.session_token, origin=ORIGIN)
    finally:
        await database.close()


async def test_player_directory_search_sort_pagination_and_privacy(database_url: str) -> None:
    database = Database(database_url)
    try:
        prefix = "Website_" + secrets.token_hex(6)
        suffix = int(secrets.token_hex(6), 16)
        alice = active_player(prefix + " Alice", suffix, 0)
        bob = active_player(prefix + " Bob", suffix, 1)
        hidden = PlayerRecord(telegram_user_id=suffix + 2, public_nickname=prefix + " Draft")
        single = active_player(prefix + " Single", suffix, 3)
        zero = active_player(prefix + " Zero", suffix, 4)
        async with database.transaction() as session:
            session.add_all([bob, alice, hidden, single, zero])
        fixture = await build_fixture(database)
        await record_game(database, fixture, results=[
            (alice, Decimal(1), Decimal(30)), (bob, Decimal(2), Decimal(20)),
            (single, Decimal(3), Decimal(10)),
        ], attempts={})
        second_game = await record_game(database, fixture, results=[
            (bob, Decimal(1), Decimal(30)), (alice, Decimal(2), Decimal(20)),
        ], attempts={})
        async with database.transaction() as session:
            rating = await session.get(RulesetRatingRecord, ("si", alice.id))
            rating.rating = Decimal("1250.5")
            alternate = GameRulesetVersionRecord(key=prefix, version=1, name="Another ruleset")
            session.add(alternate)
            session.add(RulesetRatingRecord(ruleset_key=prefix, player_id=alice.id, rating=9000))
        profiles = PlayerProfileService(database)
        first = await profiles.list_players(search=prefix.lower(), limit=1)
        assert first["total"] == 2
        assert first["ruleset_key"] == "si"
        assert {item["key"] for item in first["rulesets"]} >= {"si", prefix}
        assert first["items"] == [{
            "id": str(alice.id), "label": alice.public_nickname, "rating": 1250.5, "games": 2,
        }]
        second = await profiles.list_players(search=prefix, offset=first["next_offset"], limit=1)
        assert second["items"][0]["id"] == str(bob.id)
        assert second["next_offset"] is None
        descending = await profiles.list_players(search=prefix, order="name_desc")
        assert descending["items"][0]["id"] == str(bob.id)
        assert descending["supported_orders"] == [
            "name_asc", "name_desc", "rating_asc", "rating_desc",
            "games_asc", "games_desc",
        ]
        with pytest.raises(ValueError):
            await profiles.list_players(search=prefix, order="rating_descending")
        assert (await profiles.list_players(search=prefix + "%"))["total"] == 0
        assert (await profiles.list_players(search=prefix, ruleset_key=prefix))["total"] == 0
        async with database.transaction() as session:
            game = await session.get(GameRecord, second_game)
            game.game_ruleset_version_id = alternate.id
        # One result in each ruleset does not qualify as two games in either one.
        assert (await profiles.list_players(search=prefix, ruleset_key="si"))["total"] == 0
        assert (await profiles.list_players(search=prefix, ruleset_key=prefix))["total"] == 0
        public = await profiles.profile(None, alice.id)
        assert "real_name" not in public["player"]
        assert "telegram_username" not in public["player"]
        own = await profiles.profile(alice.id, alice.id)
        assert own["player"]["real_name"] == alice.real_name
        with pytest.raises(LookupError):
            await profiles.profile(None, hidden.id)
    finally:
        await database.close()


async def test_website_public_tournaments_and_manager_authorization(database_url: str) -> None:
    database = Database(database_url)
    try:
        fixture = await build_fixture(database)
        tournaments = TournamentService(database)
        private = await tournaments.list_visible(None, search=fixture.tournament.slug)
        assert private.total == 0
        with pytest.raises(LookupError):
            await tournaments.tournament_details(fixture.tournament.id, None)
        with pytest.raises(PermissionError):
            await tournaments.list_visible(None, role="admin")
        with pytest.raises(PermissionError):
            await tournaments.list_visible(None, role="manager")
        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament.id)
            tournament.visibility = "public"
            tournament.status = "active"
        public = await tournaments.list_visible(None, search=fixture.tournament.slug)
        assert public.total == 1
        assert tuple(public.items[0].available_actions) == ("info",)
        managed = await tournaments.list_visible(
            fixture.manager.id, search=fixture.tournament.slug, include_managed_public=True
        )
        assert managed.total == 1 and managed.items[0].managed
        auth = auth_service(database)
        http = MiniAppHttpServer(auth, ApplicationGateway(database), website_origins={ORIGIN})
        public_request = make_mocked_request(
            "GET", "/api/miniapp/routes/resolve?path=/tournaments", headers={"Origin": ORIGIN}
        )
        assert (await http._headers(public_request, http._resolve_route)).status == 200
        for player, expected_status in [(fixture.manager, 200), (fixture.outsider, 403)]:
            credentials = await auth.create_website_session(
                login_data(player.telegram_user_id), origin=ORIGIN
            )
            request = make_mocked_request(
                "GET", "/api/miniapp/routes/resolve?path="
                f"/manager/tournaments/{fixture.tournament.id}/management",
                headers={
                    "Origin": ORIGIN,
                    "Cookie": f"__Host-sitg_session={credentials.session_token}",
                },
            )
            response = await http._headers(request, http._resolve_route)
            assert response.status == expected_status
    finally:
        await database.close()


@pytest.mark.parametrize("with_origin", [True, False])
async def test_guest_routes_use_public_projections(database_url: str, with_origin: bool) -> None:
    database = Database(database_url)
    try:
        fixture = await build_fixture(database)
        origin = "http://127.0.0.1:5174"
        auth = MiniAppAuthService(
            database, bot_token=BOT_TOKEN, session_signing_key="w" * 32,
            allowed_origins={origin, "https://mini.example.test"},
        )
        http = MiniAppHttpServer(auth, ApplicationGateway(database), website_origins={origin})
        headers = {"Host": "127.0.0.1:5174"}
        if with_origin:
            headers["Origin"] = origin

        async def resolve(path: str):
            request = make_mocked_request(
                "GET", f"/api/miniapp/routes/resolve?path={quote(path, safe='')}", headers=headers
            )
            return await http._headers(request, http._resolve_route)

        players = await resolve("/players")
        assert players.status == 200
        assert json.loads(players.text)["resource"]["kind"] == "players"
        for item in json.loads(players.text)["resource"]["items"]:
            assert set(item) == {"id", "label", "rating", "games"}
        path = f"/tournaments?include_managed_public=true&search={fixture.tournament.slug}"
        private_list = await resolve(path)
        assert private_list.status == 200
        assert json.loads(private_list.text)["resource"]["items"] == []
        assert (await resolve(f"/tournaments/{fixture.tournament.id}")).status == 404

        async with database.transaction() as session:
            tournament = await session.get(TournamentRecord, fixture.tournament.id)
            tournament.visibility = "public"
            tournament.status = "active"
        public = await resolve(path)
        assert public.status == 200
        resource = json.loads(public.text)["resource"]
        assert resource["kind"] == "tournaments"
        assert resource["total"] == 1
        assert resource["items"][0]["id"] == str(fixture.tournament.id)
        assert resource["items"][0]["available_actions"] == ["info"]
        assert (await resolve(f"/tournaments/{fixture.tournament.id}")).status == 200
        for path in ("/library", f"/manager/tournaments/{fixture.tournament.id}/management"):
            response = await resolve(path)
            assert response.status == 401
            assert json.loads(response.text)["error"]["code"] == "authentication_required"
    finally:
        await database.close()


async def test_player_directory_orders_by_rating_and_games(database_url: str) -> None:
    database = Database(database_url)
    try:
        prefix = "Website_" + secrets.token_hex(6)
        suffix = int(secrets.token_hex(6), 16)
        alice = active_player(prefix + " Alice", suffix, 0)
        bob = active_player(prefix + " Bob", suffix, 1)
        carol = active_player("Unlisted Carol", suffix, 2)
        dave = active_player("Unlisted Dave", suffix, 3)
        erin = active_player("Unlisted Erin", suffix, 4)
        async with database.transaction() as session:
            session.add_all([alice, bob, carol, dave, erin])
        fixture = await build_fixture(database)
        # Each game's first player receives the rating row in record_game, so
        # every game needs a distinct leader; seats double as the per-player
        # game sequence, so repeat players need distinct seats. Alice ends with
        # two settled SI games and a 1250.5 rating; Bob ends with three at 1050.
        await record_game(database, fixture, results=[
            (alice, Decimal(1), Decimal(30)), (bob, Decimal(2), Decimal(20)),
        ], attempts={})
        await record_game(database, fixture, results=[
            (bob, Decimal(1), Decimal(30)), (carol, Decimal(2), Decimal(20)),
        ], attempts={})
        await record_game(database, fixture, results=[
            (dave, Decimal(1), Decimal(30)), (erin, Decimal(2), Decimal(20)),
            (bob, Decimal(3), Decimal(10)),
        ], attempts={})
        await record_game(database, fixture, results=[
            (carol, Decimal(1), Decimal(30)), (alice, Decimal(2), Decimal(20)),
        ], attempts={})
        async with database.transaction() as session:
            rating = await session.get(RulesetRatingRecord, ("si", alice.id))
            assert rating is not None
            rating.rating = Decimal("1250.5")
        profiles = PlayerProfileService(database)

        by_rating_desc = await profiles.list_players(search=prefix, order="rating_desc")
        assert [item["id"] for item in by_rating_desc["items"]] == [
            str(alice.id), str(bob.id),
        ]
        by_rating_asc = await profiles.list_players(search=prefix, order="rating_asc")
        assert [item["id"] for item in by_rating_asc["items"]] == [
            str(bob.id), str(alice.id),
        ]
        by_games_desc = await profiles.list_players(search=prefix, order="games_desc")
        assert [item["id"] for item in by_games_desc["items"]] == [
            str(bob.id), str(alice.id),
        ]
        by_games_asc = await profiles.list_players(search=prefix, order="games_asc")
        assert [item["id"] for item in by_games_asc["items"]] == [
            str(alice.id), str(bob.id),
        ]
        assert by_games_desc["total"] == 2
    finally:
        await database.close()
