"""Index technical retention candidates and record payload compaction."""

import sqlalchemy as sa
from alembic import op

revision = "0020_database_maintenance"
down_revision = "0019_lobby_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("durable_jobs", "outbox_events"):
        op.add_column(table, sa.Column("payload_pruned_at", sa.DateTime(timezone=True)))
    for table, status, timestamp in (
        ("durable_jobs", "succeeded", "completed_at"),
        ("durable_jobs", "failed", "failed_at"),
        ("outbox_events", "delivered", "delivered_at"),
        ("outbox_events", "failed", "failed_at"),
    ):
        predicate = f"status = '{status}' AND payload_pruned_at IS NULL"
        if table == "outbox_events":
            predicate += " AND topic <> 'telegram.chat.message'"
        op.create_index(
            f"ix_{table}_maintenance_{status}", table, [timestamp, "id"],
            postgresql_where=sa.text(predicate),
        )
    op.create_index(
        "ix_application_request_audit_maintenance", "application_request_audit",
        ["outcome", "completed_at", "id"],
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_application_request_audit_maintenance", "application_request_audit")
    for table, statuses in (
        ("outbox_events", ("delivered", "failed")),
        ("durable_jobs", ("succeeded", "failed")),
    ):
        for status in statuses:
            op.drop_index(f"ix_{table}_maintenance_{status}", table)
        op.drop_column(table, "payload_pruned_at")
