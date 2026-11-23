"""Pacing rules that go beyond one mailbox's own gap.

- Mailboxes on the same domain never send together: after any email from @fastnexa.com, the domain's other
  mailboxes wait a random 1–3 minutes (on top of their own gap).
- A company (the recipients' email domain) gets at most COMPANY_DAILY_LIMIT emails in any 24 hours, opening
  emails and follow-ups together, across all campaigns and mailboxes. Free-mail domains (gmail.com…) are
  different people, so they aren't limited.
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, union_all, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import CampaignRecipient, FollowUpEmail, FollowUpStatus, Mailbox, RecipientStatus
from app.services.providers import KNOWN

DOMAIN_SPACING_SECONDS = (60, 180)
COMPANY_DAILY_LIMIT = 2
COMPANY_WINDOW = timedelta(hours=24)

# Personal addresses: everyone at these domains is a different person, not one company.
FREE_MAIL = frozenset(
    {
        *KNOWN,
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "web.de",
        "mail.com",
        "mail.ru",
        "inbox.ru",
        "list.ru",
        "bk.ru",
        "qq.com",
        "163.com",
        "126.com",
        "sina.com",
        "rediffmail.com",
        "fastmail.com",
        "hey.com",
        "tutanota.com",
        "tuta.io",
        "zoho.in",
        "yandex.ua",
        "t-online.de",
        "orange.fr",
        "free.fr",
        "laposte.net",
        "libero.it",
        "btinternet.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "cox.net",
        "shaw.ca",
        "rogers.com",
        "bigpond.com",
        "optusnet.com.au",
        "naver.com",
        "daum.net",
        "seznam.cz",
        "wp.pl",
        "o2.pl",
    }
)


def email_domain(column: ColumnElement) -> ColumnElement:
    return func.lower(func.split_part(column, "@", 2))


def space_domain(db: Session, mailbox: Mailbox, at: datetime, rng: random.Random) -> None:
    """Call right after `mailbox` takes an email to send: hold back the domain's other mailboxes a little."""
    domain = mailbox.email.rsplit("@", 1)[-1].lower()
    others = db.scalars(
        select(Mailbox.id).where(Mailbox.id != mailbox.id, email_domain(Mailbox.email) == domain)
    ).all()
    for other in others:
        earliest = at + timedelta(seconds=rng.randint(*DOMAIN_SPACING_SECONDS))
        db.execute(
            update(Mailbox)
            .where(Mailbox.id == other, or_(Mailbox.next_send_at.is_(None), Mailbox.next_send_at < earliest))
            .values(next_send_at=earliest)
        )


def full_companies(db: Session, at: datetime) -> list[str]:
    """Company domains that already got COMPANY_DAILY_LIMIT emails in the last 24 hours."""
    since = at - COMPANY_WINDOW
    opening = select(email_domain(CampaignRecipient.email).label("domain")).where(
        or_(
            (CampaignRecipient.status == RecipientStatus.SENT) & (CampaignRecipient.sent_at >= since),
            CampaignRecipient.status == RecipientStatus.SENDING,
        )
    )
    follow_ups = (
        select(email_domain(CampaignRecipient.email).label("domain"))
        .join(FollowUpEmail, FollowUpEmail.recipient_id == CampaignRecipient.id)
        .where(
            or_(
                (FollowUpEmail.status == FollowUpStatus.SENT) & (FollowUpEmail.sent_at >= since),
                FollowUpEmail.status == FollowUpStatus.SENDING,
            )
        )
    )
    sent = union_all(opening, follow_ups).subquery()
    rows = db.scalars(
        select(sent.c.domain).group_by(sent.c.domain).having(func.count() >= COMPANY_DAILY_LIMIT)
    ).all()
    return [d for d in rows if d and d not in FREE_MAIL]


def not_full(full: list[str]) -> list[ColumnElement]:
    """Conditions for a recipient query: skip people at companies that reached today's limit."""
    return [email_domain(CampaignRecipient.email).not_in(full)] if full else []
