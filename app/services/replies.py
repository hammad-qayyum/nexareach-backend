"""Reply checking: read each mailbox's inbox and notice answers to our emails (plan section 17, basic).

The worker runs check_all() every 5 minutes. For each mailbox with an incoming mail (IMAP) server it reads
the messages that arrived since the last check, read-only, and matches them to people we emailed from
that mailbox: first by the thread headers (In-Reply-To / References hold our Message-IDs), then by the
sender's address. What a message can be:

- a reply: stored as a Reply (shown on the Replies page), the person's replied_at is set and their
  follow-ups stop. Later messages in the same conversation are stored too;
- an opt-out ("unsubscribe", "remove me", "not interested"…): as a reply, plus the address goes on the
  do-not-contact list, so no campaign emails them again;
- an auto-reply (out of office): ignored, follow-ups continue;
- a bounce (the mail server couldn't deliver): follow-ups stop, the bounce is recorded for the
  Deliverability page, a hard bounce (the address doesn't exist) puts the address on do-not-contact, and
  the bounce email is moved to a "Bounces" folder.

Mailboxes without IMAP can't be checked; people can be marked as replied by hand instead.

answer() sends an answer written on the Replies page, from the mailbox that got the reply, in the same thread.
"""

import email
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import (
    EMAILED,
    AnswerStatus,
    CampaignRecipient,
    DeliverySource,
    DncKind,
    DoNotContact,
    FollowUpEmail,
    FollowUpStatus,
    Lead,
    Mailbox,
    MailboxStatus,
    Reply,
    ReplyAnswer,
    ReplyKind,
)
from app.services import deals_optouts, delivery, mailer, sending, warmup
from app.services.app_settings import email_footer
from app.services.audit import SYSTEM, Actor, log_activity
from app.services.email_html import to_text

log = logging.getLogger(__name__)

# Replies are looked for to emails sent in the last this many days.
WATCH_DAYS = 60
# Longest reply text kept (characters).
MAX_TEXT = 100_000
OPT_OUT_REASON = "Asked not to be emailed (found in their reply)"

ReadInbox = Callable[[Mailbox, int | None, int | None, date], mailer.InboxBatch]
OpenInbox = Callable[[Mailbox], mailer.Inbox]
BOUNCES_FOLDER = "Bounces"

