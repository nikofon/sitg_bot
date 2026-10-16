"""Private packet scope and isolated solo training history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0021_training"
down_revision = "0020_database_maintenance"
branch_labels = None
depends_on = None


def _uuid(name, target=None, *, primary_key=False, nullable=False):
    args = [sa.ForeignKey(target)] if target else []
    return sa.Column(name, pg.UUID(as_uuid=True), *args, primary_key=primary_key, nullable=nullable)


def upgrade() -> None:
    for table in ("packet_drafts", "logical_packets"):
        op.add_column(
            table,
            sa.Column(
                "scope",
                sa.String(16),
                nullable=False,
                server_default="tournament",
            ),
        )
    op.alter_column("packet_drafts", "creation_tournament_id", nullable=True)
    op.create_check_constraint(
        None,
        "packet_drafts",
        "(scope = 'tournament' AND creation_tournament_id IS NOT NULL) OR "
        "(scope = 'training' AND creation_tournament_id IS NULL AND uploader_id IS NOT NULL)",
    )
    op.create_check_constraint(None, "logical_packets", "scope IN ('tournament', 'training')")
    op.create_check_constraint(
        None,
        "logical_packets",
        "scope <> 'training' OR uploader_id IS NOT NULL",
    )
    op.create_index(
        "ix_logical_packets_owner_scope", "logical_packets", ["uploader_id", "scope", "id"]
    )
    op.drop_constraint("ck_player_telegram_navigation_87f899138f57", "player_telegram_navigation")
    op.create_check_constraint(
        None,
        "player_telegram_navigation",
        "interaction_mode IN ('player', 'manager', 'admin', 'training')",
    )
    op.execute("""
        CREATE FUNCTION reject_private_packet_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM id FROM logical_packets WHERE id = NEW.packet_id AND scope = 'tournament'
                FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Private packets cannot be assigned'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER private_packet_assignment BEFORE INSERT OR UPDATE
            ON tournament_packet_assignments FOR EACH ROW
            EXECUTE FUNCTION reject_private_packet_assignment()
    """)
    op.execute("""
        CREATE FUNCTION reject_private_draft_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM id FROM packet_drafts WHERE id = NEW.draft_id AND scope = 'tournament'
                FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Private drafts cannot be assigned'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER private_draft_assignment BEFORE INSERT OR UPDATE
            ON packet_draft_tournaments FOR EACH ROW
            EXECUTE FUNCTION reject_private_draft_assignment()
    """)
    op.create_table(
        "training_sessions",
        _uuid("id", primary_key=True),
        _uuid("owner_id", "players.id"),
        _uuid("packet_version_id", "packet_versions.id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("disclosure_receipt", pg.JSONB, nullable=False),
        sa.Column("settings", pg.JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("question_index", sa.Integer, nullable=False),
        sa.Column("token_cursor", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("presentation_version", sa.Integer, nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("hidden_deadline", sa.DateTime(timezone=True)),
        sa.Column("text_hidden", sa.Boolean, nullable=False),
        sa.Column("best_buzz_consumed", sa.Boolean, nullable=False),
        sa.Column("paused_phase", sa.String(24)),
        sa.Column("paused_remaining", sa.Numeric),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active', 'paused', 'completed', 'stopped')"),
        sa.CheckConstraint("phase IN ('reading', 'waiting', 'answering', 'revealed', 'best_buzz')"),
        sa.CheckConstraint("version >= 1 AND question_index >= 0 AND token_cursor >= 0"),
    )
    op.create_index(
        "uq_training_active_owner",
        "training_sessions",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    op.create_index(
        "ix_training_deadline",
        "training_sessions",
        ["deadline"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "training_members",
        _uuid("session_id", "training_sessions.id", primary_key=True),
        _uuid("player_id", "players.id", primary_key=True),
        sa.Column("seat", sa.Integer, nullable=False),
        sa.Column("presentation", pg.JSONB, nullable=False),
        sa.UniqueConstraint("session_id", "seat"),
        sa.CheckConstraint("seat = 0"),
    )
    op.create_table(
        "training_questions",
        _uuid("session_id", "training_sessions.id", primary_key=True),
        sa.Column("position", sa.Integer, primary_key=True),
        _uuid("question_revision_id", "question_revisions.id"),
        _uuid("theme_revision_id", "theme_revisions.id"),
        sa.Column("value", sa.Integer, nullable=False),
        sa.Column("best_buzz", pg.JSONB),
        sa.CheckConstraint("position >= 0"),
    )
    op.create_table(
        "training_attempts",
        _uuid("id", primary_key=True),
        _uuid("session_id", "training_sessions.id"),
        _uuid("player_id", "players.id"),
        _uuid("question_revision_id", "question_revisions.id"),
        sa.Column("question_index", sa.Integer, nullable=False),
        sa.Column("answer", sa.Text),
        sa.Column("correct", sa.Boolean),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("token_cursor", sa.Integer, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("session_id", "question_index"),
        sa.CheckConstraint("outcome IN ('answered', 'revealed', 'skipped', 'timeout')"),
    )
    op.execute("""
        CREATE FUNCTION protect_training_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (EXISTS (SELECT 1 FROM training_questions WHERE question_revision_id = OLD.id)
                OR EXISTS (SELECT 1 FROM packet_questions pq
                    JOIN packet_versions pv ON pv.id = pq.packet_version_id
                    JOIN logical_packets p ON p.id = pv.packet_id
                    WHERE pq.question_revision_id = OLD.id AND p.scope = 'training'))
                AND (OLD.text, OLD.answer, OLD.accepted_answers, OLD.rejected_answers,
                     OLD.commentary, OLD.form, OLD.source) IS DISTINCT FROM
                    (NEW.text, NEW.answer, NEW.accepted_answers, NEW.rejected_answers,
                     NEW.commentary, NEW.form, NEW.source)
            THEN RAISE EXCEPTION 'Pinned training question revisions are immutable'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER training_revision_immutable BEFORE UPDATE ON question_revisions
            FOR EACH ROW EXECUTE FUNCTION protect_training_revision()
    """)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM packet_drafts WHERE scope='training')")
    ):
        raise RuntimeError("Retain training uploads before downgrading packet scope")
    op.execute(
        "UPDATE player_telegram_navigation SET interaction_mode='player' "
        "WHERE interaction_mode='training'"
    )
    op.execute("DROP TRIGGER training_revision_immutable ON question_revisions")
    op.execute("DROP FUNCTION protect_training_revision()")
    for table in (
        "training_attempts",
        "training_questions",
        "training_members",
        "training_sessions",
    ):
        op.drop_table(table)
    op.execute("DROP TRIGGER private_draft_assignment ON packet_draft_tournaments")
    op.execute("DROP FUNCTION reject_private_draft_assignment()")
    op.execute("DROP TRIGGER private_packet_assignment ON tournament_packet_assignments")
    op.execute("DROP FUNCTION reject_private_packet_assignment()")
    # Training uploads have no valid tournament representation; preserve them.
    for table in ("packet_drafts", "logical_packets", "player_telegram_navigation"):
        for constraint in sa.inspect(connection).get_check_constraints(table):
            if "scope" in constraint["sqltext"] or "interaction_mode" in constraint["sqltext"]:
                op.drop_constraint(op.f(constraint["name"]), table)
    op.create_check_constraint(
        None,
        "player_telegram_navigation",
        "interaction_mode IN ('player', 'manager', 'admin')",
    )
    op.alter_column("packet_drafts", "creation_tournament_id", nullable=False)
    op.drop_index("ix_logical_packets_owner_scope", "logical_packets")
    for table in ("packet_drafts", "logical_packets"):
        op.drop_column(table, "scope")
