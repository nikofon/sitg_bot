from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Textual checks need stable names for Alembic to compare and replace them.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "ck": "ck_%(table_name)s_%(check_digest)s",
            "check_digest": lambda constraint, table: sha256(
                str(constraint.sqltext).encode()
            ).hexdigest()[:12],
        }
    )


def uuid_column() -> Mapped[UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlayerRecord(Base, TimestampMixin):
    __tablename__ = "players"

    id: Mapped[UUID] = uuid_column()
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    real_name: Mapped[str | None] = mapped_column(String(200))
    public_nickname: Mapped[str | None] = mapped_column(String(200))
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    telegram_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    preferred_locale: Mapped[str] = mapped_column(
        String(2), nullable=False, default="ru", server_default=text("'ru'")
    )
    registration_step: Mapped[str] = mapped_column(
        String(24), nullable=False, default="real_name", server_default=text("'real_name'")
    )
    registration_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    profile_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    telegram_username_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reputation: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    suspicion: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    game_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="registration", server_default=text("'registration'")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('registration', 'active', 'anonymized')"),
        CheckConstraint("preferred_locale IN ('ru', 'en')"),
        CheckConstraint(
            "registration_step IN ('real_name', 'nickname', 'telegram_public', 'complete')"
        ),
        CheckConstraint("profile_version >= 0"),
        CheckConstraint(
            "(status = 'registration' AND registration_completed_at IS NULL) OR "
            "(status <> 'registration')"
        ),
        CheckConstraint(
            "status <> 'active' OR (registration_step = 'complete' "
            "AND registration_completed_at IS NOT NULL "
            "AND real_name IS NOT NULL AND public_nickname IS NOT NULL)"
        ),
        CheckConstraint("reputation BETWEEN 0 AND 100"),
        CheckConstraint("suspicion >= 0"),
    )


class PlayerBanRecord(Base):
    """One reversible moderation ban per player; the reason is shown to the player."""

    __tablename__ = "player_bans"

    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    banned_by_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifted_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(reason) > 0"),
        CheckConstraint("lifted_at IS NULL OR lifted_by_id IS NOT NULL"),
    )


class BugReportRecord(Base):
    __tablename__ = "bug_reports"

    id: Mapped[UUID] = uuid_column()
    reporter_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    commentary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(commentary) BETWEEN 1 AND 4000"),
        Index("ix_bug_reports_created", "created_at"),
    )


class PlatformAdministratorRecord(Base, TimestampMixin):
    __tablename__ = "platform_administrators"

    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    granted_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramGameViewRecord(Base):
    """Recoverable Telegram presentation cursor, independent of domain timing."""

    __tablename__ = "telegram_game_views"
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    flow_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    messages: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlayerTelegramNavigationRecord(Base):
    __tablename__ = "player_telegram_navigation"

    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    interaction_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="player", server_default=text("'player'")
    )
    player_context: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tournament", server_default=text("'tournament'")
    )
    selected_player_tournament_id: Mapped[UUID | None] = mapped_column(ForeignKey("tournaments.id"))
    selected_manager_tournament_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tournaments.id")
    )
    active_chat_id: Mapped[UUID | None] = mapped_column(ForeignKey("classic_chats.id"))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("interaction_mode IN ('player', 'manager', 'admin')"),
        CheckConstraint("version >= 1"),
    )


class TournamentTypeVersionRecord(Base, TimestampMixin):
    __tablename__ = "tournament_type_versions"

    id: Mapped[UUID] = uuid_column()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("key", "version"),
        CheckConstraint("version >= 1"),
    )


class GameRulesetVersionRecord(Base, TimestampMixin):
    __tablename__ = "game_ruleset_versions"

    id: Mapped[UUID] = uuid_column()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("key", "version"),
        CheckConstraint("version >= 1"),
    )


class TournamentRecord(Base, TimestampMixin):
    __tablename__ = "tournaments"

    id: Mapped[UUID] = uuid_column()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    registration_code: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True,
        server_default=text("replace(gen_random_uuid()::text, '-', '')"),
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    organizer_contacts: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel: Mapped[str] = mapped_column(Text, nullable=False, default="")
    moderation_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="normal", server_default=text("'normal'")
    )
    moderated_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = mapped_column(String(24), nullable=False, default="private")
    language: Mapped[str] = mapped_column(String(35), nullable=False, default="und")
    payment_type: Mapped[str] = mapped_column(String(24), nullable=False, default="free")
    registration_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    registration_open_override: Mapped[bool | None] = mapped_column(Boolean)
    registration_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignore_late_registrations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_reminded_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    participants_finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    type_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_type_versions.id"), nullable=False
    )
    game_ruleset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("game_ruleset_versions.id"), nullable=False
    )
    created_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'completed', 'archived')"),
        CheckConstraint("moderation_status IN ('normal', 'halted', 'abolished')"),
        CheckConstraint("visibility IN ('public', 'private')"),
        CheckConstraint("payment_type IN ('free', 'one-time', 'per-stage')"),
        CheckConstraint(
            "registration_ends_at IS NULL OR registration_starts_at IS NULL "
            "OR registration_ends_at > registration_starts_at"
        ),
        CheckConstraint(
            "starts_at IS NULL OR registration_ends_at IS NULL OR starts_at >= registration_ends_at"
        ),
        CheckConstraint(
            "planned_ends_at IS NULL OR starts_at IS NULL OR planned_ends_at > starts_at"
        ),
        CheckConstraint(
            "actual_ends_at IS NULL OR actual_starts_at IS NULL "
            "OR actual_ends_at >= actual_starts_at",
            name="ck_tournaments_actual_finish_after_start",
        ),
        CheckConstraint("settings_version >= 1"),
        Index("ix_tournaments_listing", "visibility", "status", "starts_at"),
    )


class TournamentPricingPlanRecord(Base, TimestampMixin):
    __tablename__ = "tournament_pricing_plans"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))

    __table_args__ = (
        UniqueConstraint("tournament_id", "name"),
        UniqueConstraint("tournament_id", "position"),
        CheckConstraint("length(trim(name)) > 0"),
        CheckConstraint("position >= 1"),
    )


