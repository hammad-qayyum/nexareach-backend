"""SMTP: test a mailbox's login and send one message. One connection per email; we send slowly."""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import certifi

from app.core.config import get_settings
from app.core.crypto import decrypt
from app.models import Mailbox, SmtpSecurity

TIMEOUT_SECONDS = 30


class AuthFailed(Exception):
    """The server rejected the mailbox's username or password."""


class SendFailed(Exception):
    def __init__(self, message: str, *, temporary: bool, code: int | None = None):
        super().__init__(message)
        self.temporary = temporary
        self.code = code


@dataclass
class Sent:
    message_id: str
    response: str


def _connect(mailbox: Mailbox) -> smtplib.SMTP:
    # Mozilla's CA list from certifi, so TLS checks don't depend on the machine's own store
    # (python.org builds on macOS ship without one).
    context = ssl.create_default_context(cafile=certifi.where())
    if mailbox.smtp_security == SmtpSecurity.SSL:
        conn: smtplib.SMTP = smtplib.SMTP_SSL(
            mailbox.smtp_host, mailbox.smtp_port, timeout=TIMEOUT_SECONDS, context=context
        )
    else:
        if mailbox.smtp_security == SmtpSecurity.NONE and not get_settings().smtp_allow_plain:
            raise SendFailed("Unencrypted SMTP is turned off on this server.", temporary=False)
        conn = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=TIMEOUT_SECONDS)
        if mailbox.smtp_security == SmtpSecurity.STARTTLS:
            conn.starttls(context=context)
    conn.ehlo_or_helo_if_needed()
    # Mailpit (security NONE) may not offer AUTH at all; real servers always do.
    if mailbox.smtp_security != SmtpSecurity.NONE or conn.has_extn("auth"):
        try:
            conn.login(mailbox.smtp_username, decrypt(mailbox.smtp_password_encrypted))
        except smtplib.SMTPAuthenticationError as e:
            conn.close()
            raise AuthFailed(_reply(e.smtp_code, e.smtp_error)) from e
    return conn


def _reply(code: int, text: bytes | str) -> str:
    text = text.decode(errors="replace") if isinstance(text, bytes) else text
    return f"{code} {text}".strip()[:1000]


def _translate(e: Exception) -> SendFailed | AuthFailed:
    if isinstance(e, (AuthFailed, SendFailed)):
        return e
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        code, text = next(iter(e.recipients.values()))
        return SendFailed(_reply(code, text), temporary=400 <= code < 500, code=code)
    if isinstance(e, smtplib.SMTPResponseException):
        return SendFailed(
            _reply(e.smtp_code, e.smtp_error), temporary=400 <= e.smtp_code < 500, code=e.smtp_code
        )
    if isinstance(e, ssl.SSLError):
        return SendFailed(f"TLS error: {e}", temporary=False)
    if isinstance(e, (smtplib.SMTPException, OSError)):
        # Disconnects, timeouts, refused connections: worth another try later.
        return SendFailed(f"Connection problem: {e}", temporary=True)
    return SendFailed(str(e), temporary=True)


def check_login(mailbox: Mailbox) -> None:
    """Log in without sending anything. Raises AuthFailed or SendFailed."""
    try:
        conn = _connect(mailbox)
    except Exception as e:
        raise _translate(e) from e
    try:
        conn.quit()
    except smtplib.SMTPException:
        pass


def build_message(mailbox: Mailbox, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mailbox.display_name, mailbox.email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=mailbox.email.rsplit("@", 1)[1])
    msg.set_content(body)
    return msg


def send(mailbox: Mailbox, msg: EmailMessage) -> Sent:
    """Send one message. Raises AuthFailed or SendFailed (with .temporary)."""
    to = msg["To"]
    try:
        conn = _connect(mailbox)
        try:
            # Step by step rather than send_message(), to keep the server's final reply (it has the queue ID).
            code, text = conn.mail(mailbox.email)
            if code != 250:
                raise smtplib.SMTPSenderRefused(code, text, mailbox.email)
            code, text = conn.rcpt(to)
            if code not in (250, 251):
                raise smtplib.SMTPRecipientsRefused({to: (code, text)})
            code, text = conn.data(msg.as_bytes())
            if code != 250:
                raise smtplib.SMTPDataError(code, text)
        finally:
            try:
                conn.quit()
            except smtplib.SMTPException:
                conn.close()
    except Exception as e:
        raise _translate(e) from e
    return Sent(message_id=msg["Message-ID"], response=_reply(code, text))
