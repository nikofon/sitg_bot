import hashlib
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.token_requests import TokenInventoryItem, TournamentTokenRequestService
from sitg_bot.storage.models import (
    PlatformAdministratorRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    TournamentCreationTokenDeliveryRecord,
    TournamentCreationTokenRecord,
    TournamentCreationTokenRequestRecord,
)

ENCRYPTION_KEY = "test-only-token-delivery-key-32-characters"
DEFAULT_PLAYER_ID = UUID(int=1)


class FakeDatabase:
    def __init__(self, session: object) -> None:
        self.session = session

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self.session


def active_player(player_id: UUID = DEFAULT_PLAYER_ID) -> PlayerRecord:
    return PlayerRecord(
        id=player_id,
        telegram_user_id=42,
        real_name="Private Player Name",
        public_nickname="Public player",
        status="active",
    )


def test_service_requires_a_substantial_encryption_key() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        TournamentTokenRequestService(object(), delivery_encryption_key="short")  # type: ignore[arg-type]


def test_pending_request_and_request_token_relation_are_unique() -> None:
    pending_index = next(
        index
        for index in TournamentCreationTokenRequestRecord.__table__.indexes
        if index.name == "uq_tournament_creation_token_requests_pending"
    )

    assert pending_index.unique is True
    assert str(pending_index.dialect_options["postgresql"]["where"]) == "status = 'pending'"
    assert TournamentCreationTokenRecord.__table__.c.request_id.unique is True


class CreateRequestSession:
    def __init__(self, player: PlayerRecord, pending: object = None) -> None:
        self.player = player
        self.pending = pending
        self.added: list[object] = []
        self.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: ()))
        self.flush = AsyncMock(side_effect=self._assign_request_defaults)

    async def get(self, model: type[object], identity: object) -> object | None:
        if model is PlayerRecord and identity == self.player.id:
            return self.player
        return None

    async def scalar(self, _query: object) -> object:
        return self.pending

    def add(self, value: object) -> None:
        self.added.append(value)

    def _assign_request_defaults(self) -> None:
        for value in self.added:
            if isinstance(value, TournamentCreationTokenRequestRecord) and value.id is None:
                value.id = UUID(int=10)
                value.created_at = datetime(2026, 9, 2, tzinfo=UTC)


async def test_request_creation_is_idempotent_and_notifies_admins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = active_player()
    session = CreateRequestSession(player)
    service = TournamentTokenRequestService(
        FakeDatabase(session),  # type: ignore[arg-type]
        delivery_encryption_key=ENCRYPTION_KEY,
    )
    snapshot = object()
    service._request_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]

    async def create_for_administrators(
        _cls: type[NotificationWriter],
        current_session: CreateRequestSession,
        **values: object,
    ) -> tuple[PlayerNotificationRecord, ...]:
        notification = PlayerNotificationRecord(
            recipient_player_id=UUID(int=2),
            audience="admin",
            kind=str(values["kind"]),
            deduplication_key=str(values["deduplication_key"]),
            payload=dict(values["payload"]),  # type: ignore[arg-type]
        )
        current_session.add(notification)
        return (notification,)

    monkeypatch.setattr(
        NotificationWriter,
        "create_for_administrators",
        classmethod(create_for_administrators),
    )

    created = await service.create_request(
        player.id,
        tournament_name=" Autumn Open ",
        justification=" Original event ",
    )

    request = next(
        value for value in session.added if isinstance(value, TournamentCreationTokenRequestRecord)
    )
    notification = next(
        value for value in session.added if isinstance(value, PlayerNotificationRecord)
    )
    assert created is snapshot
    assert request.tournament_name == "Autumn Open"
    assert request.justification == "Original event"
    assert notification.audience == "admin"
    assert "real_name" not in notification.payload

    duplicate_session = CreateRequestSession(player, pending=request)
    duplicate_service = TournamentTokenRequestService(
        FakeDatabase(duplicate_session),  # type: ignore[arg-type]
        delivery_encryption_key=ENCRYPTION_KEY,
    )
    duplicate_service._request_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=snapshot
    )
    duplicate = await duplicate_service.create_request(
        player.id, tournament_name="Autumn Open", justification="Ignored retry"
    )

    assert duplicate is snapshot
    assert duplicate_session.added == []