class TournamentPricingPlanPriceRecord(Base):
    __tablename__ = "tournament_pricing_plan_prices"

    pricing_plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_pricing_plans.id", ondelete="CASCADE"), primary_key=True
    )
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    amount: Mapped[Any] = mapped_column(Numeric(14, 2), nullable=False)

    __table_args__ = (
        CheckConstraint("amount > 0"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'"),
    )


class TournamentPolicyVersionRecord(Base, TimestampMixin):
    __tablename__ = "tournament_policy_versions"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    default_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    player_mutable_parameters: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    policies: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))

    __table_args__ = (
        UniqueConstraint("tournament_id", "version"),
        CheckConstraint("version >= 1"),
    )


class TournamentManagerRecord(Base, TimestampMixin):
    __tablename__ = "tournament_managers"

    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    granted_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TournamentMembershipRecord(Base, TimestampMixin):
    __tablename__ = "tournament_memberships"

    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    rating: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False, default=1000)
    rating_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrolled_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    participation_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    participation_confirmed_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    registration_rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_rejection_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TournamentRegistrationRequirementRecord(Base, TimestampMixin):
    __tablename__ = "tournament_registration_requirements"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_tournament_id: Mapped[UUID | None] = mapped_column(ForeignKey("tournaments.id"))
    target_packet_id: Mapped[UUID | None] = mapped_column(ForeignKey("logical_packets.id"))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('has-played-tournament', 'has-not-played-tournament', 'has-not-seen-packet')"
        ),
        CheckConstraint(
            "(kind IN ('has-played-tournament', 'has-not-played-tournament') "
            "AND target_tournament_id IS NOT NULL AND target_packet_id IS NULL) OR "
            "(kind = 'has-not-seen-packet' AND target_tournament_id IS NULL "
            "AND target_packet_id IS NOT NULL)"
        ),
        Index(
            "ix_registration_requirements_active",
            "tournament_id",
            "kind",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class TournamentRegistrationAttemptRecord(Base, TimestampMixin):
    __tablename__ = "tournament_registration_attempts"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluated_requirement_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    failure_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        Index(
            "ix_registration_attempts_player",
            "tournament_id",
            "player_id",
            "created_at",
        ),
    )


class TournamentCreationTokenRequestRecord(Base, TimestampMixin):
    __tablename__ = "tournament_creation_token_requests"

    id: Mapped[UUID] = uuid_column()
    requester_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    tournament_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    justification: Mapped[str | None] = mapped_column(String(2000))
    decided_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    decision_note: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')"),
        CheckConstraint(
            "(status = 'pending' AND decided_by_id IS NULL AND decided_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_by_id IS NOT NULL "
            "AND decided_at IS NOT NULL)"
        ),
        Index(
            "uq_tournament_creation_token_requests_pending",
            "requester_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_tournament_creation_token_requests_queue", "status", "created_at", "id"),
        Index("ix_tournament_creation_token_requests_requester", "requester_id", "created_at"),
    )


class TournamentCreationTokenRecord(Base, TimestampMixin):
    __tablename__ = "tournament_creation_tokens"

    id: Mapped[UUID] = uuid_column()
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    fingerprint: Mapped[str] = mapped_column(String(12), nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tournament_creation_token_requests.id"), unique=True
    )
    issued_by_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    intended_creator_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    tournament_id: Mapped[UUID | None] = mapped_column(ForeignKey("tournaments.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))

    __table_args__ = (
        CheckConstraint(
            "used_at IS NULL OR (used_by_id IS NOT NULL AND tournament_id IS NOT NULL)"
        ),
        Index("ix_tournament_creation_tokens_creator", "intended_creator_id", "created_at"),
    )


class TournamentCreationTokenDeliveryRecord(Base, TimestampMixin):
    __tablename__ = "tournament_creation_token_deliveries"

    token_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_creation_tokens.id"), primary_key=True
    )
    recipient_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'delivered', 'failed')"),
        CheckConstraint(
            "(status = 'pending' AND encrypted_token IS NOT NULL "
            "AND delivered_at IS NULL AND failed_at IS NULL) OR "
            "(status = 'delivered' AND encrypted_token IS NULL "
            "AND delivered_at IS NOT NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND delivered_at IS NULL AND failed_at IS NOT NULL)"
        ),
        Index("ix_tournament_creation_token_deliveries_recipient", "recipient_player_id", "status"),
    )


class AuthorRecord(Base, TimestampMixin):
    __tablename__ = "authors"

    id: Mapped[UUID] = uuid_column()
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100))
    second_name: Mapped[str | None] = mapped_column(String(100))
    surname: Mapped[str | None] = mapped_column(String(100))
    telegram_link: Mapped[str | None] = mapped_column(String(200))
    telegram_username: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_authors_display_name", func.lower(display_name), "id"),
        Index("ix_authors_telegram_username", func.lower(telegram_username)),
    )


