"""Allow optional SI zero-point questions and add player packet blocks."""

import sqlalchemy as sa
from alembic import op

revision = "0016_zero_points_and_packet_blocks"
down_revision = "0015_subscription_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_packet_questions_c0d1729bd8b9", "packet_questions", type_="check")
    op.create_check_constraint("ck_packet_questions_f186ca47eaf0", "packet_questions", "value >= 0")
    op.drop_constraint(
        "ck_si_player_question_suspicion_metrics_9b25fdf55900",
        "si_player_question_suspicion_metrics", type_="check",
    )
    op.create_check_constraint(
        "ck_si_player_question_suspicion_metrics_618096806f13",
        "si_player_question_suspicion_metrics", "question_value >= 0",
    )
    op.create_table(
        "player_packet_blocks",
        sa.Column("player_id", sa.Uuid(), primary_key=True),
        sa.Column("packet_id", sa.Uuid(), primary_key=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.ForeignKeyConstraint(["packet_id"], ["logical_packets.id"]),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("player_packet_blocks")
    # Refuse downgrade if zero-point content or gameplay facts would become invalid.
    op.create_check_constraint("ck_packet_questions_c0d1729bd8b9", "packet_questions", "value > 0")
    op.create_check_constraint(
        "ck_si_player_question_suspicion_metrics_9b25fdf55900",
        "si_player_question_suspicion_metrics", "question_value > 0",
    )
    op.drop_constraint("ck_packet_questions_f186ca47eaf0", "packet_questions", type_="check")
    op.drop_constraint(
        "ck_si_player_question_suspicion_metrics_618096806f13",
        "si_player_question_suspicion_metrics", type_="check",
    )
