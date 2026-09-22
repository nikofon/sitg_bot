"""Separate library viewing conditions from packet playability."""

import sqlalchemy as sa
from alembic import op

revision = "0010_library_viewing_rules"
down_revision = "0009_tournament_player_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_tournament_packet_assignments_e7dbbad4676e"),
        "tournament_packet_assignments", type_="check",
    )
    op.alter_column("tournament_packet_assignments", "access_level_by_members",
                    new_column_name="library_viewing_rule")
    op.execute("""
        UPDATE tournament_packet_assignments SET library_viewing_rule =
            CASE library_viewing_rule
                WHEN 'read-after-play' THEN 'after-play'
                WHEN 'read-or-play' THEN 'anytime'
                ELSE 'never' END
    """)
    op.create_check_constraint(
        op.f("ck_tournament_packet_assignments_063c234a9ce0"),
        "tournament_packet_assignments",
        "library_viewing_rule IN ('never', 'after-play', 'anytime')",
    )
    # Preserve explicit per-player allow/deny choices independently of viewing rules.
    op.alter_column("tournament_packet_entitlements", "playable",
                    existing_type=sa.Boolean(), nullable=True)
    op.execute("""
        UPDATE tournament_packet_entitlements SET playable = CASE
            WHEN access_level = 'no-access' THEN false
            WHEN access_level IS NOT NULL OR playable THEN true
            ELSE NULL END
    """)
    op.drop_constraint(
        op.f("ck_tournament_packet_entitlements_b7803cf5f3c0"),
        "tournament_packet_entitlements", type_="check",
    )
    op.drop_column("tournament_packet_entitlements", "access_level")
    op.execute("""
        UPDATE tournament_policy_versions SET policies =
            (policies - 'packet_access_rule_default') || jsonb_build_object(
                'library_viewing_rule_default', CASE policies->>'packet_access_rule_default'
                    WHEN 'no-access' THEN 'never'
                    WHEN 'play-only' THEN 'never'
                    WHEN 'read-or-play' THEN 'anytime'
                    ELSE 'after-play' END)
    """)


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_tournament_packet_assignments_063c234a9ce0"),
        "tournament_packet_assignments", type_="check",
    )
    op.alter_column("tournament_packet_assignments", "library_viewing_rule",
                    new_column_name="access_level_by_members")
    op.execute("""
        UPDATE tournament_packet_assignments SET access_level_by_members =
            CASE access_level_by_members
                WHEN 'after-play' THEN 'read-after-play'
                WHEN 'anytime' THEN 'read-or-play'
                ELSE 'play-only' END
    """)
    op.create_check_constraint(
        op.f("ck_tournament_packet_assignments_e7dbbad4676e"),
        "tournament_packet_assignments",
        "access_level_by_members IN "
        "('no-access', 'play-only', 'read-after-play', 'read-or-play')",
    )
    op.add_column("tournament_packet_entitlements", sa.Column("access_level", sa.String(24)))
    op.execute("""
        UPDATE tournament_packet_entitlements SET access_level = CASE
            WHEN playable THEN 'play-only' WHEN playable = false THEN 'no-access' ELSE NULL END,
            playable = coalesce(playable, false)
    """)
    op.alter_column("tournament_packet_entitlements", "playable",
                    existing_type=sa.Boolean(), nullable=False)
    op.create_check_constraint(
        op.f("ck_tournament_packet_entitlements_b7803cf5f3c0"),
        "tournament_packet_entitlements",
        "access_level IS NULL OR access_level IN "
        "('no-access', 'play-only', 'read-after-play', 'read-or-play')",
    )
    op.execute("""
        UPDATE tournament_policy_versions SET policies =
            (policies - 'library_viewing_rule_default') || jsonb_build_object(
                'packet_access_rule_default', CASE policies->>'library_viewing_rule_default'
                    WHEN 'never' THEN 'play-only'
                    WHEN 'anytime' THEN 'read-or-play'
                    ELSE 'read-after-play' END)
    """)