class PlayerAuthorLinkRequestRecord(Base, TimestampMixin):
    __tablename__ = "player_author_link_requests"

    id: Mapped[UUID] = uuid_column()
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    author_id: Mapped[UUID] = mapped_column(ForeignKey("authors.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    request_note: Mapped[str | None] = mapped_column(String(1000))
    decided_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    decision_note: Mapped[str | None] = mapped_column(String(1000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')"),
        CheckConstraint(
            "(status = 'pending' AND decided_by_id IS NULL AND decided_at IS NULL "
            "AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND decided_by_id IS NULL AND decided_at IS NULL "
            "AND cancelled_at IS NOT NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_by_id IS NOT NULL "
            "AND decided_at IS NOT NULL AND cancelled_at IS NULL)"
        ),
        Index(
            "uq_player_author_link_requests_pending",
            "player_id",
            "author_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_player_author_link_requests_queue", "status", "created_at", "id"),
        Index("ix_player_author_link_requests_player", "player_id", "created_at", "id"),
    )


class PlayerAuthorLinkRecord(Base, TimestampMixin):
    __tablename__ = "player_author_links"

    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    author_id: Mapped[UUID] = mapped_column(ForeignKey("authors.id"), primary_key=True)
    approved_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("player_author_link_requests.id"), nullable=False, unique=True
    )
    approved_by_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlayerNotificationRecord(Base, TimestampMixin):
    __tablename__ = "player_notifications"

    id: Mapped[UUID] = uuid_column()
    recipient_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("audience IN ('player', 'manager', 'admin')"),
        Index(
            "ix_player_notifications_recipient",
            "recipient_player_id",
            "created_at",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index(
            "ix_player_notifications_admin",
            "created_at",
            postgresql_where=text("audience = 'admin' AND read_at IS NULL"),
        ),
    )


class PlayerNotificationAlertRecord(Base):
    __tablename__ = "player_notification_alerts"

    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    last_alerted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApplicationRequestAuditRecord(Base, TimestampMixin):
    __tablename__ = "application_request_audit"

    id: Mapped[UUID] = uuid_column()
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_player_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    client_name: Mapped[str] = mapped_column(String(80), nullable=False)
    client_version: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key_digest: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(
        String(20), nullable=False, default="started", server_default=text("'started'")
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    resource_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("channel IN ('console', 'telegram_bot', 'mini_app', 'internal')"),
        CheckConstraint("outcome IN ('started', 'succeeded', 'rejected', 'failed')"),
        Index("ix_application_request_audit_correlation", "correlation_id"),
        Index("ix_application_request_audit_actor", "actor_player_id", "created_at"),
        Index("ix_application_request_audit_action", "action", "created_at"),
    )


class ApplicationIdempotencyRecord(Base, TimestampMixin):
    __tablename__ = "application_idempotency"

    id: Mapped[UUID] = uuid_column()
    actor_scope: Mapped[str] = mapped_column(String(100), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed', 'failed')"),
        UniqueConstraint("actor_scope", "channel", "action", "key_digest"),
        Index("ix_application_idempotency_pending", "status", "lease_expires_at"),
    )


class OutboxEventRecord(Base, TimestampMixin):
    __tablename__ = "outbox_events"

    id: Mapped[UUID] = uuid_column()
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    partition_key: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str | None] = mapped_column(String(60))
    aggregate_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    aggregate_sequence: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12, server_default=text("12")
    )
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'delivered', 'failed')"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts"),
        CheckConstraint("aggregate_sequence IS NULL OR aggregate_sequence >= 0"),
        CheckConstraint(
            "(status = 'processing' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND lease_token IS NULL AND lease_expires_at IS NULL)"
        ),
        CheckConstraint(
            "(status = 'delivered' AND delivered_at IS NOT NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND failed_at IS NOT NULL AND delivered_at IS NULL) OR "
            "(status IN ('pending', 'processing') AND delivered_at IS NULL AND failed_at IS NULL)"
        ),
        CheckConstraint(
            "(aggregate_type IS NULL AND aggregate_id IS NULL) OR "
            "(aggregate_type IS NOT NULL AND aggregate_id IS NOT NULL)"
        ),
        Index("ix_outbox_events_dispatch", "status", "available_at", "created_at"),
        Index(
            "ix_outbox_events_partition_order",
            "partition_key",
            "aggregate_sequence",
            "created_at",
        ),
    )


class DurableJobRecord(Base, TimestampMixin):
    __tablename__ = "durable_jobs"

    id: Mapped[UUID] = uuid_column()
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default=text("'queued'")
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12, server_default=text("12")
    )
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts"),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)"
        ),
        CheckConstraint(
            "(status = 'succeeded' AND completed_at IS NOT NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND failed_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('queued', 'running', 'cancelled') "
            "AND completed_at IS NULL AND failed_at IS NULL)"
        ),
        Index("ix_durable_jobs_claim", "status", "scheduled_at", "priority", "created_at"),
    )


class TelegramUpdateReceiptRecord(Base, TimestampMixin):
    __tablename__ = "telegram_update_receipts"

    id: Mapped[UUID] = uuid_column()
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="processing", server_default=text("'processing'")
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (
        CheckConstraint("environment IN ('production', 'test')"),
        CheckConstraint("status IN ('processing', 'completed', 'failed')"),
        UniqueConstraint("bot_id", "environment", "update_id"),
        Index("ix_telegram_update_receipts_lease", "status", "lease_expires_at"),
    )


class MiniAppSessionRecord(Base, TimestampMixin):
    __tablename__ = "mini_app_sessions"

    id: Mapped[UUID] = uuid_column()
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(500), nullable=False)
    telegram_auth_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("environment IN ('production', 'test')"),
        Index("ix_mini_app_sessions_player", "player_id", "expires_at"),
        Index("ix_mini_app_sessions_expiry", "expires_at", "revoked_at"),
    )


class ApplicationLaunchReferenceRecord(Base, TimestampMixin):
    __tablename__ = "application_launch_references"

    id: Mapped[UUID] = uuid_column()
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    route: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_by_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    intended_player_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    one_time: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    redeemed_by_player_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "route IN ('lobby', 'report', 'manager_settings', 'manager_management', "
            "'packet_draft')",
            name="ck_application_launch_references_route",
        ),
        CheckConstraint("environment IN ('production', 'test')"),
        Index("ix_application_launch_references_expiry", "expires_at", "redeemed_at"),
    )


class TournamentAuthorRecord(Base, TimestampMixin):
    __tablename__ = "tournament_authors"

    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), primary_key=True)
    author_id: Mapped[UUID] = mapped_column(ForeignKey("authors.id"), primary_key=True)
    added_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))


class PacketDraftRecord(Base, TimestampMixin):
    __tablename__ = "packet_drafts"

    id: Mapped[UUID] = uuid_column()
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation_errors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    validation_warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    uploader_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    creation_tournament_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournaments.id"), nullable=False
    )
    confirmed_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("packet_versions.id", use_alter=True, name="fk_draft_published_version")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_locale: Mapped[str | None] = mapped_column(String(10))
    author_bindings: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    lead_author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded', 'parsed', 'awaiting_confirmation', "
            "'validation_failed', 'rejected', 'published')"
        ),
        CheckConstraint("version >= 1"),
    )


class LogicalPacketRecord(Base, TimestampMixin):
    __tablename__ = "logical_packets"

    id: Mapped[UUID] = uuid_column()
    uploader_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statistical_author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))


