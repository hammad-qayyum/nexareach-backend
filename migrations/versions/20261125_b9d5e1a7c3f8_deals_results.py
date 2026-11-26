"""deals results

Revision ID: b9d5e1a7c3f8
Revises: a8c4d0f6b2e7
Create Date: 2026-11-25 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9d5e1a7c3f8"
down_revision: str | None = "a8c4d0f6b2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deals_lead_events",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("deals_lead_id", sa.String(100), nullable=False),
        sa.Column("into_lead_id", sa.String(100), nullable=True),
        sa.Column("reply_id", sa.BigInteger(), nullable=True),
        sa.Column("from_stage", sa.String(30), nullable=True),
        sa.Column("to_stage", sa.String(30), nullable=True),
        sa.Column("value_usd", sa.Integer(), nullable=True),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deals_lead_events")),
    )
    op.create_index(op.f("ix_deals_lead_events_deals_lead_id"), "deals_lead_events", ["deals_lead_id"])
    op.create_table(
        "deals_lead_results",
        sa.Column("deals_lead_id", sa.String(100), nullable=False),
        sa.Column("handover_id", sa.BigInteger(), nullable=True),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("value_usd", sa.Integer(), nullable=True),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["handover_id"],
            ["deals_handovers.id"],
            name=op.f("fk_deals_lead_results_handover_id_deals_handovers"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("deals_lead_id", name=op.f("pk_deals_lead_results")),
    )
    op.add_column("campaign_recipients", sa.Column("subject_version", sa.String(998), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_recipients", "subject_version")
    op.drop_table("deals_lead_results")
    op.drop_index(op.f("ix_deals_lead_events_deals_lead_id"), table_name="deals_lead_events")
    op.drop_table("deals_lead_events")
