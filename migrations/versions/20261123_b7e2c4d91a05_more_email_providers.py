"""more email providers

Revision ID: b7e2c4d91a05
Revises: 9a26426d3057
Create Date: 2026-11-23 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "b7e2c4d91a05"
down_revision: str | None = "9a26426d3057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("GOOGLE", "MICROSOFT", "OTHER", "UNKNOWN")
NEW = (
    "GOOGLE",
    "MICROSOFT",
    "ZOHO",
    "YAHOO",
    "ICLOUD",
    "PROTON",
    "GODADDY",
    "TITAN",
    "YANDEX",
    "AMAZON",
    "MIMECAST",
    "PROOFPOINT",
    "OWN_SERVER",
    "OTHER",
    "UNKNOWN",
)


def _allow(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_leads_email_provider"), "leads", type_="check")
    listed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(op.f("ck_leads_email_provider"), "leads", f"email_provider IN ({listed})")


def upgrade() -> None:
    # Autogenerate doesn't see enum values inside CHECK constraints: allow the new provider names.
    # Existing leads keep what they have until someone presses "Recheck providers" on their group.
    _allow(NEW)


def downgrade() -> None:
    # The new names don't exist in the old version: they were all "Other" there.
    listed = ", ".join(f"'{v}'" for v in OLD)
    op.execute(f"UPDATE leads SET email_provider = 'OTHER' WHERE email_provider NOT IN ({listed})")
    _allow(OLD)
