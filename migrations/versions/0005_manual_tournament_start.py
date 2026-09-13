"""Manual tournament starts, scheduled reminders, and inherited round access."""

import sqlalchemy as sa
from alembic import op

revision = "0005_manual_tournament_start"
down_revision = "0004_merge_classic_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tournaments", sa.Column("actual_starts_at", sa.DateTime(timezone=True)))
    op.add_column("tournaments", sa.Column("start_reminded_for", sa.DateTime(timezone=True)))
    # Preserve tournaments that already have a started competition or assigned games.
    op.execute("""
        UPDATE tournaments t SET actual_starts_at = COALESCE(
            (SELECT min(started_at) FROM classic_stages s WHERE s.tournament_id = t.id),
            (SELECT min(created_at) FROM games g WHERE g.tournament_id = t.id),
            t.actual_ends_at
        )
    """)
    op.drop_constraint("ck_tournaments_9661fd0dd466", "tournaments", type_="check")
    op.create_check_constraint(
        "ck_tournaments_actual_finish_after_start", "tournaments",
        "actual_ends_at IS NULL OR actual_starts_at IS NULL "
        "OR actual_ends_at >= actual_starts_at",
    )
    for field in ("discoverable", "playable"):
        op.alter_column("classic_rounds", field, existing_type=sa.Boolean(), nullable=True)
        op.execute(sa.text(f"""
            UPDATE classic_rounds r SET {field} = NULL FROM classic_stages s
            WHERE r.stage_id = s.id AND s.started_at IS NULL AND r.{field} = false
        """))


def downgrade() -> None:
    for field in ("discoverable", "playable"):
        op.execute(sa.text(f"UPDATE classic_rounds SET {field} = false WHERE {field} IS NULL"))
        op.alter_column("classic_rounds", field, existing_type=sa.Boolean(), nullable=False)
    op.drop_constraint("ck_tournaments_actual_finish_after_start", "tournaments", type_="check")
    # The old schema cannot represent completion before the planned start.
    op.execute("""
        UPDATE tournaments SET starts_at = NULL
        WHERE actual_ends_at < starts_at
    """)
    op.create_check_constraint(
        "ck_tournaments_9661fd0dd466", "tournaments",
        "actual_ends_at IS NULL OR starts_at IS NULL OR actual_ends_at >= starts_at",
    )
    op.drop_column("tournaments", "start_reminded_for")
    op.drop_column("tournaments", "actual_starts_at")
