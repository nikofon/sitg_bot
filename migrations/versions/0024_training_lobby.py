"""Persist solo training lobby selections independently of running sessions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_training_lobby"
down_revision = "0023_copy_sharing_theme_quotas"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("players", sa.Column("training_lobby", JSONB(), nullable=True))
    op.add_column(
        "players",
        sa.Column(
            "training_lobby_version", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade():
    op.drop_column("players", "training_lobby_version")
    op.drop_column("players", "training_lobby")
