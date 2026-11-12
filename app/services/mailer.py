"""SMTP: test a mailbox's login and send one message. One connection per email; we send slowly."""

import contextlib
import imaplib
import re
import smtplib
import socket
import ssl
import time
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
    def __init__(self, message: str, *, temporary: bool, code: int | None = None, uncertain: bool = False):
        super().__init__(message)
        self.temporary = temporary
        self.code = code
        # The message may already have been accepted (the connection broke while handing it over).
        # Retrying could email the person twice, so callers must not retry.
        self.uncertain = uncertain


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
        return SendFailed(
            f"Secure connection failed ({e}). "
            "Check that Security matches the port: SSL for 465, STARTTLS for 587.",
            temporary=False,
        )
    if isinstance(e, socket.gaierror):
        return SendFailed(
            "The server name wasn't found. Check the SMTP server, e.g. mail.fastnexa.com.", temporary=True
        )
    if isinstance(e, ConnectionRefusedError):
        return SendFailed(
            "The server refused the connection on that port. Check the port: 465 for SSL, 587 for STARTTLS.",
            temporary=True,
        )
    if isinstance(e, TimeoutError):
        return SendFailed(
            "No answer from the server (timed out). "
            "Check the server name and port; some networks block mail ports.",
            temporary=True,
        )
    if isinstance(e, (smtplib.SMTPException, OSError)):
        # Disconnects and other network trouble: worth another try later.
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


def build_message(
    mailbox: Mailbox, to: str, subject: str, body: str, html: str | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mailbox.display_name, mailbox.email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=mailbox.email.rsplit("@", 1)[1])
    msg.set_content(body)
    if html:
        # multipart/alternative: mail apps show the HTML, and fall back to the plain text.
        msg.add_alternative(f"<!doctype html><html><body>{html}</body></html>", subtype="html")
    return msg


# --- Sent copy over IMAP ----------------------------------------------------------------------------

SENT_NAMES = ("INBOX.Sent", "Sent", "Sent Items", "Sent Messages", "INBOX.Sent Items", "INBOX.Sent Messages")


class SentCopyFailed(Exception):
    pass


def _imap(mailbox: Mailbox) -> imaplib.IMAP4_SSL:
    if not mailbox.imap_host:
        raise SentCopyFailed("No incoming mail (IMAP) server is set for this mailbox.")
    context = ssl.create_default_context(cafile=certifi.where())
    conn = imaplib.IMAP4_SSL(
        mailbox.imap_host, mailbox.imap_port, ssl_context=context, timeout=TIMEOUT_SECONDS
    )
    try:
        conn.login(mailbox.smtp_username, decrypt(mailbox.smtp_password_encrypted))
    except imaplib.IMAP4.error as e:
        conn.logout()
        raise SentCopyFailed(f"The IMAP server rejected the username or password ({e}).") from e
    return conn


def _find_sent_folder(conn: imaplib.IMAP4_SSL) -> str:
    status, rows = conn.list()
    if status != "OK":
        raise SentCopyFailed("Couldn't list the mailbox's folders.")
    folders: list[tuple[str, str]] = []
    for raw in rows or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        match = re.match(r'\((?P<flags>[^)]*)\) "?(?P<sep>[^" ]*)"? (?P<name>.+)$', line)
        if match:
            folders.append((match["flags"], match["name"].strip().strip('"')))
    for flags, name in folders:
        if "\\Sent" in flags:
            return name
    names = {name: name for _, name in folders}
    for candidate in SENT_NAMES:
        if candidate in names:
            return candidate
    raise SentCopyFailed("No Sent folder found in this mailbox.")


def check_imap(mailbox: Mailbox) -> str:
    """Sign in to IMAP and find the Sent folder. Returns its name. Raises SentCopyFailed."""
    try:
        conn = _imap(mailbox)
    except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
        raise SentCopyFailed(f"Couldn't connect to the IMAP server ({e}).") from e
    try:
        return _find_sent_folder(conn)
    finally:
        # A failed goodbye changes nothing about what was already done.
        with contextlib.suppress(Exception):
            conn.logout()


def save_sent_copy(mailbox: Mailbox, msg: EmailMessage) -> str:
    """Append the sent message to the Sent folder, marked as read. Returns the folder used."""
    try:
        conn = _imap(mailbox)
    except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
        raise SentCopyFailed(f"Couldn't connect to the IMAP server ({e}).") from e
    try:
        folder = mailbox.sent_folder or _find_sent_folder(conn)
        quoted = '"' + folder.replace('"', '\\"') + '"'
        status, data = conn.append(quoted, "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != "OK":
            raise SentCopyFailed(f"The IMAP server refused the copy ({data!r}).")
        return folder
    except (OSError, imaplib.IMAP4.error) as e:
        raise SentCopyFailed(f"Saving the copy failed ({e}).") from e
    finally:
        # A failed goodbye changes nothing about what was already done.
        with contextlib.suppress(Exception):
            conn.logout()


def send(mailbox: Mailbox, msg: EmailMessage) -> Sent:
    """Send one message. Raises AuthFailed or SendFailed (with .temporary and .uncertain)."""
    to = msg["To"]
    handing_over = False
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
            handing_over = True
            code, text = conn.data(msg.as_bytes())
            handing_over = False
            if code != 250:
                raise smtplib.SMTPDataError(code, text)
        finally:
            # The message is already accepted or refused by now; a failed goodbye changes nothing.
            try:
                conn.quit()
            except Exception:
                conn.close()
    except Exception as e:
        if handing_over and not isinstance(e, smtplib.SMTPResponseException):
            # No definite answer from the server after the message went over the wire.
            raise SendFailed(
                f"The connection broke while the email was being handed over ({e}). It may have been "
                "delivered, so it wasn't retried.",
                temporary=False,
                uncertain=True,
            ) from e
        raise _translate(e) from e
    return Sent(message_id=msg["Message-ID"], response=_reply(code, text))