class TournamentPacketAssignmentRecord(Base, TimestampMixin):
    __tablename__ = "tournament_packet_assignments"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    packet_id: Mapped[UUID] = mapped_column(ForeignKey("logical_packets.id"), nullable=False)
    adopted_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("packet_versions.id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    discoverable_by_members: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    playable_by_members: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_visible_by_members: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    editable_by_members: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    library_viewing_rule: Mapped[str] = mapped_column(
        String(24), nullable=False, default="after-play"
    )
    assigned_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))

    __table_args__ = (
        UniqueConstraint("tournament_id", "packet_id"),
        CheckConstraint("status IN ('active', 'retired')"),
        CheckConstraint(
            "library_viewing_rule IN ('never', 'after-play', 'anytime')"
        ),
    )


class TournamentPacketEntitlementRecord(Base, TimestampMixin):
    __tablename__ = "tournament_packet_entitlements"

    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_packet_assignments.id"), primary_key=True
    )
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    discoverable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # None inherits member-wide playability; False explicitly revokes it.
    playable: Mapped[bool | None] = mapped_column(Boolean)
    content_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    editable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    granted_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PacketDraftTournamentRecord(Base, TimestampMixin):
    __tablename__ = "packet_draft_tournaments"

    draft_id: Mapped[UUID] = mapped_column(ForeignKey("packet_drafts.id"), primary_key=True)
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), primary_key=True)


class PacketVersionRecord(Base):
    __tablename__ = "packet_versions"

    id: Mapped[UUID] = uuid_column()
    packet_id: Mapped[UUID] = mapped_column(
        ForeignKey("logical_packets.id"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    lead_author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))
    language: Mapped[str] = mapped_column(String(35), nullable=False, default="und")
    library_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="published")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("packet_versions.id"))
    source_draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_drafts.id"), nullable=False, unique=True
    )
    change_type: Mapped[str] = mapped_column(String(20), nullable=False, default="initial")
    change_reason: Mapped[str] = mapped_column(Text, nullable=False, default="Initial publication")
    published_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("packet_id", "version_number"),
        CheckConstraint("state IN ('published', 'archived')"),
        CheckConstraint("change_type IN ('initial', 'correction', 'substitution')"),
        CheckConstraint("year IS NULL OR year BETWEEN 1000 AND 9999"),
    )


class ThemeRecord(Base, TimestampMixin):
    __tablename__ = "themes"

    id: Mapped[UUID] = uuid_column()
    packet_id: Mapped[UUID] = mapped_column(ForeignKey("logical_packets.id"), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statistical_author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))


class ThemeRevisionRecord(Base):
    __tablename__ = "theme_revisions"

    id: Mapped[UUID] = uuid_column()
    theme_id: Mapped[UUID] = mapped_column(ForeignKey("themes.id"), nullable=False)
    packet_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_versions.id"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))
    commentary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("theme_id", "revision_number"),
        UniqueConstraint("packet_version_id", "position"),
    )


class LogicalQuestionRecord(Base, TimestampMixin):
    __tablename__ = "logical_questions"

    id: Mapped[UUID] = uuid_column()
    packet_id: Mapped[UUID] = mapped_column(ForeignKey("logical_packets.id"), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statistical_author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))


class QuestionRevisionRecord(Base):
    __tablename__ = "question_revisions"

    id: Mapped[UUID] = uuid_column()
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("logical_questions.id"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_answers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    rejected_answers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    commentary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    form: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author_id: Mapped[UUID | None] = mapped_column(ForeignKey("authors.id"))

    __table_args__ = (UniqueConstraint("question_id", "revision_number"),)


class PacketQuestionRecord(Base):
    __tablename__ = "packet_questions"

    packet_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_versions.id"), primary_key=True
    )
    theme_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("theme_revisions.id"), primary_key=True
    )
    question_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("question_revisions.id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("theme_revision_id", "position"),
        UniqueConstraint("theme_revision_id", "value"),
        CheckConstraint("value > 0"),
    )


class GameRecord(Base, TimestampMixin):
    __tablename__ = "games"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    tournament_type_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_type_versions.id"), nullable=False
    )
    game_ruleset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("game_ruleset_versions.id"), nullable=False
    )
    tournament_policy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_policy_versions.id"), nullable=False
    )
    rating_confidence_model: Mapped[str] = mapped_column(
        String(40), nullable=False, default="time_weighted"
    )
    host_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    source_lobby_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pregame_lobbies.id", use_alter=True, name="fk_game_source_lobby")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="lobby")
    phase: Mapped[str] = mapped_column(String(24), nullable=False, default="lobby")
    current_round_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("question_rounds.id", use_alter=True, name="fk_game_current_round")
    )
    accepted_buzzer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("game_participants.id", use_alter=True, name="fk_game_accepted_buzzer")
    )
    buzz_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progression_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    join_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_abandonment_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progression_stage: Mapped[str | None] = mapped_column(String(32))
    assignment_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    question_token_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('lobby', 'active', 'completed', 'finalized', 'failed_to_start', "
            "'cancelled', 'abandoned', 'invalidated')"
        ),
        CheckConstraint("phase IN ('lobby', 'countdown', 'intermission', 'question', 'finished')"),
        CheckConstraint(
            "progression_stage IS NULL OR progression_stage IN "
            "('ready_countdown', 'theme_start', 'theme_commentary', 'question_start', "
            "'next_question', 'theme_complete', 'theme_scoreboard', 'question_reveal_start', "
            "'question_reveal', 'buzz_timer_start', 'finish')"
        ),
        CheckConstraint("question_token_index >= 0"),
        Index(
            "ix_games_suspicion_evaluation",
            "game_ruleset_version_id",
            "status",
            "completed_at",
        ),
        Index("ix_games_join_deadline", "status", "join_deadline"),
        Index(
            "ix_games_pause_abandonment_deadline",
            "status",
            "paused",
            "pause_abandonment_deadline",
        ),
    )


