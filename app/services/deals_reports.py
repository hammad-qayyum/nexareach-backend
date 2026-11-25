"""What cold email earns (Deals integration step 5): MQLs, pipeline and Won revenue, by campaign, mailbox,
email version (the subject as written, after spintax) and lead list.

- MQLs: handovers Deals answered created or existing, not counting repeat replies from someone already handed.
- Auto / approved / manual: how each MQL got there.
- Reached proposal: the lead is at Proposal, Negotiation or Won.
- Won: count and total value (USD); Lost: count and reasons.
- Revenue per 1,000 emails: Won value ÷ campaign and follow-up emails sent × 1,000.

MQLs are counted by when they were handed over; emails by when they were sent.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    EMAILED,
    Campaign,
    CampaignRecipient,
    DealsHandover,
    DealsLeadResult,
    FollowUpEmail,
    FollowUpStatus,
    LeadGroup,
    Mailbox,
    campaign_groups,
    lead_group_members,
)
from app.services.deals_events import PROPOSAL_OR_LATER

DIMENSIONS = ("campaign", "mailbox", "version", "lead_list")


@dataclass
class Row:
    key: str
    label: str
    emails_sent: int = 0
    mqls: int = 0
    auto: int = 0
    approved: int = 0
    manual: int = 0
    reached_proposal: int = 0
    won: int = 0
    won_usd: int = 0
    lost: int = 0
    lost_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def revenue_per_1000(self) -> float | None:
        return round(self.won_usd / self.emails_sent * 1000, 2) if self.emails_sent else None

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "revenue_per_1000": self.revenue_per_1000}


@dataclass(frozen=True)
class Info:
    campaign_id: int
    mailbox_id: int | None
    version: str | None
    group_id: int | None


def _info(db: Session, recipient_ids: set[int]) -> dict[int, Info]:
    """Each recipient's campaign, mailbox, subject version and lead list (one of the campaign's groups)."""
    if not recipient_ids:
        return {}
    ids = list(recipient_ids)
    group = (
        select(CampaignRecipient.id.label("rid"), func.min(campaign_groups.c.group_id).label("group_id"))
        .join(lead_group_members, lead_group_members.c.lead_id == CampaignRecipient.lead_id)
        .join(
            campaign_groups,
            (campaign_groups.c.group_id == lead_group_members.c.group_id)
            & (campaign_groups.c.campaign_id == CampaignRecipient.campaign_id),
        )
        .where(CampaignRecipient.id.in_(ids))
        .group_by(CampaignRecipient.id)
        .subquery()
    )
    rows = db.execute(
        select(
            CampaignRecipient.id,
            CampaignRecipient.campaign_id,
            CampaignRecipient.mailbox_id,
            CampaignRecipient.subject_version,
            group.c.group_id,
        )
        .outerjoin(group, group.c.rid == CampaignRecipient.id)
        .where(CampaignRecipient.id.in_(ids))
    )
    return {rid: Info(cid, mid, ver, gid) for rid, cid, mid, ver, gid in rows}


def _key(info: Info, dimension: str, mailbox_id: int | None = None) -> str:
    if dimension == "mailbox":
        return str(mailbox_id if mailbox_id is not None else info.mailbox_id)
    if dimension == "version":
        return f"{info.campaign_id}|{info.version or ''}"
    if dimension == "lead_list":
        return str(info.group_id) if info.group_id is not None else "none"
    return str(info.campaign_id)


def _labels(db: Session, dimension: str, keys: set[str]) -> dict[str, str]:
    if dimension == "mailbox":
        ids = [int(k) for k in keys if k.isdigit()]
        return {
            str(i): e for i, e in db.execute(select(Mailbox.id, Mailbox.email).where(Mailbox.id.in_(ids)))
        }
    if dimension == "lead_list":
        ids = [int(k) for k in keys if k.isdigit()]
        names = {
            str(i): n
            for i, n in db.execute(select(LeadGroup.id, LeadGroup.name).where(LeadGroup.id.in_(ids)))
        }
        return {**names, "none": "(no list)"}
    campaign_ids = {int(k.split("|")[0]) for k in keys if k.split("|")[0].isdigit()}
    names = {
        i: n for i, n in db.execute(select(Campaign.id, Campaign.name).where(Campaign.id.in_(campaign_ids)))
    }
    if dimension == "version":
        return {
            k: f"{k.split('|', 1)[1] or '(no subject)'} · {names.get(int(k.split('|')[0]), '?')}"
            for k in keys
        }
    return {k: names.get(int(k), "?") for k in keys if k.isdigit()}


def compute(db: Session, start: datetime, end: datetime, dimension: str = "campaign") -> dict[str, Any]:
    """Rows for one breakdown, most Won revenue first, plus a total row."""
    if dimension not in DIMENSIONS:
        raise ValueError(dimension)
    openings = (
        db.execute(
            select(CampaignRecipient.id).where(
                CampaignRecipient.status.in_(EMAILED),
                CampaignRecipient.sent_at >= start,
                CampaignRecipient.sent_at < end,
            )
        )
        .scalars()
        .all()
    )
    follow_ups = db.execute(
        select(FollowUpEmail.recipient_id, FollowUpEmail.mailbox_id).where(
            FollowUpEmail.status == FollowUpStatus.SENT,
            FollowUpEmail.sent_at >= start,
            FollowUpEmail.sent_at < end,
        )
    ).all()
    handovers = db.execute(
        select(DealsHandover, DealsLeadResult)
        .outerjoin(DealsLeadResult, DealsLeadResult.deals_lead_id == DealsHandover.deals_lead_id)
        .where(
            DealsHandover.status.in_(["created", "existing"]),
            DealsHandover.band != "repeat",
            DealsHandover.created_at >= start,
            DealsHandover.created_at < end,
        )
    ).all()
    info = _info(
        db,
        set(openings) | {r for r, _ in follow_ups} | {h.recipient_id for h, _ in handovers if h.recipient_id},
    )

    rows: dict[str, Row] = defaultdict(lambda: Row(key="", label=""))
    total = Row(key="total", label="All")
    for rid in openings:
        if rid in info:
            rows[_key(info[rid], dimension)].emails_sent += 1
            total.emails_sent += 1
    for rid, mailbox_id in follow_ups:
        if rid in info:
            rows[_key(info[rid], dimension, mailbox_id)].emails_sent += 1
            total.emails_sent += 1
    for handover, result in handovers:
        if handover.recipient_id not in info:
            continue
        for row in (rows[_key(info[handover.recipient_id], dimension)], total):
            row.mqls += 1
            if handover.band in ("auto", "approved", "manual"):
                setattr(row, handover.band, getattr(row, handover.band) + 1)
            stage = result.stage if result else None
            if stage in PROPOSAL_OR_LATER:
                row.reached_proposal += 1
            if stage == "won":
                row.won += 1
                row.won_usd += result.value_usd or 0
            if stage == "lost":
                row.lost += 1
                reason = (result.lost_reason or "No reason given").strip()
                row.lost_reasons[reason] = row.lost_reasons.get(reason, 0) + 1

    labels = _labels(db, dimension, set(rows))
    out = []
    for key, row in rows.items():
        row.key, row.label = key, labels.get(key, key)
        out.append(row)
    out.sort(key=lambda r: (-r.won_usd, -r.mqls, -r.emails_sent, r.label))
    return {"dimension": dimension, "rows": [r.as_dict() for r in out], "total": total.as_dict()}
