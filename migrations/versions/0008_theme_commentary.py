"""Theme commentary and tournament descriptions."""

import sqlalchemy as sa
from alembic import op

revision = "0008_theme_commentary"
down_revision = "0007_registration_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("theme_revisions", sa.Column("commentary", sa.Text(), nullable=True))
    op.execute("UPDATE theme_revisions SET commentary = '' WHERE commentary IS NULL")
    op.alter_column("theme_revisions", "commentary", existing_type=sa.Text(), nullable=False)
    op.add_column("tournaments", sa.Column("description", sa.Text(), nullable=True))
    op.execute("UPDATE tournaments SET description = '' WHERE description IS NULL")
    op.alter_column("tournaments", "description", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("ck_games_a1f33c7c9c3b", "games", type_="check")
    op.create_check_constraint(
        "ck_games_a1f33c7c9c3b",
        "games",
        "progression_stage IS NULL OR progression_stage IN "
        "('ready_countdown', 'theme_start', 'theme_commentary', 'question_start', "
        "'next_question', 'theme_complete', 'theme_scoreboard', 'question_reveal_start', "
        "'question_reveal', 'buzz_timer_start', 'finish')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_games_a1f33c7c9c3b", "games", type_="check")
    op.create_check_constraint(
        "ck_games_a1f33c7c9c3b",
        "games",
        "progression_stage IS NULL OR progression_stage IN "
        "('ready_countdown', 'theme_start', 'question_start', 'next_question', "
        "'theme_complete', 'theme_scoreboard', 'question_reveal_start', "
        "'question_reveal', 'buzz_timer_start', 'finish')",
    )
    op.drop_column("tournaments", "description")
    op.drop_column("theme_revisions", "commentary")