"""Ladder subscription cards and explicit packet access overrides."""

import sqlalchemy as sa
from alembic import op

revision = "0015_subscription_cards"
down_revision = "0014_contact_rejected_answers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournament_packet_entitlements", sa.Column("discoverable_override", sa.Boolean())
    )
    op.add_column("tournament_packet_entitlements", sa.Column("readable_override", sa.Boolean()))
    op.create_table(
        "tournament_subscription_cards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tournament_id", sa.Uuid(), sa.ForeignKey("tournaments.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("packet_count", sa.Integer()),
        sa.Column("discoverable", sa.Boolean()),
        sa.Column("readable", sa.Boolean()),
        sa.Column("playable", sa.Boolean()),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("packet_count IS NULL OR packet_count >= 1"),
    )
    op.create_table(
        "tournament_subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "card_id", sa.Uuid(), sa.ForeignKey("tournament_subscription_cards.id"), nullable=False
        ),
        sa.Column("player_id", sa.Uuid(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("remaining_packets", sa.Integer()),
        sa.Column("assigned_by_id", sa.Uuid(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("remaining_packets IS NULL OR remaining_packets >= 0"),
    )


def downgrade() -> None:
    op.drop_table("tournament_subscriptions")
    op.drop_table("tournament_subscription_cards")
    op.drop_column("tournament_packet_entitlements", "readable_override")
    op.drop_column("tournament_packet_entitlements", "discoverable_override")
