"""Allow different answers to be appealed after an earlier appeal is rejected."""

from alembic import op

revision = "0012_appeals_per_answer"
down_revision = "0011_merge_chats_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("appeals_game_id_round_id_key", "appeals", type_="unique")
    op.create_unique_constraint(
        "appeals_game_id_target_attempt_id_key", "appeals", ["game_id", "target_attempt_id"]
    )


def downgrade() -> None:
    # Fails rather than discarding appeals if a question has multiple records.
    op.create_unique_constraint("appeals_game_id_round_id_key", "appeals", ["game_id", "round_id"])
    op.drop_constraint("appeals_game_id_target_attempt_id_key", "appeals", type_="unique")