class GameParticipantRecord(Base, TimestampMixin):
    __tablename__ = "game_participants"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    seat: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    global_game_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    joined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_chair: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False, default=0)
    final_place: Mapped[Any | None] = mapped_column(Numeric(8, 4))
    __table_args__ = (
        UniqueConstraint("game_id", "player_id"),
        UniqueConstraint("game_id", "seat"),
        UniqueConstraint("tournament_id", "player_id", "rating_sequence"),
        UniqueConstraint("player_id", "global_game_sequence"),
        CheckConstraint("rating_sequence >= 1"),
        CheckConstraint("global_game_sequence >= 1"),
        CheckConstraint("seat BETWEEN 1 AND 12"),
        Index(
            "uq_active_game_per_player",
            "player_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class ClassicStageRecord(Base, TimestampMixin):
    __tablename__ = "classic_stages"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    stage_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scheme_key: Mapped[str | None] = mapped_column(String(40))
    round_count: Mapped[int | None] = mapped_column(Integer)
    players_per_game: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seeds: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    place_points: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: ["4", "3", "2", "1"]
    )
    score_multiplier: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False, default="0.02")
    random_seed: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("tournament_id", "kind"),
        CheckConstraint("kind IN ('first', 'playoff')"),
        CheckConstraint("stage_type IN ('none', 'groups', 'quiz', 'swiss', 'playoff')"),
        CheckConstraint(
            "stage_type != 'swiss' OR (kind = 'first' AND round_count IS NOT NULL "
            "AND round_count >= 1 AND players_per_game IS NOT NULL "
            "AND players_per_game BETWEEN 2 AND 12)",
            name="swiss_configuration",
        ),
    )


class ClassicRoundRecord(Base):
    __tablename__ = "classic_rounds"

    id: Mapped[UUID] = uuid_column()
    stage_id: Mapped[UUID] = mapped_column(ForeignKey("classic_stages.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    assignment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tournament_packet_assignments.id")
    )
    discoverable: Mapped[bool | None] = mapped_column(Boolean)
    playable: Mapped[bool | None] = mapped_column(Boolean)
    start_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("stage_id", "number"), CheckConstraint("number >= 1"))


class ClassicMatchRecord(Base):
    __tablename__ = "classic_matches"

    id: Mapped[UUID] = uuid_column()
    round_id: Mapped[UUID] = mapped_column(ForeignKey("classic_rounds.id"), nullable=False)
    group_number: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    sources: Mapped[list] = mapped_column(JSONB, nullable=False)
    seats: Mapped[list] = mapped_column(JSONB, nullable=False)
    results: Mapped[list | None] = mapped_column(JSONB)
    randomized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    game_id: Mapped[UUID | None] = mapped_column(ForeignKey("games.id"), unique=True)

    __table_args__ = (
        UniqueConstraint("round_id", "group_number", "number"),
        CheckConstraint("group_number >= 1 AND number >= 1"),
    )


class ClassicChatRecord(Base, TimestampMixin):
    """Persistent chat and shared advisory game time for one prescribed match."""

    __tablename__ = "classic_chats"

    id: Mapped[UUID] = uuid_column()
    match_id: Mapped[UUID] = mapped_column(
        ForeignKey("classic_matches.id"), nullable=False, unique=True
    )
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))


class ClassicChatMessageRecord(Base, TimestampMixin):
    __tablename__ = "classic_chat_messages"

    id: Mapped[UUID] = uuid_column()
    chat_id: Mapped[UUID] = mapped_column(
        ForeignKey("classic_chats.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(), nullable=False, unique=True
    )
    sender_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    system: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (CheckConstraint("kind IN ('text', 'media', 'system')"),)


class ClassicChatMemberRecord(Base, TimestampMixin):
    __tablename__ = "classic_chat_members"

    chat_id: Mapped[UUID] = mapped_column(ForeignKey("classic_chats.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    last_read_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    message_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class GameObserverRecord(Base, TimestampMixin):
    __tablename__ = "game_observers"

    id: Mapped[UUID] = uuid_column()
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("game_id", "player_id"),
        Index("ix_active_game_observers_player", "player_id", postgresql_where=text("active")),
    )


class GameThemeRecord(Base):
    __tablename__ = "game_themes"

    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    theme_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("theme_revisions.id"), primary_key=True
    )
    source_packet_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_versions.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("game_id", "position"),
        CheckConstraint("position >= 1"),
    )


class GamePacketVersionRecord(Base):
    __tablename__ = "game_packet_versions"

    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    packet_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_versions.id"), primary_key=True
    )
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_packet_assignments.id"), nullable=False
    )
    selection_order: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("game_id", "selection_order"),
        CheckConstraint("selection_order >= 1"),
    )


class PlayerExposureClaimRecord(Base, TimestampMixin):
    __tablename__ = "player_exposure_claims"

    id: Mapped[UUID] = uuid_column()
    game_id: Mapped[UUID | None] = mapped_column(ForeignKey("games.id"))
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    packet_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_versions.id"), nullable=False
    )
    claim_namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_id: Mapped[UUID] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved")
    burnt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("game_id", "player_id", "claim_namespace", "claim_id"),
        CheckConstraint("state IN ('reserved', 'burnt', 'released')"),
        Index(
            "uq_live_exposure_claim_per_player",
            "player_id",
            "claim_namespace",
            "claim_id",
            unique=True,
            postgresql_where=text("state IN ('reserved', 'burnt')"),
        ),
        Index("ix_player_exposure_packet", "player_id", "packet_version_id", "state"),
    )


class PregameLobbyRecord(Base, TimestampMixin):
    __tablename__ = "pregame_lobbies"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    tournament_type_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_type_versions.id"), nullable=False
    )
    game_ruleset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("game_ruleset_versions.id"), nullable=False
    )
    tournament_policy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_policy_versions.id"), nullable=False
    )
    creator_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="assembling")
    invitation_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    max_players: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    game_id: Mapped[UUID | None] = mapped_column(ForeignKey("games.id"), unique=True)
    searching: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    search_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_into_lobby_id: Mapped[UUID | None] = mapped_column(ForeignKey("pregame_lobbies.id"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    validation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('assembling', 'started', 'cancelled', 'expired', 'merged')"),
        CheckConstraint(
            "(searching AND search_started_at IS NOT NULL AND status = 'assembling') OR "
            "(NOT searching AND search_started_at IS NULL)",
            name="ck_pregame_lobby_search_state",
        ),
        CheckConstraint(
            "max_players BETWEEN 1 AND 12",
            name="ck_pregame_lobby_size",
        ),
        Index(
            "ix_searching_pregame_lobbies",
            "tournament_id",
            "tournament_type_version_id",
            "game_ruleset_version_id",
            "tournament_policy_version_id",
            "search_started_at",
            postgresql_where=text("searching AND status = 'assembling'"),
        ),
    )


class PregameLobbyPacketRecord(Base, TimestampMixin):
    __tablename__ = "pregame_lobby_packets"

    lobby_id: Mapped[UUID] = mapped_column(ForeignKey("pregame_lobbies.id"), primary_key=True)
    packet_id: Mapped[UUID] = mapped_column(ForeignKey("logical_packets.id"), primary_key=True)
    packet_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("packet_versions.id"), nullable=False
    )
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_packet_assignments.id"), nullable=False
    )
    selection_order: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("lobby_id", "selection_order"),
        CheckConstraint("selection_order >= 1"),
    )


