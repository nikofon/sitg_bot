"""Durable knowledge links, recipient hash claims and coloured tags."""

from alembic import op

revision = "0025_knowledge_catalogue"
down_revision = "0024_training_lobby"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE knowledge_tags ADD COLUMN colour VARCHAR(7) NOT NULL DEFAULT '#64748b'")
    op.execute("""
        ALTER TABLE knowledge_tags ADD CONSTRAINT ck_knowledge_tag_colour
        CHECK (colour ~ '^#[0-9a-f]{6}$')
    """)
    op.execute("""
        CREATE TABLE knowledge_share_links (
            id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES players(id),
            token VARCHAR(64) NOT NULL UNIQUE, creation_key_hash VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), deactivated_at TIMESTAMPTZ,
            UNIQUE (owner_id, creation_key_hash)
        )
    """)
    op.execute("CREATE INDEX ix_knowledge_share_links_owner_id ON knowledge_share_links(owner_id)")
    op.execute("""
        CREATE TABLE knowledge_import_hashes (
            recipient_id UUID NOT NULL REFERENCES players(id),
            content_fingerprint VARCHAR(64) NOT NULL,
            PRIMARY KEY (recipient_id, content_fingerprint)
        )
    """)
    # Preserve prior imports, including edited/deleted copies, without deleting any facts.
    op.execute("""
        INSERT INTO knowledge_import_hashes (recipient_id, content_fingerprint)
        SELECT copy.recipient_id, fact.content_fingerprint
        FROM knowledge_fact_copies copy JOIN knowledge_facts fact ON fact.id = copy.fact_id
        UNION
        SELECT copy.recipient_id, fact.content_fingerprint
        FROM knowledge_fact_copies copy JOIN knowledge_facts fact ON fact.id = copy.source_fact_id
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        CREATE INDEX ix_knowledge_facts_owner_fingerprint
        ON knowledge_facts(owner_id, content_fingerprint)
    """)
    op.execute("ALTER TABLE knowledge_dissection_requests ADD COLUMN theme_revision_ids JSONB")
    op.execute("""
        UPDATE knowledge_dissection_requests dissection
        SET theme_revision_ids = coverage.themes
        FROM (
            SELECT dissection.id, jsonb_agg(DISTINCT question.theme_revision_id::text) AS themes
            FROM knowledge_dissection_requests dissection
            JOIN training_analysis_requests request
                ON request.request_id = dissection.analysis_request_id
            JOIN packet_questions question
                ON question.packet_version_id::text = dissection.packet_revision
                AND (request.source->'question_revision_ids') ? question.question_revision_id::text
            WHERE dissection.status = 'succeeded' AND dissection.application IS NOT NULL
            GROUP BY dissection.id
        ) coverage WHERE coverage.id = dissection.id
    """)
    op.execute("""
        CREATE TABLE training_best_buzz_completions (
            owner_id UUID NOT NULL REFERENCES players(id),
            packet_version_id UUID NOT NULL REFERENCES packet_versions(id),
            calculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_id, packet_version_id)
        )
    """)
    op.execute("""
        INSERT INTO training_best_buzz_completions (owner_id, packet_version_id, calculated_at)
        SELECT request.owner_player_id, version.id, min(request.completed_at)
        FROM training_analysis_requests request JOIN packet_versions version
            ON version.id::text = request.source->>'packet_revision_id'
        WHERE request.kind = 'best_buzz' AND request.status = 'succeeded'
        GROUP BY request.owner_player_id, version.id
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    op.drop_table("training_best_buzz_completions")
    op.drop_column("knowledge_dissection_requests", "theme_revision_ids")
    op.drop_index("ix_knowledge_facts_owner_fingerprint", table_name="knowledge_facts")
    op.drop_table("knowledge_import_hashes")
    op.drop_table("knowledge_share_links")
    op.drop_constraint("ck_knowledge_tag_colour", "knowledge_tags", type_="check")
    op.drop_column("knowledge_tags", "colour")
