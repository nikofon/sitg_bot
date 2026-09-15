import hashlib
import hmac
from datetime import timedelta

import pytest
from test_telegram_miniapp_auth import BOT_TOKEN, NOW, _auth_service

from sitg_bot.services.miniapp_auth import MiniAppAuthenticationError


def signed_login(**changes: str) -> dict[str, str]:
    values = {"id": "42", "first_name": "Player", "auth_date": str(int(NOW.timestamp()))}
    values.update(changes)
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    values["hash"] = hmac.new(
        hashlib.sha256(BOT_TOKEN.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    return values


def test_website_login_verifies_telegram_signature() -> None:
    _auth_service().verify_website_login(signed_login(username="player_42"), now=NOW)


@pytest.mark.parametrize("field,value", [("id", "43"), ("username", "someone_else")])
def test_website_login_rejects_tampered_identity(field: str, value: str) -> None:
    values = signed_login()
    values[field] = value
    with pytest.raises(MiniAppAuthenticationError):
        _auth_service().verify_website_login(values, now=NOW)


@pytest.mark.parametrize("seconds", [-301, 31, 10**12])
def test_website_login_rejects_expired_or_future_signatures(seconds: int) -> None:
    values = signed_login(auth_date=str(int(NOW.timestamp()) + seconds))
    with pytest.raises(MiniAppAuthenticationError):
        _auth_service().verify_website_login(values, now=NOW)


def test_website_login_rejects_unexpected_signed_claims() -> None:
    with pytest.raises(MiniAppAuthenticationError):
        _auth_service().verify_website_login(signed_login(is_admin="true"), now=NOW)


def test_website_login_cannot_use_miniapp_signature() -> None:
    values = signed_login()
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()) if key != "hash")
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(MiniAppAuthenticationError):
        _auth_service().verify_website_login(values, now=NOW + timedelta(seconds=1))