class PregameLobbyMemberRecord(Base, TimestampMixin):
    __tablename__ = "pregame_lobby_members"

    id: Mapped[UUID] = uuid_column()
    lobby_id: Mapped[UUID] = mapped_column(ForeignKey("pregame_lobbies.id"), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    join_order: Mapped[int] = mapped_column(Integer, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_violations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="player")
    fresh_content_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("lobby_id", "player_id"),
        UniqueConstraint("lobby_id", "join_order"),
        CheckConstraint("role IN ('player', 'observer')"),
        Index(
            "uq_active_pregame_lobby_per_player",
            "player_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class PregameLobbyEventRecord(Base):
    __tablename__ = "pregame_lobby_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lobby_id: Mapped[UUID] = mapped_column(ForeignKey("pregame_lobbies.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("lobby_id", "sequence"),)


class PlayerBlacklistRecord(Base, TimestampMixin):
    __tablename__ = "player_blacklists"

    blocker_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    blocked_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)

    __table_args__ = (
        CheckConstraint(
            "blocker_player_id <> blocked_player_id", name="ck_player_blacklist_not_self"
        ),
        Index("ix_player_blacklists_blocked_player", "blocked_player_id"),
    )


class RatingLedgerRecord(Base):
    __tablename__ = "rating_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    rating_before: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False)
    delta: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False)
    rating_after: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False)
    confidence_before: Mapped[Any] = mapped_column(Numeric(8, 6), nullable=False)
    confidence_after: Mapped[Any] = mapped_column(Numeric(8, 6), nullable=False)
    k_factor: Mapped[Any] = mapped_column(Numeric(10, 4), nullable=False)
    rating_model: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, default="pairwise_elo")
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("game_id", "player_id"),
        CheckConstraint("rating_after = rating_before + delta"),
        CheckConstraint("confidence_before BETWEEN 0 AND 1"),
        CheckConstraint("confidence_after BETWEEN 0 AND 1"),
        CheckConstraint("k_factor > 0"),
        CheckConstraint("reason IN ('pairwise_elo', 'admin_correction')"),
        Index(
            "ix_rating_ledger_history",
            "tournament_id",
            "player_id",
            "played_at",
        ),
    )


class RulesetRatingRecord(Base, TimestampMixin):
    __tablename__ = "ruleset_ratings"

    ruleset_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    rating: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False, default=1000)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_ruleset_ratings_player", "player_id", "ruleset_key"),)


class RulesetRatingLedgerRecord(Base):
    __tablename__ = "ruleset_rating_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ruleset_key: Mapped[str] = mapped_column(String(64), nullable=False)
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    rating_before: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False)
    delta: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False)
    rating_after: Mapped[Any] = mapped_column(Numeric(14, 4), nullable=False)
    confidence_before: Mapped[Any] = mapped_column(Numeric(8, 6), nullable=False)
    confidence_after: Mapped[Any] = mapped_column(Numeric(8, 6), nullable=False)
    k_factor: Mapped[Any] = mapped_column(Numeric(10, 4), nullable=False)
    tournament_weight: Mapped[Any] = mapped_column(Numeric(10, 4), nullable=False)
    rating_model: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, default="pairwise_elo")
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("game_id", "player_id", "reason"),
        CheckConstraint("rating_after = rating_before + delta"),
        CheckConstraint("confidence_before BETWEEN 0 AND 1"),
        CheckConstraint("confidence_after BETWEEN 0 AND 1"),
        CheckConstraint("k_factor > 0"),
        CheckConstraint("tournament_weight > 0"),
        CheckConstraint("reason IN ('pairwise_elo', 'admin_correction')"),
        Index(
            "ix_ruleset_rating_ledger_history",
            "ruleset_key",
            "player_id",
            "played_at",
        ),
    )


class GameResultRecord(Base, TimestampMixin):
    __tablename__ = "game_results"

    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    tournament_policy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tournament_policy_versions.id"), nullable=False
    )
    place: Mapped[Any] = mapped_column(Numeric(8, 4), nullable=False)
    score: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("place >= 1"),)


class QuestionRoundRecord(Base):
    __tablename__ = "question_rounds"

    id: Mapped[UUID] = uuid_column()
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    question_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("question_revisions.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("game_id", "sequence"),
        CheckConstraint("status IN ('pending', 'active', 'completed', 'invalidated')"),
    )


