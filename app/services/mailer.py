"""SMTP: test a mailbox's login and send one message. One connection per email; we send slowly."""

import contextlib
import imaplib
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import date
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


# Mail servers that only exist on this machine (Mailpit). A development copy may only send through these.
LOCAL_SMTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "mailpit", "host.docker.internal"})


def development_block(mailbox: Mailbox) -> str | None:
    """Why this mailbox may not send here, or None. Only applies when APP_ENV=development."""
    if not get_settings().is_development or mailbox.smtp_host.strip().lower() in LOCAL_SMTP_HOSTS:
        return None
    return (
        f"This is a development copy (APP_ENV=development), so it only sends through Mailpit. "
        f"{mailbox.email} uses the real mail server {mailbox.smtp_host}."
    )


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
    mailbox: Mailbox,
    to: str,
    subject: str,
    body: str,
    html: str | None = None,
    *,
    thread: list[str] | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    """`thread`: Message-IDs of the earlier emails in the conversation, oldest first. A follow-up sets
    In-Reply-To and References from them, so mail apps show it under the first email."""
    msg = EmailMessage()
    msg["From"] = formataddr((mailbox.display_name, mailbox.email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id or make_msgid(domain=mailbox.email.rsplit("@", 1)[1])
    if thread:
        msg["In-Reply-To"] = thread[-1]
        msg["References"] = " ".join(thread)
    msg.set_content(body)
    if html:
        # multipart/alternative: mail apps show the HTML, and fall back to the plain text.
        msg.add_alternative(f"<!doctype html><html><body>{html}</body></html>", subtype="html")
    return msg


# --- Sent copy over IMAP ----------------------------------------------------------------------------

SENT_NAMES = ("INBOX.Sent", "Sent", "Sent Items", "Sent Messages", "INBOX.Sent Items", "INBOX.Sent Messages")


class SentCopyFailed(Exception):
    pass


def _imap_connection(host: str, port: int) -> imaplib.IMAP4:
    """IMAP over SSL; plain IMAP only for a test server on this computer (GreenMail), never a real one."""
    if host.strip().lower() in LOCAL_SMTP_HOSTS and get_settings().smtp_allow_plain and port != 993:
        return imaplib.IMAP4(host, port, timeout=TIMEOUT_SECONDS)
    context = ssl.create_default_context(cafile=certifi.where())
    return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=TIMEOUT_SECONDS)


def _imap(mailbox: Mailbox) -> imaplib.IMAP4:
    """Signed in to the account's IMAP. Works for a Mailbox or a WarmupHelper (same field names)."""
    if not mailbox.imap_host:
        raise SentCopyFailed("No incoming mail (IMAP) server is set for this mailbox.")
    conn = _imap_connection(mailbox.imap_host, mailbox.imap_port)
    try:
        conn.login(mailbox.smtp_username, decrypt(mailbox.smtp_password_encrypted))
    except imaplib.IMAP4.error as e:
        conn.logout()
        raise SentCopyFailed(f"The IMAP server rejected the username or password ({e}).") from e
    return conn


def _list_folders(conn: imaplib.IMAP4) -> list[tuple[str, str, str]]:
    """(flags, delimiter, name) for every folder."""
    status, rows = conn.list()
    if status != "OK":
        raise SentCopyFailed("Couldn't list the mailbox's folders.")
    folders: list[tuple[str, str, str]] = []
    for raw in rows or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        match = re.match(r'\((?P<flags>[^)]*)\) "?(?P<sep>[^" ]*)"? (?P<name>.+)$', line)
        if match:
            folders.append((match["flags"], match["sep"], match["name"].strip().strip('"')))
    return folders


def _find_sent_folder(conn: imaplib.IMAP4) -> str:
    folders = [(flags, name) for flags, _, name in _list_folders(conn)]
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


# --- Inbox over IMAP (reply checking) ---------------------------------------------------------------

# Enough of each message for its text (stored for the Replies page); big attachments are cut off.
INBOX_TEXT_BYTES = 300_000
INBOX_BATCH = 200


class InboxFailed(Exception):
    pass


@dataclass
class InboxMessage:
    uid: int
    # The first INBOX_TEXT_BYTES of the message, headers included (enough to parse; big attachments cut off).
    raw: bytes


@dataclass
class InboxBatch:
    uidvalidity: int
    messages: list[InboxMessage]


def read_inbox(mailbox: Mailbox, after_uid: int | None, uidvalidity: int | None, since: date) -> InboxBatch:
    """New messages in INBOX, read-only (nothing is marked as read).

    Picks up after `after_uid` when the inbox still has the same UIDVALIDITY; otherwise (first check, or
    the server renumbered the inbox) reads everything that arrived since `since`. At most INBOX_BATCH
    messages per call, oldest first; the next call continues.
    """
    try:
        conn = _imap(mailbox)
    except SentCopyFailed as e:
        raise InboxFailed(str(e)) from e
    except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
        raise InboxFailed(f"Couldn't connect to the IMAP server ({e}).") from e
    try:
        status, data = conn.select("INBOX", readonly=True)
        if status != "OK":
            raise InboxFailed(f"Couldn't open the inbox ({data!r}).")
        current = _uidvalidity(conn)
        if after_uid is not None and uidvalidity == current:
            status, data = conn.uid("SEARCH", None, f"UID {after_uid + 1}:*")
        else:
            status, data = conn.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            raise InboxFailed(f"Couldn't search the inbox ({data!r}).")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        # "UID n:*" always returns the newest message, even when it is older than n.
        uids = [u for u in uids if after_uid is None or uidvalidity != current or u > after_uid]
        messages = [_fetch(conn, uid) for uid in uids[:INBOX_BATCH]]
        return InboxBatch(uidvalidity=current, messages=[m for m in messages if m is not None])
    except (OSError, imaplib.IMAP4.error) as e:
        raise InboxFailed(f"Reading the inbox failed ({e}).") from e
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _uidvalidity(conn: imaplib.IMAP4_SSL) -> int:
    _, data = conn.response("UIDVALIDITY")
    if data and data[0]:
        return int(data[0])
    status, data = conn.status("INBOX", "(UIDVALIDITY)")
    match = re.search(rb"UIDVALIDITY (\d+)", data[0] if status == "OK" and data and data[0] else b"")
    if not match:
        raise InboxFailed("The IMAP server didn't report the inbox's UIDVALIDITY.")
    return int(match.group(1))


def _fetch(conn: imaplib.IMAP4_SSL, uid: int) -> InboxMessage | None:
    # One piece (headers and the start of the body together). Asking for the header and the text separately
    # and joining them breaks when a server returns the two in the other order, which servers may do.
    status, data = conn.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{INBOX_TEXT_BYTES}>)")
    if status != "OK":
        return None
    parts = [item[1] for item in data if isinstance(item, tuple) and len(item) == 2]
    if not parts:
        return None
    return InboxMessage(uid=uid, raw=parts[0])


