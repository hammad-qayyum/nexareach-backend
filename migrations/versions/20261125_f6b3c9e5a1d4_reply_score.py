"""reply score and review list

Revision ID: f6b3c9e5a1d4
Revises: e5a2b8d4f9c3
Create Date: 2026-11-25 18:00:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6b3c9e5a1d4"
down_revision: str | None = "e5a2b8d4f9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The starting rules from the Deals integration guide (24 Nov 2026). Admins edit them in Settings → Reply score.
STARTING_RULES = [
    (
        "Clear interest",
        "words",
        ["interested", "let's talk", "lets talk", "sounds good", "keen", "tell me more", "happy to chat"],
        None,
        40,
        None,
    ),
    (
        "Wants a meeting",
        "words",
        ["call", "meeting", "schedule", "book a", "calendar", "available on", "zoom", "teams", "google meet"],
        None,
        30,
        None,
    ),
    (
        "Asks about price",
        "words",
        ["price", "pricing", "cost", "quote", "proposal", "budget", "rates"],
        None,
        30,
        None,
    ),
    (
        "Asks for details",
        "words",
        ["send details", "more information", "more info", "brochure", "case study", "portfolio"],
        None,
        20,
        None,
    ),
    ("Gives phone or time", "signal", [], "phone_or_time", 15, None),
    ("Asks a question", "signal", [], "question_mark", 10, None),
    ("Company email", "signal", [], "company_domain", 10, None),
    ("Brings a colleague", "signal", [], "colleague_cc", 10, None),
    (
        "Not now",
        "words",
        ["not now", "maybe later", "next quarter", "next year", "no budget"],
        None,
        -30,
        None,
    ),
    ("Wrong person", "words", ["wrong person", "not my area", "no longer work"], None, -40, None),
    (
        "Hard stop: no",
        "words",
        [
            "not interested",
            "no thanks",
            "unsubscribe",
            "remove me",
            "stop emailing",
            "don't contact",
            "do not contact",
        ],
        None,
        0,
        "no",
    ),
    # Set by the existing auto-reply / out-of-office / bounce / warm-up checks, never by words.
    ("Hard stop: machine", "signal", [], "machine", 0, "machine"),
]


def upgrade() -> None:
    rules = op.create_table(
        "reply_score_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("words", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("pattern", sa.Text(), nullable=True),
        sa.Column("signal", sa.String(30), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("hard_stop", sa.String(10), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reply_score_rules")),
    )
    op.bulk_insert(
        rules,
        [
            {
                "name": name,
                "kind": kind,
                "words": json.loads(json.dumps(words)),
                "signal": signal,
                "points": points,
                "hard_stop": hard_stop,
                "enabled": True,
                "position": i,
            }
            for i, (name, kind, words, signal, points, hard_stop) in enumerate(STARTING_RULES)
        ],
    )
    op.add_column("replies", sa.Column("cc", postgresql.JSONB(), nullable=True))
    op.add_column("replies", sa.Column("score", sa.Integer(), nullable=True))
    op.add_column("replies", sa.Column("score_rules", postgresql.JSONB(), nullable=True))
    op.add_column("replies", sa.Column("score_band", sa.String(10), nullable=True))
    op.add_column("replies", sa.Column("review_status", sa.String(10), nullable=True))
    op.add_column("replies", sa.Column("reviewed_by_id", sa.Integer(), nullable=True))
    op.add_column("replies", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("replies", sa.Column("review_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_replies_reviewed_by_id_users"), "replies", "users", ["reviewed_by_id"], ["id"]
    )
    op.create_index(op.f("ix_replies_review_status"), "replies", ["review_status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_replies_review_status"), table_name="replies")
    op.drop_constraint(op.f("fk_replies_reviewed_by_id_users"), "replies", type_="foreignkey")
    for column in (
        "review_reason",
        "reviewed_at",
        "reviewed_by_id",
        "review_status",
        "score_band",
        "score_rules",
        "score",
        "cc",
    ):
        op.drop_column("replies", column)
    op.drop_table("reply_score_rules")
