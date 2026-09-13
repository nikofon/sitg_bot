"""Tournament moderation and append-only global rating reversals."""

import sqlalchemy as sa
from alembic import op

revision = "0006_admin_management"
down_revision = "0005_manual_tournament_start"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column("moderation_status", sa.String(24), nullable=False, server_default="normal"),
    )
    op.add_column(
        "tournaments", sa.Column("moderated_by_id", sa.UUID(), sa.ForeignKey("players.id"))
    )
    op.add_column("tournaments", sa.Column("moderated_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        None, "tournaments", "moderation_status IN ('normal', 'halted', 'abolished')"
    )
    op.drop_constraint(
        "ruleset_rating_ledger_game_id_player_id_key", "ruleset_rating_ledger", type_="unique"
    )
    op.create_unique_constraint(
        "ruleset_rating_ledger_game_player_reason_key",
        "ruleset_rating_ledger",
        ["game_id", "player_id", "reason"],
    )


def downgrade() -> None:
    # Reversal facts must not be deleted to fit the old uniqueness constraint.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM tournaments WHERE moderation_status <> 'normal')")
    ):
        raise RuntimeError("Resolve tournament moderation before downgrading")
    op.drop_constraint(
        "ruleset_rating_ledger_game_player_reason_key", "ruleset_rating_ledger", type_="unique"
    )
    op.create_unique_constraint(
        "ruleset_rating_ledger_game_id_player_id_key",
        "ruleset_rating_ledger",
        ["game_id", "player_id"],
    )
    op.drop_column("tournaments", "moderated_at")
    op.drop_column("tournaments", "moderated_by_id")
    op.drop_column("tournaments", "moderation_status")
