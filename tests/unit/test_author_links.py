from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.services.author_links import AuthorLinkService
from sitg_bot.services.notifications import NotificationWriter
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.storage.models import (
    AuthorRecord,
    PlatformAdministratorRecord,
    PlayerAuthorLinkRecord,
    PlayerAuthorLinkRequestRecord,
    PlayerNotificationRecord,
    PlayerRecord,
)


class FakeDatabase:
    def __init__(self, session: object) -> None:
        self.session = session

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self.session


class AuthorCreationSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush = AsyncMock()

    def add(self, value: object) -> None:
        self.added.append(value)


async def test_author_name_is_reused_across_import_operations() -> None:
    session = AuthorCreationSession()
    session.scalar = AsyncMock(return_value=None)

    first_cache: dict[str, AuthorRecord] = {}
    first = await PacketAdminService._author(session, "Same Name", first_cache)  # type: ignore[arg-type]
    repeated = await PacketAdminService._author(  # type: ignore[arg-type]
        session, " Same   Name ", first_cache
    )

    assert first is repeated
    assert len(session.added) == 1

    # A later import treats a namesake as the same person when a registered
    # author with the same name (ignoring capitalisation and word order)
    # already exists.
    registered = AuthorRecord(id=UUID(int=1), display_name="Name Same")
    session.scalar = AsyncMock(return_value=registered)
    matched = await PacketAdminService._author(session, "Same Name", {})  # type: ignore[arg-type]

    assert matched is registered
    assert len(session.added) == 1

    # Without a match a fresh author record is still registered.
    session.scalar = AsyncMock(return_value=None)
    created = await PacketAdminService._author(session, "Another Name", {})  # type: ignore[arg-type]

    assert created is not None
    assert created.display_name == "Another Name"
    assert len(session.added) == 2


def test_author_name_match_forms_ignore_case_and_word_order() -> None:
    assert PacketAdminService._name_match_forms("Name Surname") == (
        PacketAdminService._name_match_forms("surname   name")
    )
    assert set(PacketAdminService._name_match_forms("Name Surname")) == {
        "name surname",
        "surname name",
    }
    assert PacketAdminService._name_match_forms("  ") == ()
    assert PacketAdminService._name_match_forms("Name Surname") != (
        PacketAdminService._name_match_forms("Name Surname Jr")
    )


def test_author_names_are_non_unique_and_pending_request_is_uniquely_indexed() -> None:
    display_name_column = AuthorRecord.__table__.c.display_name
    pending_index = next(
        index
        for index in PlayerAuthorLinkRequestRecord.__table__.indexes
        if index.name == "uq_player_author_link_requests_pending"
    )

    assert display_name_column.unique is not True
    assert pending_index.unique is True
    assert str(pending_index.dialect_options["postgresql"]["where"]) == "status = 'pending'"
    assert [column.name for column in PlayerAuthorLinkRecord.__table__.primary_key] == [
        "player_id",
        "author_id",
    ]


def test_author_and_request_cursors_are_opaque_and_round_trip() -> None:
    author = AuthorRecord(id=UUID(int=10), display_name="Автор")
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    request = PlayerAuthorLinkRequestRecord(
        id=UUID(int=11),
        player_id=UUID(int=1),
        author_id=author.id,
        status="pending",
        created_at=now,
    )

    name_cursor = AuthorLinkService._encode_name_cursor(author)
    time_cursor = AuthorLinkService._encode_time_cursor(request)

    assert AuthorLinkService._decode_name_cursor(name_cursor) == ("автор", author.id)
    assert AuthorLinkService._decode_time_cursor(time_cursor) == (now, request.id)


