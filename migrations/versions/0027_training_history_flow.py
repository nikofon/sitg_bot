"""Training playback evidence, accepted-buzz timing, solo appeals and lobby presentation."""

from alembic import op

revision = "0027_training_history_flow"
down_revision = "0026_knowledge_images"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE training_questions
            ADD COLUMN opened_at TIMESTAMPTZ,
            ADD COLUMN buzzed_at TIMESTAMPTZ,
            ADD COLUMN buzz_delay_seconds NUMERIC,
            ADD COLUMN buzz_token_cursor INTEGER,
            ADD COLUMN paused_seconds NUMERIC NOT NULL DEFAULT 0
    """)
    # Never synthesize legacy playback or buzz timestamps from pinned questions,
    # disclosure receipts, attempt creation times or the final reveal cursor.
    op.execute("""
        ALTER TABLE training_sessions
            ADD COLUMN paused_at TIMESTAMPTZ,
            ADD COLUMN paused_hidden_remaining NUMERIC
    """)
    op.execute("""
        ALTER TABLE training_attempts
            ADD COLUMN original_correct BOOLEAN,
            ADD COLUMN appeal_status VARCHAR(16),
            ADD COLUMN appealed_at TIMESTAMPTZ,
            ADD COLUMN appeal_resolved_at TIMESTAMPTZ,
            ADD COLUMN appeal_was_paused BOOLEAN NOT NULL DEFAULT false,
            ADD CONSTRAINT ck_training_attempt_appeal_status CHECK (
                appeal_status IS NULL OR appeal_status IN ('voting', 'accepted', 'rejected')
            )
    """)
    # Before solo appeals existed, the stored verdict was the original verdict.
    # NULL remains unknown/neutral; do not invent an outcome for legacy records.
    op.execute("UPDATE training_attempts SET original_correct = correct")
    op.execute("""
        ALTER TABLE players ADD COLUMN training_lobby_presentation
            JSONB NOT NULL DEFAULT '{}'::jsonb
    """)
    op.execute("""
        CREATE INDEX ix_training_sessions_owner_created
        ON training_sessions(owner_id, created_at, id)
    """)
    op.execute("""
        CREATE INDEX ix_training_questions_revision
        ON training_questions(question_revision_id)
    """)
    op.drop_index("ix_training_deadline", table_name="training_sessions")
    op.execute("""
        CREATE INDEX ix_training_deadline ON training_sessions(deadline)
        WHERE status IN ('active', 'paused')
    """)


def downgrade():
    op.drop_index("ix_training_deadline", table_name="training_sessions")
    op.execute("""
        CREATE INDEX ix_training_deadline ON training_sessions(deadline)
        WHERE status = 'active'
    """)
    op.drop_index("ix_training_questions_revision", table_name="training_questions")
    op.drop_index("ix_training_sessions_owner_created", table_name="training_sessions")
    op.drop_column("players", "training_lobby_presentation")
    op.drop_constraint("ck_training_attempt_appeal_status", "training_attempts", type_="check")
    for column in ("appeal_was_paused", "appeal_resolved_at", "appealed_at",
                   "appeal_status", "original_correct"):
        op.drop_column("training_attempts", column)
    for column in ("paused_hidden_remaining", "paused_at"):
        op.drop_column("training_sessions", column)
    for column in (
        "paused_seconds", "buzz_token_cursor", "buzz_delay_seconds", "buzzed_at", "opened_at"
    ):
        op.drop_column("training_questions", column)
