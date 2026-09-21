import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from aiogram.types import Chat, Message, Update, User

from sitg_bot.application.contracts import ActionCode
from sitg_bot.bot.auth import private_update_user_id
from sitg_bot.bot.miniapps import mini_app_launch_url, mini_app_route_url
from sitg_bot.services.launch_references import LaunchReferenceService
from sitg_bot.services.miniapp_auth import (
    MiniAppAuthenticationError,
    MiniAppAuthService,
    MiniAppCsrfError,
)
from sitg_bot.services.telegram_auth import TelegramUpdateRejected
from sitg_bot.storage.models import (
    ApplicationLaunchReferenceRecord,
    MiniAppSessionRecord,
    TelegramUpdateReceiptRecord,
)

BOT_TOKEN = "123456:test-secret"
SESSION_KEY = "s" * 32
NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def _message_update(
    *,
    user_id: int = 42,
    chat_id: int = 42,
    chat_type: str = "private",
    sender_chat: Chat | None = None,
) -> Update:
    return Update(
        update_id=100,
        message=Message(
            message_id=10,
            date=NOW,
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Player"),
            sender_chat=sender_chat,
            text="/start",
        ),
    )


def _auth_service() -> MiniAppAuthService:
    return MiniAppAuthService(
        object(),  # type: ignore[arg-type]
        bot_token=BOT_TOKEN,
        session_signing_key=SESSION_KEY,
        allowed_origins={"https://mini.example.test"},
        environment="test",
    )


def _signed_init_data(
    *,
    auth_date: datetime = NOW,
    user_id: int = 42,
    extra: dict[str, str] | None = None,
) -> str:
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Player", "username": "player_42"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        **(extra or {}),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_private_bot_update_uses_only_matching_from_user_identity() -> None:
    update = _message_update()

    assert private_update_user_id(update, verified_transport=True) == 42


@pytest.mark.parametrize(
    "update",
    (
        _message_update(chat_id=-100, chat_type="group"),
        _message_update(chat_id=43),
        _message_update(sender_chat=Chat(id=-100, type="group")),
    ),
)
def test_bot_update_rejects_group_mismatch_and_anonymous_sender(update: Update) -> None:
    with pytest.raises(TelegramUpdateRejected):
        private_update_user_id(update, verified_transport=True)


def test_bot_update_requires_verified_transport() -> None:
    with pytest.raises(TelegramUpdateRejected):
        private_update_user_id(_message_update(), verified_transport=False)


def test_bot_update_rejects_channel_posts() -> None:
    message = _message_update().message
    assert message is not None
    update = Update(update_id=101, channel_post=message)

    with pytest.raises(TelegramUpdateRejected):
        private_update_user_id(update, verified_transport=True)


def test_mini_app_validates_official_hmac_shape_and_age() -> None:
    values = _auth_service().verify_init_data(_signed_init_data(), now=NOW)

    assert values["auth_date"] == str(int(NOW.timestamp()))
    assert json.loads(values["user"])["id"] == 42


def test_mini_app_rejects_tampering_and_stale_authorization() -> None:
    service = _auth_service()
    tampered = _signed_init_data().replace("player_42", "attacker")

    with pytest.raises(MiniAppAuthenticationError):
        service.verify_init_data(tampered, now=NOW)
    with pytest.raises(MiniAppAuthenticationError, match="expired"):
        service.verify_init_data(_signed_init_data(auth_date=NOW - timedelta(minutes=6)), now=NOW)


def test_mini_app_rejects_group_launch_context() -> None:
    with pytest.raises(MiniAppAuthenticationError, match="Group and channel"):
        _auth_service().verify_init_data(
            _signed_init_data(extra={"chat_type": "supergroup"}), now=NOW
        )


def test_mini_app_security_policy_uses_exact_origin_and_strict_headers() -> None:
    policy = _auth_service().security_policy

    headers = policy.response_headers("https://mini.example.test/")

    assert headers["Access-Control-Allow-Origin"] == "https://mini.example.test"
    assert headers["Access-Control-Allow-Credentials"] == "true"
    assert "frame-ancestors https://web.telegram.org" in headers["Content-Security-Policy"]
    with pytest.raises(MiniAppAuthenticationError):
        policy.response_headers("https://attacker.example")


