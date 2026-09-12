"""Preset Classic stages, rounds, games, and passive Chair seats."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_classic_tournaments"
down_revision = "0002_token_delivery_pgcrypto"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "game_participants",
        sa.Column("is_chair", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "classic_stages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "tournament_id", pg.UUID(as_uuid=True), sa.ForeignKey("tournaments.id"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("stage_type", sa.String(16), nullable=False),
        sa.Column("scheme_key", sa.String(40)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("seeds", pg.JSONB(), nullable=False),
        sa.Column("place_points", pg.JSONB(), nullable=False),
        sa.Column("score_multiplier", sa.Numeric(24, 8), nullable=False),
        sa.Column("random_seed", sa.String(64), nullable=False),
        sa.UniqueConstraint("tournament_id", "kind"),
        sa.CheckConstraint("kind IN ('first', 'playoff')"),
        sa.CheckConstraint("stage_type IN ('none', 'groups', 'quiz', 'playoff')"),
    )
    op.create_table(
        "classic_rounds",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "stage_id", pg.UUID(as_uuid=True), sa.ForeignKey("classic_stages.id"), nullable=False
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column(
            "assignment_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tournament_packet_assignments.id"),
        ),
        sa.Column("discoverable", sa.Boolean(), nullable=False),
        sa.Column("playable", sa.Boolean(), nullable=False),
        sa.Column("start_deadline", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("stage_id", "number"),
        sa.CheckConstraint("number >= 1"),
    )
    op.create_table(
        "classic_matches",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "round_id", pg.UUID(as_uuid=True), sa.ForeignKey("classic_rounds.id"), nullable=False
        ),
        sa.Column("group_number", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("sources", pg.JSONB(), nullable=False),
        sa.Column("seats", pg.JSONB(), nullable=False),
        sa.Column("results", pg.JSONB()),
        sa.Column("randomized", sa.Boolean(), nullable=False),
        sa.Column("game_id", pg.UUID(as_uuid=True), sa.ForeignKey("games.id"), unique=True),
        sa.UniqueConstraint("round_id", "group_number", "number"),
        sa.CheckConstraint("group_number >= 1 AND number >= 1"),
    )


def downgrade() -> None:
    op.drop_table("classic_matches")
    op.drop_table("classic_rounds")
    op.drop_table("classic_stages")
    op.drop_column("game_participants", "is_chair")