class PlayerQuestionStateRecord(Base):
    __tablename__ = "player_question_states"

    id: Mapped[UUID] = uuid_column()
    round_id: Mapped[UUID] = mapped_column(ForeignKey("question_rounds.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(ForeignKey("game_participants.id"), nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    buzzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_buzz_order: Mapped[int | None] = mapped_column(Integer)
    buzz_revealed_fraction: Mapped[Any | None] = mapped_column(Numeric(8, 6))
    buzz_time_remaining_fraction: Mapped[Any | None] = mapped_column(Numeric(8, 6))

    __table_args__ = (
        UniqueConstraint("round_id", "participant_id"),
        CheckConstraint("buzz_revealed_fraction IS NULL OR buzz_revealed_fraction BETWEEN 0 AND 1"),
        CheckConstraint(
            "buzz_time_remaining_fraction IS NULL OR buzz_time_remaining_fraction BETWEEN 0 AND 1"
        ),
    )


class AnswerAttemptRecord(Base, TimestampMixin):
    __tablename__ = "answer_attempts"

    id: Mapped[UUID] = uuid_column()
    round_id: Mapped[UUID] = mapped_column(ForeignKey("question_rounds.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(ForeignKey("game_participants.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_answer: Mapped[str | None] = mapped_column(Text)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    original_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    judged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("round_id", "participant_id"),
        UniqueConstraint("round_id", "attempt_number"),
    )


class AppealRecord(Base, TimestampMixin):
    __tablename__ = "appeals"

    id: Mapped[UUID] = uuid_column()
    tournament_id: Mapped[UUID] = mapped_column(ForeignKey("tournaments.id"), nullable=False)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("question_rounds.id"), nullable=False)
    appellant_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("game_participants.id"), nullable=False
    )
    target_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("answer_attempts.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="voting")
    voting_rule: Mapped[str] = mapped_column(String(16), nullable=False)
    electorate_size: Mapped[int] = mapped_column(Integer, nullable=False)
    vote_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    escalation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commentary_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ticket_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commentary: Mapped[str | None] = mapped_column(Text)
    game_was_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision_source: Mapped[str | None] = mapped_column(String(24))
    decided_by_manager_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("game_id", "target_attempt_id"),
        CheckConstraint("kind IN ('accept_incorrect', 'reject_correct')"),
        CheckConstraint(
            "status IN ('voting', 'awaiting_escalation', 'awaiting_commentary', "
            "'escalated', 'accepted', 'rejected')"
        ),
        CheckConstraint("voting_rule IN ('majority', 'unanimous')"),
        CheckConstraint("electorate_size >= 1"),
        CheckConstraint(
            "decision_source IS NULL OR decision_source IN "
            "('player_vote', 'player_declined', 'manager', 'timeout')"
        ),
        Index("ix_appeals_due", "status", "vote_deadline", "escalation_deadline"),
        Index("ix_appeals_manager_queue", "tournament_id", "status", "ticket_expires_at"),
    )


class AppealVoteRecord(Base):
    __tablename__ = "appeal_votes"

    appeal_id: Mapped[UUID] = mapped_column(ForeignKey("appeals.id"), primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("game_participants.id"), primary_key=True
    )
    approve: Mapped[bool | None] = mapped_column(Boolean)
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScoreLedgerRecord(Base):
    __tablename__ = "score_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(ForeignKey("game_participants.id"), nullable=False)
    round_id: Mapped[UUID | None] = mapped_column(ForeignKey("question_rounds.id"))
    attempt_id: Mapped[UUID | None] = mapped_column(ForeignKey("answer_attempts.id"))
    appeal_id: Mapped[UUID | None] = mapped_column(ForeignKey("appeals.id"), index=True)
    delta: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    correction_of_id: Mapped[int | None] = mapped_column(ForeignKey("score_ledger.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "reason IN ('correct_answer', 'incorrect_answer', 'answer_timeout', "
            "'appeal_correction', 'admin_correction')"
        ),
    )


class GameEventRecord(Base):
    __tablename__ = "game_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("game_id", "sequence"),)


class ReputationVoteRecord(Base, TimestampMixin):
    __tablename__ = "reputation_votes"

    id: Mapped[UUID] = uuid_column()
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    voter_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    target_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("game_id", "voter_player_id", "target_player_id"),
        CheckConstraint("voter_player_id <> target_player_id"),
        CheckConstraint("value IN (-1, 1)"),
        Index(
            "ix_reputation_votes_throttle",
            "voter_player_id",
            "target_player_id",
            "created_at",
        ),
    )


class PlayerReportRecord(Base, TimestampMixin):
    __tablename__ = "player_reports"

    id: Mapped[UUID] = uuid_column()
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    reporter_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    reported_player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("game_id", "reporter_player_id", "reported_player_id"),
        CheckConstraint("reporter_player_id <> reported_player_id"),
        Index("ix_player_reports_target", "reported_player_id", "kind", "created_at"),
    )


class ReputationLedgerRecord(Base):
    __tablename__ = "reputation_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    game_id: Mapped[UUID | None] = mapped_column(ForeignKey("games.id"))
    vote_id: Mapped[UUID | None] = mapped_column(ForeignKey("reputation_votes.id"), unique=True)
    report_id: Mapped[UUID | None] = mapped_column(ForeignKey("player_reports.id"), unique=True)
    reputation_before: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reputation_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("reputation_before BETWEEN 0 AND 100"),
        CheckConstraint("reputation_after BETWEEN 0 AND 100"),
        CheckConstraint("reputation_after = reputation_before + applied_delta"),
        CheckConstraint("reason IN ('player_vote', 'toxicity_report', 'admin_correction')"),
        Index("ix_reputation_ledger_player", "player_id", "created_at"),
    )


class SuspicionEvaluationRecord(Base, TimestampMixin):
    __tablename__ = "suspicion_evaluations"

    id: Mapped[UUID] = uuid_column()
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    ruleset_key: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("player_id", "ruleset_key", "period_start", "period_end"),
        CheckConstraint("period_end > period_start"),
        CheckConstraint("status IN ('queued', 'processing', 'postponed', 'evaluated')"),
        CheckConstraint("attempts >= 0"),
        Index("ix_suspicion_evaluations_pending", "status", "scheduled_at"),
    )


class SuspicionEvaluationScheduleRecord(Base):
    __tablename__ = "suspicion_evaluation_schedules"

    ruleset_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("period_end > period_start"),)


class SISuspicionMaterializedGameRecord(Base):
    __tablename__ = "si_suspicion_materialized_games"

    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    materialized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SIPlayerGameSuspicionMetricRecord(Base):
    __tablename__ = "si_player_game_suspicion_metrics"

    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_buzzes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buzz_revealed_fraction_total: Mapped[Any] = mapped_column(
        Numeric(24, 8), nullable=False, default=0
    )
    buzz_revealed_fraction_squared_total: Mapped[Any] = mapped_column(
        Numeric(24, 8), nullable=False, default=0
    )
    buzz_revealed_fraction_observations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    late_buzzes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_value_exposures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_value_correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint(
            "accepted_buzzes >= 0 AND buzz_revealed_fraction_total >= 0 "
            "AND buzz_revealed_fraction_squared_total >= 0 "
            "AND buzz_revealed_fraction_observations >= 0 AND late_buzzes >= 0 "
            "AND high_value_exposures >= 0 AND high_value_correct >= 0"
        ),
        CheckConstraint("buzz_revealed_fraction_observations <= accepted_buzzes"),
        CheckConstraint("late_buzzes <= accepted_buzzes"),
        CheckConstraint("high_value_correct <= high_value_exposures"),
        Index("ix_si_player_game_suspicion_history", "player_id", "completed_at"),
    )


