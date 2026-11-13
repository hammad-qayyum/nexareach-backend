"""forget a person: campaign history can outlive the lead

Revision ID: c41e9d2a7b10
Revises: fb7bdf594e4c
Create Date: 2026-11-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "c41e9d2a7b10"
down_revision: str | None = "fb7bdf594e4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("campaign_recipients", "lead_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM campaign_recipients WHERE lead_id IS NULL")
    op.alter_column("campaign_recipients", "lead_id", existing_type=sa.Integer(), nullable=False)
