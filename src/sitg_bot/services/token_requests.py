import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerRecord,
    TournamentCreationTokenDeliveryRecord,
    TournamentCreationTokenRecord,
    TournamentCreationTokenRequestRecord,
)


class TokenPlaintextUnavailable(ValueError):
    """Raised when one-time token plaintext cannot be delivered again."""


@dataclass(frozen=True, slots=True)
class TokenDeliverySnapshot:
    status: str
    delivered_at: datetime | None
    failed_at: datetime | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class TokenRequestSnapshot:
    request_id: UUID
    requester_id: UUID
    requester_nickname: str
    requester_real_name: str | None
    requester_telegram_username: str | None
    requester_telegram_user_id: int | None
    tournament_name: str
    status: str
    justification: str | None
    decision_note: str | None
    decided_by_id: UUID | None
    created_at: datetime
    decided_at: datetime | None
    token_id: UUID | None
    token_fingerprint: str | None
    token_expires_at: datetime | None
    delivery: TokenDeliverySnapshot | None


@dataclass(frozen=True, slots=True)
class TokenRequestPage:
    items: tuple[TokenRequestSnapshot, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TokenInventoryItem:
    source: str
    request_id: UUID | None
    request_status: str | None
    requested_at: datetime | None
    tournament_name: str | None
    decision_note: str | None
    token_id: UUID | None
    fingerprint: str | None
    token_status: str | None
    issued_at: datetime | None
    expires_at: datetime | None
    used_at: datetime | None
    tournament_id: UUID | None
    revoked_at: datetime | None
    delivery: TokenDeliverySnapshot | None


@dataclass(frozen=True, slots=True)
class TokenInventoryPage:
    items: tuple[TokenInventoryItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class DeliveredCreationToken:
    token_id: UUID
    token: str
    fingerprint: str
    expires_at: datetime


class TournamentTokenRequestService:
    """Owns creator-token requests, approval, inventory, and one-time secret delivery."""

    def __init__(
        self,
        database: Database,
        *,
        delivery_encryption_key: str | None = None,
        token_lifetime: timedelta = timedelta(hours=24),
    ) -> None:
        if delivery_encryption_key is not None and len(delivery_encryption_key) < 32:
            raise ValueError("Token delivery encryption key must contain at least 32 characters")
        if token_lifetime <= timedelta(0):
            raise ValueError("Tournament creation token lifetime must be positive")
        self.database = database
        self.delivery_encryption_key = delivery_encryption_key
        self.token_lifetime = token_lifetime

    async def create_request(
        self,
        requester_id: UUID,
        *,
        tournament_name: str,
        justification: str | None = None,
    ) -> TokenRequestSnapshot:
        normalized_name = tournament_name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError("Tournament name must contain between 1 and 200 characters")
        normalized_justification = self._note(justification, label="Justification")
        async with self.database.transaction() as session:
            await session.execute(
                select(func.pg_advisory_xact_lock(self._requester_lock_key(requester_id)))
            )
            requester = await session.get(PlayerRecord, requester_id)
            if (
                requester is None
                or requester.status != "active"
                or requester.public_nickname is None
            ):
                raise LookupError("Active requester not found")
            pending = await session.scalar(
                select(TournamentCreationTokenRequestRecord)
                .where(
                    TournamentCreationTokenRequestRecord.requester_id == requester_id,
                    TournamentCreationTokenRequestRecord.status == "pending",
                )
                .with_for_update()
            )
            if pending is not None:
                return await self._request_snapshot(session, pending)
            request = TournamentCreationTokenRequestRecord(
                requester_id=requester_id,
                tournament_name=normalized_name,
                justification=normalized_justification,
                status="pending",
            )
            session.add(request)
            await session.flush()
            notification_key = f"tournament-token-request:{request.id}:created"
            notification_payload = {
                "request_id": str(request.id),
                "requester_id": str(requester.id),
                "requester_nickname": requester.public_nickname,
                "tournament_name": normalized_name,
            }
            await NotificationWriter.create_for_administrators(
                session,
                kind="tournament_token.request_created",
                deduplication_key=notification_key,
                payload=notification_payload,
            )
            return await self._request_snapshot(session, request)

    async def pending_queue(
        self, administrator_id: UUID, *, cursor: str | None = None, limit: int = 20
    ) -> TokenRequestPage:
        return await self._admin_queue(administrator_id, resolved=False, cursor=cursor, limit=limit)

    async def resolved_queue(
        self, administrator_id: UUID, *, cursor: str | None = None, limit: int = 20
    ) -> TokenRequestPage:
        return await self._admin_queue(administrator_id, resolved=True, cursor=cursor, limit=limit)

    async def decide_request(
        self,
        request_id: UUID,
        administrator_id: UUID,
        *,
        approve: bool,
        note: str | None = None,
        lifetime: timedelta | None = None,
    ) -> TokenRequestSnapshot:
        normalized_note = self._note(note, label="Decision note")
        desired_status = "approved" if approve else "rejected"
        ttl = lifetime or self.token_lifetime
        if ttl <= timedelta(0):
            raise ValueError("Tournament creation token lifetime must be positive")

        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            request = await session.scalar(
                select(TournamentCreationTokenRequestRecord)
                .where(TournamentCreationTokenRequestRecord.id == request_id)
                .with_for_update()
            )
            if request is None:
                raise LookupError("Tournament creation token request not found")
            if request.status == desired_status:
                return await self._request_snapshot(session, request)
            if request.status != "pending":
                raise ValueError("Tournament creation token request is already resolved")
            requester = await session.get(PlayerRecord, request.requester_id)
            if requester is None or requester.status != "active":
                raise LookupError("Active requester not found")

            now = datetime.now(UTC)
            request.status = desired_status
            request.decided_by_id = administrator_id
            request.decision_note = normalized_note
            request.decided_at = now
            if approve:
                raw_token = secrets.token_urlsafe(32)
                digest = self._token_digest(raw_token)
                token = TournamentCreationTokenRecord(
                    token_digest=digest,
                    fingerprint=digest[:12],
                    request_id=request.id,
                    issued_by_id=administrator_id,
                    intended_creator_id=request.requester_id,
                    expires_at=now + ttl,
                )
                session.add(token)
                await session.flush()
                if self.delivery_encryption_key is not None:
                    encrypted = func.pgp_sym_encrypt(
                        raw_token,
                        self.delivery_encryption_key,
                        "cipher-algo=aes256, compress-algo=0",
                    )
                    session.add(
                        TournamentCreationTokenDeliveryRecord(
                            token_id=token.id,
                            recipient_player_id=request.requester_id,
                            encrypted_token=cast(Any, encrypted),
                            status="pending",
                        )
                    )
            notification_key = f"tournament-token-request:{request.id}:decided"
            notification_payload = {
                "request_id": str(request.id),
                "status": desired_status,
            }
            await NotificationWriter.create_for_player(
                session,
                recipient_player_id=request.requester_id,
                audience="manager",
                kind="tournament_token.request_decided",
                deduplication_key=notification_key,
                payload=notification_payload,
            )
            await session.flush()
            return await self._request_snapshot(session, request)

    async def claim_token_once(
        self, request_id: UUID, requester_id: UUID
    ) -> DeliveredCreationToken:
        if self.delivery_encryption_key is None:
            raise TokenPlaintextUnavailable("Token plaintext delivery is not configured")
        terminal_failure: str | None = None
        delivered: DeliveredCreationToken | None = None
        async with self.database.transaction() as session:
            request = await session.get(TournamentCreationTokenRequestRecord, request_id)
            if request is None or request.requester_id != requester_id:
                raise LookupError("Approved token request not found")
            if request.status != "approved":
                raise ValueError("Token request is not approved")
            token = await session.scalar(
                select(TournamentCreationTokenRecord)
                .where(TournamentCreationTokenRecord.request_id == request.id)
                .with_for_update()
            )
            if token is None:
                raise LookupError("Issued tournament creation token not found")
            delivery = await session.get(
                TournamentCreationTokenDeliveryRecord,
                token.id,
                with_for_update=True,
            )
            if delivery is None or delivery.recipient_player_id != requester_id:
                raise LookupError("Token delivery not found")
            if delivery.status != "pending" or delivery.encrypted_token is None:
                raise TokenPlaintextUnavailable("Token plaintext is no longer available")
            now = datetime.now(UTC)
            terminal_reason = self._terminal_token_reason(token, now=now)
            if terminal_reason is not None:
                delivery.status = "failed"
                delivery.failed_at = now
                delivery.failure_reason = terminal_reason
                delivery.encrypted_token = None
                await session.flush()
                terminal_failure = terminal_reason
            else:
                raw_token = await session.scalar(
                    select(
                        func.pgp_sym_decrypt(
                            delivery.encrypted_token,
                            self.delivery_encryption_key,
                        )
                    )
                )
                if not isinstance(raw_token, str):
                    raise RuntimeError("Token delivery decryption returned an invalid value")
                delivery.status = "delivered"
                delivery.delivered_at = now
                delivery.encrypted_token = None
                await session.flush()
                delivered = DeliveredCreationToken(
                    token.id, raw_token, token.fingerprint, token.expires_at
                )
        if terminal_failure is not None:
            raise ValueError(f"Token cannot be delivered: {terminal_failure}")
        assert delivered is not None
        return delivered

    async def mark_delivery_failed(
        self,
        token_id: UUID,
        administrator_id: UUID,
        *,
        reason: str,
    ) -> TokenDeliverySnapshot:
        normalized_reason = self._required_note(reason, label="Delivery failure reason")
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            delivery = await session.get(
                TournamentCreationTokenDeliveryRecord,
                token_id,
                with_for_update=True,
            )
            if delivery is None:
                raise LookupError("Token delivery not found")
            if delivery.status == "delivered":
                raise ValueError("Delivered token cannot be marked failed")
            if delivery.status != "failed":
                delivery.status = "failed"
                delivery.failed_at = datetime.now(UTC)
                delivery.failure_reason = normalized_reason
                delivery.encrypted_token = None
                await session.flush()
            return self._delivery_snapshot(delivery)

    async def my_tokens(self, requester_id: UUID) -> tuple[TokenInventoryItem, ...]:
        async with self.database.transaction() as session:
            requester = await session.get(PlayerRecord, requester_id)
            if requester is None or requester.status != "active":
                raise LookupError("Active requester not found")
            requests = tuple(
                (
                    await session.execute(
                        select(TournamentCreationTokenRequestRecord)
                        .where(TournamentCreationTokenRequestRecord.requester_id == requester_id)
                        .order_by(TournamentCreationTokenRequestRecord.created_at.desc())
                    )
                ).scalars()
            )
            tokens = tuple(
                (
                    await session.execute(
                        select(TournamentCreationTokenRecord)
                        .where(TournamentCreationTokenRecord.intended_creator_id == requester_id)
                        .order_by(TournamentCreationTokenRecord.created_at.desc())
                    )
                ).scalars()
            )
            tokens_by_request = {
                token.request_id: token for token in tokens if token.request_id is not None
            }
            now = datetime.now(UTC)
            items = [
                await self._inventory_item(
                    session,
                    request=request,
                    token=tokens_by_request.get(request.id),
                    now=now,
                )
                for request in requests
            ]
            items.extend(
                [
                    await self._inventory_item(session, request=None, token=token, now=now)
                    for token in tokens
                    if token.request_id is None
                ]
            )
            return tuple(
                sorted(
                    items,
                    key=self._inventory_sort_key,
                    reverse=True,
                )
            )

    async def my_tokens_page(
        self,
        requester_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> TokenInventoryPage:
        if limit < 1 or limit > 100:
            raise ValueError("Token inventory limit must be between 1 and 100")
        cursor_key = self._decode_inventory_cursor(cursor)
        items = await self.my_tokens(requester_id)
        if cursor_key is not None:
            items = tuple(item for item in items if self._inventory_sort_key(item) < cursor_key)
        visible = items[:limit]
        return TokenInventoryPage(
            visible,
            (
                self._encode_inventory_cursor(visible[-1])
                if len(items) > limit and visible
                else None
            ),
        )

    async def _admin_queue(
        self,
        administrator_id: UUID,
        *,
        resolved: bool,
        cursor: str | None,
        limit: int,
    ) -> TokenRequestPage:
        if limit < 1 or limit > 100:
            raise ValueError("Token request queue limit must be between 1 and 100")
        cursor_time, cursor_id = self._decode_cursor(cursor)
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            statement = select(TournamentCreationTokenRequestRecord).where(
                TournamentCreationTokenRequestRecord.status != "pending"
                if resolved
                else TournamentCreationTokenRequestRecord.status == "pending"
            )
            if cursor_time is not None and cursor_id is not None:
                statement = statement.where(
                    or_(
                        TournamentCreationTokenRequestRecord.created_at > cursor_time,
                        (TournamentCreationTokenRequestRecord.created_at == cursor_time)
                        & (TournamentCreationTokenRequestRecord.id > cursor_id),
                    )
                )
            records = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            TournamentCreationTokenRequestRecord.created_at,
                            TournamentCreationTokenRequestRecord.id,
                        ).limit(limit + 1)
                    )
                ).scalars()
            )
            visible = records[:limit]
            items = tuple([await self._request_snapshot(session, item) for item in visible])
            next_cursor = (
                self._encode_cursor(visible[-1]) if len(records) > limit and visible else None
            )
            return TokenRequestPage(items, next_cursor)

    async def _request_snapshot(
        self, session: AsyncSession, request: TournamentCreationTokenRequestRecord
    ) -> TokenRequestSnapshot:
        requester = await session.get(PlayerRecord, request.requester_id)
        if requester is None or requester.public_nickname is None:
            raise LookupError("Token requester identity is unavailable")
        token = await session.scalar(
            select(TournamentCreationTokenRecord).where(
                TournamentCreationTokenRecord.request_id == request.id
            )
        )
        delivery = (
            await session.get(TournamentCreationTokenDeliveryRecord, token.id)
            if token is not None
            else None
        )
        return TokenRequestSnapshot(
            request.id,
            requester.id,
            requester.public_nickname,
            requester.real_name,
            requester.telegram_username,
            requester.telegram_user_id,
            request.tournament_name,
            request.status,
            request.justification,
            request.decision_note,
            request.decided_by_id,
            request.created_at,
            request.decided_at,
            token.id if token else None,
            token.fingerprint if token else None,
            token.expires_at if token else None,
            self._delivery_snapshot(delivery) if delivery else None,
        )

    async def _inventory_item(
        self,
        session: AsyncSession,
        *,
        request: TournamentCreationTokenRequestRecord | None,
        token: TournamentCreationTokenRecord | None,
        now: datetime,
    ) -> TokenInventoryItem:
        delivery = (
            await session.get(TournamentCreationTokenDeliveryRecord, token.id)
            if token is not None
            else None
        )
        return TokenInventoryItem(
            "requested" if request is not None else "direct",
            request.id if request else None,
            request.status if request else None,
            request.created_at if request else None,
            request.tournament_name if request else None,
            request.decision_note if request else None,
            token.id if token else None,
            token.fingerprint if token else None,
            self._token_status(token, now=now) if token else None,
            token.created_at if token else None,
            token.expires_at if token else None,
            token.used_at if token else None,
            token.tournament_id if token else None,
            token.revoked_at if token else None,
            self._delivery_snapshot(delivery) if delivery else None,
        )

    @staticmethod
    def _token_status(token: TournamentCreationTokenRecord, *, now: datetime) -> str:
        if token.revoked_at is not None:
            return "revoked"
        if token.used_at is not None:
            return "used"
        if token.expires_at <= now:
            return "expired"
        return "issued"

    @classmethod
    def _terminal_token_reason(
        cls, token: TournamentCreationTokenRecord, *, now: datetime
    ) -> str | None:
        status = cls._token_status(token, now=now)
        return None if status == "issued" else status

    @staticmethod
    def _delivery_snapshot(
        delivery: TournamentCreationTokenDeliveryRecord,
    ) -> TokenDeliverySnapshot:
        return TokenDeliverySnapshot(
            delivery.status,
            delivery.delivered_at,
            delivery.failed_at,
            delivery.failure_reason,
        )

    @staticmethod
    def _inventory_sort_key(item: TokenInventoryItem) -> tuple[datetime, UUID, str]:
        timestamp = item.requested_at or item.issued_at or datetime.min.replace(tzinfo=UTC)
        identifier = item.request_id or item.token_id
        assert identifier is not None
        return timestamp, identifier, item.source

    @classmethod
    def _encode_inventory_cursor(cls, item: TokenInventoryItem) -> str:
        timestamp, identifier, source = cls._inventory_sort_key(item)
        value = {
            "timestamp": timestamp.isoformat(),
            "id": str(identifier),
            "source": source,
        }
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_inventory_cursor(
        cursor: str | None,
    ) -> tuple[datetime, UUID, str] | None:
        if cursor is None:
            return None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            timestamp = datetime.fromisoformat(str(value["timestamp"]))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError
            source = str(value["source"])
            if source not in {"requested", "direct"}:
                raise ValueError
            return timestamp, UUID(str(value["id"])), source
        except (
            binascii.Error,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("Invalid token inventory cursor") from error

    @staticmethod
    async def _require_administrator(session: AsyncSession, player_id: UUID) -> None:
        administrator = await session.get(PlatformAdministratorRecord, player_id)
        if administrator is None or administrator.revoked_at is not None:
            raise PermissionError("Platform administrator role is required")

    @staticmethod
    def _note(value: str | None, *, label: str) -> str | None:
        normalized = value.strip() if value is not None else None
        if not normalized:
            return None
        if len(normalized) > 2000:
            raise ValueError(f"{label} cannot exceed 2000 characters")
        return normalized

    @classmethod
    def _required_note(cls, value: str, *, label: str) -> str:
        normalized = cls._note(value, label=label)
        if normalized is None:
            raise ValueError(f"{label} is required")
        return normalized

    @staticmethod
    def _requester_lock_key(requester_id: UUID) -> int:
        digest = hashlib.sha256(b"token-request:" + requester_id.bytes).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    @staticmethod
    def _token_digest(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @staticmethod
    def _encode_cursor(request: TournamentCreationTokenRequestRecord) -> str:
        value = {"created_at": request.created_at.isoformat(), "id": str(request.id)}
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
        if cursor is None:
            return None, None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            if not isinstance(value, dict):
                raise ValueError
            created_at = datetime.fromisoformat(str(value["created_at"]))
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                raise ValueError
            return created_at, UUID(str(value["id"]))
        except (
            binascii.Error,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("Invalid token request cursor") from error
