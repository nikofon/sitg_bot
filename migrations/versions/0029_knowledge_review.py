"""Fact attribution, one-time palettes and owner-private import reviews."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0029_knowledge_review"
down_revision = "0028_training_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_facts", sa.Column("author", sa.String(300), nullable=False, server_default="")
    )
    op.create_table(
        "knowledge_tag_seeds",
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), primary_key=True),
    )
    op.create_table(
        "knowledge_imports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("creator_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "dissection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_dissection_requests.id"),
            unique=True,
        ),
        sa.Column("candidates", JSONB(), nullable=False),
        sa.Column("decision", JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("type IN ('dissection', 'collection')"),
        sa.CheckConstraint("status IN ('working', 'pending', 'approved', 'rejected', 'failed')"),
    )
    op.create_index("ix_knowledge_imports_owner_id", "knowledge_imports", ["owner_id"])


def downgrade():
    op.drop_table("knowledge_imports")
    op.drop_table("knowledge_tag_seeds")
    op.drop_column("knowledge_facts", "author")
