"""deals handover

Revision ID: a8c4d0f6b2e7
Revises: f6b3c9e5a1d4
Create Date: 2026-11-25 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8c4d0f6b2e7"
down_revision: str | None = "f6b3c9e5a1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = ("WAITING", "SENDING", "SENT", "FAILED", "SKIPPED", "CANCELLED", "BLOCKED_BY_DEALS")
NEW_STATUSES = (*OLD_STATUSES, "HANDED_TO_DEALS")


def _allow_statuses(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_campaign_recipients_recipient_status"), "campaign_recipients", type_="check")
    listed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(
        op.f("ck_campaign_recipients_recipient_status"), "campaign_recipients", f"status IN ({listed})"
    )


def upgrade() -> None:
    op.create_table(
        "deals_handovers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("reply_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_id", sa.Integer(), nullable=True),
        sa.Column("band", sa.String(10), nullable=False),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_name", sa.String(200), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("deals_lead_id", sa.String(100), nullable=True),
        sa.Column("deals_lead_url", sa.Text(), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["reply_id"], ["replies.id"], name=op.f("fk_deals_handovers_reply_id_replies"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["campaign_recipients.id"],
                                name=op.f("fk_deals_handovers_recipient_id_campaign_recipients"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name=op.f("fk_deals_handovers_lead_id_leads"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], name=op.f("fk_deals_handovers_approved_by_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deals_handovers")),
        sa.UniqueConstraint("reply_id", name=op.f("uq_deals_handovers_reply_id")),
    )
    op.create_index(op.f("ix_deals_handovers_recipient_id"), "deals_handovers", ["recipient_id"])
    op.create_index(op.f("ix_deals_handovers_status"), "deals_handovers", ["status"])
    op.create_index(op.f("ix_deals_handovers_deals_lead_id"), "deals_handovers", ["deals_lead_id"])
    op.add_column("leads", sa.Column("handed_to_deals_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("deals_lead_id", sa.String(100), nullable=True))
    op.add_column("leads", sa.Column("deals_lead_url", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("deals_service_key", sa.String(50), nullable=True))
    _allow_statuses(NEW_STATUSES)


def downgrade() -> None:
    # The old version has no such status: those people were simply sent.
    op.execute("UPDATE campaign_recipients SET status = 'SENT' WHERE status = 'HANDED_TO_DEALS'")
    _allow_statuses(OLD_STATUSES)
    op.drop_column("campaigns", "deals_service_key")
    op.drop_column("leads", "deals_lead_url")
    op.drop_column("leads", "deals_lead_id")
    op.drop_column("leads", "handed_to_deals_at")
    op.drop_table("deals_handovers")
