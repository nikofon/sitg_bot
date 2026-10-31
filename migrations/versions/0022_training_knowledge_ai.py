"""Persist knowledge collections and training analysis.

Revision ID: 0022_training_knowledge_ai
Revises: 0021_training
"""

from alembic import op

revision = "0022_training_knowledge_ai"
down_revision = "0021_training"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE training_categories (\n\tid UUID NOT NULL, "
        "\n\tcode VARCHAR(50) NOT NULL, \n\tdisplay_name VARCHAR(200) NOT NULL, "
        "\n\tis_default BOOLEAN DEFAULT false NOT NULL, \n\tpolicy_version INTEGER NOT NULL, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_training_categories_code UNIQUE (code), "
        "\n\tCONSTRAINT ck_training_categories_252efd1328a0 CHECK (length(code) > 0), "
        "\n\tCONSTRAINT ck_training_categories_148ac0de727d CHECK (length(display_name) > 0), "
        "\n\tCONSTRAINT ck_training_categories_f57e5d3ed41a CHECK (policy_version >= 1)\n)"
    )
    op.execute(
        "CREATE TABLE training_feature_policies (\n\tid UUID NOT NULL, "
        "\n\tversion INTEGER NOT NULL, \n\tfeatures JSONB NOT NULL, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (id), "
        "\n\tCONSTRAINT uq_training_feature_policies_version UNIQUE (version), "
        "\n\tCONSTRAINT ck_training_feature_policies_b093cc40900c CHECK (version >= 1)\n)"
    )
    op.execute(
        "CREATE TABLE training_policy_state (\n\tsingleton SERIAL NOT NULL, "
        "\n\tdefault_category_id UUID NOT NULL, "
        "\n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (singleton), "
        "\n\tCONSTRAINT ck_training_policy_state_160ec0da2dd0 CHECK (singleton = 1)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_facts (\n\tid UUID NOT NULL, \n\towner_id UUID NOT NULL, "
        "\n\ttitle VARCHAR(200) NOT NULL, \n\ttitle_key VARCHAR(400) NOT NULL, "
        "\n\tdescription TEXT NOT NULL, \n\tlinks JSONB NOT NULL, \n\tprovenance JSONB NOT NULL, "
        "\n\tcontent_fingerprint VARCHAR(64) NOT NULL, \n\tcontent_version INTEGER NOT NULL, "
        "\n\tapplied_candidate_id VARCHAR(19), \n\tapplied_content_version INTEGER, "
        "\n\tstatistic_status VARCHAR(20) NOT NULL, \n\tdeleted_at TIMESTAMP WITH TIME ZONE, "
        "\n\tmerged_into_id UUID, "
        "\n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (id), \n\tUNIQUE (id, owner_id), "
        "\n\tCONSTRAINT ck_knowledge_facts_e504701ff7cf CHECK (content_version >= 1), "
        "\n\tCONSTRAINT ck_knowledge_facts_93b4dfd197d5 "
        "CHECK (length(title) BETWEEN 1 AND 200 AND length(description) BETWEEN 1 AND 4000), "
        "\n\tCONSTRAINT ck_knowledge_facts_edfd17a8d42a "
        "CHECK (statistic_status IN ('none', 'pending', 'fresh', 'stale')), "
        "\n\tFOREIGN KEY(owner_id) REFERENCES players (id), "
        "\n\tFOREIGN KEY(merged_into_id) REFERENCES knowledge_facts (id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_sharing_grants (\n\tid UUID NOT NULL, "
        "\n\towner_id UUID NOT NULL, \n\trecipient_id UUID NOT NULL, "
        "\n\tgranted_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "\n\trevoked_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), "
        "\n\tUNIQUE (owner_id, recipient_id), "
        "\n\tCONSTRAINT ck_knowledge_sharing_grants_045e3989e370 CHECK (owner_id <> recipient_id), "
        "\n\tFOREIGN KEY(owner_id) REFERENCES players (id), "
        "\n\tFOREIGN KEY(recipient_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_tags (\n\tid UUID NOT NULL, \n\towner_id UUID NOT NULL, "
        "\n\tname VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (owner_id, name), "
        "\n\tUNIQUE (id, owner_id), \n\tFOREIGN KEY(owner_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE TABLE training_analysis_artifacts (\n\tid UUID NOT NULL, "
        "\n\tartifact_key VARCHAR(64) NOT NULL, \n\tkind VARCHAR(30) NOT NULL, "
        "\n\tshared BOOLEAN DEFAULT false NOT NULL, \n\towner_player_id UUID, "
        "\n\tsource_fingerprint VARCHAR(64) NOT NULL, "
        "\n\tinstruction_digest VARCHAR(64) NOT NULL, \n\tlanguage VARCHAR(8) NOT NULL, "
        "\n\tmodel VARCHAR(100) NOT NULL, \n\tprompt_version VARCHAR(30) NOT NULL, "
        "\n\tschema_version VARCHAR(30) NOT NULL, \n\ttokenizer_version VARCHAR(30) NOT NULL, "
        "\n\tgeneration INTEGER DEFAULT 1 NOT NULL, "
        "\n\tstatus VARCHAR(24) DEFAULT 'pending' NOT NULL, "
        "\n\tpayload JSONB, \n\tcheckpoint JSONB, "
        "\n\tlease_token UUID, \n\tlease_expires_at TIMESTAMP WITH TIME ZONE, "
        "\n\tprovider_request_ids JSONB NOT NULL, \n\tprovider_usage JSONB, \n\tlast_error TEXT, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tfailed_at TIMESTAMP WITH TIME ZONE, "
        "\n\tPRIMARY KEY (id), "
        "\n\tCONSTRAINT uq_training_analysis_artifacts_key UNIQUE (artifact_key), "
        "\n\tCONSTRAINT ck_training_analysis_artifacts_e725ba5b3f51 "
        "CHECK (kind IN ('dissection', 'best_buzz', 'common_knowledge')), "
        "\n\tCONSTRAINT ck_training_analysis_artifacts_626f2e9b7a40 CHECK (generation >= 1), "
        "\n\tCONSTRAINT ck_training_analysis_artifacts_21e591b65a6a "
        "CHECK (status IN ('pending', 'running', 'succeeded', "
        "'partially_succeeded', 'failed', 'cancelled')), "
        "\n\tCONSTRAINT ck_training_analysis_artifacts_2233cd349861 "
        "CHECK ((shared AND owner_player_id IS NULL) "
        "OR (NOT shared AND owner_player_id IS NOT NULL)), "
        "\n\tCONSTRAINT ck_training_analysis_artifacts_02f372be3b4d "
        "CHECK ((status = 'running' AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR (status <> 'running' "
        "AND lease_token IS NULL AND lease_expires_at IS NULL)), "
        "\n\tCONSTRAINT ck_training_analysis_artifacts_bac7163b33d1 "
        "CHECK ((status IN ('succeeded', 'partially_succeeded') "
        "AND completed_at IS NOT NULL AND failed_at IS NULL) "
        "OR (status = 'failed' AND failed_at IS NOT NULL AND completed_at IS NULL) "
        "OR (status IN ('pending', 'running', 'cancelled') "
        "AND completed_at IS NULL AND failed_at IS NULL)), "
        "\n\tFOREIGN KEY(owner_player_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE TABLE training_category_assignments (\n\tplayer_id UUID NOT NULL, "
        "\n\tcategory_id UUID NOT NULL, "
        "\n\tassigned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (player_id), \n\tFOREIGN KEY(player_id) REFERENCES players (id), "
        "\n\tFOREIGN KEY(category_id) REFERENCES training_categories (id)\n)"
    )
    op.execute(
        "CREATE TABLE training_usage_periods (\n\tid UUID NOT NULL, "
        "\n\tplayer_id UUID NOT NULL, \n\tfeature VARCHAR(50) NOT NULL, "
        "\n\tperiod_start TIMESTAMP WITH TIME ZONE NOT NULL, "
        "\n\tperiod_end TIMESTAMP WITH TIME ZONE NOT NULL, "
        "\n\treserved INTEGER DEFAULT 0 NOT NULL, \n\tsettled INTEGER DEFAULT 0 NOT NULL, "
        "\n\tPRIMARY KEY (id), "
        "\n\tCONSTRAINT uq_training_usage_periods_scope UNIQUE (player_id, feature, period_start), "
        "\n\tCONSTRAINT ck_training_usage_periods_75ea60540553 "
        "CHECK (reserved >= 0 AND settled >= 0), "
        "\n\tCONSTRAINT ck_training_usage_periods_c05604b2fb2e CHECK (period_end > period_start), "
        "\n\tFOREIGN KEY(player_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_dissection_requests (\n\tid UUID NOT NULL, "
        "\n\towner_id UUID NOT NULL, \n\tpacket_id UUID NOT NULL, "
        "\n\tpacket_revision VARCHAR(36) NOT NULL, \n\tinstructions TEXT NOT NULL, "
        "\n\tcomment TEXT NOT NULL, \n\tinstruction_digest VARCHAR(16) NOT NULL, "
        "\n\tbase BOOLEAN NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tapplication JSONB, "
        "\n\tanalysis_request_id VARCHAR(300), "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (id), "
        "\n\tUNIQUE (owner_id, packet_id, packet_revision, instruction_digest), "
        "\n\tCONSTRAINT ck_knowledge_dissection_requests_1dcf4135a6ad "
        "CHECK (status IN ('pending', 'succeeded', 'partially_succeeded', 'failed')), "
        "\n\tFOREIGN KEY(owner_id) REFERENCES players (id), "
        "\n\tFOREIGN KEY(packet_id) REFERENCES logical_packets (id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_fact_tags (\n\tfact_id UUID NOT NULL, "
        "\n\ttag_id UUID NOT NULL, \n\towner_id UUID NOT NULL, \n\tPRIMARY KEY (fact_id, tag_id), "
        "\n\tFOREIGN KEY(fact_id, owner_id) REFERENCES knowledge_facts (id, owner_id) "
        "ON DELETE CASCADE, "
        "\n\tFOREIGN KEY(tag_id, owner_id) REFERENCES knowledge_tags (id, owner_id) "
        "ON DELETE CASCADE, \n\tFOREIGN KEY(owner_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_statistic_snapshots (\n\tid UUID NOT NULL, "
        "\n\tfact_id UUID NOT NULL, \n\towner_id UUID NOT NULL, "
        "\n\tcontent_version INTEGER NOT NULL, "
        "\n\tmentions JSONB, \n\tcommon JSONB, \n\tbingo JSONB, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tPRIMARY KEY (id), \n\tFOREIGN KEY(fact_id) REFERENCES knowledge_facts (id), "
        "\n\tFOREIGN KEY(owner_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE TABLE training_analysis_requests (\n\tid UUID NOT NULL, "
        "\n\trequest_id VARCHAR(300) NOT NULL, \n\towner_player_id UUID NOT NULL, "
        "\n\tkind VARCHAR(30) NOT NULL, \n\tartifact_id UUID NOT NULL, "
        "\n\tinstructions TEXT NOT NULL, \n\tsource JSONB NOT NULL, "
        "\n\tstatus VARCHAR(24) DEFAULT 'queued' NOT NULL, \n\terror TEXT, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "\n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), "
        "\n\tCONSTRAINT uq_training_analysis_requests_request UNIQUE (request_id), "
        "\n\tCONSTRAINT ck_training_analysis_requests_e725ba5b3f51 "
        "CHECK (kind IN ('dissection', 'best_buzz', 'common_knowledge')), "
        "\n\tCONSTRAINT ck_training_analysis_requests_ca3bde9af7e1 CHECK (length(request_id) > 0), "
        "\n\tCONSTRAINT ck_training_analysis_requests_f014e7219173 "
        "CHECK (status IN ('queued', 'running', 'succeeded', "
        "'partially_succeeded', 'failed', 'cancelled')), "
        "\n\tCONSTRAINT ck_training_analysis_requests_3c4512fa9cc2 "
        "CHECK ((status IN ('succeeded', 'partially_succeeded', 'failed', 'cancelled') "
        "AND completed_at IS NOT NULL) "
        "OR (status IN ('queued', 'running') AND completed_at IS NULL)), "
        "\n\tFOREIGN KEY(owner_player_id) REFERENCES players (id), "
        "\n\tFOREIGN KEY(artifact_id) REFERENCES training_analysis_artifacts (id)\n)"
    )
    op.execute(
        "CREATE TABLE training_usage_ledger (\n\tid UUID NOT NULL, "
        "\n\treservation_id UUID NOT NULL, \n\tperiod_id UUID NOT NULL, "
        "\n\tplayer_id UUID NOT NULL, \n\tfeature VARCHAR(50) NOT NULL, "
        "\n\trequest_id VARCHAR(300) NOT NULL, \n\tentry_type VARCHAR(20) NOT NULL, "
        "\n\tunits INTEGER NOT NULL, \n\tprovider_usage JSONB, "
        "\n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), "
        "\n\tCONSTRAINT uq_training_usage_ledger_request_entry UNIQUE (request_id, entry_type), "
        "\n\tCONSTRAINT ck_training_usage_ledger_a658358fb31d "
        "CHECK (entry_type IN ('reserve', 'settle', 'release')), "
        "\n\tCONSTRAINT ck_training_usage_ledger_bdb6c9ad8fd3 CHECK (units >= 1), "
        "\n\tCONSTRAINT ck_training_usage_ledger_ca3bde9af7e1 CHECK (length(request_id) > 0), "
        "\n\tFOREIGN KEY(period_id) REFERENCES training_usage_periods (id), "
        "\n\tFOREIGN KEY(player_id) REFERENCES players (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_facts_owner_candidate "
        "ON knowledge_facts (owner_id, applied_candidate_id)"
    )
    op.execute("CREATE INDEX ix_knowledge_facts_owner_id ON knowledge_facts (owner_id)")
    op.execute(
        "CREATE INDEX ix_knowledge_facts_owner_title ON knowledge_facts (owner_id, title_key, id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_sharing_grants_owner_id ON knowledge_sharing_grants (owner_id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_sharing_grants_recipient_id "
        "ON knowledge_sharing_grants (recipient_id)"
    )
    op.execute(
        "CREATE INDEX ix_training_analysis_artifacts_owner "
        "ON training_analysis_artifacts (owner_player_id, kind, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_training_category_assignments_category_id "
        "ON training_category_assignments (category_id)"
    )
    op.execute(
        "CREATE INDEX ix_training_usage_periods_player "
        "ON training_usage_periods (player_id, feature, period_start)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_dissection_requests_analysis_request_id "
        "ON knowledge_dissection_requests (analysis_request_id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_dissection_requests_owner_id "
        "ON knowledge_dissection_requests (owner_id)"
    )
    op.execute("CREATE INDEX ix_knowledge_fact_tags_tag ON knowledge_fact_tags (tag_id, fact_id)")
    op.execute(
        "CREATE INDEX ix_knowledge_snapshots_fact_version "
        "ON knowledge_statistic_snapshots (fact_id, content_version, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_training_analysis_requests_artifact "
        "ON training_analysis_requests (artifact_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_training_analysis_requests_owner "
        "ON training_analysis_requests (owner_player_id, kind, status)"
    )
    op.execute(
        "CREATE INDEX ix_training_usage_ledger_period_id ON training_usage_ledger (period_id)"
    )
    op.execute(
        "CREATE INDEX ix_training_usage_ledger_player "
        "ON training_usage_ledger (player_id, feature, created_at)"
    )

    op.create_index(
        "ix_knowledge_facts_owner_updated", "knowledge_facts", ["owner_id", "updated_at", "id"]
    )
    op.create_index(
        "ix_knowledge_facts_owner_created", "knowledge_facts", ["owner_id", "created_at", "id"]
    )


def downgrade():
    op.drop_table("training_usage_ledger")
    op.drop_table("training_analysis_requests")
    op.drop_table("knowledge_statistic_snapshots")
    op.drop_table("knowledge_fact_tags")
    op.drop_table("knowledge_dissection_requests")
    op.drop_table("training_usage_periods")
    op.drop_table("training_category_assignments")
    op.drop_table("training_analysis_artifacts")
    op.drop_table("knowledge_tags")
    op.drop_table("knowledge_sharing_grants")
    op.drop_table("knowledge_facts")
    op.drop_table("training_policy_state")
    op.drop_table("training_feature_policies")
    op.drop_table("training_categories")
