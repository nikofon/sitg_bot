"""Manual AI administration and constant-time quota reset.

Revision ID: 0031_ai_administration
Revises: 0030_public_analysis_cache
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_ai_administration"
down_revision = "0030_public_analysis_cache"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "training_policy_state",
        sa.Column("admin_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "training_policy_state",
        sa.Column("quota_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("training_policy_state", sa.Column("free_policy_version", sa.Integer()))
    op.add_column("training_policy_state", sa.Column("premium_policy_version", sa.Integer()))
    op.add_column("training_policy_state", sa.Column("admin_features", postgresql.JSONB()))
    op.create_table(
        "training_ai_tiers",
        sa.Column(
            "player_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("players.id"),
            primary_key=True,
        ),
        sa.Column("tier", sa.String(10), nullable=False),
        sa.CheckConstraint("tier IN ('free', 'premium', 'no_ai')"),
    )
    op.create_table(
        "training_provider_health",
        sa.Column("profile", sa.String(64), primary_key=True),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("error", sa.String(20)),
    )
    op.add_column(
        "training_usage_periods",
        sa.Column("quota_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("uq_training_usage_periods_scope", "training_usage_periods")
    op.create_unique_constraint(
        "uq_training_usage_periods_scope",
        "training_usage_periods",
        ["player_id", "feature", "period_start", "period_end", "quota_generation"],
    )
    op.execute(
        "CREATE INDEX ix_training_analysis_health ON training_analysis_artifacts "
        "((provider_usage ->> 'execution_profile'), completed_at DESC) "
        "WHERE status = 'succeeded' AND (provider_usage -> 'calls') @> '[{}]'::jsonb"
    )
    # Literal case-insensitive prefix lookup, not unbounded substring scanning.
    op.execute(
        "CREATE INDEX ix_players_ai_name_prefix "
        "ON players (lower(public_nickname) text_pattern_ops)"
    )


def downgrade():
    # Multiple generations cannot be merged without invalidating reservation references.
    # Keep the generation-aware unique key until the column is dropped; no ledger deletion.
    op.drop_index("ix_training_analysis_health", "training_analysis_artifacts")
    op.drop_index("ix_players_ai_name_prefix", "players")
    op.drop_table("training_provider_health")
    op.drop_table("training_ai_tiers")
    for column in (
        "admin_features",
        "premium_policy_version",
        "free_policy_version",
        "quota_generation",
        "admin_version",
    ):
        op.drop_column("training_policy_state", column)
    # Preserve counters and ledger on downgrade: collapse duplicate scopes onto one
    # canonical row, move ledger references, and sum counters before removing generation.
    op.execute("""
        CREATE TEMP TABLE ai_period_merge ON COMMIT DROP AS
        SELECT id, first_value(id) OVER (
            PARTITION BY player_id, feature, period_start ORDER BY quota_generation DESC, id
        ) AS keep_id FROM training_usage_periods
    """)
    op.execute("""
        UPDATE training_usage_ledger l SET period_id = m.keep_id
        FROM ai_period_merge m WHERE l.period_id = m.id AND m.id <> m.keep_id
    """)
    op.execute("""
        UPDATE training_usage_periods p SET reserved = totals.reserved, settled = totals.settled
        FROM (SELECT m.keep_id, sum(p.reserved) AS reserved, sum(p.settled) AS settled
              FROM ai_period_merge m JOIN training_usage_periods p ON p.id = m.id
              GROUP BY m.keep_id) totals WHERE p.id = totals.keep_id
    """)
    op.execute(
        "DELETE FROM training_usage_periods p USING ai_period_merge m "
        "WHERE p.id = m.id AND m.id <> m.keep_id"
    )
    op.drop_constraint("uq_training_usage_periods_scope", "training_usage_periods")
    op.drop_column("training_usage_periods", "quota_generation")
    op.create_unique_constraint(
        "uq_training_usage_periods_scope",
        "training_usage_periods",
        ["player_id", "feature", "period_start"],
    )
