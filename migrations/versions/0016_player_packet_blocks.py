"""Player packet blocks: player-scoped packets treated as burnt without claims."""

import sqlalchemy as sa
from alembic import op

revision = "0016_player_packet_blocks"
down_revision = "0015_subscription_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_packet_blocks",
        sa.Column("player_id", sa.Uuid(), primary_key=True),
        sa.Column("packet_id", sa.Uuid(), primary_key=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.ForeignKeyConstraint(["packet_id"], ["logical_packets.id"]),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("player_packet_blocks")
