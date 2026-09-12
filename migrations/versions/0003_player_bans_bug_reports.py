"""Player bans and bug reports."""

import sqlalchemy as sa
from alembic import op

revision = "0003_player_bans_bug_reports"
down_revision = "0002_token_delivery_pgcrypto"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_bans",
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("banned_by_id", sa.UUID(), nullable=False),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifted_by_id", sa.UUID(), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("player_id"),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.ForeignKeyConstraint(["banned_by_id"], ["players.id"]),
        sa.ForeignKeyConstraint(["lifted_by_id"], ["players.id"]),
        sa.CheckConstraint("length(reason) > 0"),
        sa.CheckConstraint("lifted_at IS NULL OR lifted_by_id IS NOT NULL"),
    )
    op.create_table(
        "bug_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("reporter_player_id", sa.UUID(), nullable=False),
        sa.Column("commentary", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["reporter_player_id"], ["players.id"]),
        sa.CheckConstraint("length(commentary) BETWEEN 1 AND 4000"),
    )
    op.create_index("ix_bug_reports_created", "bug_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_bug_reports_created", table_name="bug_reports")
    op.drop_table("bug_reports")
    op.drop_table("player_bans")
