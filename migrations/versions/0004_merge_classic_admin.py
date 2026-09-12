"""Merge Classic tournament and administration migrations."""

revision = "0004_merge_classic_admin"
down_revision = ("0003_classic_tournaments", "0003_player_bans_bug_reports")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