class DecisionSession:
    def __init__(
        self,
        request: TournamentCreationTokenRequestRecord,
        requester: PlayerRecord,
        administrator: PlatformAdministratorRecord,
    ) -> None:
        self.request = request
        self.requester = requester
        self.administrator = administrator
        self.added: list[object] = []
        self.flush = AsyncMock(side_effect=self._assign_token_defaults)

    async def get(self, model: type[object], identity: object) -> object | None:
        if model is PlatformAdministratorRecord and identity == self.administrator.player_id:
            return self.administrator
        if model is PlayerRecord and identity == self.requester.id:
            return self.requester
        return None

    async def scalar(self, _query: object) -> TournamentCreationTokenRequestRecord:
        return self.request

    def add(self, value: object) -> None:
        self.added.append(value)

    def _assign_token_defaults(self) -> None:
        for value in self.added:
            if isinstance(value, TournamentCreationTokenRecord) and value.id is None:
                value.id = UUID(int=20)
                value.created_at = datetime(2026, 9, 2, tzinfo=UTC)


async def test_approval_atomically_creates_bound_token_and_encrypted_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requester = active_player()
    administrator = PlatformAdministratorRecord(player_id=UUID(int=2), revoked_at=None)
    request = TournamentCreationTokenRequestRecord(
        id=UUID(int=10),
        requester_id=requester.id,
        status="pending",
        tournament_name="Autumn Open",
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    session = DecisionSession(request, requester, administrator)
    service = TournamentTokenRequestService(
        FakeDatabase(session),  # type: ignore[arg-type]
        delivery_encryption_key=ENCRYPTION_KEY,
    )
    snapshot = object()
    service._request_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]
    monkeypatch.setattr(NotificationWriter, "_schedule_alert", AsyncMock())
    raw_token = "plaintext-token-only-for-this-test"
    monkeypatch.setattr(
        "sitg_bot.services.token_requests.secrets.token_urlsafe", lambda _n: raw_token
    )

    result = await service.decide_request(
        request.id,
        administrator.player_id,
        approve=True,
        note="Approved",
    )

    token = next(
        value for value in session.added if isinstance(value, TournamentCreationTokenRecord)
    )
    delivery = next(
        value for value in session.added if isinstance(value, TournamentCreationTokenDeliveryRecord)
    )
    notification = next(
        value for value in session.added if isinstance(value, PlayerNotificationRecord)
    )
    assert result is snapshot
    assert request.status == "approved"
    assert request.decided_by_id == administrator.player_id
    assert token.intended_creator_id == requester.id
    assert token.request_id == request.id
    assert token.token_digest == hashlib.sha256(raw_token.encode()).hexdigest()
    assert token.fingerprint == token.token_digest[:12]
    assert delivery.recipient_player_id == requester.id
    assert delivery.encrypted_token != raw_token
    assert notification.payload == {"request_id": str(request.id), "status": "approved"}


