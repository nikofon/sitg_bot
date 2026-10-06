"""Persist the shared appeal selection and voting deadline."""

import sqlalchemy as sa
from alembic import op

revision = "0018_appeal_selection_timer"
down_revision = "0017_coauthorship"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("games", sa.Column("appeal_selection_deadline", sa.DateTime(timezone=True)))
    op.add_column("games", sa.Column("appeal_selection_player_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_games_appeal_selection_player_id_players", "games", "players",
        ["appeal_selection_player_id"], ["id"],
    )
    op.add_column("games", sa.Column(
        "appeal_selection_was_paused", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))


def downgrade() -> None:
    op.drop_column("games", "appeal_selection_was_paused")
    op.drop_constraint("fk_games_appeal_selection_player_id_players", "games", type_="foreignkey")
    op.drop_column("games", "appeal_selection_player_id")
    op.drop_column("games", "appeal_selection_deadline")
