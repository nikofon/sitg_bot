"""Bound public bare cache lookup by exact source and execution profile."""

import sqlalchemy as sa
from alembic import op

revision = "0030_public_analysis_cache"
down_revision = "0029_knowledge_review"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_imports", sa.Column("creation_key_hash", sa.String(64)))
    op.create_unique_constraint(
        "uq_knowledge_imports_owner_id_creator_id_creation_key_hash",
        "knowledge_imports",
        ["owner_id", "creator_id", "creation_key_hash"],
    )
    op.create_index(
        "ix_training_analysis_public_source",
        "training_analysis_artifacts",
        ["source_fingerprint", "model", "prompt_version", "schema_version", "tokenizer_version"],
        postgresql_where=sa.text("shared AND kind = 'dissection' AND status = 'succeeded'"),
    )


def downgrade():
    op.drop_index("ix_training_analysis_public_source", "training_analysis_artifacts")
    op.drop_constraint(
        "uq_knowledge_imports_owner_id_creator_id_creation_key_hash",
        "knowledge_imports",
        type_="unique",
    )
    op.drop_column("knowledge_imports", "creation_key_hash")
