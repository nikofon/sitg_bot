"""Immutable authenticated raster attachments, separate from fact text and sources."""

from alembic import op

revision = "0026_knowledge_images"
down_revision = "0025_knowledge_catalogue"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE knowledge_images (
            id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES players(id),
            content_type VARCHAR(32) NOT NULL, data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (content_type IN ('image/png', 'image/jpeg', 'image/webp')),
            CHECK (octet_length(data) BETWEEN 1 AND 2097152)
        )
    """)
    op.execute("CREATE INDEX ix_knowledge_images_owner_id ON knowledge_images(owner_id)")
    op.execute(
        "ALTER TABLE knowledge_facts ADD COLUMN image_ids JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade():
    op.drop_column("knowledge_facts", "image_ids")
    op.drop_table("knowledge_images")