def test_mini_app_mutation_requires_post_json_csrf_and_idempotency() -> None:
    service = _auth_service()
    csrf_token = "csrf-secret"
    record = SimpleNamespace(csrf_digest=service._secret_digest("csrf", csrf_token))

    service._validate_mutation(  # type: ignore[arg-type]
        record,
        method="POST",
        csrf_token=csrf_token,
        idempotency_key="mutation-123",
        content_type="application/json; charset=utf-8",
    )
    with pytest.raises(MiniAppCsrfError):
        service._validate_mutation(  # type: ignore[arg-type]
            record,
            method="GET",
            csrf_token=csrf_token,
            idempotency_key="mutation-123",
            content_type="application/json",
        )
    with pytest.raises(MiniAppCsrfError):
        service._validate_mutation(  # type: ignore[arg-type]
            record,
            method="POST",
            csrf_token="wrong",
            idempotency_key="mutation-123",
            content_type="application/json",
        )


def test_launch_reference_payload_is_compact_and_opaque() -> None:
    raw_reference = "abcDEF_123-xyz"

    assert LaunchReferenceService.parse_telegram_payload(f"lr_{raw_reference}") == raw_reference
    with pytest.raises(ValueError):
        LaunchReferenceService.parse_telegram_payload("lobby:550e8400-e29b-41d4-a716-446655440000")


def test_tournament_launch_url_preserves_display_filters() -> None:
    url = mini_app_route_url(
        "https://mini.example.test/app",
        "tournaments",
        query={"role": "manager", "relationship": "managed"},
    )

    parsed = urlsplit(url)
    assert parsed.path == "/app/tournaments"
    query = parse_qs(parsed.query)
    assert query.pop("_launch") == ["1"]
    assert query == {"role": ["manager"], "relationship": ["managed"]}
    assert "player_id" not in url


@pytest.mark.parametrize("route", [
    "tournaments", "players/00000000-0000-0000-0000-000000000001", "library",
    "admin/management", "tournaments/00000000-0000-0000-0000-000000000002",
])
def test_menu_launches_use_stable_transition_urls(route: str) -> None:
    first = urlsplit(mini_app_route_url("https://mini.example.test", route))
    second = urlsplit(mini_app_route_url("https://mini.example.test", route))

    assert first.path == second.path == f"/{route}"
    assert first == second
    assert parse_qs(first.query) == {"_launch": ["1"]}


def test_tournament_profile_launch_url_preserves_the_section_query() -> None:
    url = mini_app_route_url(
        "https://mini.example.test/app",
        "tournaments/00000000-0000-0000-0000-000000000002",
        query={"section": "leaders"},
    )

    parsed = urlsplit(url)
    assert parsed.path == "/app/tournaments/00000000-0000-0000-0000-000000000002"
    query = parse_qs(parsed.query)
    assert query.pop("_launch") == ["1"]
    assert query == {"section": ["leaders"]}


def test_tournament_profile_launch_url_rejects_non_identifier_routes() -> None:
    with pytest.raises(ValueError, match="Unsupported Mini App route"):
        mini_app_route_url("https://mini.example.test", "tournaments/managed-cup")


def test_manager_settings_launch_url_uses_only_an_opaque_reference() -> None:
    reference = SimpleNamespace(
        value="opaque-reference",
        telegram_payload="lr_opaque-reference",
    )

    url = mini_app_launch_url(
        "https://mini.example.test/app",
        "manager/tournaments",
        reference,  # type: ignore[arg-type]
    )

    assert url == (
        "https://mini.example.test/app/manager/tournaments/opaque-reference/settings"
        "?tgWebAppStartParam=lr_opaque-reference"
    )


def test_authentication_records_have_required_deduplication_and_secret_indexes() -> None:
    update_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in TelegramUpdateReceiptRecord.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("bot_id", "environment", "update_id") in update_uniques
    assert MiniAppSessionRecord.__table__.c.token_digest.unique
    assert ApplicationLaunchReferenceRecord.__table__.c.token_digest.unique
    launch_checks = {
        str(constraint.sqltext)
        for constraint in ApplicationLaunchReferenceRecord.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert any("manager_settings" in check for check in launch_checks)
    assert ActionCode.LOBBY_READY_UPDATE.value.startswith("lobbies.")
