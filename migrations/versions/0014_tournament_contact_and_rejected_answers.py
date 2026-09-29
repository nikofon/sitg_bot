"""Tournament organizer contacts, tournament channel, and unaccepted answers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_tournament_contact_and_rejected_answers"
down_revision = "0013_classic_swiss"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tournaments", sa.Column("organizer_contacts", sa.Text(), nullable=True))
    op.execute("UPDATE tournaments SET organizer_contacts = '' WHERE organizer_contacts IS NULL")
    op.alter_column(
        "tournaments", "organizer_contacts", existing_type=sa.Text(), nullable=False
    )
    op.add_column("tournaments", sa.Column("channel", sa.Text(), nullable=True))
    op.execute("UPDATE tournaments SET channel = '' WHERE channel IS NULL")
    op.alter_column("tournaments", "channel", existing_type=sa.Text(), nullable=False)
    op.add_column(
        "question_revisions",
        sa.Column("rejected_answers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        "UPDATE question_revisions SET rejected_answers = '[]'::jsonb "
        "WHERE rejected_answers IS NULL"
    )
    op.alter_column(
        "question_revisions",
        "rejected_answers",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("question_revisions", "rejected_answers")
    op.drop_column("tournaments", "channel")
    op.drop_column("tournaments", "organizer_contacts")