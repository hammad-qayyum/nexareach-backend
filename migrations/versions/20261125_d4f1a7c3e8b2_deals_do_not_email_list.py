"""deals do-not-email list

Revision ID: d4f1a7c3e8b2
Revises: c3d8e5f20b17
Create Date: 2026-11-25 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f1a7c3e8b2"
down_revision: str | None = "c3d8e5f20b17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = ("WAITING", "SENDING", "SENT", "FAILED", "SKIPPED", "CANCELLED")
NEW_STATUSES = (*OLD_STATUSES, "BLOCKED_BY_DEALS")


def _allow_statuses(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_campaign_recipients_recipient_status"), "campaign_recipients", type_="check")
    listed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(
        op.f("ck_campaign_recipients_recipient_status"), "campaign_recipients", f"status IN ({listed})"
    )


def upgrade() -> None:
    op.create_table(
        "deals_blocklist",
        sa.Column("hash", sa.CHAR(64), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("hash", name=op.f("pk_deals_blocklist")),
    )
    op.create_table(
        "deals_sync_state",
        sa.Column("name", sa.String(30), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_deals_sync_state")),
    )
    # Worked out by Postgres from the email, so every existing lead has it at once (no backfill to run).
    # convert_to() isn't marked immutable (it depends on the encoding), so a generated column can't use it
    # directly; the database is always UTF-8, so this wrapper is.
    op.execute(
        "CREATE FUNCTION nexareach_email_hash(email text) RETURNS char(64) LANGUAGE sql IMMUTABLE STRICT "
        "PARALLEL SAFE AS $$ SELECT encode(sha256(convert_to(lower(btrim(email)), 'UTF8')), 'hex') $$"
    )
    op.add_column(
        "leads",
        sa.Column(
            "email_hash", sa.CHAR(64), sa.Computed("nexareach_email_hash(email)", persisted=True), nullable=False
        ),
    )
    op.create_index(op.f("ix_leads_email_hash"), "leads", ["email_hash"])
    # Autogenerate doesn't see enum values inside CHECK constraints.
    _allow_statuses(NEW_STATUSES)


def downgrade() -> None:
    # The old version has no such status: they were skipped there, with the reason kept.
    op.execute(
        "UPDATE campaign_recipients SET status = 'SKIPPED', skip_reason = 'blocked_by_deals' "
        "WHERE status = 'BLOCKED_BY_DEALS'"
    )
    _allow_statuses(OLD_STATUSES)
    op.drop_index(op.f("ix_leads_email_hash"), table_name="leads")
    op.drop_column("leads", "email_hash")
    op.execute("DROP FUNCTION nexareach_email_hash(text)")
    op.drop_table("deals_sync_state")
    op.drop_table("deals_blocklist")
