"""Private tester identities, audited player links and immutable packet membership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_testers"
down_revision = "0020_database_maintenance"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
    ]


def upgrade() -> None:
    op.create_table(
        "testers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.String(300), nullable=False),
        sa.Column("first_name", sa.String(100)),
        sa.Column("second_name", sa.String(100)),
        sa.Column("surname", sa.String(100)),
        sa.Column("telegram_link", sa.String(200)),
        sa.Column("telegram_username", sa.String(64)),
        *timestamps(),
    )
    op.create_index("ix_testers_display_name", "testers", [sa.text("lower(display_name)"), "id"])
    op.create_table(
        "player_tester_link_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("player_id", sa.Uuid(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("tester_id", sa.Uuid(), sa.ForeignKey("testers.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("request_note", sa.String(1000)),
        sa.Column("decided_by_id", sa.Uuid(), sa.ForeignKey("players.id")),
        sa.Column("decision_note", sa.String(1000)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')"),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by_id IS NULL AND decided_at IS NULL "
            "AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND decided_by_id IS NULL AND decided_at IS NULL "
            "AND cancelled_at IS NOT NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_by_id IS NOT NULL "
            "AND decided_at IS NOT NULL AND cancelled_at IS NULL)"
        ),
    )
    op.create_index(
        "uq_player_tester_link_requests_pending",
        "player_tester_link_requests",
        ["player_id", "tester_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_player_tester_link_requests_queue",
        "player_tester_link_requests",
        ["status", "created_at", "id"],
    )
    op.create_index(
        "ix_player_tester_link_requests_player",
        "player_tester_link_requests",
        ["player_id", "created_at", "id"],
    )
    op.create_table(
        "player_tester_links",
        sa.Column("player_id", sa.Uuid(), sa.ForeignKey("players.id"), primary_key=True),
        sa.Column("tester_id", sa.Uuid(), sa.ForeignKey("testers.id"), primary_key=True),
        sa.Column(
            "approved_request_id",
            sa.Uuid(),
            sa.ForeignKey("player_tester_link_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("approved_by_id", sa.Uuid(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
    )
    op.create_table(
        "packet_version_testers",
        sa.Column(
            "packet_version_id", sa.Uuid(), sa.ForeignKey("packet_versions.id"), primary_key=True
        ),
        sa.Column("tester_id", sa.Uuid(), sa.ForeignKey("testers.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("packet_version_id", "position"),
    )
    op.create_index(
        "ix_packet_version_testers_tester",
        "packet_version_testers",
        ["tester_id", "packet_version_id"],
    )
    op.add_column(
        "packet_drafts",
        sa.Column(
            "tester_bindings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("packet_drafts", "tester_bindings")
    op.drop_table("packet_version_testers")
    op.drop_table("player_tester_links")
    op.drop_table("player_tester_link_requests")
    op.drop_table("testers")
