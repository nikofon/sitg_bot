"""Index the revision-scoped normal-play history aggregates without blocking writes.

Revision ID: 0032_play_history_revision_index
Revises: 0031_ai_administration
"""

from alembic import op

revision = "0032_play_history_revision_index"
down_revision = "0031_ai_administration"
branch_labels = None
depends_on = None

INDEX = "ix_question_rounds_question_revision"


def upgrade():
    # Child round_id joins already have leading UNIQUE indexes. Only the
    # revision filter needs a new path. PostgreSQL requires concurrent DDL
    # outside a transaction; Alembic commits earlier migrations at this point.
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX, "question_rounds", ["question_revision_id"], postgresql_concurrently=True,
        )


def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name="question_rounds", postgresql_concurrently=True)