# --- Read-write IMAP for warm-up (helper actions and filing) ------------------------------------------

SPAM_NAMES = (
    "[Gmail]/Spam",
    "[Google Mail]/Spam",
    "Junk",
    "Junk Email",
    "Junk E-mail",
    "Bulk",
    "Bulk Mail",
    "Spam",
    "INBOX.Spam",
    "INBOX.Junk",
    "INBOX.spam",
)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Inbox:
    """A read-write IMAP session on one account: find a message, move it, flag it, create folders.

    For warm-up only; reply checking reads read-only (read_inbox). Raises InboxFailed.
    """

    def __init__(self, account: Mailbox) -> None:
        try:
            self.conn = _imap(account)
        except SentCopyFailed as e:
            raise InboxFailed(str(e)) from e
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
            raise InboxFailed(f"Couldn't connect to the IMAP server ({e}).") from e
        self.capabilities = {str(c).upper() for c in self.conn.capabilities}
        self._selected: str | None = None

    def __enter__(self) -> "Inbox":
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            self.conn.logout()

    def _ok(self, result: tuple[str, list], what: str) -> list:
        status, data = result
        if status != "OK":
            raise InboxFailed(f"{what} failed ({data!r}).")
        return data

    def _select(self, folder: str) -> None:
        if self._selected != folder:
            self._ok(self.conn.select(_quote(folder)), f"Opening {folder}")
            self._selected = folder

    @property
    def is_gmail(self) -> bool:
        return "X-GM-EXT-1" in self.capabilities

    def folders(self) -> list[tuple[str, str, str]]:
        try:
            return _list_folders(self.conn)
        except SentCopyFailed as e:
            raise InboxFailed(str(e)) from e

    def spam_folder(self) -> str | None:
        folders = self.folders()
        for flags, _, name in folders:
            if "\\Junk" in flags or "\\Spam" in flags:
                return name
        names = {name.lower(): name for _, _, name in folders}
        for candidate in SPAM_NAMES:
            if candidate.lower() in names:
                return names[candidate.lower()]
        return None

    def ensure_folder(self, name: str) -> str:
        """The folder's full name, creating it if needed (cPanel servers keep folders under "INBOX.")."""
        folders = self.folders()
        existing = {n.lower(): n for _, _, n in folders}
        sep = next((s for _, s, n in folders if n.upper() == "INBOX" and s), ".")
        nested = any(n.upper().startswith(f"INBOX{sep}") for _, _, n in folders)
        candidates = [f"INBOX{sep}{name}", name] if nested else [name, f"INBOX{sep}{name}"]
        for candidate in candidates:
            if candidate.lower() in existing:
                return existing[candidate.lower()]
        for candidate in candidates:
            status, _ = self.conn.create(_quote(candidate))
            if status == "OK":
                return candidate
        raise InboxFailed(f"Couldn't create the {name} folder.")

    def search(self, folder: str, text: str, headers: tuple[str, ...] = ("Message-ID",)) -> list[str]:
        """UIDs in the folder whose given headers contain the text."""
        self._select(folder)
        criteria: list[str] = ["HEADER", headers[-1], f'"{text}"']
        for header in reversed(headers[:-1]):
            criteria = ["OR", "HEADER", header, f'"{text}"', *criteria]
        data = self._ok(self.conn.uid("SEARCH", None, *criteria), "Searching")
        return [u.decode() for u in (data[0] or b"").split()]

    def gmail_search(self, folder: str, text: str, query: str) -> list[str]:
        """UIDs whose Message-ID contains the text and that match a Gmail search ("category:promotions")."""
        self._select(folder)
        criteria = ["HEADER", "Message-ID", f'"{text}"', "X-GM-RAW", '"' + query.replace('"', "") + '"']
        data = self._ok(self.conn.uid("SEARCH", None, *criteria), "Searching")
        return [u.decode() for u in (data[0] or b"").split()]

    def header_text(self, folder: str, uid: str) -> str:
        self._select(folder)
        data = self._ok(
            self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)])"),
            "Reading",
        )
        return b"".join(part[1] for part in data if isinstance(part, tuple)).decode(errors="replace")

    def move(self, folder: str, uid: str, destination: str) -> None:
        self._select(folder)
        if "MOVE" in self.capabilities:
            self._ok(self.conn.uid("MOVE", uid, _quote(destination)), "Moving")
            return
        self._ok(self.conn.uid("COPY", uid, _quote(destination)), "Copying")
        self._ok(self.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)"), "Removing the original")
        self._ok(self.conn.expunge(), "Removing the original")

    def mark_engaged(self, folder: str, uid: str) -> None:
        """Read, starred, and (on Gmail) important: what a person does with an email they value."""
        self._select(folder)
        self._ok(self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen \\Flagged)"), "Flagging")
        if self.is_gmail:
            with contextlib.suppress(imaplib.IMAP4.error):
                self.conn.uid("STORE", uid, "+X-GM-LABELS", "(\\Important)")


def send(mailbox: Mailbox, msg: EmailMessage) -> Sent:
    """Send one message. Raises AuthFailed or SendFailed (with .temporary and .uncertain)."""
    if reason := development_block(mailbox):
        raise SendFailed(reason, temporary=False)
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
