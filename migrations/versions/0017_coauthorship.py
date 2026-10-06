"""Add equal coauthors and preserve question authorship inheritance."""

import sqlalchemy as sa
from alembic import op

revision = "0017_coauthorship"
down_revision = "0016_zero_points_packet_blocks"
branch_labels = None
depends_on = None

TABLES = {
    "theme_coauthors": "themes",
    "theme_revision_coauthors": "theme_revisions",
    "question_coauthors": "logical_questions",
    "question_revision_coauthors": "question_revisions",
}


def upgrade() -> None:
    for table, parent in TABLES.items():
        op.create_table(
            table,
            sa.Column("content_id", sa.Uuid(), sa.ForeignKey(f"{parent}.id"), primary_key=True),
            sa.Column("author_id", sa.Uuid(), sa.ForeignKey("authors.id"), primary_key=True),
            sa.Column("position", sa.Integer(), nullable=False),
        )
        op.create_index(f"ix_{table}_author_id", table, ["author_id"])
    op.add_column("question_revisions", sa.Column(
        "inherits_theme_authors", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    # Legacy revisions did not record inheritance. Recover explicit JSON authors
    # from their saved drafts where possible; otherwise retain the old equality rule.
    op.execute("""
        UPDATE question_revisions q SET inherits_theme_authors = true
        FROM packet_questions p
        JOIN theme_revisions t ON t.id = p.theme_revision_id
        JOIN packet_versions v ON v.id = p.packet_version_id
        JOIN packet_drafts d ON d.id = v.source_draft_id
        WHERE q.id = p.question_revision_id
          AND q.author_id IS NOT DISTINCT FROM t.author_id
          AND COALESCE(d.content #>> ARRAY['themes', (t.position - 1)::text,
              'questions', (p.position - 1)::text, 'author'], '') = ''
    """)


def downgrade() -> None:
    # Refuse to silently discard coauthor identities.
    for table in TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("Cannot downgrade while coauthored content exists")
    op.drop_column("question_revisions", "inherits_theme_authors")
    for table in reversed(TABLES):
        op.drop_table(table)