async def test_approval_without_plaintext_delivery_key_still_issues_usable_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requester = active_player()
    administrator = PlatformAdministratorRecord(player_id=UUID(int=2), revoked_at=None)
    request = TournamentCreationTokenRequestRecord(
        id=UUID(int=10),
        requester_id=requester.id,
        tournament_name="Autumn Open",
        status="pending",
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    session = DecisionSession(request, requester, administrator)
    service = TournamentTokenRequestService(FakeDatabase(session))  # type: ignore[arg-type]
    service._request_snapshot = AsyncMock(return_value=object())  # type: ignore[method-assign]
    monkeypatch.setattr(NotificationWriter, "_schedule_alert", AsyncMock())

    await service.decide_request(request.id, administrator.player_id, approve=True)

    assert any(isinstance(item, TournamentCreationTokenRecord) for item in session.added)
    assert not any(
        isinstance(item, TournamentCreationTokenDeliveryRecord) for item in session.added
    )


class ClaimSession:
    def __init__(
        self,
        request: TournamentCreationTokenRequestRecord,
        token: TournamentCreationTokenRecord,
        delivery: TournamentCreationTokenDeliveryRecord,
        raw_token: str,
    ) -> None:
        self.request = request
        self.token = token
        self.delivery = delivery
        self.scalar_values: list[object] = [token, raw_token]
        self.flush = AsyncMock()

    async def get(self, model: type[object], identity: object, **_kwargs: object) -> object | None:
        if model is TournamentCreationTokenRequestRecord and identity == self.request.id:
            return self.request
        if model is TournamentCreationTokenDeliveryRecord and identity == self.token.id:
            return self.delivery
        return None

    async def scalar(self, _query: object) -> object:
        return self.scalar_values.pop(0)


def delivery_fixture(
    *, expires_at: datetime
) -> tuple[
    TournamentCreationTokenRequestRecord,
    TournamentCreationTokenRecord,
    TournamentCreationTokenDeliveryRecord,
]:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    request = TournamentCreationTokenRequestRecord(
        id=UUID(int=10), requester_id=UUID(int=1), status="approved", created_at=now,
        tournament_name="Autumn Open",
    )
    token = TournamentCreationTokenRecord(
        id=UUID(int=20),
        token_digest="a" * 64,
        fingerprint="a" * 12,
        request_id=request.id,
        issued_by_id=UUID(int=2),
        intended_creator_id=request.requester_id,
        expires_at=expires_at,
        created_at=now,
    )
    delivery = TournamentCreationTokenDeliveryRecord(
        token_id=token.id,
        recipient_player_id=request.requester_id,
        encrypted_token=b"ciphertext",
        status="pending",
        created_at=now,
    )
    return request, token, delivery


async def test_plaintext_can_be_claimed_only_once() -> None:
    request, token, delivery = delivery_fixture(expires_at=datetime.now(UTC) + timedelta(hours=1))
    session = ClaimSession(request, token, delivery, "one-time-token")
    service = TournamentTokenRequestService(
        FakeDatabase(session),  # type: ignore[arg-type]
        delivery_encryption_key=ENCRYPTION_KEY,
    )

    delivered = await service.claim_token_once(request.id, request.requester_id)

    assert delivered.token == "one-time-token"
    assert delivery.status == "delivered"
    assert delivery.encrypted_token is None
    assert delivery.delivered_at is not None

    second_session = ClaimSession(request, token, delivery, "must-not-be-returned")
    second_service = TournamentTokenRequestService(
        FakeDatabase(second_session),  # type: ignore[arg-type]
        delivery_encryption_key=ENCRYPTION_KEY,
    )
    with pytest.raises(ValueError, match="no longer available"):
        await second_service.claim_token_once(request.id, request.requester_id)


async def test_expired_delivery_is_durably_failed_before_error_is_returned() -> None:
    request, token, delivery = delivery_fixture(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    session = ClaimSession(request, token, delivery, "must-not-be-returned")
    session.scalar_values = [token]
    service = TournamentTokenRequestService(
        FakeDatabase(session),  # type: ignore[arg-type]
        delivery_encryption_key=ENCRYPTION_KEY,
    )

    with pytest.raises(ValueError, match="expired"):
        await service.claim_token_once(request.id, request.requester_id)

    assert delivery.status == "failed"
    assert delivery.failure_reason == "expired"
    assert delivery.encrypted_token is None


def test_token_inventory_status_prioritizes_terminal_states() -> None:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    _, token, _ = delivery_fixture(expires_at=now + timedelta(hours=1))

    assert TournamentTokenRequestService._token_status(token, now=now) == "issued"
    token.expires_at = now - timedelta(seconds=1)
    assert TournamentTokenRequestService._token_status(token, now=now) == "expired"
    token.used_at = now
    assert TournamentTokenRequestService._token_status(token, now=now) == "used"
    token.revoked_at = now
    assert TournamentTokenRequestService._token_status(token, now=now) == "revoked"


def test_token_inventory_metadata_cannot_expose_plaintext_or_digest() -> None:
    field_names = {field.name for field in fields(TokenInventoryItem)}

    assert "token" not in field_names
    assert "token_digest" not in field_names
    assert "fingerprint" in field_names
