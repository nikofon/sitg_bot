"""Create the pre-release production schema and seed tournament types and SI v1.

This frozen baseline requires an empty database. Earlier development revisions
are intentionally not supported.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "players",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("real_name", sa.String(length=200), nullable=True),
        sa.Column("public_nickname", sa.String(length=200), nullable=True),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("telegram_public", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "preferred_locale", sa.String(length=2), server_default=sa.text("'ru'"), nullable=False
        ),
        sa.Column(
            "registration_step",
            sa.String(length=24),
            server_default=sa.text("'real_name'"),
            nullable=False,
        ),
        sa.Column("registration_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profile_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "profile_updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("telegram_username_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reputation", sa.Integer(), nullable=False),
        sa.Column("suspicion", sa.Integer(), nullable=False),
        sa.Column("game_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'registration'"), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'registration' AND registration_completed_at IS NULL) OR (status "
            "<> 'registration')",
            name="ck_players_8cb607601db3",
        ),
        sa.CheckConstraint("preferred_locale IN ('ru', 'en')", name="ck_players_645c34ec52e0"),
        sa.CheckConstraint(
            "registration_step IN ('real_name', 'nickname', 'telegram_public', 'complete')",
            name="ck_players_78ee9c89ebbc",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR (registration_step = 'complete' AND "
            "registration_completed_at IS NOT NULL AND real_name IS NOT NULL AND "
            "public_nickname IS NOT NULL)",
            name="ck_players_b4592634c773",
        ),
        sa.CheckConstraint(
            "status IN ('registration', 'active', 'anonymized')", name="ck_players_6dc2f451b5a9"
        ),
        sa.CheckConstraint("profile_version >= 0", name="ck_players_bebd752f2b8a"),
        sa.CheckConstraint("reputation BETWEEN 0 AND 100", name="ck_players_9e2aadf21ff6"),
        sa.CheckConstraint("suspicion >= 0", name="ck_players_56414d2bc395"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_table(
        "tournament_type_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_tournament_type_versions_b093cc40900c"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "version"),
    )
    op.create_table(
        "game_ruleset_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("parameter_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_game_ruleset_versions_b093cc40900c"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "version"),
    )
    op.create_table(
        "authors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("second_name", sa.String(length=100), nullable=True),
        sa.Column("surname", sa.String(length=100), nullable=True),
        sa.Column("telegram_link", sa.String(length=200), nullable=True),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_authors_telegram_username",
        "authors",
        [sa.literal_column("lower(telegram_username)")],
        unique=False,
    )
    op.create_index(
        "ix_authors_display_name",
        "authors",
        [sa.literal_column("lower(display_name)"), "id"],
        unique=False,
    )
    op.create_table(
        "application_idempotency",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("actor_scope", sa.String(length=100), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_application_idempotency_8f8bb73fea88",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_scope", "channel", "action", "key_digest"),
    )
    op.create_index(
        "ix_application_idempotency_pending",
        "application_idempotency",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("deduplication_key", sa.String(length=300), nullable=False),
        sa.Column("partition_key", sa.String(length=200), nullable=False),
        sa.Column("aggregate_type", sa.String(length=60), nullable=True),
        sa.Column("aggregate_id", sa.UUID(), nullable=True),
        sa.Column("aggregate_sequence", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("12"), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'delivered' AND delivered_at IS NOT NULL AND failed_at IS NULL) OR"
            " (status = 'failed' AND failed_at IS NOT NULL AND delivered_at IS NULL) OR "
            "(status IN ('pending', 'processing') AND delivered_at IS NULL AND failed_at "
            "IS NULL)",
            name="ck_outbox_events_f9a3c6fb69d0",
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_token IS NOT NULL AND lease_expires_at IS "
            "NOT NULL) OR (status <> 'processing' AND lease_token IS NULL AND "
            "lease_expires_at IS NULL)",
            name="ck_outbox_events_cbbb265fa5bc",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'failed')",
            name="ck_outbox_events_705a36272d26",
        ),
        sa.CheckConstraint(
            "(aggregate_type IS NULL AND aggregate_id IS NULL) OR (aggregate_type IS NOT "
            "NULL AND aggregate_id IS NOT NULL)",
            name="ck_outbox_events_1d8ff456afef",
        ),
        sa.CheckConstraint(
            "aggregate_sequence IS NULL OR aggregate_sequence >= 0",
            name="ck_outbox_events_9ebe002cbb33",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="ck_outbox_events_203dcb11235a",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_index(
        "ix_outbox_events_dispatch",
        "outbox_events",
        ["status", "available_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_partition_order",
        "outbox_events",
        ["partition_key", "aggregate_sequence", "created_at"],
        unique=False,
    )
    op.create_table(
        "durable_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("deduplication_key", sa.String(length=300), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("12"), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT "
            "NULL) OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at "
            "IS NULL)",
            name="ck_durable_jobs_02f372be3b4d",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND completed_at IS NOT NULL AND failed_at IS NULL) OR"
            " (status = 'failed' AND failed_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('queued', 'running', 'cancelled') AND completed_at IS NULL AND "
            "failed_at IS NULL)",
            name="ck_durable_jobs_981d1e9d753d",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_durable_jobs_8b087b80fd6b",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="ck_durable_jobs_203dcb11235a",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_index(
        "ix_durable_jobs_claim",
        "durable_jobs",
        ["status", "scheduled_at", "priority", "created_at"],
        unique=False,
    )
    op.create_table(
        "telegram_update_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'processing'"), nullable=False
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'test')", name="ck_telegram_update_receipts_e95aa4749567"
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_telegram_update_receipts_407562009a0e",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "environment", "update_id"),
    )
    op.create_index(
        "ix_telegram_update_receipts_lease",
        "telegram_update_receipts",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "suspicion_evaluation_schedules",
        sa.Column("ruleset_key", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "period_end > period_start", name="ck_suspicion_evaluation_schedules_c05604b2fb2e"
        ),
        sa.PrimaryKeyConstraint("ruleset_key", "period_start", "period_end"),
    )
    op.create_table(
        "si_suspicion_baselines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rating_bucket", sa.Integer(), nullable=False),
        sa.Column(
            "average_buzz_revealed_fraction", sa.Numeric(precision=8, scale=6), nullable=True
        ),
        sa.Column("rare_correct_rate_cutoff", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "average_buzz_revealed_fraction IS NULL OR average_buzz_revealed_fraction "
            "BETWEEN 0 AND 1",
            name="ck_si_suspicion_baselines_a49a796eab5e",
        ),
        sa.CheckConstraint(
            "period_end > period_start", name="ck_si_suspicion_baselines_c05604b2fb2e"
        ),
        sa.CheckConstraint(
            "rare_correct_rate_cutoff IS NULL OR rare_correct_rate_cutoff BETWEEN 0 AND 1",
            name="ck_si_suspicion_baselines_89a618b21cee",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period_start", "period_end", "rating_bucket"),
    )
    op.create_table(
        "platform_administrators",
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("granted_by_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("player_id"),
    )
    op.create_table(
        "tournaments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("visibility", sa.String(length=24), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("payment_type", sa.String(length=24), nullable=False),
        sa.Column("registration_open", sa.Boolean(), nullable=False),
        sa.Column("registration_open_override", sa.Boolean(), nullable=True),
        sa.Column("registration_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ignore_late_registrations",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("participants_finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("type_version_id", sa.UUID(), nullable=False),
        sa.Column("game_ruleset_version_id", sa.UUID(), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "payment_type IN ('free', 'one-time', 'per-stage')", name="ck_tournaments_f1a92ab30f90"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'archived')",
            name="ck_tournaments_32fa58579562",
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')", name="ck_tournaments_6b7dd92f9e8b"
        ),
        sa.CheckConstraint(
            "actual_ends_at IS NULL OR starts_at IS NULL OR actual_ends_at >= starts_at",
            name="ck_tournaments_9661fd0dd466",
        ),
        sa.CheckConstraint(
            "planned_ends_at IS NULL OR starts_at IS NULL OR planned_ends_at > starts_at",
            name="ck_tournaments_5cf670a69cfe",
        ),
        sa.CheckConstraint(
            "registration_ends_at IS NULL OR registration_starts_at IS NULL OR "
            "registration_ends_at > registration_starts_at",
            name="ck_tournaments_d614e52eb9b8",
        ),
        sa.CheckConstraint("settings_version >= 1", name="ck_tournaments_2060bf26b5c2"),
        sa.CheckConstraint(
            "starts_at IS NULL OR registration_ends_at IS NULL OR starts_at >= regi"
            "stration_ends_at",
            name="ck_tournaments_51e6cd06cae8",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_ruleset_version_id"],
            ["game_ruleset_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["type_version_id"],
            ["tournament_type_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(
        "ix_tournaments_listing", "tournaments", ["visibility", "status", "starts_at"], unique=False
    )
    op.create_table(
        "tournament_creation_token_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("requester_id", sa.UUID(), nullable=False),
        sa.Column("tournament_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("justification", sa.String(length=2000), nullable=True),
        sa.Column("decided_by_id", sa.UUID(), nullable=True),
        sa.Column("decision_note", sa.String(length=2000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by_id IS NULL AND decided_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_by_id IS NOT NULL AND "
            "decided_at IS NOT NULL)",
            name="ck_tournament_creation_token_requests_cd52eb972bbc",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_tournament_creation_token_requests_ed21ce3dfa69",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tournament_creation_token_requests_requester",
        "tournament_creation_token_requests",
        ["requester_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tournament_creation_token_requests_queue",
        "tournament_creation_token_requests",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_tournament_creation_token_requests_pending",
        "tournament_creation_token_requests",
        ["requester_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "player_author_link_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("request_note", sa.String(length=1000), nullable=True),
        sa.Column("decided_by_id", sa.UUID(), nullable=True),
        sa.Column("decision_note", sa.String(length=1000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by_id IS NULL AND decided_at IS NULL AND "
            "cancelled_at IS NULL) OR (status = 'cancelled' AND decided_by_id IS NULL AND"
            " decided_at IS NULL AND cancelled_at IS NOT NULL) OR (status IN ('approved',"
            " 'rejected') AND decided_by_id IS NOT NULL AND decided_at IS NOT NULL AND "
            "cancelled_at IS NULL)",
            name="ck_player_author_link_requests_ded43f9664ee",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name="ck_player_author_link_requests_1fb01495771a",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_player_author_link_requests_pending",
        "player_author_link_requests",
        ["player_id", "author_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_player_author_link_requests_player",
        "player_author_link_requests",
        ["player_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_player_author_link_requests_queue",
        "player_author_link_requests",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "player_notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recipient_player_id", sa.UUID(), nullable=False),
        sa.Column("audience", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("deduplication_key", sa.String(length=300), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "audience IN ('player', 'manager', 'admin')",
            name="ck_player_notifications_13bc7055206c",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_index(
        "ix_player_notifications_recipient",
        "player_notifications",
        ["recipient_player_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "ix_player_notifications_admin",
        "player_notifications",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("audience = 'admin' AND read_at IS NULL"),
    )
    op.create_table(
        "player_notification_alerts",
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("player_id"),
    )
    op.create_table(
        "application_request_audit",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column("actor_player_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("client_name", sa.String(length=80), nullable=False),
        sa.Column("client_version", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "outcome", sa.String(length=20), server_default=sa.text("'started'"), nullable=False
        ),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("resource_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('console', 'telegram_bot', 'mini_app', 'internal')",
            name="ck_application_request_audit_8bf91718e35c",
        ),
        sa.CheckConstraint(
            "outcome IN ('started', 'succeeded', 'rejected', 'failed')",
            name="ck_application_request_audit_55d297698d54",
        ),
        sa.ForeignKeyConstraint(
            ["actor_player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_request_audit_action",
        "application_request_audit",
        ["action", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_application_request_audit_correlation",
        "application_request_audit",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_application_request_audit_actor",
        "application_request_audit",
        ["actor_player_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "mini_app_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_digest", sa.String(length=64), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("origin", sa.String(length=500), nullable=False),
        sa.Column("telegram_auth_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'test')", name="ck_mini_app_sessions_e95aa4749567"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_mini_app_sessions_expiry",
        "mini_app_sessions",
        ["expires_at", "revoked_at"],
        unique=False,
    )
    op.create_index(
        "ix_mini_app_sessions_player",
        "mini_app_sessions",
        ["player_id", "expires_at"],
        unique=False,
    )
    op.create_table(
        "application_launch_references",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("route", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("created_by_player_id", sa.UUID(), nullable=False),
        sa.Column("intended_player_id", sa.UUID(), nullable=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("one_time", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("redeemed_by_player_id", sa.UUID(), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'test')",
            name="ck_application_launch_references_e95aa4749567",
        ),
        sa.CheckConstraint(
            "route IN ('lobby', 'report', 'manager_settings', 'manager_management',"
            " 'packet_draft')",
            name="ck_application_launch_references_route",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["intended_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by_player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_application_launch_references_expiry",
        "application_launch_references",
        ["expires_at", "redeemed_at"],
        unique=False,
    )
    op.create_table(
        "logical_packets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("uploader_id", sa.UUID(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("statistical_author_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["statistical_author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["uploader_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "player_blacklists",
        sa.Column("blocker_player_id", sa.UUID(), nullable=False),
        sa.Column("blocked_player_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "blocker_player_id <> blocked_player_id", name="ck_player_blacklist_not_self"
        ),
        sa.ForeignKeyConstraint(
            ["blocked_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["blocker_player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("blocker_player_id", "blocked_player_id"),
    )
    op.create_index(
        "ix_player_blacklists_blocked_player",
        "player_blacklists",
        ["blocked_player_id"],
        unique=False,
    )
    op.create_table(
        "ruleset_ratings",
        sa.Column("ruleset_key", sa.String(length=64), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("rating", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("ruleset_key", "player_id"),
    )
    op.create_index(
        "ix_ruleset_ratings_player", "ruleset_ratings", ["player_id", "ruleset_key"], unique=False
    )
    op.create_table(
        "suspicion_evaluations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("ruleset_key", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'postponed', 'evaluated')",
            name="ck_suspicion_evaluations_336ebe5bc1af",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_suspicion_evaluations_34c54dbe05a2"),
        sa.CheckConstraint(
            "period_end > period_start", name="ck_suspicion_evaluations_c05604b2fb2e"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("player_id", "ruleset_key", "period_start", "period_end"),
    )
    op.create_index(
        "ix_suspicion_evaluations_pending",
        "suspicion_evaluations",
        ["status", "scheduled_at"],
        unique=False,
    )
    op.create_table(
        "si_player_suspicion_aggregates",
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("games_played", sa.Integer(), nullable=False),
        sa.Column("accepted_buzzes", sa.Integer(), nullable=False),
        sa.Column(
            "buzz_revealed_fraction_total", sa.Numeric(precision=24, scale=8), nullable=False
        ),
        sa.Column(
            "buzz_revealed_fraction_squared_total",
            sa.Numeric(precision=24, scale=8),
            nullable=False,
        ),
        sa.Column("buzz_revealed_fraction_observations", sa.Integer(), nullable=False),
        sa.Column("late_buzzes", sa.Integer(), nullable=False),
        sa.Column("high_value_exposures", sa.Integer(), nullable=False),
        sa.Column("high_value_correct", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "buzz_revealed_fraction_observations <= accepted_buzzes",
            name="ck_si_player_suspicion_aggregates_afa6d413a55d",
        ),
        sa.CheckConstraint(
            "games_played >= 0 AND accepted_buzzes >= 0 AND buzz_revealed_fraction_total "
            ">= 0 AND buzz_revealed_fraction_squared_total >= 0 AND "
            "buzz_revealed_fraction_observations >= 0 AND late_buzzes >= 0 AND "
            "high_value_exposures >= 0 AND high_value_correct >= 0",
            name="ck_si_player_suspicion_aggregates_5958ea1c5a7a",
        ),
        sa.CheckConstraint(
            "high_value_correct <= high_value_exposures",
            name="ck_si_player_suspicion_aggregates_982298f21703",
        ),
        sa.CheckConstraint(
            "late_buzzes <= accepted_buzzes", name="ck_si_player_suspicion_aggregates_251f48a0a084"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("player_id"),
    )
    op.create_table(
        "player_telegram_navigation",
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column(
            "interaction_mode",
            sa.String(length=16),
            server_default=sa.text("'player'"),
            nullable=False,
        ),
        sa.Column(
            "player_context",
            sa.String(length=16),
            server_default=sa.text("'tournament'"),
            nullable=False,
        ),
        sa.Column("selected_player_tournament_id", sa.UUID(), nullable=True),
        sa.Column("selected_manager_tournament_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "interaction_mode IN ('player', 'manager', 'admin')",
            name="ck_player_telegram_navigation_87f899138f57",
        ),
        sa.CheckConstraint("version >= 1", name="ck_player_telegram_navigation_b093cc40900c"),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["selected_manager_tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["selected_player_tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("player_id"),
    )
    op.create_table(
        "tournament_pricing_plans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name="ck_tournament_pricing_plans_b4c8ae8f75c2"
        ),
        sa.CheckConstraint("position >= 1", name="ck_tournament_pricing_plans_adf200d36a7d"),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tournament_id", "name"),
        sa.UniqueConstraint("tournament_id", "position"),
    )
    op.create_table(
        "tournament_policy_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("default_parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "player_mutable_parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("policies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_tournament_policy_versions_b093cc40900c"),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tournament_id", "version"),
    )
    op.create_table(
        "tournament_managers",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("granted_by_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("tournament_id", "player_id"),
    )
    op.create_table(
        "tournament_memberships",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rating", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("rating_sequence", sa.Integer(), nullable=False),
        sa.Column("enrolled_by_id", sa.UUID(), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_id", sa.UUID(), nullable=True),
        sa.Column("participation_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("participation_confirmed_by_id", sa.UUID(), nullable=True),
        sa.Column("registration_rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "registration_rejection_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["enrolled_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participation_confirmed_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("tournament_id", "player_id"),
    )
    op.create_table(
        "tournament_registration_requirements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("target_tournament_id", sa.UUID(), nullable=True),
        sa.Column("target_packet_id", sa.UUID(), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind IN ('has-played-tournament', 'has-not-played-tournament') AND "
            "target_tournament_id IS NOT NULL AND target_packet_id IS NULL) OR (kind = "
            "'has-not-seen-packet' AND target_tournament_id IS NULL AND target_packet_id "
            "IS NOT NULL)",
            name="ck_tournament_registration_requirements_d8a32c658b1f",
        ),
        sa.CheckConstraint(
            "kind IN ('has-played-tournament', 'has-not-played-tournament', 'has-not-seen-packet')",
            name="ck_tournament_registration_requirements_eab956462983",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_packet_id"],
            ["logical_packets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registration_requirements_active",
        "tournament_registration_requirements",
        ["tournament_id", "kind"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "tournament_registration_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column(
            "evaluated_requirement_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("failure_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registration_attempts_player",
        "tournament_registration_attempts",
        ["tournament_id", "player_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "tournament_creation_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=12), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("issued_by_id", sa.UUID(), nullable=False),
        sa.Column("intended_creator_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_id", sa.UUID(), nullable=True),
        sa.Column("tournament_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR (used_by_id IS NOT NULL AND tournament_id IS NOT NULL)",
            name="ck_tournament_creation_tokens_11ceec41dfd3",
        ),
        sa.ForeignKeyConstraint(
            ["intended_creator_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["tournament_creation_token_requests.id"],
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["used_by_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_tournament_creation_tokens_creator",
        "tournament_creation_tokens",
        ["intended_creator_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "player_author_links",
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("approved_request_id", sa.UUID(), nullable=False),
        sa.Column("approved_by_id", sa.UUID(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["approved_request_id"],
            ["player_author_link_requests.id"],
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("player_id", "author_id"),
        sa.UniqueConstraint("approved_request_id"),
    )
    op.create_table(
        "tournament_authors",
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("added_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["added_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("tournament_id", "author_id"),
    )
    op.create_table(
        "packet_drafts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_filename", sa.String(length=500), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validation_errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validation_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uploader_id", sa.UUID(), nullable=True),
        sa.Column("creation_tournament_id", sa.UUID(), nullable=False),
        sa.Column("confirmed_by_id", sa.UUID(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_version_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_locale", sa.String(length=10), nullable=True),
        sa.Column(
            "author_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("lead_author_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'parsed', 'awaiting_confirmation', "
            "'validation_failed', 'rejected', 'published')",
            name="ck_packet_drafts_49dd57f59a70",
        ),
        sa.CheckConstraint("version >= 1", name="ck_packet_drafts_b093cc40900c"),
        sa.ForeignKeyConstraint(
            ["confirmed_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["creation_tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["lead_author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["uploader_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "themes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("packet_id", sa.UUID(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("statistical_author_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"],
            ["logical_packets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["statistical_author_id"],
            ["authors.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "logical_questions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("packet_id", sa.UUID(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("statistical_author_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"],
            ["logical_packets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["statistical_author_id"],
            ["authors.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "suspicion_evidence",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("baseline_id", sa.UUID(), nullable=True),
        sa.Column("ruleset_key", sa.String(length=64), nullable=False),
        sa.Column("signal", sa.String(length=64), nullable=False),
        sa.Column("algorithm_version", sa.String(length=32), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["si_suspicion_baselines.id"],
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["suspicion_evaluations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_id", "signal"),
    )
    op.create_index(
        "ix_suspicion_evidence_player",
        "suspicion_evidence",
        ["player_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "tournament_pricing_plan_prices",
        sa.Column("pricing_plan_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_tournament_pricing_plan_prices_b285548ea304"
        ),
        sa.CheckConstraint("amount > 0", name="ck_tournament_pricing_plan_prices_408e2e8be2f0"),
        sa.ForeignKeyConstraint(
            ["pricing_plan_id"], ["tournament_pricing_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("pricing_plan_id", "currency"),
    )
    op.create_table(
        "tournament_creation_token_deliveries",
        sa.Column("token_id", sa.UUID(), nullable=False),
        sa.Column("recipient_player_id", sa.UUID(), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND encrypted_token IS NOT NULL AND delivered_at IS NULL"
            " AND failed_at IS NULL) OR (status = 'delivered' AND encrypted_token IS NULL"
            " AND delivered_at IS NOT NULL AND failed_at IS NULL) OR (status = 'failed' "
            "AND delivered_at IS NULL AND failed_at IS NOT NULL)",
            name="ck_tournament_creation_token_deliveries_b6f40c6ad8eb",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_tournament_creation_token_deliveries_12871bcc9d00",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["tournament_creation_tokens.id"],
        ),
        sa.PrimaryKeyConstraint("token_id"),
    )
    op.create_index(
        "ix_tournament_creation_token_deliveries_recipient",
        "tournament_creation_token_deliveries",
        ["recipient_player_id", "status"],
        unique=False,
    )
    op.create_table(
        "packet_draft_tournaments",
        sa.Column("draft_id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["packet_drafts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("draft_id", "tournament_id"),
    )
    op.create_table(
        "packet_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("packet_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("lead_author_id", sa.UUID(), nullable=True),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("library_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_version_id", sa.UUID(), nullable=True),
        sa.Column("source_draft_id", sa.UUID(), nullable=False),
        sa.Column("change_type", sa.String(length=20), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("published_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change_type IN ('initial', 'correction', 'substitution')",
            name="ck_packet_versions_e10513aa1b46",
        ),
        sa.CheckConstraint(
            "state IN ('published', 'archived')", name="ck_packet_versions_89c3e6517142"
        ),
        sa.CheckConstraint(
            "year IS NULL OR year BETWEEN 1000 AND 9999", name="ck_packet_versions_37fb53b68349"
        ),
        sa.ForeignKeyConstraint(
            ["lead_author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"],
            ["logical_packets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["packet_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["published_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_draft_id"],
            ["packet_drafts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("packet_id", "version_number"),
        sa.UniqueConstraint("source_draft_id"),
    )
    op.create_index(
        op.f("ix_packet_versions_packet_id"), "packet_versions", ["packet_id"], unique=False
    )
    op.create_table(
        "question_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("accepted_answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("commentary", sa.Text(), nullable=False),
        sa.Column("form", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["logical_questions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_id", "revision_number"),
    )
    op.create_index(
        op.f("ix_question_revisions_question_id"),
        "question_revisions",
        ["question_id"],
        unique=False,
    )
    op.create_table(
        "games",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("tournament_type_version_id", sa.UUID(), nullable=False),
        sa.Column("game_ruleset_version_id", sa.UUID(), nullable=False),
        sa.Column("tournament_policy_version_id", sa.UUID(), nullable=False),
        sa.Column("rating_confidence_model", sa.String(length=40), nullable=False),
        sa.Column("host_player_id", sa.UUID(), nullable=False),
        sa.Column("source_lobby_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("current_round_id", sa.UUID(), nullable=True),
        sa.Column("accepted_buzzer_id", sa.UUID(), nullable=True),
        sa.Column("buzz_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progression_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("join_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_abandonment_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progression_stage", sa.String(length=32), nullable=True),
        sa.Column("assignment_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("question_token_index", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "phase IN ('lobby', 'countdown', 'intermission', 'question', 'finished')",
            name="ck_games_19584c0f4167",
        ),
        sa.CheckConstraint(
            "progression_stage IS NULL OR progression_stage IN ('ready_countdown', "
            "'theme_start', 'question_start', 'next_question', 'theme_complete', "
            "'theme_scoreboard', 'question_reveal_start', 'question_reveal', "
            "'buzz_timer_start', 'finish')",
            name="ck_games_a1f33c7c9c3b",
        ),
        sa.CheckConstraint(
            "status IN ('lobby', 'active', 'completed', 'finalized', 'failed_to_start', "
            "'cancelled', 'abandoned', 'invalidated')",
            name="ck_games_2425d5255fe2",
        ),
        sa.CheckConstraint("question_token_index >= 0", name="ck_games_56f883302339"),
        sa.ForeignKeyConstraint(
            ["game_ruleset_version_id"],
            ["game_ruleset_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["host_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_policy_version_id"],
            ["tournament_policy_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_type_version_id"],
            ["tournament_type_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_games_suspicion_evaluation",
        "games",
        ["game_ruleset_version_id", "status", "completed_at"],
        unique=False,
    )
    op.create_index(
        "ix_games_pause_abandonment_deadline",
        "games",
        ["status", "paused", "pause_abandonment_deadline"],
        unique=False,
    )
    op.create_index("ix_games_join_deadline", "games", ["status", "join_deadline"], unique=False)
    op.create_table(
        "si_question_suspicion_aggregates",
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("exposures", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "correct BETWEEN 0 AND exposures",
            name="ck_si_question_suspicion_aggregates_ac0848718cf3",
        ),
        sa.CheckConstraint(
            "exposures >= 0", name="ck_si_question_suspicion_aggregates_80e1e4bdbcc1"
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["logical_questions.id"],
        ),
        sa.PrimaryKeyConstraint("question_id"),
    )
    op.create_table(
        "telegram_game_views",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("flow_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("game_id", "player_id"),
    )
    op.create_table(
        "tournament_packet_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("packet_id", sa.UUID(), nullable=False),
        sa.Column("adopted_version_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("discoverable_by_members", sa.Boolean(), nullable=False),
        sa.Column("playable_by_members", sa.Boolean(), nullable=False),
        sa.Column("content_visible_by_members", sa.Boolean(), nullable=False),
        sa.Column("editable_by_members", sa.Boolean(), nullable=False),
        sa.Column("access_level_by_members", sa.String(length=24), nullable=False),
        sa.Column("assigned_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "access_level_by_members IN ('no-access', 'play-only', 'read-after-play', "
            "'read-or-play')",
            name="ck_tournament_packet_assignments_e7dbbad4676e",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'retired')", name="ck_tournament_packet_assignments_f691b4a303ec"
        ),
        sa.ForeignKeyConstraint(
            ["adopted_version_id"],
            ["packet_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"],
            ["logical_packets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tournament_id", "packet_id"),
    )
    op.create_table(
        "theme_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("theme_id", sa.UUID(), nullable=False),
        sa.Column("packet_version_id", sa.UUID(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["authors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_version_id"],
            ["packet_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["theme_id"],
            ["themes.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("packet_version_id", "position"),
        sa.UniqueConstraint("theme_id", "revision_number"),
    )
    op.create_table(
        "game_participants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("seat", sa.Integer(), nullable=False),
        sa.Column("rating_sequence", sa.Integer(), nullable=False),
        sa.Column("global_game_sequence", sa.Integer(), nullable=False),
        sa.Column("joined", sa.Boolean(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("final_place", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("global_game_sequence >= 1", name="ck_game_participants_47561da14be8"),
        sa.CheckConstraint("rating_sequence >= 1", name="ck_game_participants_67fd2d09f5ac"),
        sa.CheckConstraint("seat BETWEEN 1 AND 12", name="ck_game_participants_ca47f0d3293a"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "player_id"),
        sa.UniqueConstraint("game_id", "seat"),
        sa.UniqueConstraint("player_id", "global_game_sequence"),
        sa.UniqueConstraint("tournament_id", "player_id", "rating_sequence"),
    )
    op.create_index(
        "uq_active_game_per_player",
        "game_participants",
        ["player_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "game_observers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "player_id"),
    )
    op.create_index(
        "ix_active_game_observers_player",
        "game_observers",
        ["player_id"],
        unique=False,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "player_exposure_claims",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=True),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("packet_version_id", sa.UUID(), nullable=False),
        sa.Column("claim_namespace", sa.String(length=64), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("burnt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'burnt', 'released')",
            name="ck_player_exposure_claims_705b0b562886",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_version_id"],
            ["packet_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "player_id", "claim_namespace", "claim_id"),
    )
    op.create_index(
        "uq_live_exposure_claim_per_player",
        "player_exposure_claims",
        ["player_id", "claim_namespace", "claim_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('reserved', 'burnt')"),
    )
    op.create_index(
        "ix_player_exposure_packet",
        "player_exposure_claims",
        ["player_id", "packet_version_id", "state"],
        unique=False,
    )
    op.create_table(
        "pregame_lobbies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("tournament_type_version_id", sa.UUID(), nullable=False),
        sa.Column("game_ruleset_version_id", sa.UUID(), nullable=False),
        sa.Column("tournament_policy_version_id", sa.UUID(), nullable=False),
        sa.Column("creator_player_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("invitation_code", sa.String(length=64), nullable=False),
        sa.Column("max_players", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=True),
        sa.Column("searching", sa.Boolean(), nullable=False),
        sa.Column("search_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_into_lobby_id", sa.UUID(), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(searching AND search_started_at IS NOT NULL AND status = 'assembling') OR "
            "(NOT searching AND search_started_at IS NULL)",
            name="ck_pregame_lobby_search_state",
        ),
        sa.CheckConstraint(
            "status IN ('assembling', 'started', 'cancelled', 'expired', 'merged')",
            name="ck_pregame_lobbies_c8f1966d3276",
        ),
        sa.CheckConstraint("max_players BETWEEN 1 AND 12", name="ck_pregame_lobby_size"),
        sa.ForeignKeyConstraint(
            ["creator_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_ruleset_version_id"],
            ["game_ruleset_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_lobby_id"],
            ["pregame_lobbies.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_policy_version_id"],
            ["tournament_policy_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_type_version_id"],
            ["tournament_type_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id"),
        sa.UniqueConstraint("invitation_code"),
    )
    op.create_index(
        "ix_searching_pregame_lobbies",
        "pregame_lobbies",
        [
            "tournament_id",
            "tournament_type_version_id",
            "game_ruleset_version_id",
            "tournament_policy_version_id",
            "search_started_at",
        ],
        unique=False,
        postgresql_where=sa.text("searching AND status = 'assembling'"),
    )
    op.create_table(
        "rating_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("rating_before", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("delta", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("rating_after", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("confidence_before", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("confidence_after", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("k_factor", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("rating_model", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('pairwise_elo', 'admin_correction')", name="ck_rating_ledger_d272be261729"
        ),
        sa.CheckConstraint(
            "confidence_after BETWEEN 0 AND 1", name="ck_rating_ledger_d7454a739e0e"
        ),
        sa.CheckConstraint(
            "confidence_before BETWEEN 0 AND 1", name="ck_rating_ledger_d547b8a3c75a"
        ),
        sa.CheckConstraint("k_factor > 0", name="ck_rating_ledger_6672d9d9be6f"),
        sa.CheckConstraint(
            "rating_after = rating_before + delta", name="ck_rating_ledger_f1f08c7e6def"
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "player_id"),
    )
    op.create_index(
        "ix_rating_ledger_history",
        "rating_ledger",
        ["tournament_id", "player_id", "played_at"],
        unique=False,
    )
    op.create_table(
        "ruleset_rating_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ruleset_key", sa.String(length=64), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("rating_before", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("delta", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("rating_after", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("confidence_before", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("confidence_after", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("k_factor", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("tournament_weight", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("rating_model", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('pairwise_elo', 'admin_correction')",
            name="ck_ruleset_rating_ledger_d272be261729",
        ),
        sa.CheckConstraint(
            "confidence_after BETWEEN 0 AND 1", name="ck_ruleset_rating_ledger_d7454a739e0e"
        ),
        sa.CheckConstraint(
            "confidence_before BETWEEN 0 AND 1", name="ck_ruleset_rating_ledger_d547b8a3c75a"
        ),
        sa.CheckConstraint("k_factor > 0", name="ck_ruleset_rating_ledger_6672d9d9be6f"),
        sa.CheckConstraint(
            "rating_after = rating_before + delta", name="ck_ruleset_rating_ledger_f1f08c7e6def"
        ),
        sa.CheckConstraint("tournament_weight > 0", name="ck_ruleset_rating_ledger_f60be3610dda"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "player_id"),
    )
    op.create_index(
        "ix_ruleset_rating_ledger_history",
        "ruleset_rating_ledger",
        ["ruleset_key", "player_id", "played_at"],
        unique=False,
    )
    op.create_table(
        "game_results",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("tournament_policy_version_id", sa.UUID(), nullable=False),
        sa.Column("place", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("score", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("place >= 1", name="ck_game_results_5304e799386a"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_policy_version_id"],
            ["tournament_policy_versions.id"],
        ),
        sa.PrimaryKeyConstraint("game_id", "player_id"),
    )
    op.create_table(
        "question_rounds",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("question_revision_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'completed', 'invalidated')",
            name="ck_question_rounds_9890be40d1d4",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_revision_id"],
            ["question_revisions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "sequence"),
    )
    op.create_table(
        "game_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "sequence"),
    )
    op.create_table(
        "reputation_votes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("voter_player_id", sa.UUID(), nullable=False),
        sa.Column("target_player_id", sa.UUID(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("value IN (-1, 1)", name="ck_reputation_votes_585d41c3e5b4"),
        sa.CheckConstraint(
            "voter_player_id <> target_player_id", name="ck_reputation_votes_716299d79772"
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["voter_player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "voter_player_id", "target_player_id"),
    )
    op.create_index(
        "ix_reputation_votes_throttle",
        "reputation_votes",
        ["voter_player_id", "target_player_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "player_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("reporter_player_id", sa.UUID(), nullable=False),
        sa.Column("reported_player_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reporter_player_id <> reported_player_id", name="ck_player_reports_f1d0aa286b4f"
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reported_player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reporter_player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "reporter_player_id", "reported_player_id"),
    )
    op.create_index(
        "ix_player_reports_target",
        "player_reports",
        ["reported_player_id", "kind", "created_at"],
        unique=False,
    )
    op.create_table(
        "si_suspicion_materialized_games",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "materialized_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.PrimaryKeyConstraint("game_id"),
    )
    op.create_table(
        "si_player_game_suspicion_metrics",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_buzzes", sa.Integer(), nullable=False),
        sa.Column(
            "buzz_revealed_fraction_total", sa.Numeric(precision=24, scale=8), nullable=False
        ),
        sa.Column(
            "buzz_revealed_fraction_squared_total",
            sa.Numeric(precision=24, scale=8),
            nullable=False,
        ),
        sa.Column("buzz_revealed_fraction_observations", sa.Integer(), nullable=False),
        sa.Column("late_buzzes", sa.Integer(), nullable=False),
        sa.Column("high_value_exposures", sa.Integer(), nullable=False),
        sa.Column("high_value_correct", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "accepted_buzzes >= 0 AND buzz_revealed_fraction_total >= 0 AND "
            "buzz_revealed_fraction_squared_total >= 0 AND "
            "buzz_revealed_fraction_observations >= 0 AND late_buzzes >= 0 AND "
            "high_value_exposures >= 0 AND high_value_correct >= 0",
            name="ck_si_player_game_suspicion_metrics_707f49ffa4b0",
        ),
        sa.CheckConstraint(
            "buzz_revealed_fraction_observations <= accepted_buzzes",
            name="ck_si_player_game_suspicion_metrics_afa6d413a55d",
        ),
        sa.CheckConstraint(
            "high_value_correct <= high_value_exposures",
            name="ck_si_player_game_suspicion_metrics_982298f21703",
        ),
        sa.CheckConstraint(
            "late_buzzes <= accepted_buzzes",
            name="ck_si_player_game_suspicion_metrics_251f48a0a084",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("game_id", "player_id"),
    )
    op.create_index(
        "ix_si_player_game_suspicion_history",
        "si_player_game_suspicion_metrics",
        ["player_id", "completed_at"],
        unique=False,
    )
    op.create_table(
        "tournament_packet_entitlements",
        sa.Column("assignment_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("discoverable", sa.Boolean(), nullable=False),
        sa.Column("playable", sa.Boolean(), nullable=False),
        sa.Column("content_visible", sa.Boolean(), nullable=False),
        sa.Column("editable", sa.Boolean(), nullable=False),
        sa.Column("access_level", sa.String(length=24), nullable=True),
        sa.Column("granted_by_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "access_level IS NULL OR access_level IN ('no-access', 'play-only', "
            "'read-after-play', 'read-or-play')",
            name="ck_tournament_packet_entitlements_b7803cf5f3c0",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["tournament_packet_assignments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("assignment_id", "player_id"),
    )
    op.create_table(
        "packet_questions",
        sa.Column("packet_version_id", sa.UUID(), nullable=False),
        sa.Column("theme_revision_id", sa.UUID(), nullable=False),
        sa.Column("question_revision_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.CheckConstraint("value > 0", name="ck_packet_questions_c0d1729bd8b9"),
        sa.ForeignKeyConstraint(
            ["packet_version_id"],
            ["packet_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_revision_id"],
            ["question_revisions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["theme_revision_id"],
            ["theme_revisions.id"],
        ),
        sa.PrimaryKeyConstraint("packet_version_id", "theme_revision_id", "question_revision_id"),
        sa.UniqueConstraint("theme_revision_id", "position"),
        sa.UniqueConstraint("theme_revision_id", "value"),
    )
    op.create_table(
        "game_themes",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("theme_revision_id", sa.UUID(), nullable=False),
        sa.Column("source_packet_version_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 1", name="ck_game_themes_adf200d36a7d"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_packet_version_id"],
            ["packet_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["theme_revision_id"],
            ["theme_revisions.id"],
        ),
        sa.PrimaryKeyConstraint("game_id", "theme_revision_id"),
        sa.UniqueConstraint("game_id", "position"),
    )
    op.create_table(
        "game_packet_versions",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("packet_version_id", sa.UUID(), nullable=False),
        sa.Column("assignment_id", sa.UUID(), nullable=False),
        sa.Column("selection_order", sa.Integer(), nullable=False),
        sa.CheckConstraint("selection_order >= 1", name="ck_game_packet_versions_afd57055b11a"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["tournament_packet_assignments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_version_id"],
            ["packet_versions.id"],
        ),
        sa.PrimaryKeyConstraint("game_id", "packet_version_id"),
        sa.UniqueConstraint("game_id", "selection_order"),
    )
    op.create_table(
        "pregame_lobby_packets",
        sa.Column("lobby_id", sa.UUID(), nullable=False),
        sa.Column("packet_id", sa.UUID(), nullable=False),
        sa.Column("packet_version_id", sa.UUID(), nullable=False),
        sa.Column("assignment_id", sa.UUID(), nullable=False),
        sa.Column("selection_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("selection_order >= 1", name="ck_pregame_lobby_packets_afd57055b11a"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["tournament_packet_assignments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["lobby_id"],
            ["pregame_lobbies.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"],
            ["logical_packets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packet_version_id"],
            ["packet_versions.id"],
        ),
        sa.PrimaryKeyConstraint("lobby_id", "packet_id"),
        sa.UniqueConstraint("lobby_id", "selection_order"),
    )
    op.create_table(
        "pregame_lobby_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lobby_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("join_order", sa.Integer(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("validation_violations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("fresh_content_confirmed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('player', 'observer')", name="ck_pregame_lobby_members_862eb988cf90"
        ),
        sa.ForeignKeyConstraint(
            ["lobby_id"],
            ["pregame_lobbies.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lobby_id", "join_order"),
        sa.UniqueConstraint("lobby_id", "player_id"),
    )
    op.create_index(
        "uq_active_pregame_lobby_per_player",
        "pregame_lobby_members",
        ["player_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "pregame_lobby_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lobby_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["lobby_id"],
            ["pregame_lobbies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lobby_id", "sequence"),
    )
    op.create_table(
        "player_question_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=False),
        sa.Column("participant_id", sa.UUID(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("attempted", sa.Boolean(), nullable=False),
        sa.Column("buzzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_buzz_order", sa.Integer(), nullable=True),
        sa.Column("buzz_revealed_fraction", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("buzz_time_remaining_fraction", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.CheckConstraint(
            "buzz_revealed_fraction IS NULL OR buzz_revealed_fraction BETWEEN 0 AND 1",
            name="ck_player_question_states_c9cef616511d",
        ),
        sa.CheckConstraint(
            "buzz_time_remaining_fraction IS NULL OR buzz_time_remaining_fraction BETWEEN 0 AND 1",
            name="ck_player_question_states_670eed93d221",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["game_participants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["question_rounds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("round_id", "participant_id"),
    )
    op.create_table(
        "answer_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=False),
        sa.Column("participant_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("submitted_answer", sa.Text(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("original_correct", sa.Boolean(), nullable=False),
        sa.Column("final_correct", sa.Boolean(), nullable=False),
        sa.Column(
            "judged_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["game_participants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["question_rounds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("round_id", "attempt_number"),
        sa.UniqueConstraint("round_id", "participant_id"),
    )
    op.create_table(
        "reputation_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=True),
        sa.Column("vote_id", sa.UUID(), nullable=True),
        sa.Column("report_id", sa.UUID(), nullable=True),
        sa.Column("reputation_before", sa.Integer(), nullable=False),
        sa.Column("requested_delta", sa.Integer(), nullable=False),
        sa.Column("applied_delta", sa.Integer(), nullable=False),
        sa.Column("reputation_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('player_vote', 'toxicity_report', 'admin_correction')",
            name="ck_reputation_ledger_09e94f36d60c",
        ),
        sa.CheckConstraint(
            "reputation_after = reputation_before + applied_delta",
            name="ck_reputation_ledger_0c743a0664d5",
        ),
        sa.CheckConstraint(
            "reputation_after BETWEEN 0 AND 100", name="ck_reputation_ledger_d22bcb8a877c"
        ),
        sa.CheckConstraint(
            "reputation_before BETWEEN 0 AND 100", name="ck_reputation_ledger_48bcd65abd81"
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["player_reports.id"],
        ),
        sa.ForeignKeyConstraint(
            ["vote_id"],
            ["reputation_votes.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id"),
        sa.UniqueConstraint("vote_id"),
    )
    op.create_index(
        "ix_reputation_ledger_player",
        "reputation_ledger",
        ["player_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "si_player_question_suspicion_metrics",
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=False),
        sa.Column("question_value", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("buzzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("buzz_revealed_fraction", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("buzz_time_remaining_fraction", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.CheckConstraint(
            "buzz_revealed_fraction IS NULL OR buzz_revealed_fraction BETWEEN 0 AND 1",
            name="ck_si_player_question_suspicion_metrics_c9cef616511d",
        ),
        sa.CheckConstraint(
            "buzz_time_remaining_fraction IS NULL OR buzz_time_remaining_fraction BETWEEN 0 AND 1",
            name="ck_si_player_question_suspicion_metrics_670eed93d221",
        ),
        sa.CheckConstraint(
            "question_value > 0", name="ck_si_player_question_suspicion_metrics_9b25fdf55900"
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["logical_questions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["question_rounds.id"],
        ),
        sa.PrimaryKeyConstraint("game_id", "player_id", "question_id"),
        sa.UniqueConstraint("round_id", "player_id"),
    )
    op.create_index(
        "ix_si_player_question_suspicion_player",
        "si_player_question_suspicion_metrics",
        ["player_id", "game_id"],
        unique=False,
    )
    op.create_index(
        "ix_si_player_question_suspicion_question",
        "si_player_question_suspicion_metrics",
        ["question_id", "player_id"],
        unique=False,
    )
    op.create_table(
        "suspicion_evidence_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["suspicion_evidence.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["logical_questions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["question_rounds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evidence_id", "game_id", "round_id"),
    )
    op.create_index(
        "ix_suspicion_evidence_actions_evidence",
        "suspicion_evidence_actions",
        ["evidence_id", "id"],
        unique=False,
    )
    op.create_table(
        "suspicion_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("report_id", sa.UUID(), nullable=True),
        sa.Column("evaluation_id", sa.UUID(), nullable=True),
        sa.Column("administrator_id", sa.UUID(), nullable=True),
        sa.Column("ruleset_key", sa.String(length=64), nullable=True),
        sa.Column("suspicion_before", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("suspicion_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(reason = 'admin_clearance' AND administrator_id IS NOT NULL AND "
            "suspicion_after = 0 AND delta <= 0) OR (reason <> 'admin_clearance' AND "
            "administrator_id IS NULL AND delta > 0)",
            name="ck_suspicion_ledger_13f52469ff07",
        ),
        sa.CheckConstraint(
            "suspicion_after = suspicion_before + delta", name="ck_suspicion_ledger_07d7e216e867"
        ),
        sa.CheckConstraint("suspicion_after >= 0", name="ck_suspicion_ledger_d5528f6faf1e"),
        sa.CheckConstraint("suspicion_before >= 0", name="ck_suspicion_ledger_7095c857d198"),
        sa.ForeignKeyConstraint(
            ["administrator_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["suspicion_evaluations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["player_reports.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_id", "reason"),
        sa.UniqueConstraint("report_id"),
    )
    op.create_index(
        "ix_suspicion_ledger_player", "suspicion_ledger", ["player_id", "created_at"], unique=False
    )
    op.create_table(
        "appeals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tournament_id", sa.UUID(), nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=False),
        sa.Column("appellant_participant_id", sa.UUID(), nullable=False),
        sa.Column("target_attempt_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("voting_rule", sa.String(length=16), nullable=False),
        sa.Column("electorate_size", sa.Integer(), nullable=False),
        sa.Column("vote_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("escalation_enabled", sa.Boolean(), nullable=False),
        sa.Column("escalation_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commentary_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ticket_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commentary", sa.Text(), nullable=True),
        sa.Column("game_was_paused", sa.Boolean(), nullable=False),
        sa.Column("decision_source", sa.String(length=24), nullable=True),
        sa.Column("decided_by_manager_id", sa.UUID(), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision_source IS NULL OR decision_source IN ('player_vote', "
            "'player_declined', 'manager', 'timeout')",
            name="ck_appeals_305d93b1f7d0",
        ),
        sa.CheckConstraint(
            "kind IN ('accept_incorrect', 'reject_correct')", name="ck_appeals_07198ec511d3"
        ),
        sa.CheckConstraint(
            "status IN ('voting', 'awaiting_escalation', 'awaiting_commentary', "
            "'escalated', 'accepted', 'rejected')",
            name="ck_appeals_00414772441b",
        ),
        sa.CheckConstraint(
            "voting_rule IN ('majority', 'unanimous')", name="ck_appeals_26a87fe7adaf"
        ),
        sa.CheckConstraint("electorate_size >= 1", name="ck_appeals_ee56cef22c8c"),
        sa.ForeignKeyConstraint(
            ["appellant_participant_id"],
            ["game_participants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_manager_id"],
            ["players.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["question_rounds.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_attempt_id"],
            ["answer_attempts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "round_id"),
    )
    op.create_index(
        "ix_appeals_due",
        "appeals",
        ["status", "vote_deadline", "escalation_deadline"],
        unique=False,
    )
    op.create_index(
        "ix_appeals_manager_queue",
        "appeals",
        ["tournament_id", "status", "ticket_expires_at"],
        unique=False,
    )
    op.create_table(
        "appeal_votes",
        sa.Column("appeal_id", sa.UUID(), nullable=False),
        sa.Column("participant_id", sa.UUID(), nullable=False),
        sa.Column("approve", sa.Boolean(), nullable=True),
        sa.Column("voted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["appeal_id"],
            ["appeals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["game_participants.id"],
        ),
        sa.PrimaryKeyConstraint("appeal_id", "participant_id"),
    )
    op.create_table(
        "score_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.UUID(), nullable=False),
        sa.Column("participant_id", sa.UUID(), nullable=False),
        sa.Column("round_id", sa.UUID(), nullable=True),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("appeal_id", sa.UUID(), nullable=True),
        sa.Column("delta", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("correction_of_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('correct_answer', 'incorrect_answer', 'answer_timeout', "
            "'appeal_correction', 'admin_correction')",
            name="ck_score_ledger_abbd8a740499",
        ),
        sa.ForeignKeyConstraint(
            ["appeal_id"],
            ["appeals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["answer_attempts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["correction_of_id"],
            ["score_ledger.id"],
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["game_participants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["question_rounds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_score_ledger_appeal_id"), "score_ledger", ["appeal_id"], unique=False)
    op.create_foreign_key(
        "fk_game_current_round",
        "games",
        "question_rounds",
        ["current_round_id"],
        ["id"],
        use_alter=True,
    )
    op.create_foreign_key(
        "fk_draft_published_version",
        "packet_drafts",
        "packet_versions",
        ["published_version_id"],
        ["id"],
        use_alter=True,
    )
    op.create_foreign_key(
        "fk_game_accepted_buzzer",
        "games",
        "game_participants",
        ["accepted_buzzer_id"],
        ["id"],
        use_alter=True,
    )
    op.create_foreign_key(
        "fk_game_source_lobby",
        "games",
        "pregame_lobbies",
        ["source_lobby_id"],
        ["id"],
        use_alter=True,
    )
    op.execute(
        "INSERT INTO tournament_type_versions (id, key, version, name, rules, active)"
        " VALUES ('10000000-0000-0000-0000-000000000001', 'ladder', 1, 'Ladder', "
        '\'{"compatible_rulesets": ["si"], "minimum_players": 1, "maximum_players": '
        '12, "open_ended": true, "rated": true, "supports_hybrid_matchmaking": '
        "true}', TRUE)"
    )
    op.execute(
        "INSERT INTO tournament_type_versions (id, key, version, name, rules, active)"
        " VALUES ('10000000-0000-0000-0000-000000000002', 'classic', 1, 'Classic', "
        '\'{"compatible_rulesets": ["si"], "minimum_players": 1, "maximum_players": '
        '12, "scheduled": true, "finite_packets": true, '
        '"supports_hybrid_matchmaking": false}\', TRUE)'
    )
    op.execute(
        "INSERT INTO game_ruleset_versions (id, key, version, name, parameter_schema,"
        " content_schema, active) VALUES ('20000000-0000-0000-0000-000000000001', "
        '\'si\', 1, \'SI\', \'{"ready_delay": "non_negative_number", "message_delay": '
        '"non_negative_number", "game_start_to_first_theme_delay": '
        '"non_negative_number", "theme_to_first_question_delay": '
        '"non_negative_number", "question_cost_announcement_delay": '
        '"non_negative_number", "question_token_delay": "non_negative_number", '
        '"question_token_target_chars": "positive_integer", '
        '"buzz_timer_countdown_delay": "non_negative_number", "buzz_timeout": '
        '"non_negative_number", "answer_timeout": "non_negative_number", '
        '"between_questions_delay": "non_negative_number", '
        '"last_question_to_theme_complete_delay": "non_negative_number", '
        '"theme_complete_to_scoreboard_delay": "non_negative_number", '
        '"between_themes_delay": "non_negative_number", "pausing_allowed": "boolean",'
        ' "question_values": "positive_integer_list", "minus_multiplier": '
        '"non_negative_number", "theme_count": "positive_integer"}\', \'{"play_unit": '
        '"si_theme", "exposure_claims": ["theme", "question"], "progression": '
        '"buzz_and_answer"}\', TRUE)'
    )


def downgrade() -> None:
    op.drop_constraint("fk_game_source_lobby", "games", type_="foreignkey")
    op.drop_constraint("fk_game_accepted_buzzer", "games", type_="foreignkey")
    op.drop_constraint("fk_draft_published_version", "packet_drafts", type_="foreignkey")
    op.drop_constraint("fk_game_current_round", "games", type_="foreignkey")
    op.drop_table("score_ledger")
    op.drop_table("appeal_votes")
    op.drop_table("appeals")
    op.drop_table("suspicion_ledger")
    op.drop_table("suspicion_evidence_actions")
    op.drop_table("si_player_question_suspicion_metrics")
    op.drop_table("reputation_ledger")
    op.drop_table("answer_attempts")
    op.drop_table("player_question_states")
    op.drop_table("pregame_lobby_events")
    op.drop_table("pregame_lobby_members")
    op.drop_table("pregame_lobby_packets")
    op.drop_table("game_packet_versions")
    op.drop_table("game_themes")
    op.drop_table("packet_questions")
    op.drop_table("tournament_packet_entitlements")
    op.drop_table("si_player_game_suspicion_metrics")
    op.drop_table("si_suspicion_materialized_games")
    op.drop_table("player_reports")
    op.drop_table("reputation_votes")
    op.drop_table("game_events")
    op.drop_table("question_rounds")
    op.drop_table("game_results")
    op.drop_table("ruleset_rating_ledger")
    op.drop_table("rating_ledger")
    op.drop_table("pregame_lobbies")
    op.drop_table("player_exposure_claims")
    op.drop_table("game_observers")
    op.drop_table("game_participants")
    op.drop_table("theme_revisions")
    op.drop_table("tournament_packet_assignments")
    op.drop_table("telegram_game_views")
    op.drop_table("si_question_suspicion_aggregates")
    op.drop_table("games")
    op.drop_table("question_revisions")
    op.drop_table("packet_versions")
    op.drop_table("packet_draft_tournaments")
    op.drop_table("tournament_creation_token_deliveries")
    op.drop_table("tournament_pricing_plan_prices")
    op.drop_table("suspicion_evidence")
    op.drop_table("logical_questions")
    op.drop_table("themes")
    op.drop_table("packet_drafts")
    op.drop_table("tournament_authors")
    op.drop_table("player_author_links")
    op.drop_table("tournament_creation_tokens")
    op.drop_table("tournament_registration_attempts")
    op.drop_table("tournament_registration_requirements")
    op.drop_table("tournament_memberships")
    op.drop_table("tournament_managers")
    op.drop_table("tournament_policy_versions")
    op.drop_table("tournament_pricing_plans")
    op.drop_table("player_telegram_navigation")
    op.drop_table("si_player_suspicion_aggregates")
    op.drop_table("suspicion_evaluations")
    op.drop_table("ruleset_ratings")
    op.drop_table("player_blacklists")
    op.drop_table("logical_packets")
    op.drop_table("application_launch_references")
    op.drop_table("mini_app_sessions")
    op.drop_table("application_request_audit")
    op.drop_table("player_notification_alerts")
    op.drop_table("player_notifications")
    op.drop_table("player_author_link_requests")
    op.drop_table("tournament_creation_token_requests")
    op.drop_table("tournaments")
    op.drop_table("platform_administrators")
    op.drop_table("si_suspicion_baselines")
    op.drop_table("suspicion_evaluation_schedules")
    op.drop_table("telegram_update_receipts")
    op.drop_table("durable_jobs")
    op.drop_table("outbox_events")
    op.drop_table("application_idempotency")
    op.drop_table("authors")
    op.drop_table("game_ruleset_versions")
    op.drop_table("tournament_type_versions")
    op.drop_table("players")
