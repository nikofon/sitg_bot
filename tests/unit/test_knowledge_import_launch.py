"""Simulated Telegram /start -> inline WebApp -> authenticated import transport.

No Telegram or provider network calls. This is not real-client acceptance evidence.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from aiogram.types import Message
from aiohttp.test_utils import TestClient, TestServer
from test_knowledge_catalogue_http import HEADERS, BoundaryAuth
from test_miniapp_http import FakeGateway

from sitg_bot.application.contracts import ActionCode, GatewayResponse
from sitg_bot.bot.handlers import common
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.miniapps.links import knowledge_import_app_url, knowledge_import_bot_url
from sitg_bot.bot.presenters.render import telegram_keyboard
from sitg_bot.miniapp_http import MiniAppHttpServer

TOKEN = "a" * 43


@pytest.mark.parametrize("locale", ["en", "ru"])
async def test_registered_start_route_sends_webapp_button_without_importing(monkeypatch, locale):
    send = AsyncMock()
    monkeypatch.setattr(common, "send_message_model", send)
    backend = SimpleNamespace(_execute=AsyncMock())
    message = Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(UTC),
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Recipient"},
            "text": f"/start kl_{TOKEN}",
        }
    )
    await common.router.propagate_event(
        "message",
        message,
        bot=SimpleNamespace(),
        backend=backend,
        telegram_update_claim=SimpleNamespace(),
        localization=LocalizationService(),
        locale=locale,
        navigation=SimpleNamespace(context="menu"),
        state=SimpleNamespace(clear=AsyncMock()),
        launch_links="https://mini.example.test/nested/",
    )
    model = send.await_args.args[1]
    markup = telegram_keyboard(model.keyboard)
    url = markup.inline_keyboard[0][0].web_app.url
    assert url == f"https://mini.example.test/nested/knowledge?_launch=1#import={TOKEN}"
    assert TOKEN not in urlsplit(url).query
    backend._execute.assert_not_awaited()


async def test_unregistered_recipient_preserves_launch_until_registration_completes(monkeypatch):
    send, register = AsyncMock(), AsyncMock()
    monkeypatch.setattr(common, "send_message_model", send)
    monkeypatch.setattr(common, "resume_registration", register)
    data = {}

    async def update_data(**values):
        data.update(values)

    state = SimpleNamespace(
        clear=AsyncMock(),
        update_data=update_data,
        get_data=AsyncMock(side_effect=lambda: dict(data)),
    )
    message = SimpleNamespace(text=f"/start kl_{TOKEN}")
    await common.handle_start(
        message,
        SimpleNamespace(),
        SimpleNamespace(),
        LocalizationService(),
        "en",
        None,
        state,
        "https://mini.example.test/base",
    )
    assert data["registration_knowledge_import_url"].endswith(f"#import={TOKEN}")
    assert register.await_count == 1
    send.assert_not_awaited()
    await common.resume_invitation(
        message, SimpleNamespace(), SimpleNamespace(), LocalizationService(), "en", state
    )
    assert data["registration_knowledge_import_url"] is None
    assert send.await_args.args[1].keyboard.rows[0][0].web_app_url.endswith(f"#import={TOKEN}")


async def test_unconfigured_bot_launch_does_not_offer_a_broken_webapp(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(common, "send_message_model", send)
    await common.handle_start(
        SimpleNamespace(text=f"/start kl_{TOKEN}"),
        SimpleNamespace(),
        SimpleNamespace(),
        LocalizationService(),
        "en",
        SimpleNamespace(context="menu"),
        SimpleNamespace(clear=AsyncMock()),
    )
    assert send.await_args.args[1].keyboard is None


@pytest.mark.parametrize("base", ["http://mini.example.test", "javascript:alert(1)"])
def test_import_launch_requires_https_and_safe_token(base):
    with pytest.raises(ValueError):
        knowledge_import_app_url(base, TOKEN)
    with pytest.raises(ValueError):
        knowledge_import_app_url("https://mini.example.test", "bad/token")
    with pytest.raises(ValueError):
        knowledge_import_bot_url("unsafe/bot", TOKEN)


class LinkGateway(FakeGateway):
    async def execute(self, principal, request):
        self.requests.append(request)
        link = {
            "id": str(UUID(int=1)),
            "token": TOKEN,
            "url": f"/knowledge#import={TOKEN}",
            "active": True,
            "created_at": datetime.now(UTC).isoformat(),
            "deactivated_at": None,
        }
        return GatewayResponse(
            action=request.operation.action,
            correlation_id=request.metadata.correlation_id,
            ok=True,
            data={"links": [link]}
            if request.operation.action == ActionCode.KNOWLEDGE_SHARING
            else link,
        )


@pytest.mark.parametrize("configured", [True, False])
async def test_http_share_urls_use_actual_bot_identity_no_app_short_name(configured):
    notifier = (
        SimpleNamespace(bot_username=AsyncMock(return_value="ActualTestBot"))
        if configured
        else None
    )
    gateway = LinkGateway()
    http = MiniAppHttpServer(BoundaryAuth(), gateway, selection_notifier=notifier)
    async with TestClient(TestServer(http.application)) as client:
        for method, path in (("GET", "sharing"), ("POST", "link_create")):
            response = await client.request(
                method,
                f"/api/miniapp/knowledge/{path}",
                headers=HEADERS,
                **({"json": {}} if method == "POST" else {}),
            )
            assert response.status == 200
            data = await response.json()
            link = data["links"][0] if method == "GET" else data
            assert link["launch_available"] == configured
            assert link["app_url"] == f"/knowledge#import={TOKEN}"
            if configured:
                assert link["url"] == f"https://t.me/ActualTestBot?start=kl_{TOKEN}"
                payload = parse_qs(urlsplit(link["url"]).query)["start"][0]
                assert payload == f"kl_{TOKEN}" and len(payload) <= 64
            else:
                assert link["url"] is None
