import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.services.author_exposure import burn_author_content
from sitg_bot.services.navigation import TelegramNavigationService
from sitg_bot.services.notifications import NOTIFICATION_ALERT_COOLDOWN, NotificationWriter
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AuthorRecord,
    LogicalPacketRecord,
    LogicalQuestionRecord,
    PacketQuestionRecord,
    PacketVersionRecord,
    PlatformAdministratorRecord,
    PlayerAuthorLinkRecord,
    PlayerAuthorLinkRequestRecord,
    PlayerNotificationAlertRecord,
    PlayerNotificationRecord,
    PlayerRecord,
    QuestionRevisionRecord,
    ThemeRecord,
    ThemeRevisionRecord,
    TournamentAuthorRecord,
    TournamentRecord,
)


@dataclass(frozen=True, slots=True)
class AuthorAuthorshipMetadata:
    packet_count: int
    theme_count: int
    question_count: int
    packet_names: tuple[str, ...]
    theme_names: tuple[str, ...]
    tournament_names: tuple[str, ...]
    years: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AuthorSummary:
    author_id: UUID
    display_name: str
    authorship: AuthorAuthorshipMetadata


@dataclass(frozen=True, slots=True)
class AuthorSearchPage:
    items: tuple[AuthorSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AuthorLinkRequestSnapshot:
    request_id: UUID
    player_id: UUID
    player_nickname: str
    author: AuthorSummary
    status: str
    request_note: str | None
    decision_note: str | None
    decided_by_id: UUID | None
    created_at: datetime
    decided_at: datetime | None
    cancelled_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthorLinkRequestPage:
    items: tuple[AuthorLinkRequestSnapshot, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class LinkedPlayerSummary:
    player_id: UUID
    public_nickname: str


@dataclass(frozen=True, slots=True)
class PlayerAuthorshipProjection:
    player_id: UUID
    public_nickname: str
    authors: tuple[AuthorSummary, ...]


@dataclass(frozen=True, slots=True)
class AuthorProfileProjection:
    author: AuthorSummary
    linked_players: tuple[LinkedPlayerSummary, ...]


@dataclass(frozen=True, slots=True)
class NotificationSnapshot:
    notification_id: UUID
    kind: str
    payload: dict[str, Any]
    created_at: datetime
    read_at: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: tuple[NotificationSnapshot, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class NotificationReadResult:
    read_count: int


@dataclass(frozen=True, slots=True)
class NotificationAlertResult:
    audiences: tuple[str, ...]


class AuthorLinkService:
    """Owns author discovery and the audited player-to-author identity workflow."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def search_authors(
        self, query: str = "", *, cursor: str | None = None, limit: int = 20
    ) -> AuthorSearchPage:
        if limit < 1 or limit > 100:
            raise ValueError("Author search limit must be between 1 and 100")
        normalized_query = " ".join(query.strip().split())
        if len(normalized_query) > 300:
            raise ValueError("Author search query cannot exceed 300 characters")
        cursor_name, cursor_id = self._decode_name_cursor(cursor)

        async with self.database.transaction() as session:
            normalized_name = func.lower(AuthorRecord.display_name)
            statement = select(AuthorRecord)
            if normalized_query:
                pattern = f"%{self._escape_like(normalized_query.lower())}%"
                statement = statement.where(normalized_name.like(pattern, escape="\\"))
            if cursor_name is not None and cursor_id is not None:
                statement = statement.where(
                    or_(
                        normalized_name > cursor_name,
                        and_(
                            normalized_name == cursor_name,
                            AuthorRecord.id > cursor_id,
                        ),
                    )
                )
            authors = tuple(
                (
                    await session.execute(
                        statement.order_by(normalized_name, AuthorRecord.id).limit(limit + 1)
                    )
                ).scalars()
            )
            visible = authors[:limit]
            items = tuple([await self._author_summary(session, author) for author in visible])
            next_cursor = (
                self._encode_name_cursor(visible[-1]) if len(authors) > limit and visible else None
            )
            return AuthorSearchPage(items, next_cursor)

    async def create_request(
        self, player_id: UUID, author_id: UUID, *, note: str | None = None
    ) -> AuthorLinkRequestSnapshot:
        normalized_note = self._note(note, label="Request note")
        async with self.database.transaction() as session:
            await session.execute(
                select(func.pg_advisory_xact_lock(self._pair_lock_key(player_id, author_id)))
            )
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active" or player.public_nickname is None:
                raise LookupError("Active player not found")
            author = await session.get(AuthorRecord, author_id)
            if author is None:
                raise LookupError("Author not found")
            linked = await session.get(PlayerAuthorLinkRecord, (player_id, author_id))
            if linked is not None:
                raise ValueError("Player is already linked to this author")
            pending = await session.scalar(
                select(PlayerAuthorLinkRequestRecord)
                .where(
                    PlayerAuthorLinkRequestRecord.player_id == player_id,
                    PlayerAuthorLinkRequestRecord.author_id == author_id,
                    PlayerAuthorLinkRequestRecord.status == "pending",
                )
                .with_for_update()
            )
            if pending is not None:
                return await self._request_snapshot(session, pending)
            request = PlayerAuthorLinkRequestRecord(
                player_id=player_id,
                author_id=author_id,
                request_note=normalized_note,
                status="pending",
            )
            session.add(request)
            await session.flush()
            notification_key = f"author-link-request:{request.id}:created"
            notification_payload = {
                "request_id": str(request.id),
                "player_id": str(player.id),
                "player_nickname": player.public_nickname,
                "author_id": str(author.id),
                "author_name": author.display_name,
            }
            await NotificationWriter.create_for_administrators(
                session,
                kind="author_link.request_created",
                deduplication_key=notification_key,
                payload=notification_payload,
            )
            return await self._request_snapshot(session, request)

    async def cancel_request(self, request_id: UUID, player_id: UUID) -> AuthorLinkRequestSnapshot:
        async with self.database.transaction() as session:
            request = await session.scalar(
                select(PlayerAuthorLinkRequestRecord)
                .where(PlayerAuthorLinkRequestRecord.id == request_id)
                .with_for_update()
            )
            if request is None or request.player_id != player_id:
                raise LookupError("Author link request not found")
            if request.status == "cancelled":
                return await self._request_snapshot(session, request)
            if request.status != "pending":
                raise ValueError("Only a pending author link request can be cancelled")
            request.status = "cancelled"
            request.cancelled_at = datetime.now(UTC)
            await session.flush()
            return await self._request_snapshot(session, request)

    async def player_requests(
        self, player_id: UUID, *, cursor: str | None = None, limit: int = 20
    ) -> AuthorLinkRequestPage:
        return await self._request_page(
            player_id=player_id, cursor=cursor, limit=limit, require_admin_id=None
        )

    async def admin_queue(
        self, administrator_id: UUID, *, cursor: str | None = None, limit: int = 20
    ) -> AuthorLinkRequestPage:
        return await self._request_page(
            player_id=None,
            cursor=cursor,
            limit=limit,
            require_admin_id=administrator_id,
        )

    async def decide_request(
        self,
        request_id: UUID,
        administrator_id: UUID,
        *,
        approve: bool,
        note: str | None = None,
    ) -> AuthorLinkRequestSnapshot:
        normalized_note = self._note(note, label="Decision note")
        desired_status = "approved" if approve else "rejected"
        async with self.database.transaction() as session:
            await self._require_administrator(session, administrator_id)
            request = await session.scalar(
                select(PlayerAuthorLinkRequestRecord)
                .where(PlayerAuthorLinkRequestRecord.id == request_id)
                .with_for_update()
            )
            if request is None:
                raise LookupError("Author link request not found")
            if request.status == desired_status:
                return await self._request_snapshot(session, request)
            if request.status != "pending":
                raise ValueError("Author link request is already closed")

            now = datetime.now(UTC)
            if approve:
                existing = await session.get(
                    PlayerAuthorLinkRecord, (request.player_id, request.author_id)
                )
                if existing is not None:
                    raise ValueError("Player is already linked to this author")
                session.add(
                    PlayerAuthorLinkRecord(
                        player_id=request.player_id,
                        author_id=request.author_id,
                        approved_request_id=request.id,
                        approved_by_id=administrator_id,
                        approved_at=now,
                    )
                )
            if approve:
                await burn_author_content(session, author_id=request.author_id)
            request.status = desired_status
            request.decided_by_id = administrator_id
            request.decision_note = normalized_note
            request.decided_at = now
            notification_key = f"author-link-request:{request.id}:decided"
            notification_payload = {
                "request_id": str(request.id),
                "author_id": str(request.author_id),
                "status": desired_status,
            }
            await NotificationWriter.create_for_player(
                session,
                recipient_player_id=request.player_id,
                audience="player",
                kind="author_link.request_decided",
                deduplication_key=notification_key,
                payload=notification_payload,
            )
            await session.flush()
            return await self._request_snapshot(session, request)

    async def player_authorship_projection(self, player_id: UUID) -> PlayerAuthorshipProjection:
        async with self.database.transaction() as session:
            player = await session.get(PlayerRecord, player_id)
            if player is None or player.status != "active" or player.public_nickname is None:
                raise LookupError("Active player not found")
            authors = tuple(
                (
                    await session.execute(
                        select(AuthorRecord)
                        .join(
                            PlayerAuthorLinkRecord,
                            PlayerAuthorLinkRecord.author_id == AuthorRecord.id,
                        )
                        .where(PlayerAuthorLinkRecord.player_id == player_id)
                        .order_by(func.lower(AuthorRecord.display_name), AuthorRecord.id)
                    )
                ).scalars()
            )
            summaries = tuple([await self._author_summary(session, author) for author in authors])
            return PlayerAuthorshipProjection(player.id, player.public_nickname, summaries)

    async def author_profile_projection(self, author_id: UUID) -> AuthorProfileProjection:
        async with self.database.transaction() as session:
            author = await session.get(AuthorRecord, author_id)
            if author is None:
                raise LookupError("Author not found")
            players = tuple(
                (
                    await session.execute(
                        select(PlayerRecord)
                        .join(
                            PlayerAuthorLinkRecord,
                            PlayerAuthorLinkRecord.player_id == PlayerRecord.id,
                        )
                        .where(
                            PlayerAuthorLinkRecord.author_id == author_id,
                            PlayerRecord.status == "active",
                            PlayerRecord.public_nickname.is_not(None),
                        )
                        .order_by(func.lower(PlayerRecord.public_nickname), PlayerRecord.id)
                    )
                ).scalars()
            )
            return AuthorProfileProjection(
                await self._author_summary(session, author),
                tuple(
                    LinkedPlayerSummary(player.id, player.public_nickname)
                    for player in players
                    if player.public_nickname is not None
                ),
            )

    async def notifications(
        self, player_id: UUID, *, audience: str = "player", limit: int = 50
    ) -> tuple[NotificationSnapshot, ...]:
        page = await self.notification_page(player_id, audience=audience, limit=limit)
        return page.items

    async def notification_page(
        self,
        player_id: UUID,
        *,
        audience: str = "player",
        read_state: str = "all",
        cursor: str | None = None,
        limit: int = 20,
    ) -> NotificationPage:
        if limit < 1 or limit > 100:
            raise ValueError("Notification limit must be between 1 and 100")
        cursor_time, cursor_id = self._decode_time_cursor(cursor)
        async with self.database.transaction() as session:
            if audience == "admin":
                await self._require_administrator(session, player_id)
                statement = select(PlayerNotificationRecord).where(
                    PlayerNotificationRecord.audience == "admin",
                    PlayerNotificationRecord.recipient_player_id == player_id,
                )
            elif audience in {"player", "manager"}:
                statement = select(PlayerNotificationRecord).where(
                    PlayerNotificationRecord.audience == audience,
                    PlayerNotificationRecord.recipient_player_id == player_id,
                )
            else:
                raise ValueError("Unknown notification audience")
            if read_state == "unseen":
                statement = statement.where(PlayerNotificationRecord.read_at.is_(None))
            elif read_state == "seen":
                statement = statement.where(PlayerNotificationRecord.read_at.is_not(None))
            elif read_state != "all":
                raise ValueError("Unknown notification read state")
            if cursor_time is not None and cursor_id is not None:
                statement = statement.where(
                    or_(
                        PlayerNotificationRecord.created_at < cursor_time,
                        (PlayerNotificationRecord.created_at == cursor_time)
                        & (PlayerNotificationRecord.id < cursor_id),
                    )
                )
            records = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            PlayerNotificationRecord.created_at.desc(),
                            PlayerNotificationRecord.id.desc(),
                        ).limit(limit + 1)
                    )
                ).scalars()
            )
            visible = records[:limit]
            return NotificationPage(
                tuple(self._notification_snapshot(record) for record in visible),
                (
                    self._encode_notification_cursor(visible[-1])
                    if len(records) > limit and visible
                    else None
                ),
            )

    async def mark_notification_read(
        self, notification_id: UUID, player_id: UUID
    ) -> NotificationSnapshot:
        async with self.database.transaction() as session:
            record = await session.get(
                PlayerNotificationRecord, notification_id, with_for_update=True
            )
            if record is None:
                raise LookupError("Notification not found")
            if record.audience == "admin":
                await self._require_administrator(session, player_id)
            if record.recipient_player_id != player_id:
                raise PermissionError("Notification does not belong to this player")
            if record.read_at is None:
                record.read_at = datetime.now(UTC)
                await session.flush()
            return self._notification_snapshot(record)

    async def mark_notifications_read(
        self, player_id: UUID, *, audience: str
    ) -> NotificationReadResult:
        async with self.database.transaction() as session:
            if audience == "admin":
                await self._require_administrator(session, player_id)
                filters = (
                    PlayerNotificationRecord.audience == "admin",
                    PlayerNotificationRecord.recipient_player_id == player_id,
                )
            elif audience in {"player", "manager"}:
                filters = (
                    PlayerNotificationRecord.audience == audience,
                    PlayerNotificationRecord.recipient_player_id == player_id,
                )
            else:
                raise ValueError("Unknown notification audience")
            result = await session.execute(
                update(PlayerNotificationRecord)
                .where(*filters, PlayerNotificationRecord.read_at.is_(None))
                .values(read_at=datetime.now(UTC))
                .returning(PlayerNotificationRecord.id)
            )
            return NotificationReadResult(len(tuple(result.scalars())))

    async def claim_notification_alerts(
        self,
        player_id: UUID,
        *,
        audience: str,
        cooldown: timedelta = NOTIFICATION_ALERT_COOLDOWN,
    ) -> NotificationAlertResult:
        now = datetime.now(UTC)
        async with self.database.transaction() as session:
            if (
                await session.scalar(
                    select(PlayerRecord.id).where(PlayerRecord.id == player_id).with_for_update()
                )
                is None
            ):
                raise LookupError("Player not found")
            if audience == "admin":
                await self._require_administrator(session, player_id)
                notification_filters = (
                    PlayerNotificationRecord.audience == "admin",
                    PlayerNotificationRecord.recipient_player_id == player_id,
                )
            elif audience in {"player", "manager"}:
                notification_filters = (
                    PlayerNotificationRecord.audience == audience,
                    PlayerNotificationRecord.recipient_player_id == player_id,
                )
            else:
                raise ValueError("Unknown notification audience")
            if await TelegramNavigationService._active_game(session, player_id) is not None:
                return NotificationAlertResult(())
            if (
                await session.scalar(
                    select(PlayerNotificationRecord.id)
                    .where(
                        *notification_filters,
                        PlayerNotificationRecord.read_at.is_(None),
                    )
                    .limit(1)
                )
                is None
            ):
                return NotificationAlertResult(())
            alert = await session.get(
                PlayerNotificationAlertRecord,
                player_id,
                with_for_update=True,
            )
            if alert is not None and alert.last_alerted_at > now - cooldown:
                return NotificationAlertResult(())
            if alert is None:
                session.add(
                    PlayerNotificationAlertRecord(
                        player_id=player_id,
                        last_alerted_at=now,
                    )
                )
            else:
                alert.last_alerted_at = now
            await session.flush()
            return NotificationAlertResult((audience,))

    async def _request_page(
        self,
        *,
        player_id: UUID | None,
        cursor: str | None,
        limit: int,
        require_admin_id: UUID | None,
    ) -> AuthorLinkRequestPage:
        if limit < 1 or limit > 100:
            raise ValueError("Author link request limit must be between 1 and 100")
        cursor_time, cursor_id = self._decode_time_cursor(cursor)
        async with self.database.transaction() as session:
            if require_admin_id is not None:
                await self._require_administrator(session, require_admin_id)
            statement = select(PlayerAuthorLinkRequestRecord)
            if player_id is None:
                statement = statement.where(PlayerAuthorLinkRequestRecord.status == "pending")
            else:
                player = await session.get(PlayerRecord, player_id)
                if player is None:
                    raise LookupError("Player not found")
                statement = statement.where(PlayerAuthorLinkRequestRecord.player_id == player_id)
            if cursor_time is not None and cursor_id is not None:
                statement = statement.where(
                    or_(
                        PlayerAuthorLinkRequestRecord.created_at > cursor_time,
                        (PlayerAuthorLinkRequestRecord.created_at == cursor_time)
                        & (PlayerAuthorLinkRequestRecord.id > cursor_id),
                    )
                )
            records = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            PlayerAuthorLinkRequestRecord.created_at,
                            PlayerAuthorLinkRequestRecord.id,
                        ).limit(limit + 1)
                    )
                ).scalars()
            )
            visible = records[:limit]
            items = tuple([await self._request_snapshot(session, item) for item in visible])
            next_cursor = (
                self._encode_time_cursor(visible[-1]) if len(records) > limit and visible else None
            )
            return AuthorLinkRequestPage(items, next_cursor)

    async def _request_snapshot(
        self, session: AsyncSession, request: PlayerAuthorLinkRequestRecord
    ) -> AuthorLinkRequestSnapshot:
        player = await session.get(PlayerRecord, request.player_id)
        author = await session.get(AuthorRecord, request.author_id)
        if player is None or player.public_nickname is None or author is None:
            raise LookupError("Author link request identity is unavailable")
        return AuthorLinkRequestSnapshot(
            request.id,
            player.id,
            player.public_nickname,
            await self._author_summary(session, author),
            request.status,
            request.request_note,
            request.decision_note,
            request.decided_by_id,
            request.created_at,
            request.decided_at,
            request.cancelled_at,
        )

    async def _author_summary(self, session: AsyncSession, author: AuthorRecord) -> AuthorSummary:
        lead_rows = (
            await session.execute(
                select(
                    PacketVersionRecord.packet_id,
                    PacketVersionRecord.name,
                    PacketVersionRecord.year,
                )
                .where(
                    PacketVersionRecord.packet_id == LogicalPacketRecord.id,
                    LogicalPacketRecord.statistical_author_id == author.id,
                )
                .order_by(PacketVersionRecord.published_at.desc())
            )
        ).all()
        theme_rows = (
            await session.execute(
                select(
                    ThemeRevisionRecord.theme_id,
                    ThemeRevisionRecord.name,
                    PacketVersionRecord.packet_id,
                    PacketVersionRecord.name,
                    PacketVersionRecord.year,
                )
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == ThemeRevisionRecord.packet_version_id,
                )
                .where(
                    ThemeRevisionRecord.theme_id == ThemeRecord.id,
                    ThemeRecord.statistical_author_id == author.id,
                )
            )
        ).all()
        question_rows = (
            await session.execute(
                select(
                    QuestionRevisionRecord.question_id,
                    PacketVersionRecord.packet_id,
                    PacketVersionRecord.name,
                    PacketVersionRecord.year,
                )
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                )
                .join(
                    PacketVersionRecord,
                    PacketVersionRecord.id == PacketQuestionRecord.packet_version_id,
                )
                .where(
                    QuestionRevisionRecord.question_id == LogicalQuestionRecord.id,
                    LogicalQuestionRecord.statistical_author_id == author.id,
                )
            )
        ).all()
        tournament_names = tuple(
            (
                await session.execute(
                    select(TournamentRecord.name)
                    .join(
                        TournamentAuthorRecord,
                        TournamentAuthorRecord.tournament_id == TournamentRecord.id,
                    )
                    .where(TournamentAuthorRecord.author_id == author.id)
                    .order_by(func.lower(TournamentRecord.name), TournamentRecord.id)
                )
            ).scalars()
        )
        packet_names = (
            {name for _, name, _ in lead_rows}
            | {packet_name for _, _, _, packet_name, _ in theme_rows}
            | {packet_name for _, _, packet_name, _ in question_rows}
        )
        years = (
            {year for _, _, year in lead_rows if year is not None}
            | {year for _, _, _, _, year in theme_rows if year is not None}
            | {year for _, _, _, year in question_rows if year is not None}
        )
        packet_ids = (
            {packet_id for packet_id, _, _ in lead_rows}
            | {packet_id for _, _, packet_id, _, _ in theme_rows}
            | {packet_id for _, packet_id, _, _ in question_rows}
        )
        metadata = AuthorAuthorshipMetadata(
            len(packet_ids),
            len({theme_id for theme_id, _, _, _, _ in theme_rows}),
            len({question_id for question_id, _, _, _ in question_rows}),
            tuple(sorted(packet_names, key=str.casefold)[:5]),
            tuple(
                sorted(
                    {theme_name for _, theme_name, _, _, _ in theme_rows},
                    key=str.casefold,
                )[:5]
            ),
            tuple(dict.fromkeys(tournament_names))[:5],
            tuple(sorted(years)),
        )
        return AuthorSummary(author.id, author.display_name, metadata)

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
        if len(normalized) > 1000:
            raise ValueError(f"{label} cannot exceed 1000 characters")
        return normalized

    @staticmethod
    def _pair_lock_key(player_id: UUID, author_id: UUID) -> int:
        digest = hashlib.sha256(player_id.bytes + author_id.bytes).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _encode_name_cursor(author: AuthorRecord) -> str:
        return AuthorLinkService._encode_cursor(
            {"name": author.display_name.lower(), "id": str(author.id)}
        )

    @staticmethod
    def _decode_name_cursor(cursor: str | None) -> tuple[str | None, UUID | None]:
        if cursor is None:
            return None, None
        value = AuthorLinkService._decode_cursor(cursor)
        try:
            return str(value["name"]), UUID(str(value["id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid author search cursor") from error

    @staticmethod
    def _encode_time_cursor(request: PlayerAuthorLinkRequestRecord) -> str:
        return AuthorLinkService._encode_cursor(
            {"created_at": request.created_at.isoformat(), "id": str(request.id)}
        )

    @staticmethod
    def _encode_notification_cursor(notification: PlayerNotificationRecord) -> str:
        return AuthorLinkService._encode_cursor(
            {"created_at": notification.created_at.isoformat(), "id": str(notification.id)}
        )

    @staticmethod
    def _decode_time_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
        if cursor is None:
            return None, None
        value = AuthorLinkService._decode_cursor(cursor)
        try:
            created_at = datetime.fromisoformat(str(value["created_at"]))
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                raise ValueError
            return created_at, UUID(str(value["id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid author link request cursor") from error

    @staticmethod
    def _encode_cursor(value: dict[str, str]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> dict[str, Any]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid cursor") from error
        if not isinstance(value, dict):
            raise ValueError("Invalid cursor")
        return value

    @staticmethod
    def _notification_snapshot(record: PlayerNotificationRecord) -> NotificationSnapshot:
        return NotificationSnapshot(
            record.id, record.kind, record.payload, record.created_at, record.read_at
        )
