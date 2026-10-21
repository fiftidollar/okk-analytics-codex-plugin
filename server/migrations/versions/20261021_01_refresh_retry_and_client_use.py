"""Refresh retry pair and the first sign-in of a registered client.

Both columns are nullable additions: the previous gateway version keeps working against this
schema, and the downgrade drops them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261021_01"
down_revision: str | Sequence[str] | None = "20260714_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("oauth_tokens", sa.Column("successor_sealed", sa.Text(), nullable=True))
    op.add_column(
        "oauth_clients", sa.Column("first_authorized_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Clients that already have tokens or codes are in use: never treat them as abandoned.
    op.execute(
        """
        UPDATE oauth_clients SET first_authorized_at = created_at
        WHERE EXISTS (SELECT 1 FROM oauth_tokens t WHERE t.client_id = oauth_clients.client_id)
           OR EXISTS (SELECT 1 FROM oauth_authorization_codes c WHERE c.client_id = oauth_clients.client_id)
        """
    )


def downgrade() -> None:
    op.drop_column("oauth_clients", "first_authorized_at")
    op.drop_column("oauth_tokens", "successor_sealed")
