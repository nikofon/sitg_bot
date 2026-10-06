"""Persist lobby summary and settings message identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_lobby_messages"
down_revision = "0018_appeal_selection_timer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pregame_lobby_members", sa.Column(
        "telegram_messages", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ))


def downgrade() -> None:
    op.drop_column("pregame_lobby_members", "telegram_messages")
