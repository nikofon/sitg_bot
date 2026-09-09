import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from sqlalchemy import select

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.services.players import PlayerAccountService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import MiniAppSessionRecord, PlayerRecord


class MiniAppAuthenticationError(PermissionError):
    """Raised when Telegram initData or an application session is not trustworthy."""


class MiniAppCsrfError(PermissionError):
    """Raised when a browser mutation does not satisfy CSRF protections."""


@dataclass(frozen=True, slots=True)
class MiniAppSessionCredentials:
    session_token: str
    csrf_token: str
    expires_at: datetime
    locale: str
    cookie_name: str = "__Host-sitg_session"
    cookie_path: str = "/"
    cookie_secure: bool = True
    cookie_http_only: bool = True
    cookie_same_site: str = "Strict"


@dataclass(frozen=True, slots=True)
class MiniAppSessionContext:
    session_id: UUID
    principal: ApplicationPrincipal
    bot_id: int
    environment: str
    origin: str
    expires_at: datetime
    action: ActionCode
    idempotency_key: str | None
    registration_status: str
    telegram_public: bool
    preferred_locale: str


@dataclass(frozen=True, slots=True)
class MiniAppSecurityPolicy:
    allowed_origins: frozenset[str]

    @property
    def content_security_policy(self) -> str:
        return "; ".join(
            (
                "default-src 'self'",
                "base-uri 'none'",
                "object-src 'none'",
                "form-action 'self'",
                "frame-src 'none'",
                "script-src 'self' https://telegram.org",
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: https:",
                "connect-src 'self'",
                "frame-ancestors https://web.telegram.org https://*.telegram.org",
            )
        )

    def response_headers(self, origin: str) -> dict[str, str]:
        normalized = MiniAppAuthService.normalize_origin(origin)
        if normalized not in self.allowed_origins:
            raise MiniAppAuthenticationError("Origin is not allowed")
        return {
            "Access-Control-Allow-Origin": normalized,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": (
                "Content-Type, X-CSRF-Token, X-Idempotency-Key, X-Correlation-ID"
            ),
            "Vary": "Origin",
            "Content-Security-Policy": self.content_security_policy,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }


