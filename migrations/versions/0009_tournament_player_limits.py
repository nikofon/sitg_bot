"""Initialize player limits and independent registration and packet access modes."""

from alembic import op

revision = "0009_tournament_player_limits"
down_revision = "0008_theme_commentary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing overrides are manual registration, including started Classic stages.
    op.execute("""
        UPDATE tournaments SET registration_open = false
        WHERE registration_open_override IS NOT NULL
    """)
    # Previously no-access also represented the default disabled playability switch;
    # preserve that switch while giving these assignments the new default read rule.
    op.execute("""
        UPDATE tournament_packet_assignments SET access_level_by_members = 'read-after-play'
        WHERE access_level_by_members = 'no-access'
    """)
    op.execute("""
        UPDATE tournament_policy_versions AS p
        SET default_parameters = p.default_parameters ||
            '{"minimum_players": 4, "maximum_players": 4}'::jsonb
        WHERE p.version = (SELECT max(current.version)
            FROM tournament_policy_versions AS current
            WHERE current.tournament_id = p.tournament_id)
    """)
    op.execute("""
        UPDATE pregame_lobbies SET settings = settings ||
            '{"minimum_players": 4, "maximum_players": 4}'::jsonb,
            max_players = CASE WHEN tournament_type_version_id IN
                (SELECT id FROM tournament_type_versions WHERE key = 'classic') THEN 12 ELSE 4 END,
            validation = '[]'::jsonb, version = version + 1,
            searching = false, search_started_at = NULL
        WHERE status = 'assembling'
    """)
    op.execute("""
        UPDATE pregame_lobby_members SET ready = false
        WHERE lobby_id IN (SELECT id FROM pregame_lobbies WHERE status = 'assembling')
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE tournament_policy_versions SET default_parameters =
            default_parameters - 'minimum_players' - 'maximum_players'
    """)
    op.execute("""
        UPDATE pregame_lobbies SET settings = settings - 'minimum_players' - 'maximum_players'
        WHERE status = 'assembling'
    """)