_MESSAGE_ID = re.compile(r"<[^<>\s]+>")
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?(reply|response)|auto:|out of (the )?office|ooo\b|"
    r"away from (the )?office|on (annual )?leave|abwesenheit|réponse automatique|respuesta automática)",
    re.IGNORECASE,
)
_OPT_OUT = re.compile(
    r"\b(unsubscribe|opt[- ]?out|remove me|take me off|stop (emailing|e-mailing|sending|contacting)|"
    r"(do not|don'?t) (email|e-mail|contact|send)|no more emails|not interested)\b",
    re.IGNORECASE,
)
# Where the quoted earlier message starts in a reply.
# Gmail often wraps its "On <date>, <name> <address>" line, so "wrote:" may start the next line.
_QUOTE_START = re.compile(
    r"^(on\s[^\n]{0,250}(?:\n[^\n]{0,120})?wrote:|-{2,}\s*original message\s*-{2,}|from:\s|sent from my )",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class Watched:
    """People emailed from one mailbox who haven't replied yet."""

    by_message_id: dict[str, int] = field(default_factory=dict)
    # Address -> recipient ids, most recently emailed first.
    by_email: dict[str, list[int]] = field(default_factory=dict)
    sent_at: dict[int, datetime] = field(default_factory=dict)
    since: datetime | None = None


def _header(msg: Message, name: str) -> str:
    value = msg.get(name)
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def watched_for(db: Session, mailbox_id: int, at: datetime) -> Watched:
    watched = Watched()
    rows = db.execute(
        select(
            CampaignRecipient.id,
            CampaignRecipient.email,
            CampaignRecipient.message_id,
            CampaignRecipient.sent_at,
        )
        .where(
            CampaignRecipient.mailbox_id == mailbox_id,
            CampaignRecipient.status.in_(EMAILED),
            CampaignRecipient.lead_id.is_not(None),
            CampaignRecipient.sent_at >= at - timedelta(days=WATCH_DAYS),
        )
        .order_by(CampaignRecipient.sent_at.desc())
    ).all()
    for rid, address, message_id, sent_at in rows:
        if message_id:
            watched.by_message_id[message_id] = rid
        watched.by_email.setdefault(address.lower(), []).append(rid)
        watched.sent_at[rid] = sent_at
        watched.since = sent_at if watched.since is None else min(watched.since, sent_at)
    if rows:
        for rid, message_id in db.execute(
            select(FollowUpEmail.recipient_id, FollowUpEmail.message_id).where(
                FollowUpEmail.recipient_id.in_(watched.sent_at),
                FollowUpEmail.status == FollowUpStatus.SENT,
                FollowUpEmail.message_id.is_not(None),
            )
        ):
            watched.by_message_id[message_id] = rid
        # The conversation goes on after a reply: their messages and our answers belong to it too.
        for rid, their_id, our_id in db.execute(
            select(Reply.recipient_id, Reply.message_id, ReplyAnswer.message_id)
            .outerjoin(ReplyAnswer, ReplyAnswer.reply_id == Reply.id)
            .where(Reply.recipient_id.in_(watched.sent_at))
        ):
            watched.by_message_id[their_id] = rid
            if our_id:
                watched.by_message_id[our_id] = rid
    return watched


def _is_auto_reply(msg: Message) -> bool:
    auto = _header(msg, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Auto-Reply"):
        return True
    if _header(msg, "Precedence").strip().lower() in ("auto_reply", "bulk", "junk", "list"):
        return True
    return bool(_AUTO_SUBJECT.match(_header(msg, "Subject")))


def _is_unsubscribe_subject(msg: Message) -> bool:
    subject = " ".join(_header(msg, "Subject").lower().split())
    while subject.startswith(("re:", "aw:", "fw:", "fwd:")):
        subject = subject.split(":", 1)[1].strip()
    return subject == "unsubscribe"


def _is_complaint(msg: Message) -> bool:
    """A spam-complaint report (ARF) a mail provider sends back when someone presses "Report spam"."""
    return msg.get_content_type() == "multipart/report" and (
        str(msg.get_param("report-type", "")).lower() == "feedback-report"
    )


def _is_bounce(msg: Message, sender: str) -> bool:
    local = sender.split("@", 1)[0]
    report = msg.get_content_type() == "multipart/report"
    if not (report or local in ("mailer-daemon", "postmaster")):
        return False
    text = msg.as_string().lower()
    # A delay notice ("still trying") isn't a bounce.
    return not ("action: delayed" in text and "action: failed" not in text)


def _full_text(msg: Message) -> tuple[str, bool]:
    """The message's plain text (or its HTML as text), and whether it carried attachments."""
    plain = html = None
    attachments = False
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments = True
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, ValueError, AttributeError):
            # Unknown charset or a broken part: read what else the message holds.
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and html is None:
            html = text
    text = plain if plain is not None else (to_text(html) if html else "")
    return text.replace("\r\n", "\n"), attachments


def _reply_text(msg: Message) -> str:
    """What the person wrote, without the quoted earlier email."""
    return _strip_quote(_full_text(msg)[0])


def _strip_quote(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    text = "\n".join(lines)
    match = _QUOTE_START.search(text)
    return (text[: match.start()] if match else text).strip()


def _date(msg: Message, fallback: datetime) -> datetime:
    try:
        parsed = parsedate_to_datetime(_header(msg, "Date"))
    except (TypeError, ValueError, IndexError):
        return fallback
    if parsed is None:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _match(msg: Message, sender: str, watched: Watched, received: datetime) -> int | None:
    for ref in _MESSAGE_ID.findall(f"{_header(msg, 'In-Reply-To')} {_header(msg, 'References')}"):
        if ref in watched.by_message_id:
            return watched.by_message_id[ref]
    for rid in watched.by_email.get(sender, []):
        # Only something that arrived after we emailed them counts as an answer.
        if received >= watched.sent_at[rid] - timedelta(minutes=5):
            return rid
    return None


def bounce_details(msg: Message) -> tuple[str, str | None, str | None]:
    """(what the receiving server said, its status code, the receiving server) from a bounce email.

    Standard bounces (multipart/report) carry a machine-readable delivery-status part; others only text.
    """
    diagnostic = code = remote = None
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        blocks = payload if isinstance(payload, list) else []
        for block in blocks:
            diagnostic = diagnostic or block.get("Diagnostic-Code")
            code = code or block.get("Status")
            remote = remote or block.get("Remote-MTA")
    if not diagnostic:
        text = _full_text(msg)[0]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # The line with the server's answer ("550 5.1.1 <x@y>: user unknown"), else the start of the text.
        answer = next(
            (line for line in lines if re.search(r"\b[45]\d\d\b|\b[45]\.\d{1,3}\.\d{1,3}\b", line)), None
        )
        diagnostic = answer or " ".join(lines)[:500]
    code_text = _dsn_value(code)
    return (
        _dsn_value(diagnostic)[:2000],
        code_text.split()[0] if code_text else None,
        _dsn_value(remote)[:255] or None,
    )


def _dsn_value(value: object) -> str:
    """A delivery-status field without its type prefix ("smtp; 550 ..." -> "550 ..."), on one line."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return re.sub(r"^(smtp|dns|x-[\w-]+)\s*;\s*", "", text, flags=re.IGNORECASE)


def _bounced_recipient(msg: Message, watched: Watched) -> int | None:
    """Bounces quote the original email: find one of our Message-IDs, or else a watched address."""
    text = msg.as_string()
    for ref in _MESSAGE_ID.findall(text):
        if ref in watched.by_message_id:
            return watched.by_message_id[ref]
    lowered = text.lower()
    for address, rids in watched.by_email.items():
        if address in lowered:
            return rids[0]
    return None


def classify(msg: Message, watched: Watched, at: datetime) -> tuple[str, int] | None:
    """("reply" | "opt_out" | "bounce" | "complaint", recipient id) for a message that concerns us, else
    None."""
    if any(warmup.is_warmup(_header(msg, h)) for h in ("Message-ID", "In-Reply-To", "References")):
        return None  # warm-up traffic between our mailboxes and the helpers
    sender = parseaddr(_header(msg, "From"))[1].lower()
    if _is_complaint(msg):
        # Like a bounce, the report quotes our email.
        rid = _bounced_recipient(msg, watched)
        return ("complaint", rid) if rid is not None else None
    if _is_bounce(msg, sender):
        rid = _bounced_recipient(msg, watched)
        return ("bounce", rid) if rid is not None else None
    if _is_auto_reply(msg):
        return None
    rid = _match(msg, sender, watched, _date(msg, at))
    if rid is None:
        return None
    return ("opt_out" if _OPT_OUT.search(_reply_text(msg)) else "reply", rid)


def _add_to_do_not_contact(db: Session, recipient: CampaignRecipient) -> None:
    added = db.scalar(
        insert(DoNotContact)
        .values(value=recipient.email, kind=DncKind.EMAIL, reason=OPT_OUT_REASON)
        .on_conflict_do_nothing(index_elements=[DoNotContact.value])
        .returning(DoNotContact.value)
    )
    if added:
        log_activity(
            db,
            SYSTEM,
            "dnc.added",
            entity_type="dnc",
            entity_id=added,
            after={"reason": "reply", "campaign_id": recipient.campaign_id},
        )
        deals_optouts.queue_value(db, added)


def _complained(db: Session, recipient: CampaignRecipient) -> None:
    """They reported our email as spam: no more emails, ever, and Deals is told."""
    sending.stop_follow_ups(db, recipient, "complained")
    recipient.follow_up_stop = "complained"
    log_activity(
        db,
        SYSTEM,
        "email.complained",
        entity_type="lead",
        entity_id=recipient.lead_id,
        after={"campaign_id": recipient.campaign_id},
    )
    if not recipient.email or recipient.lead_id is None:
        return
    added = db.scalar(
        insert(DoNotContact)
        .values(value=recipient.email, kind=DncKind.EMAIL, reason=deals_optouts.COMPLAINT_REASON)
        .on_conflict_do_nothing(index_elements=[DoNotContact.value])
        .returning(DoNotContact.value)
    )
    if added:
        log_activity(
            db,
            SYSTEM,
            "dnc.added",
            entity_type="dnc",
            entity_id=added,
            after={"reason": "complaint", "campaign_id": recipient.campaign_id},
        )
        deals_optouts.queue_value(db, added)


def _message_id(msg: Message) -> str:
    found = _MESSAGE_ID.findall(_header(msg, "Message-ID"))
    if found:
        return found[0][:300]
    # Rare, but possible: make a stable one so the same message is still only stored once.
    seed = "|".join(_header(msg, h) for h in ("From", "Date", "Subject")).encode()
    return f"<no-id-{hashlib.sha256(seed).hexdigest()[:32]}@nexareach>"


def process(
    db: Session, mailbox: Mailbox, msg: Message, watched: Watched, at: datetime
) -> tuple[str, int] | None:
    """Record one inbox message if it concerns us: ("reply" | "opt_out" | "bounce", campaign id), or None."""
    found = classify(msg, watched, at)
    if found is None:
        return None
    kind, rid = found
    if kind == "reply" and _is_unsubscribe_subject(msg) and email_footer(db) is not None:
        kind = "opt_out"  # the List-Unsubscribe button sends an email with this subject
    recipient = db.get(CampaignRecipient, rid, with_for_update=True)
    if recipient is None:
        return None
    if kind == "complaint":
        if recipient.follow_up_stop == "complained":
            return None  # the same report again
        _complained(db, recipient)
        return kind, recipient.campaign_id
    if kind == "bounce":
        text, code, remote = bounce_details(msg)
        status = delivery.bounce_kind(text, code)
        added = delivery.record(
            db,
            source=DeliverySource.BOUNCE,
            status=status,
            at=min(_date(msg, at), at),
            recipient=recipient,
            mailbox_id=mailbox.id,
            response=f"{code} {text}" if code and code not in text else text,
            remote_host=remote,
            external_id=f"bounce:{mailbox.id}:{_message_id(msg)}",
        )
        if not added:
            return None
        delivery.bounced(db, recipient, status, "bounce_email")
        return kind, recipient.campaign_id

    message_id = _message_id(msg)
    if db.scalar(select(Reply.id).where(Reply.mailbox_id == mailbox.id, Reply.message_id == message_id)):
        return None
    full_text, attachments = _full_text(msg)
    name, address = parseaddr(_header(msg, "From"))
    received = min(_date(msg, at), at)
    reply = Reply(
        mailbox_id=mailbox.id,
        recipient_id=recipient.id,
        campaign_id=recipient.campaign_id,
        kind=ReplyKind.OPT_OUT if kind == "opt_out" else ReplyKind.REPLY,
        from_email=address.lower()[:320],
        from_name=name.strip()[:200] or None,
        subject=_header(msg, "Subject").strip()[:998],
        text=_strip_quote(full_text)[:MAX_TEXT],
        full_text=full_text.strip()[:MAX_TEXT],
        has_attachments=attachments,
        message_id=message_id,
        references=" ".join(_MESSAGE_ID.findall(_header(msg, "References")))[-5000:] or None,
        received_at=received,
        cc=[a.lower()[:320] for _, a in getaddresses([_header(msg, "Cc")]) if "@" in a][:50] or None,
    )
    db.add(reply)
    db.flush()
    first = recipient.replied_at is None
    if first:
        recipient.replied_at = received
    sending.stop_follow_ups(db, recipient, "opted_out" if kind == "opt_out" else "replied")
    if kind == "opt_out":
        if recipient.follow_up_stop in (None, "replied"):
            recipient.follow_up_stop = "opted_out"
        _add_to_do_not_contact(db, recipient)
    log_activity(
        db,
        SYSTEM,
        "email.replied",
        entity_type="lead",
        entity_id=recipient.lead_id,
        after={
            "campaign_id": recipient.campaign_id,
            "reply_id": reply.id,
            "opt_out": kind == "opt_out",
            "first_reply": first,
        },
    )
    _score_and_hand_over(db, reply, recipient, kind, received)
    return kind, recipient.campaign_id


def _score_and_hand_over(
    db: Session, reply: Reply, recipient: CampaignRecipient, kind: str, at: datetime
) -> None:
    """Deals integration (steps 3 and 4), each off until switched on: score the reply; a later real reply from
    someone already handed to Deals goes straight there as "repeat", whatever its score."""
    from app.services import deals_handover, reply_scores  # these import modules that import this one

    reply_scores.score(db, reply, at)
    lead = db.get(Lead, recipient.lead_id) if recipient.lead_id else None
    if kind == "reply" and lead is not None and lead.handed_to_deals_at and deals_handover.enabled(db):
        reply.review_status = None
        deals_handover.queue(db, reply, "repeat", at)


def check_mailbox(
    db: Session,
    mailbox_id: int,
    at: datetime,
    read: ReadInbox = mailer.read_inbox,
    open_inbox: OpenInbox = mailer.Inbox,
) -> dict[str, int]:
    """Read new inbox messages for one mailbox and record replies, opt-outs and bounces. Commits."""
    counts = {"messages": 0, "reply": 0, "opt_out": 0, "bounce": 0, "complaint": 0}
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None or not mailbox.imap_host or mailer.development_block(mailbox):
        return counts
    watched = watched_for(db, mailbox.id, at)
    if not watched.sent_at:
        return counts
    since = (watched.since or at).date() - timedelta(days=1)
    try:
        batch = read(mailbox, mailbox.inbox_last_uid, mailbox.inbox_uidvalidity, since)
    except mailer.InboxFailed as e:
        mailbox.inbox_error = str(e)[:1000]
        mailbox.inbox_checked_at = at
        db.commit()
        log.warning("reply check failed for mailbox %s: %s", mailbox.id, e)
        return counts

    touched: set[int] = set()
    bounce_uids: list[int] = []
    last_uid = mailbox.inbox_last_uid if batch.uidvalidity == mailbox.inbox_uidvalidity else None
    for item in batch.messages:
        counts["messages"] += 1
        last_uid = item.uid if last_uid is None else max(last_uid, item.uid)
        try:
            msg = email.message_from_bytes(item.raw)
            if parseaddr(_header(msg, "From"))[1].lower() == mailbox.email:
                continue  # our own message (a copy to ourselves)
            # One broken message must not lose the others: each is saved or undone on its own.
            with db.begin_nested():
                found = process(db, mailbox, msg, watched, at)
        except Exception:
            log.exception("couldn't read inbox message %s of mailbox %s", item.uid, mailbox.id)
            continue
        if found is not None:
            counts[found[0]] += 1
            touched.add(found[1])
            if found[0] in ("bounce", "complaint"):
                bounce_uids.append(item.uid)  # reports, not people: out of the inbox

    mailbox.inbox_uidvalidity = batch.uidvalidity
    mailbox.inbox_last_uid = last_uid
    mailbox.inbox_checked_at = at
    mailbox.inbox_error = None
    for campaign_id in touched:
        sending.finish_if_done(db, campaign_id, at)
    db.commit()
    if bounce_uids:
        _file_bounces(mailbox, bounce_uids, open_inbox)
    return counts


def _file_bounces(mailbox: Mailbox, uids: list[int], open_inbox: OpenInbox) -> None:
    """Move recorded bounce emails out of the inbox into "Bounces". Best effort: they're already recorded."""
    try:
        with open_inbox(mailbox) as inbox:
            folder = inbox.ensure_folder(BOUNCES_FOLDER)
            for uid in uids:
                inbox.move("INBOX", str(uid), folder)
    except mailer.InboxFailed as e:
        log.warning("couldn't move bounce emails of mailbox %s: %s", mailbox.id, e)


def mark_replied(db: Session, recipient: CampaignRecipient, actor: Actor, at: datetime) -> None:
    """By hand, for replies that came another way or to a mailbox without IMAP. The caller commits."""
    recipient.replied_at = at
    sending.stop_follow_ups(db, recipient, "replied")
    log_activity(
        db,
        actor,
        "email.marked_replied",
        entity_type="lead",
        entity_id=recipient.lead_id,
        after={"campaign_id": recipient.campaign_id},
    )
    sending.finish_if_done(db, recipient.campaign_id, at)


class AnswerResult:
    def __init__(self, answer: ReplyAnswer, detail: str):
        self.answer = answer
        self.detail = detail


def answer(
    db: Session,
    reply: Reply,
    body: str,
    user_id: int | None,
    actor: Actor,
    at: datetime,
    deliver: sending.Deliver | None = None,
) -> AnswerResult:
    """Send an answer to a reply, from the mailbox that got it, as part of the same thread. Commits.

    Sent straight away (it's a personal answer, not a campaign email: no sending window or daily cap).
    A failure is recorded and returned, never raised.
    """
    mailbox = db.get(Mailbox, reply.mailbox_id)
    subject = sending.reply_subject(reply.subject or "Your email")
    thread = list(dict.fromkeys([*_MESSAGE_ID.findall(reply.references or ""), reply.message_id]))[-20:]
    text = body.strip() + "\n"
    msg = mailer.build_message(mailbox, reply.from_email, subject, text, thread=thread)
    record = ReplyAnswer(
        reply_id=reply.id,
        mailbox_id=mailbox.id,
        sent_by_id=user_id,
        to_email=reply.from_email,
        subject=subject,
        body=text,
    )
    try:
        sent = (deliver or mailer.send)(mailbox, msg)
    except mailer.AuthFailed as e:
        record.status = AnswerStatus.FAILED
        record.error = f"The mail server rejected {mailbox.email}'s username or password ({e})."[:1000]
    except mailer.SendFailed as e:
        record.status = AnswerStatus.FAILED
        record.error = str(e)[:1000]
    else:
        record.status = AnswerStatus.SENT
        record.message_id = sent.message_id
        record.smtp_response = sent.response
        reply.read_at = reply.read_at or at
        reply.done_at = reply.done_at or at
    db.add(record)
    db.flush()
    ok = record.status == AnswerStatus.SENT
    log_activity(
        db,
        actor,
        "reply.answered" if ok else "reply.answer_failed",
        entity_type="reply",
        entity_id=reply.id,
        after={"mailbox": mailbox.email, "answer_id": record.id, **({} if ok else {"error": record.error})},
    )
    db.commit()
    if ok and mailbox.imap_host:
        try:
            mailbox.sent_folder = mailer.save_sent_copy(mailbox, msg)
            record.sent_copy_saved = True
        except mailer.SentCopyFailed as e:
            record.sent_copy_saved = False
            log.warning("couldn't save a Sent copy of an answer: %s", e)
        db.commit()
    detail = f"Sent from {mailbox.email}." if ok else (record.error or "Sending failed.")
    return AnswerResult(record, detail)


def check_all(read: ReadInbox = mailer.read_inbox, open_inbox: OpenInbox = mailer.Inbox) -> None:
    with SessionLocal() as db:
        mailbox_ids = db.scalars(
            select(Mailbox.id).where(
                Mailbox.imap_host.is_not(None),
                Mailbox.archived_at.is_(None),
                Mailbox.status != MailboxStatus.ERROR,
            )
        ).all()
    for mailbox_id in mailbox_ids:
        try:
            with SessionLocal() as db:
                counts = check_mailbox(db, mailbox_id, sending.now(), read, open_inbox)
            if counts["reply"] or counts["opt_out"] or counts["bounce"] or counts["complaint"]:
                log.info("mailbox %s: %s", mailbox_id, counts)
        except Exception:
            # One broken mailbox must not stop the others.
            log.exception("reply check failed for mailbox %s", mailbox_id)
