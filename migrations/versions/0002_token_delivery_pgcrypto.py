"""Enable encryption functions required by one-time token delivery."""

from alembic import op

revision = "0002_token_delivery_pgcrypto"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    # The extension may predate this migration or be shared by other applications.
    pass
