"""Representative signed Telegram launch -> real auth -> training HTTP route.

Only account/storage and gateway effects are in-memory. No runtime DB or real
Telegram initData is used. Keep empty keyboard launches rejected, not trusted.
"""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode
from uuid import UUID

import pytest
from aiogram.types import Message
from aiohttp.test_utils import make_mocked_request
from test_training_ui_boundary import training_navigation

from sitg_bot.application.contracts import ActionCode, GatewayResponse
from sitg_bot.bot.handlers.training import router
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.miniapp_auth import MiniAppAuthService

TOKEN = "123456:fixture-only"
ORIGIN = "https://mini.example.test"


def signed_init_data(age=0):
    values = {
        "auth_date": str(int((datetime.now(UTC) - timedelta(seconds=age)).timestamp())),
        "query_id": "fixture-query",
        "user": json.dumps({"id": 42, "first_name": "Player"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class MemoryAuthStorage:
    def __init__(self, locale):
        self.record = None
        self.player = SimpleNamespace(
            id=UUID(int=2),
            telegram_user_id=42,
            status="active",
            preferred_locale=locale,
            telegram_public=False,
        )

    @asynccontextmanager
    async def transaction(self):
        yield self

    def add(self, record):
        record.id = UUID(int=3)
        self.record = record

    async def scalar(self, statement):
        params = statement.compile().params
        return self.record if self.record and self.record.token_digest in params.values() else None

    async def get(self, model, identifier):
        assert identifier == self.player.id
        return self.player


def setup_http(environment, locale="en"):
    database = MemoryAuthStorage(locale)
    accounts = SimpleNamespace(
        create_or_resume_registration=AsyncMock(
            return_value=SimpleNamespace(
                account=SimpleNamespace(player_id=database.player.id, preferred_locale=locale)
            )
        )
    )
    auth = MiniAppAuthService(
        database,
        bot_token=TOKEN,
        session_signing_key="s" * 32,
        allowed_origins={ORIGIN},
        environment=environment,
        player_accounts=accounts,
    )
    gateway = SimpleNamespace(
        execute=AsyncMock(
            return_value=GatewayResponse(
                action=ActionCode.TRAINING_CURRENT,
                correlation_id=UUID(int=1),
                ok=True,
                data={"session": None, "lobby": {"version": 1, "version_id": None}},
            )
        )
    )
    return MiniAppHttpServer(auth, gateway), database, accounts, gateway


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("environment", ["test", "production"])
async def test_inline_launch_authenticates_training_route(locale, environment):
    loc = LocalizationService()
    backend = SimpleNamespace(
        _execute=AsyncMock(return_value=SimpleNamespace(data={})),
        navigation=AsyncMock(return_value=training_navigation(lobby=True)),
    )

    def message(text):
        from aiogram.types import Chat

        return Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text=text
        )

    dependencies = dict(
        backend=backend,
        telegram_update_claim=object(),
        localization=loc,
        locale=locale,
        launch_links=ORIGIN,
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.propagate_event(
            update_type="message",
            event=message(loc.text("button.training.train", locale)),
            navigation=training_navigation(),
            **dependencies,
        )
        reply = answer.await_args.kwargs["reply_markup"]
        assert reply.keyboard[0][0].web_app is None
        await router.propagate_event(
            update_type="message",
            event=message(reply.keyboard[0][0].text),
            navigation=training_navigation(lobby=True),
            **dependencies,
        )
        launch = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url
        assert launch == ORIGIN + "/training?_launch=1"
        assert "token" not in launch and "user" not in launch  # URL is not identity

    http, database, accounts, gateway = setup_http(environment, locale)
    http._json_body = AsyncMock(return_value={"init_data": signed_init_data()})
    request = make_mocked_request(
        "POST",
        "/api/miniapp/session",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    response = await http._headers(request, http._create_session)
    assert response.status == 200
    assert json.loads(response.text)["locale"] == locale
    cookie = response.cookies["__Host-sitg_session"]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "None"
    assert database.record.environment == environment and database.record.bot_id == 123456
    accounts.create_or_resume_registration.assert_awaited_once_with(42, telegram_username=None)

    route = make_mocked_request(
        "GET",
        "/api/miniapp/routes/resolve?path=/training",
        headers={"Origin": ORIGIN, "Cookie": f"__Host-sitg_session={cookie.value}"},
    )
    response = await http._headers(route, http._resolve_route)
    payload = json.loads(response.text)
    assert response.status == 200 and payload["authorization"] == {"allowed": True}
    assert (
        payload["resource"]["kind"] == "training" and payload["resource"]["lobby"]["version"] == 1
    )
    principal, operation_request = gateway.execute.await_args.args
    assert principal.player_id == database.player.id and principal.telegram_user_id == 42
    assert operation_request.operation.action == ActionCode.TRAINING_CURRENT

    # Preserve binding: production must not reuse a test session (or vice versa).
    http.auth.environment = "production" if environment == "test" else "test"
    rejected = await http._headers(route, http._resolve_route)
    assert rejected.status == 401
    assert gateway.execute.await_count == 1


@pytest.mark.parametrize("kind", ["empty", "tampered", "expired", "origin"])
async def test_training_bootstrap_does_not_relax_authentication(kind):
    http, database, accounts, gateway = setup_http("test")
    data, origin = signed_init_data(), ORIGIN
    if kind == "empty":
        data = ""  # the documented keyboard-WebApp contract
    elif kind == "tampered":
        data = data.replace("Player", "Impostor")
    elif kind == "expired":
        data = signed_init_data(age=360)
    else:
        origin = "https://attacker.example"
    http._json_body = AsyncMock(return_value={"init_data": data})
    request = make_mocked_request(
        "POST",
        "/api/miniapp/session",
        headers={"Origin": origin, "Content-Type": "application/json"},
    )
    response = await http._headers(request, http._create_session)
    assert response.status in {401, 403}
    assert database.record is None
    accounts.create_or_resume_registration.assert_not_awaited()
    gateway.execute.assert_not_awaited()


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_training_history_menu_button_launches_history_route(locale):
    loc = LocalizationService()
    backend = SimpleNamespace(
        _execute=AsyncMock(return_value=SimpleNamespace(data={})),
        navigation=AsyncMock(return_value=training_navigation()),
    )

    def message(text):
        from aiogram.types import Chat

        return Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text=text
        )

    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.propagate_event(
            update_type="message",
            event=message(loc.text("button.training.history", locale)),
            navigation=training_navigation(),
            backend=backend,
            telegram_update_claim=object(),
            localization=loc,
            locale=locale,
            launch_links=ORIGIN,
        )
        reply = answer.await_args.kwargs["reply_markup"]
        button = reply.inline_keyboard[0][0]
        assert button.text == loc.text("training.history", locale)
        assert button.web_app.url == ORIGIN + "/training/history?_launch=1"
        assert "token" not in button.web_app.url and "user" not in button.web_app.url
    backend._execute.assert_not_awaited()
