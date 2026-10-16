"""Make knowledge sharing permanent copies and meter analysis by theme."""

import json

from alembic import op
from sqlalchemy import text

revision = "0023_copy_sharing_theme_quotas"
down_revision = "0022_training_knowledge_ai"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE knowledge_fact_copies (
            source_fact_id UUID NOT NULL, 
            recipient_id UUID NOT NULL, 
            owner_id UUID NOT NULL, 
            fact_id UUID NOT NULL, 
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
            PRIMARY KEY (source_fact_id, recipient_id), 
            CONSTRAINT ck_knowledge_fact_copies_045e3989e370 CHECK (owner_id <> recipient_id), 
            FOREIGN KEY(source_fact_id, owner_id) REFERENCES knowledge_facts (id, owner_id), 
            FOREIGN KEY(fact_id, recipient_id) REFERENCES knowledge_facts (id, owner_id), 
            FOREIGN KEY(recipient_id) REFERENCES players (id), 
            FOREIGN KEY(owner_id) REFERENCES players (id), 
            UNIQUE (fact_id)
        )
    """)
    op.execute("CREATE INDEX ix_knowledge_fact_copies_owner_id ON knowledge_fact_copies (owner_id)")
    op.create_index(
        "ix_knowledge_copies_recipient_owner", "knowledge_fact_copies", ["recipient_id", "owner_id"]
    )
    # Snapshot legacy active shares before inserting any copies, including cycles.
    op.execute("""
        CREATE TEMPORARY TABLE legacy_fact_copies ON COMMIT DROP AS
        SELECT fact.id AS source_fact_id, fact.owner_id, share.recipient_id,
               gen_random_uuid() AS fact_id
        FROM knowledge_facts fact
        JOIN knowledge_sharing_grants share ON share.owner_id = fact.owner_id
        WHERE share.revoked_at IS NULL AND fact.deleted_at IS NULL
    """)
    op.execute("""
        INSERT INTO knowledge_facts
            (id, owner_id, title, title_key, description, links, provenance,
             content_fingerprint, content_version, statistic_status, created_at, updated_at)
        SELECT copy.fact_id, copy.recipient_id, fact.title, fact.title_key, fact.description,
               fact.links, '{"origin":"manual"}'::jsonb, fact.content_fingerprint, 1,
               'none', now(), now()
        FROM legacy_fact_copies copy JOIN knowledge_facts fact ON fact.id = copy.source_fact_id
    """)
    op.execute("""
        INSERT INTO knowledge_fact_copies (source_fact_id, recipient_id, owner_id, fact_id)
        SELECT source_fact_id, recipient_id, owner_id, fact_id FROM legacy_fact_copies
    """)
    op.execute("""
        INSERT INTO knowledge_tags (id, owner_id, name)
        SELECT gen_random_uuid(), recipient_id, name FROM (
            SELECT DISTINCT copy.recipient_id, tag.name
            FROM legacy_fact_copies copy
            JOIN knowledge_fact_tags association ON association.fact_id = copy.source_fact_id
            JOIN knowledge_tags tag ON tag.id = association.tag_id
        ) names ON CONFLICT (owner_id, name) DO NOTHING
    """)
    op.execute("""
        INSERT INTO knowledge_fact_tags (fact_id, tag_id, owner_id)
        SELECT copy.fact_id, recipient_tag.id, copy.recipient_id
        FROM legacy_fact_copies copy
        JOIN knowledge_fact_tags association ON association.fact_id = copy.source_fact_id
        JOIN knowledge_tags source_tag ON source_tag.id = association.tag_id
        JOIN knowledge_tags recipient_tag ON recipient_tag.owner_id = copy.recipient_id
                                          AND recipient_tag.name = source_tag.name
    """)
    op.execute("""
        INSERT INTO knowledge_statistic_snapshots
            (id, fact_id, owner_id, content_version, mentions, common, bingo)
        SELECT gen_random_uuid(), copy.fact_id, copy.recipient_id, 1,
               snapshot.mentions, snapshot.common, snapshot.bingo
        FROM legacy_fact_copies copy JOIN knowledge_facts source ON source.id = copy.source_fact_id
        JOIN LATERAL (
            SELECT * FROM knowledge_statistic_snapshots
            WHERE fact_id = source.id AND content_version = source.content_version
            ORDER BY created_at DESC, id DESC LIMIT 1
        ) snapshot ON source.statistic_status = 'fresh'
    """)
    op.execute("""
        UPDATE knowledge_facts fact SET statistic_status = 'fresh'
        FROM legacy_fact_copies copy
        WHERE fact.id = copy.fact_id AND EXISTS (
            SELECT 1 FROM knowledge_statistic_snapshots snapshot WHERE snapshot.fact_id = fact.id
        )
    """)
    op.execute("UPDATE knowledge_sharing_grants SET revoked_at = now() WHERE revoked_at IS NULL")
    op.execute("""
        UPDATE training_analysis_requests
        SET source = source || jsonb_build_object('quota_units', 1)
        WHERE NOT source ? 'quota_units'
    """)

    # Upgrade only our exact former free defaults, preserving custom categories.
    features = ("dissection", "common_knowledge", "best_buzz")
    old_disabled = {
        name: {"enabled": False, "limit": None, "reset_period": "none"} for name in features
    }
    old_enabled = {
        name: {
            "enabled": True,
            "limit": 50 if name == "common_knowledge" else 10,
            "reset_period": "daily",
            "cache_hit_charging": False,
            "queued_request_cap": 3,
            "in_flight_request_cap": 2,
        }
        for name in features
    }
    connection = op.get_bind()
    rows = connection.execute(
        text("""
        SELECT category.id, policy.features FROM training_categories category
        JOIN training_feature_policies policy ON policy.version = category.policy_version
        WHERE category.code = 'default' AND category.is_default
    """)
    ).all()
    for category_id, previous in rows:
        if previous not in (old_disabled, old_enabled):
            continue
        replacement = {
            name: {
                "enabled": previous == old_enabled,
                "limit": 50 if name == "common_knowledge" else 24,
                "reset_period": "daily",
                "cache_hit_charging": False,
                "queued_request_cap": 3,
                "in_flight_request_cap": 2,
            }
            for name in features
        }
        version = connection.scalar(
            text("SELECT coalesce(max(version), 0) + 1 FROM training_feature_policies")
        )
        connection.execute(
            text("""
            INSERT INTO training_feature_policies (id, version, features)
            VALUES (gen_random_uuid(), :version, CAST(:features AS jsonb))
        """),
            {"version": version, "features": json.dumps(replacement)},
        )
        connection.execute(
            text("""
            UPDATE training_categories SET policy_version = :version,
                display_name = CASE WHEN display_name = 'Default'
                                    THEN 'Basic free' ELSE display_name END
            WHERE id = :category_id
        """),
            {"version": version, "category_id": category_id},
        )


def downgrade():
    # Independent facts belong to recipients permanently, including after rollback.
    op.drop_table("knowledge_fact_copies")
