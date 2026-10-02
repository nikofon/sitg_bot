"""Classic tournament match chats with shared advisory game times."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0009_classic_chats"
down_revision = "0008_theme_commentary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "classic_chats",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "match_id", pg.UUID(as_uuid=True), sa.ForeignKey("classic_matches.id"), unique=True,
            nullable=False,
        ),
        sa.Column(
            "tournament_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tournaments.id"),
            nullable=False,
        ),
        sa.Column("planned_at", sa.DateTime(timezone=True)),
        sa.Column("planned_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("players.id")),
    )
    op.create_table(
        "classic_chat_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "chat_id", pg.UUID(as_uuid=True), sa.ForeignKey("classic_chats.id"), nullable=False
        ),
        sa.Column("sequence", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
        sa.Column("sender_id", pg.UUID(as_uuid=True), sa.ForeignKey("players.id")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("caption", sa.Text()),
        sa.Column("source_chat_id", sa.BigInteger()),
        sa.Column("source_message_id", sa.BigInteger()),
        sa.Column("system", pg.JSONB()),
        sa.CheckConstraint("kind IN ('text', 'media', 'system')"),
    )
    op.create_index(
        "ix_classic_chat_messages_chat_id", "classic_chat_messages", ["chat_id"]
    )
    op.create_table(
        "classic_chat_members",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "chat_id", pg.UUID(as_uuid=True), sa.ForeignKey("classic_chats.id"), primary_key=True
        ),
        sa.Column(
            "player_id", pg.UUID(as_uuid=True), sa.ForeignKey("players.id"), primary_key=True
        ),
        sa.Column(
            "last_read_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "message_ids", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.add_column(
        "player_telegram_navigation",
        sa.Column("active_chat_id", pg.UUID(as_uuid=True), sa.ForeignKey("classic_chats.id")),
    )


def downgrade() -> None:
    op.drop_column("player_telegram_navigation", "active_chat_id")
    op.drop_table("classic_chat_members")
    op.drop_index("ix_classic_chat_messages_chat_id", table_name="classic_chat_messages")
    op.drop_table("classic_chat_messages")
    op.drop_table("classic_chats")