class MiniAppAuthService:
    """Validates Telegram initData and owns short-lived origin-bound sessions."""

    MAX_INIT_DATA_BYTES = 16_384

    def __init__(
        self,
        database: Database,
        *,
        bot_token: str,
        session_signing_key: str,
        allowed_origins: set[str] | frozenset[str],
        environment: str = "production",
        authorization_max_age: timedelta = timedelta(minutes=5),
        future_clock_skew: timedelta = timedelta(seconds=30),
        session_lifetime: timedelta = timedelta(minutes=15),
        player_accounts: PlayerAccountService | None = None,
    ) -> None:
        if len(session_signing_key) < 32:
            raise ValueError("Mini App session signing key must contain at least 32 characters")
        if environment not in {"production", "test"}:
            raise ValueError("Telegram environment must be production or test")
        if min(authorization_max_age, future_clock_skew, session_lifetime) <= timedelta(0):
            raise ValueError("Mini App authentication lifetimes must be positive")
        try:
            bot_id = int(bot_token.split(":", 1)[0])
        except (ValueError, IndexError) as error:
            raise ValueError("Telegram bot token does not contain a valid bot ID") from error
        if bot_id <= 0 or ":" not in bot_token:
            raise ValueError("Telegram bot token does not contain a valid bot ID")
        normalized_origins = frozenset(self.normalize_origin(origin) for origin in allowed_origins)
        if not normalized_origins:
            raise ValueError("At least one Mini App origin is required")
        self.database = database
        self.bot_token = bot_token
        self.bot_id = bot_id
        self.session_key = session_signing_key.encode()
        self.environment = environment
        self.authorization_max_age = authorization_max_age
        self.future_clock_skew = future_clock_skew
        self.session_lifetime = session_lifetime
        self.player_accounts = player_accounts or PlayerAccountService(database)
        self.security_policy = MiniAppSecurityPolicy(normalized_origins)

    async def create_session(
        self,
        init_data: str,
        *,
        origin: str,
        now: datetime | None = None,
    ) -> MiniAppSessionCredentials:
        current_time = now or datetime.now(UTC)
        normalized_origin = self.normalize_origin(origin)
        if normalized_origin not in self.security_policy.allowed_origins:
            raise MiniAppAuthenticationError("Origin is not allowed")
        values = self.verify_init_data(init_data, now=current_time)
        user = self._telegram_user(values)
        telegram_user_id = self._positive_int(user.get("id"), label="Telegram user ID")
        if user.get("is_bot") is True:
            raise MiniAppAuthenticationError("Bot users cannot authenticate a Mini App")
        username = user.get("username")
        if username is not None and not isinstance(username, str):
            raise MiniAppAuthenticationError("Telegram username is invalid")
        draft = await self.player_accounts.create_or_resume_registration(
            telegram_user_id, telegram_username=username
        )
        raw_session_token = secrets.token_urlsafe(32)
        raw_csrf_token = secrets.token_urlsafe(32)
        expires_at = current_time + self.session_lifetime
        auth_date = datetime.fromtimestamp(int(values["auth_date"]), tz=UTC)
        async with self.database.transaction() as session:
            session.add(
                MiniAppSessionRecord(
                    token_digest=self._secret_digest("session", raw_session_token),
                    csrf_digest=self._secret_digest("csrf", raw_csrf_token),
                    player_id=draft.account.player_id,
                    bot_id=self.bot_id,
                    environment=self.environment,
                    origin=normalized_origin,
                    telegram_auth_date=auth_date,
                    expires_at=expires_at,
                    last_seen_at=current_time,
                )
            )
        return MiniAppSessionCredentials(
            raw_session_token,
            raw_csrf_token,
            expires_at,
            draft.account.preferred_locale,
        )

    async def refresh_session(
        self,
        session_token: str,
        *,
        origin: str,
        csrf_token: str | None,
        idempotency_key: str | None,
        content_type: str | None,
        now: datetime | None = None,
    ) -> MiniAppSessionCredentials:
        current_time = now or datetime.now(UTC)
        normalized_origin = self.normalize_origin(origin)
        if normalized_origin not in self.security_policy.allowed_origins:
            raise MiniAppAuthenticationError("Origin is not allowed")
        token_digest = self._secret_digest("session", session_token)
        async with self.database.transaction() as session:
            record = await session.scalar(
                select(MiniAppSessionRecord)
                .where(MiniAppSessionRecord.token_digest == token_digest)
                .with_for_update()
            )
            if record is None or record.revoked_at is not None:
                raise MiniAppAuthenticationError("Mini App session is invalid")
            if record.expires_at <= current_time:
                raise MiniAppAuthenticationError("Mini App session has expired")
            if (
                record.bot_id != self.bot_id
                or record.environment != self.environment
                or record.origin != normalized_origin
            ):
                raise MiniAppAuthenticationError("Mini App session binding does not match")
            self._validate_mutation(
                record,
                method="POST",
                csrf_token=csrf_token,
                idempotency_key=idempotency_key,
                content_type=content_type,
            )
            player = await session.get(PlayerRecord, record.player_id)
            if player is None or player.status == "anonymized":
                raise MiniAppAuthenticationError("Mini App player account is unavailable")
            raw_csrf_token = secrets.token_urlsafe(32)
            record.csrf_digest = self._secret_digest("csrf", raw_csrf_token)
            record.expires_at = current_time + self.session_lifetime
            record.last_seen_at = current_time
            return MiniAppSessionCredentials(
                session_token,
                raw_csrf_token,
                record.expires_at,
                player.preferred_locale,
            )

    def verify_init_data(self, init_data: str, *, now: datetime | None = None) -> dict[str, str]:
        if not init_data or len(init_data.encode()) > self.MAX_INIT_DATA_BYTES:
            raise MiniAppAuthenticationError("Telegram initData is missing or too large")
        try:
            pairs = parse_qsl(
                init_data,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=100,
            )
        except (ValueError, UnicodeDecodeError) as error:
            raise MiniAppAuthenticationError("Telegram initData is malformed") from error
        values: dict[str, str] = {}
        for key, value in pairs:
            if key in values:
                raise MiniAppAuthenticationError("Telegram initData contains duplicate fields")
            values[key] = value
        supplied_hash = values.get("hash")
        if supplied_hash is None or len(supplied_hash) != 64:
            raise MiniAppAuthenticationError("Telegram initData hash is missing")
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items()) if key != "hash"
        )
        secret_key = hmac.new(b"WebAppData", self.bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, supplied_hash.lower()):
            raise MiniAppAuthenticationError("Telegram initData signature is invalid")
        auth_date = self._positive_int(values.get("auth_date"), label="auth_date")
        current_time = now or datetime.now(UTC)
        authenticated_at = datetime.fromtimestamp(auth_date, tz=UTC)
        if authenticated_at > current_time + self.future_clock_skew:
            raise MiniAppAuthenticationError("Telegram initData is from the future")
        if authenticated_at < current_time - self.authorization_max_age:
            raise MiniAppAuthenticationError("Telegram initData has expired")
        self._telegram_user(values)
        chat_type = values.get("chat_type")
        if chat_type is not None and chat_type not in {"private", "sender"}:
            raise MiniAppAuthenticationError("Group and channel Mini App launches are disabled")
        if "chat" in values:
            raise MiniAppAuthenticationError("Group and channel Mini App launches are disabled")
        return values

    async def authorize_request(
        self,
        session_token: str,
        *,
        origin: str,
        method: str,
        action: ActionCode,
        csrf_token: str | None = None,
        idempotency_key: str | None = None,
        content_type: str | None = None,
        now: datetime | None = None,
    ) -> MiniAppSessionContext:
        current_time = now or datetime.now(UTC)
        normalized_origin = self.normalize_origin(origin)
        if normalized_origin not in self.security_policy.allowed_origins:
            raise MiniAppAuthenticationError("Origin is not allowed")
        token_digest = self._secret_digest("session", session_token)
        async with self.database.transaction() as session:
            record = await session.scalar(
                select(MiniAppSessionRecord)
                .where(MiniAppSessionRecord.token_digest == token_digest)
                .with_for_update()
            )
            if record is None or record.revoked_at is not None:
                raise MiniAppAuthenticationError("Mini App session is invalid")
            if record.expires_at <= current_time:
                raise MiniAppAuthenticationError("Mini App session has expired")
            if (
                record.bot_id != self.bot_id
                or record.environment != self.environment
                or record.origin != normalized_origin
            ):
                raise MiniAppAuthenticationError("Mini App session binding does not match")
            player = await session.get(PlayerRecord, record.player_id)
            if player is None or player.status == "anonymized":
                raise MiniAppAuthenticationError("Mini App player account is unavailable")
            if player.telegram_user_id is None:
                raise MiniAppAuthenticationError("Mini App player has no Telegram identity")
            if ACTION_POLICIES[action].mutation:
                self._validate_mutation(
                    record,
                    method=method,
                    csrf_token=csrf_token,
                    idempotency_key=idempotency_key,
                    content_type=content_type,
                )
            elif method.upper() not in {"GET", "HEAD"}:
                raise MiniAppCsrfError("Queries must use GET or HEAD")
            record.last_seen_at = current_time
            return MiniAppSessionContext(
                record.id,
                ApplicationPrincipal(
                    player_id=player.id,
                    telegram_user_id=player.telegram_user_id,
                ),
                record.bot_id,
                record.environment,
                record.origin,
                record.expires_at,
                action,
                idempotency_key,
                player.status,
                player.telegram_public,
                player.preferred_locale,
            )

    async def revoke_session(self, session_token: str) -> None:
        token_digest = self._secret_digest("session", session_token)
        async with self.database.transaction() as session:
            record = await session.scalar(
                select(MiniAppSessionRecord)
                .where(MiniAppSessionRecord.token_digest == token_digest)
                .with_for_update()
            )
            if record is not None and record.revoked_at is None:
                record.revoked_at = datetime.now(UTC)

    def _validate_mutation(
        self,
        record: MiniAppSessionRecord,
        *,
        method: str,
        csrf_token: str | None,
        idempotency_key: str | None,
        content_type: str | None,
    ) -> None:
        if method.upper() != "POST":
            raise MiniAppCsrfError("Mutations must use POST")
        media_type = (content_type or "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            raise MiniAppCsrfError("Mutations require application/json")
        if csrf_token is None or not hmac.compare_digest(
            record.csrf_digest, self._secret_digest("csrf", csrf_token)
        ):
            raise MiniAppCsrfError("CSRF token is invalid")
        if idempotency_key is None or not 8 <= len(idempotency_key) <= 200:
            raise MiniAppCsrfError("Mutations require an idempotency key")

    def _secret_digest(self, purpose: str, value: str) -> str:
        if not value:
            raise MiniAppAuthenticationError("Authentication token is missing")
        return hmac.new(
            self.session_key,
            f"{purpose}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _telegram_user(values: dict[str, str]) -> dict[str, object]:
        try:
            user = json.loads(values["user"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise MiniAppAuthenticationError("Telegram initData user is invalid") from error
        if not isinstance(user, dict):
            raise MiniAppAuthenticationError("Telegram initData user is invalid")
        return user

    @staticmethod
    def _positive_int(value: object, *, label: str) -> int:
        if isinstance(value, bool):
            raise MiniAppAuthenticationError(f"{label} is invalid")
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise MiniAppAuthenticationError(f"{label} is invalid") from error
        if parsed <= 0:
            raise MiniAppAuthenticationError(f"{label} is invalid")
        return parsed

    @staticmethod
    def normalize_origin(origin: str) -> str:
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise MiniAppAuthenticationError("Origin is invalid") from error
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise MiniAppAuthenticationError("Origin is invalid")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise MiniAppAuthenticationError("Non-local Mini App origins require HTTPS")
        hostname = parsed.hostname.encode("idna").decode().lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        default_port = 443 if parsed.scheme == "https" else 80
        port_suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parsed.scheme}://{hostname}{port_suffix}"
