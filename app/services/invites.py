"""Invite emails: the invite link, sent from a mailbox the admin picked in Settings.

Optional. With no mailbox chosen, the admin copies the link and sends it themselves (the link is always
shown either way). In a development copy this only works through Mailpit, like every other email.
"""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Mailbox, MailboxStatus, User
from app.services import mailer
from app.services.app_settings import get_value

INVITE_MAILBOX = "invites.mailbox_id"


def invite_mailbox(db: Session) -> Mailbox | None:
    """The chosen mailbox, if it is still usable."""
    value = get_value(db, INVITE_MAILBOX)
    mailbox = db.get(Mailbox, int(value)) if value.isdigit() else None
    if mailbox is None or mailbox.archived_at is not None:
        return None
    return mailbox


def compose_invite(user: User, invited_by: User, url: str) -> tuple[str, str]:
    days = get_settings().invite_valid_days
    subject = "You're invited to NexaReach"
    body = (
        f"Hi {user.name},\n\n"
        f"{invited_by.name} has invited you to NexaReach, FastNexa's outreach tool.\n\n"
        f"Choose your password here (the link works once and expires in {days} days):\n"
        f"{url}\n\n"
        "If you weren't expecting this, you can ignore this email.\n"
    )
    return subject, body


def email_invite(db: Session, user: User, invited_by: User, url: str) -> tuple[bool | None, str | None]:
    """Send the invite link. Returns (sent, detail); (None, None) when no invite mailbox is set up."""
    if not get_value(db, INVITE_MAILBOX):
        return None, None
    mailbox = invite_mailbox(db)
    if mailbox is None:
        return False, "The mailbox chosen for invite emails no longer exists. Pick another in Settings."
    if mailbox.status == MailboxStatus.ERROR:
        return False, f"{mailbox.email} isn't connected (its login failed). Fix it on the Mailboxes page."
    subject, body = compose_invite(user, invited_by, url)
    try:
        mailer.send(mailbox, mailer.build_message(mailbox, user.email, subject, body))
    except mailer.AuthFailed as e:
        return False, f"The mail server rejected {mailbox.email}'s username or password ({e})."
    except mailer.SendFailed as e:
        return False, str(e)
    return True, f"Emailed to {user.email} from {mailbox.email}."
