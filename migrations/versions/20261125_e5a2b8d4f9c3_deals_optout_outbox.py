"""deals opt-out outbox

Revision ID: e5a2b8d4f9c3
Revises: d4f1a7c3e8b2
Create Date: 2026-11-25 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a2b8d4f9c3"
down_revision: str | None = "d4f1a7c3e8b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deals_optout_batches",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("gave_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded", sa.Boolean(), server_default="false", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deals_optout_batches")),
    )
    op.create_table(
        "deals_optout_outbox",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("hash", sa.CHAR(64), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ref", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["deals_optout_batches.id"],
            name=op.f("fk_deals_optout_outbox_batch_id_deals_optout_batches"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deals_optout_outbox")),
        sa.UniqueConstraint("ref", name=op.f("uq_deals_optout_outbox_ref")),
    )
    op.create_index(op.f("ix_deals_optout_outbox_hash"), "deals_optout_outbox", ["hash"])
    op.create_index("ix_deals_optout_outbox_unsent", "deals_optout_outbox", ["batch_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_deals_optout_outbox_unsent", table_name="deals_optout_outbox")
    op.drop_index(op.f("ix_deals_optout_outbox_hash"), table_name="deals_optout_outbox")
    op.drop_table("deals_optout_outbox")
    op.drop_table("deals_optout_batches")
