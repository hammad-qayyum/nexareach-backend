"""gap at least three minutes

Revision ID: c3d8e5f20b17
Revises: b7e2c4d91a05
Create Date: 2026-11-23 15:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = 'c3d8e5f20b17'
down_revision: str | None = 'b7e2c4d91a05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The smallest gap between two emails from a mailbox is now 3 minutes: lift campaigns set lower.
    op.execute("UPDATE campaigns SET gap_min_seconds = 180 WHERE gap_min_seconds < 180")
    op.execute("UPDATE campaigns SET gap_max_seconds = gap_min_seconds WHERE gap_max_seconds < gap_min_seconds")


def downgrade() -> None:
    # Nothing to undo: 3 minutes or more was always allowed.
    pass
