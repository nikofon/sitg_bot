"""Durable training progression and ordered presentation events."""

from hashlib import sha256

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0028_training_lifecycle"
down_revision = "0027_training_history_flow"
branch_labels = None
depends_on = None

OLD_PHASE = "phase IN ('reading', 'waiting', 'answering', 'revealed', 'best_buzz')"


def upgrade():
    op.drop_constraint(
        "ck_training_sessions_" + sha256(OLD_PHASE.encode()).hexdigest()[:12],
        "training_sessions", type_="check",
    )
    op.create_check_constraint(
        "ck_training_session_phase", "training_sessions",
        "phase IN ('reading', 'waiting', 'answering', 'revealed', 'best_buzz', 'intermission')",
    )
    op.add_column("training_sessions", sa.Column("progression_stage", sa.String(32)))
    op.add_column("training_sessions", sa.Column(
        "event_sequence", sa.Integer(), nullable=False, server_default="0",
    ))
    op.create_table(
        "training_events",
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("training_sessions.id"),
                  primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("parameters", JSONB(), nullable=False),
    )
    # Existing revealed sessions must no longer require a manual Next to finish.
    # Other legacy phases recover lazily through their existing persisted deadlines.
    op.execute("""
        UPDATE training_sessions SET deadline = now()
        WHERE status = 'active' AND phase = 'revealed' AND deadline IS NULL
    """)
    op.execute("""
        UPDATE training_sessions SET paused_remaining = 3
        WHERE status = 'paused' AND phase = 'revealed' AND paused_remaining IS NULL
    """)


def downgrade():
    op.drop_table("training_events")
    op.execute("""
        UPDATE training_sessions SET phase = 'reading', deadline = now()
        WHERE phase = 'intermission'
    """)
    op.drop_column("training_sessions", "event_sequence")
    op.drop_column("training_sessions", "progression_stage")
    op.drop_constraint("ck_training_session_phase", "training_sessions", type_="check")
    op.create_check_constraint(
        "ck_training_sessions_" + sha256(OLD_PHASE.encode()).hexdigest()[:12],
        "training_sessions", OLD_PHASE,
    )