class RequestSession:
    def __init__(self, player: PlayerRecord, author: AuthorRecord) -> None:
        self.player = player
        self.author = author
        self.added: list[object] = []
        self.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: ()))
        self.flush = AsyncMock(side_effect=self._assign_defaults)

    async def get(self, model: type[object], identity: object, **_kwargs: object) -> object | None:
        if model is PlayerRecord and identity == self.player.id:
            return self.player
        if model is AuthorRecord and identity == self.author.id:
            return self.author
        if model is PlayerAuthorLinkRecord:
            return None
        return None

    async def scalar(self, _query: object) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    def _assign_defaults(self) -> None:
        for value in self.added:
            if isinstance(value, PlayerAuthorLinkRequestRecord) and value.id is None:
                value.id = UUID(int=100)
                value.created_at = datetime(2026, 9, 2, tzinfo=UTC)


async def test_request_creation_writes_admin_notification_without_private_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = PlayerRecord(
        id=UUID(int=1),
        telegram_user_id=42,
        real_name="Private Real Name",
        public_nickname="Public nickname",
        status="active",
    )
    author = AuthorRecord(id=UUID(int=2), display_name="Author Name")
    session = RequestSession(player, author)
    service = AuthorLinkService(FakeDatabase(session))  # type: ignore[arg-type]
    snapshot = object()
    service._request_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]

    async def create_for_administrators(
        _cls: type[NotificationWriter], current_session: RequestSession, **values: object
    ) -> tuple[PlayerNotificationRecord, ...]:
        notification = PlayerNotificationRecord(
            recipient_player_id=UUID(int=3),
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

    result = await service.create_request(player.id, author.id)

    request = next(
        value for value in session.added if isinstance(value, PlayerAuthorLinkRequestRecord)
    )
    notification = next(
        value for value in session.added if isinstance(value, PlayerNotificationRecord)
    )
    assert result is snapshot
    assert request.status == "pending"
    assert notification.audience == "admin"
    assert notification.kind == "author_link.request_created"
    assert "real_name" not in notification.payload
    assert "Private Real Name" not in notification.payload.values()


class DecisionSession:
    def __init__(
        self,
        request: PlayerAuthorLinkRequestRecord,
        administrator: PlatformAdministratorRecord,
    ) -> None:
        self.request = request
        self.administrator = administrator
        self.added: list[object] = []
        self.flush = AsyncMock()

    async def get(self, model: type[object], identity: object, **_kwargs: object) -> object | None:
        if model is PlatformAdministratorRecord and identity == self.administrator.player_id:
            return self.administrator
        if model is PlayerAuthorLinkRecord:
            return None
        return None

    async def scalar(self, _query: object) -> PlayerAuthorLinkRequestRecord:
        return self.request

    async def scalars(self, _query: object) -> tuple[object, ...]:
        # Approval burns the newly linked author's content; no persisted links exist yet.
        return ()

    def add(self, value: object) -> None:
        self.added.append(value)


async def test_approval_creates_link_audit_and_player_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    request = PlayerAuthorLinkRequestRecord(
        id=UUID(int=10),
        player_id=UUID(int=1),
        author_id=UUID(int=2),
        status="pending",
        created_at=now,
    )
    administrator = PlatformAdministratorRecord(player_id=UUID(int=3), revoked_at=None)
    session = DecisionSession(request, administrator)
    service = AuthorLinkService(FakeDatabase(session))  # type: ignore[arg-type]
    snapshot = object()
    service._request_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]
    monkeypatch.setattr(NotificationWriter, "_schedule_alert", AsyncMock())

    result = await service.decide_request(
        request.id,
        administrator.player_id,
        approve=True,
        note="Verified evidence",
    )

    link = next(value for value in session.added if isinstance(value, PlayerAuthorLinkRecord))
    notification = next(
        value for value in session.added if isinstance(value, PlayerNotificationRecord)
    )
    assert result is snapshot
    assert request.status == "approved"
    assert request.decided_by_id == administrator.player_id
    assert request.decision_note == "Verified evidence"
    assert request.decided_at is not None
    assert link.player_id == request.player_id
    assert link.author_id == request.author_id
    assert notification.recipient_player_id == request.player_id
    assert notification.payload["status"] == "approved"
