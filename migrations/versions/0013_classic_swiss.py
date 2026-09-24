"""Configurable Swiss first stages."""

import sqlalchemy as sa
from alembic import op

revision = "0013_classic_swiss"
down_revision = "0012_appeals_per_answer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("classic_stages", sa.Column("round_count", sa.Integer()))
    op.add_column("classic_stages", sa.Column("players_per_game", sa.Integer()))
    op.drop_constraint("ck_classic_stages_eeaeeeda41eb", "classic_stages", type_="check")
    op.create_check_constraint(
        "ck_classic_stages_ef5ba5f92ccb", "classic_stages",
        "stage_type IN ('none', 'groups', 'quiz', 'swiss', 'playoff')",
    )
    op.create_check_constraint(
        "swiss_configuration", "classic_stages",
        "stage_type != 'swiss' OR (kind = 'first' AND round_count IS NOT NULL "
        "AND round_count >= 1 AND players_per_game IS NOT NULL "
        "AND players_per_game BETWEEN 2 AND 12)",
    )


def downgrade() -> None:
    # Refuse downgrade while Swiss stages exist rather than discarding their state.
    op.create_check_constraint(
        "ck_classic_stages_eeaeeeda41eb", "classic_stages",
        "stage_type IN ('none', 'groups', 'quiz', 'playoff')",
    )
    op.drop_constraint("swiss_configuration", "classic_stages", type_="check")
    op.drop_constraint("ck_classic_stages_ef5ba5f92ccb", "classic_stages", type_="check")
    op.drop_column("classic_stages", "players_per_game")
    op.drop_column("classic_stages", "round_count")
