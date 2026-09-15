"""Persistent shared tournament registration invitations."""

import sqlalchemy as sa
from alembic import op

revision = "0007_registration_links"
down_revision = "0006_admin_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tournaments", sa.Column(
        "registration_code", sa.String(32), nullable=False,
        server_default=sa.text("replace(gen_random_uuid()::text, '-', '')"),
    ))
    op.create_unique_constraint(
        "uq_tournaments_registration_code", "tournaments", ["registration_code"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_tournaments_registration_code", "tournaments", type_="unique")
    op.drop_column("tournaments", "registration_code")