class SIPlayerSuspicionAggregateRecord(Base):
    __tablename__ = "si_player_suspicion_aggregates"

    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    games_played: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_buzzes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buzz_revealed_fraction_total: Mapped[Any] = mapped_column(
        Numeric(24, 8), nullable=False, default=0
    )
    buzz_revealed_fraction_squared_total: Mapped[Any] = mapped_column(
        Numeric(24, 8), nullable=False, default=0
    )
    buzz_revealed_fraction_observations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    late_buzzes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_value_exposures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_value_correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "games_played >= 0 AND accepted_buzzes >= 0 "
            "AND buzz_revealed_fraction_total >= 0 "
            "AND buzz_revealed_fraction_squared_total >= 0 "
            "AND buzz_revealed_fraction_observations >= 0 AND late_buzzes >= 0 "
            "AND high_value_exposures >= 0 "
            "AND high_value_correct >= 0"
        ),
        CheckConstraint("buzz_revealed_fraction_observations <= accepted_buzzes"),
        CheckConstraint("late_buzzes <= accepted_buzzes"),
        CheckConstraint("high_value_correct <= high_value_exposures"),
    )


class SIPlayerQuestionSuspicionMetricRecord(Base):
    __tablename__ = "si_player_question_suspicion_metrics"

    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), primary_key=True)
    question_id: Mapped[UUID] = mapped_column(ForeignKey("logical_questions.id"), primary_key=True)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("question_rounds.id"), nullable=False)
    question_value: Mapped[int] = mapped_column(Integer, nullable=False)
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    buzzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    buzz_revealed_fraction: Mapped[Any | None] = mapped_column(Numeric(8, 6))
    buzz_time_remaining_fraction: Mapped[Any | None] = mapped_column(Numeric(8, 6))

    __table_args__ = (
        UniqueConstraint("round_id", "player_id"),
        CheckConstraint("question_value > 0"),
        CheckConstraint("buzz_revealed_fraction IS NULL OR buzz_revealed_fraction BETWEEN 0 AND 1"),
        CheckConstraint(
            "buzz_time_remaining_fraction IS NULL OR buzz_time_remaining_fraction BETWEEN 0 AND 1"
        ),
        Index("ix_si_player_question_suspicion_player", "player_id", "game_id"),
        Index("ix_si_player_question_suspicion_question", "question_id", "player_id"),
    )


class SIQuestionSuspicionAggregateRecord(Base):
    __tablename__ = "si_question_suspicion_aggregates"

    question_id: Mapped[UUID] = mapped_column(ForeignKey("logical_questions.id"), primary_key=True)
    exposures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("exposures >= 0"),
        CheckConstraint("correct BETWEEN 0 AND exposures"),
    )


class SISuspicionBaselineRecord(Base, TimestampMixin):
    __tablename__ = "si_suspicion_baselines"

    id: Mapped[UUID] = uuid_column()
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rating_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    average_buzz_revealed_fraction: Mapped[Any | None] = mapped_column(Numeric(8, 6))
    rare_correct_rate_cutoff: Mapped[Any | None] = mapped_column(Numeric(8, 6))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("period_start", "period_end", "rating_bucket"),
        CheckConstraint("period_end > period_start"),
        CheckConstraint(
            "average_buzz_revealed_fraction IS NULL "
            "OR average_buzz_revealed_fraction BETWEEN 0 AND 1"
        ),
        CheckConstraint(
            "rare_correct_rate_cutoff IS NULL OR rare_correct_rate_cutoff BETWEEN 0 AND 1"
        ),
    )


class SuspicionEvidenceRecord(Base, TimestampMixin):
    __tablename__ = "suspicion_evidence"

    id: Mapped[UUID] = uuid_column()
    evaluation_id: Mapped[UUID] = mapped_column(
        ForeignKey("suspicion_evaluations.id"), nullable=False
    )
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    baseline_id: Mapped[UUID | None] = mapped_column(ForeignKey("si_suspicion_baselines.id"))
    ruleset_key: Mapped[str] = mapped_column(String(64), nullable=False)
    signal: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("evaluation_id", "signal"),
        Index("ix_suspicion_evidence_player", "player_id", "created_at"),
    )


class SuspicionEvidenceActionRecord(Base):
    __tablename__ = "suspicion_evidence_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evidence_id: Mapped[UUID] = mapped_column(ForeignKey("suspicion_evidence.id"), nullable=False)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("question_rounds.id"), nullable=False)
    question_id: Mapped[UUID] = mapped_column(ForeignKey("logical_questions.id"), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("evidence_id", "game_id", "round_id"),
        Index("ix_suspicion_evidence_actions_evidence", "evidence_id", "id"),
    )


class SuspicionLedgerRecord(Base):
    __tablename__ = "suspicion_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    report_id: Mapped[UUID | None] = mapped_column(ForeignKey("player_reports.id"), unique=True)
    evaluation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("suspicion_evaluations.id"), nullable=True
    )
    administrator_id: Mapped[UUID | None] = mapped_column(ForeignKey("players.id"))
    ruleset_key: Mapped[str | None] = mapped_column(String(64))
    suspicion_before: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    suspicion_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("evaluation_id", "reason"),
        CheckConstraint("suspicion_before >= 0"),
        CheckConstraint("suspicion_after >= 0"),
        CheckConstraint("suspicion_after = suspicion_before + delta"),
        CheckConstraint(
            "(reason = 'admin_clearance' AND administrator_id IS NOT NULL "
            "AND suspicion_after = 0 AND delta <= 0) OR "
            "(reason <> 'admin_clearance' AND administrator_id IS NULL AND delta > 0)"
        ),
        Index("ix_suspicion_ledger_player", "player_id", "created_at"),
    )
