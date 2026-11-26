"""Daily health checks of the sending domains and mail servers (plan section 19).

For each domain our mailboxes send from: MX, SPF, DKIM and DMARC records. For each mail server: its TLS
certificate (expiry and validity) and whether its IP addresses are on a blacklist. Spamhaus is only asked
with a (free) DQS key from Settings: its public servers refuse queries from most networks.

Every result is kept (history). A broken record or a blacklisting pauses the mailboxes it affects; an
expiring certificate only warns (app.services.alerts). A check that couldn't run (a DNS timeout, a list that
didn't answer) is UNKNOWN and never raises an alert.

Lookups are injectable so tests never touch real DNS or mail servers.
"""

import ipaddress
import logging
import smtplib
import socket
import ssl
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import certifi
import dns.exception
import dns.resolver
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import decrypt
from app.models import HealthCheck, HealthKind, HealthStatus, Mailbox, SmtpSecurity
from app.services import alerts
from app.services.app_settings import get_json
from app.services.mailer import LOCAL_SMTP_HOSTS

log = logging.getLogger(__name__)

SETTINGS_KEY = "deliverability.checks"  # {"dkim_selector": "default", "spamhaus_key_encrypted": ...}
DEFAULT_DKIM_SELECTOR = "default"  # what cPanel uses
DNS_TIMEOUT = 5.0
TLS_TIMEOUT = 30  # the same as sending; mail.fastnexa.com takes ~8 s to finish the handshake

# Free IP blacklists that answer anyone. (UCEPROTECT is left out on purpose: it lists whole hosting
# networks, which would pause sending for something we can't fix.)
IP_LISTS = [
    ("SpamCop", "bl.spamcop.net"),
    ("Barracuda", "b.barracudacentral.org"),
    ("PSBL", "psbl.surriel.com"),
]
# Domains that can never be real senders (development mailboxes): not checked.
TEST_DOMAINS = ("example.com", "example.net", "example.org")
TEST_TLDS = (".test", ".invalid", ".localhost", ".example", ".local")


class LookupFailed(Exception):
    """The DNS question got no answer (timeout, server failure). Not the same as "no such record"."""


Lookup = Callable[[str, str], list[str]]
CertExpiry = Callable[[str, int, SmtpSecurity], datetime]


def dns_lookup(name: str, rdtype: str) -> list[str]:
    """Records as text ([] when there are none). TXT records are joined; MX as "preference host"."""
    try:
        answer = dns.resolver.resolve(name, rdtype, lifetime=DNS_TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as e:
        raise LookupFailed(str(e) or e.__class__.__name__) from e
    records = []
    for r in answer:
        if rdtype == "TXT":
            records.append(b"".join(r.strings).decode(errors="replace"))
        elif rdtype == "MX":
            records.append(f"{r.preference} {r.exchange.to_text().rstrip('.')}")
        else:
            records.append(r.to_text())
    return records


def cert_expiry(host: str, port: int, security: SmtpSecurity) -> datetime:
    """When the mail server's certificate expires. Raises ssl.SSLError if it isn't valid, OSError if the
    server can't be reached."""
    context = ssl.create_default_context(cafile=certifi.where())
    if security == SmtpSecurity.STARTTLS:
        with smtplib.SMTP(host, port, timeout=TLS_TIMEOUT) as conn:
            conn.starttls(context=context)
            cert = conn.sock.getpeercert()  # type: ignore[union-attr]
    else:
        with (
            socket.create_connection((host, port), timeout=TLS_TIMEOUT) as sock,
            context.wrap_socket(sock, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), UTC)


def is_test_domain(domain: str) -> bool:
    d = domain.lower()
    return d.endswith(TEST_TLDS) or any(d == t or d.endswith("." + t) for t in TEST_DOMAINS)


@dataclass
class Result:
    kind: HealthKind
    target: str
    status: HealthStatus
    detail: str
    data: dict = field(default_factory=dict)


def _one(records: list[str], prefix: str) -> list[str]:
    return [r for r in records if r.strip().lower().startswith(prefix)]


def check_mx(domain: str, lookup: Lookup) -> Result:
    records = lookup(domain, "MX")
    hosts = [r.split(" ", 1)[-1] for r in records]
    if not hosts or hosts == ["."] or hosts == [""]:
        return Result(
            HealthKind.MX,
            domain,
            HealthStatus.FAIL,
            "No MX record: replies and bounces can't reach this domain.",
        )
    return Result(
        HealthKind.MX, domain, HealthStatus.OK, "Mail goes to " + ", ".join(hosts), {"records": records}
    )


def check_spf(domain: str, lookup: Lookup) -> Result:
    found = _one(lookup(domain, "TXT"), "v=spf1")
    if not found:
        return Result(
            HealthKind.SPF,
            domain,
            HealthStatus.FAIL,
            "No SPF record: receivers can't tell which servers may send.",
        )
    if len(found) > 1:
        return Result(
            HealthKind.SPF,
            domain,
            HealthStatus.FAIL,
            "More than one SPF record: receivers treat that as an error. Merge them into one.",
            {"records": found},
        )
    record = found[0]
    ending = record.split()[-1].lower()
    if ending in ("+all", "all", "?all"):
        return Result(
            HealthKind.SPF,
            domain,
            HealthStatus.WARN,
            f"Ends with {ending}: any server may send as this domain. Use ~all or -all.",
            {"record": record},
        )
    return Result(HealthKind.SPF, domain, HealthStatus.OK, record, {"record": record})


def check_dkim(domain: str, selector: str, lookup: Lookup) -> Result:
    name = f"{selector}._domainkey.{domain}"
    records = lookup(name, "TXT")
    key = next((r for r in records if "p=" in r.replace(" ", "")), None)
    if key is None:
        return Result(
            HealthKind.DKIM,
            domain,
            HealthStatus.FAIL,
            f"No DKIM key at {name}: emails aren't signed, or the selector in Settings is wrong.",
        )
    parts = {
        k.strip().lower(): v.strip()
        for k, _, v in (p.partition("=") for p in key.replace(" ", "").split(";") if "=" in p)
    }
    if not parts.get("p"):
        return Result(
            HealthKind.DKIM, domain, HealthStatus.FAIL, f"The DKIM key at {name} is empty (revoked)."
        )
    return Result(HealthKind.DKIM, domain, HealthStatus.OK, f"Key found at {name}", {"selector": selector})


def check_dmarc(domain: str, lookup: Lookup) -> Result:
    found = _one(lookup(f"_dmarc.{domain}", "TXT"), "v=dmarc1")
    if not found:
        return Result(
            HealthKind.DMARC,
            domain,
            HealthStatus.FAIL,
            "No DMARC record: Gmail and Yahoo require one from bulk senders.",
        )
    if len(found) > 1:
        return Result(
            HealthKind.DMARC, domain, HealthStatus.FAIL, "More than one DMARC record.", {"records": found}
        )
    record = found[0]
    tags = {
        k.strip().lower(): v.strip() for k, _, v in (p.partition("=") for p in record.split(";") if "=" in p)
    }
    policy = tags.get("p", "").lower()
    if "rua" not in tags:
        return Result(
            HealthKind.DMARC,
            domain,
            HealthStatus.WARN,
            "No rua= address: nobody receives the daily DMARC reports.",
            {"record": record, "policy": policy},
        )
    note = " (monitoring only; consider p=quarantine once reports look clean)" if policy == "none" else ""
    return Result(
        HealthKind.DMARC,
        domain,
        HealthStatus.OK,
        f"Policy {policy or '?'}{note}",
        {"record": record, "policy": policy},
    )


def _listed(answers: list[str]) -> bool | None:
    """True when listed, False when not, None when the list refused or errored (127.255.255.x)."""
    if not answers:
        return False
    addresses = [a for a in answers if a.startswith("127.")]
    if not addresses or all(a.startswith("127.255.255.") for a in addresses):
        return None
    return True


def check_blacklists(target: str, queries: list[tuple[str, str]], lookup: Lookup) -> Result:
    """`queries`: (list name, full DNS name to ask). Asked in parallel."""

    def ask(query: tuple[str, str]) -> tuple[str, bool | None]:
        name, dns_name = query
        try:
            return name, _listed(lookup(dns_name, "A"))
        except LookupFailed:
            return name, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(ask, queries))
    listed = [n for n, v in answers if v is True]
    unknown = [n for n, v in answers if v is None]
    clean = [n for n, v in answers if v is False]
    data = {"listed": listed, "unknown": unknown, "clean": clean}
    if listed:
        return Result(HealthKind.BLACKLIST, target, HealthStatus.FAIL, "Listed on " + ", ".join(listed), data)
    if unknown and not clean:
        return Result(
            HealthKind.BLACKLIST, target, HealthStatus.UNKNOWN, "The blacklists didn't answer.", data
        )
    detail = f"Not listed ({', '.join(clean)})"
    if unknown:
        detail += f"; no answer from {', '.join(unknown)}"
    return Result(HealthKind.BLACKLIST, target, HealthStatus.OK, detail, data)


def ip_queries(ip: str, spamhaus_key: str | None) -> list[tuple[str, str]]:
    reversed_ip = ".".join(reversed(ip.split(".")))
    queries = [(name, f"{reversed_ip}.{zone}") for name, zone in IP_LISTS]
    if spamhaus_key:
        queries.append(("Spamhaus", f"{reversed_ip}.{spamhaus_key}.zen.dq.spamhaus.net"))
    return queries


def check_certificate(
    host: str, port: int, security: SmtpSecurity, days: int, at: datetime, expiry: CertExpiry
) -> Result:
    try:
        expires = expiry(host, port, security)
    except ssl.SSLError as e:
        return Result(
            HealthKind.CERTIFICATE,
            host,
            HealthStatus.FAIL,
            f"The certificate isn't valid ({getattr(e, 'verify_message', None) or e}). Sending will fail.",
        )
    except (OSError, smtplib.SMTPException) as e:
        return Result(
            HealthKind.CERTIFICATE, host, HealthStatus.UNKNOWN, f"Couldn't connect to {host}:{port} ({e})."
        )
    left = expires - at
    data = {"expires_at": expires.isoformat(), "port": port}
    if left <= timedelta(0):
        return Result(
            HealthKind.CERTIFICATE, host, HealthStatus.FAIL, f"Expired on {expires:%d %b %Y}.", data
        )
    text = f"Valid until {expires:%d %b %Y} ({left.days} days)"
    if left <= timedelta(days=days):
        return Result(HealthKind.CERTIFICATE, host, HealthStatus.WARN, text + ": renew it soon.", data)
    return Result(HealthKind.CERTIFICATE, host, HealthStatus.OK, text, data)


def _spf_ips(result: Result) -> list[str]:
    """Single IPv4 addresses named in an SPF record (ip4:1.2.3.4 or /32): the servers we send through."""
    ips = []
    for part in str(result.data.get("record", "")).split():
        value = part.lstrip("+").lower()
        if value.startswith("ip4:"):
            addr = value[4:].removesuffix("/32")
            try:
                ipaddress.IPv4Address(addr)
            except ValueError:
                continue
            ips.append(addr)
    return ips


@dataclass
class Targets:
    domains: dict[str, list[int]]  # domain -> mailbox ids sending from it
    hosts: dict[tuple[str, int, SmtpSecurity], list[int]]  # mail server -> mailbox ids using it


def targets(db: Session) -> Targets:
    domains: dict[str, list[int]] = {}
    hosts: dict[tuple[str, int, SmtpSecurity], list[int]] = {}
    for m in db.scalars(select(Mailbox).where(Mailbox.archived_at.is_(None)).order_by(Mailbox.id)):
        domain = m.email.rsplit("@", 1)[-1]
        if not is_test_domain(domain):
            domains.setdefault(domain, []).append(m.id)
        host = m.smtp_host.strip().lower()
        if host not in LOCAL_SMTP_HOSTS and m.smtp_security != SmtpSecurity.NONE and not is_test_domain(host):
            hosts.setdefault((host, m.smtp_port, m.smtp_security), []).append(m.id)
    return Targets(domains, hosts)


def config(db: Session) -> tuple[str, str | None]:
    """(DKIM selector, Spamhaus DQS key or None)."""
    cfg = get_json(db, SETTINGS_KEY)
    key = None
    if cfg.get("spamhaus_key_encrypted"):
        try:
            key = decrypt(cfg["spamhaus_key_encrypted"])
        except ValueError:
            log.warning("the Spamhaus key can't be read (saved with another ENCRYPTION_KEY)")
    return (cfg.get("dkim_selector") or DEFAULT_DKIM_SELECTOR).strip(), key


def _safe(kind: HealthKind, target: str, fn: Callable[[], Result]) -> Result:
    try:
        return fn()
    except LookupFailed as e:
        return Result(kind, target, HealthStatus.UNKNOWN, f"The DNS lookup didn't answer ({e}).")


def run(
    db: Session, at: datetime, lookup: Lookup | None = None, expiry: CertExpiry | None = None
) -> list[Result]:
    """Run every check, store the results, update the alerts. Commits; emails new alerts."""
    lookup = lookup or dns_lookup
    expiry = expiry or cert_expiry
    found = targets(db)
    selector, spamhaus_key = config(db)
    limits = alerts.thresholds(db)
    results: list[Result] = []
    broken: list[alerts.Condition] = []
    listed: list[alerts.Condition] = []
    certs: list[alerts.Condition] = []
    ip_mailboxes: dict[str, set[int]] = {}
    # Which mailboxes each target matters for (shown on the mailbox cards).
    affects: dict[str, set[int]] = {}

    for domain, mailbox_ids in found.domains.items():
        affects.setdefault(domain, set()).update(mailbox_ids)
        checks = [
            _safe(HealthKind.MX, domain, lambda d=domain: check_mx(d, lookup)),
            _safe(HealthKind.SPF, domain, lambda d=domain: check_spf(d, lookup)),
            _safe(HealthKind.DKIM, domain, lambda d=domain: check_dkim(d, selector, lookup)),
            _safe(HealthKind.DMARC, domain, lambda d=domain: check_dmarc(d, lookup)),
        ]
        results += checks
        failed = [c for c in checks if c.status == HealthStatus.FAIL]
        if failed:
            broken.append(
                alerts.Condition(
                    f"{domain}: " + " ".join(f"{c.kind.value}: {c.detail}" for c in failed),
                    target=domain,
                    pause=mailbox_ids,
                )
            )
        spf = next(c for c in checks if c.kind == HealthKind.SPF)
        for ip in _spf_ips(spf):
            ip_mailboxes.setdefault(ip, set()).update(mailbox_ids)
        if spamhaus_key:
            dbl = check_blacklists(
                domain, [("Spamhaus DBL", f"{domain}.{spamhaus_key}.dbl.dq.spamhaus.net")], lookup
            )
            results.append(dbl)
            if dbl.status == HealthStatus.FAIL:
                listed.append(
                    alerts.Condition(f"{domain} is {dbl.detail}.", target=domain, pause=mailbox_ids)
                )

    for (host, port, security), mailbox_ids in found.hosts.items():
        affects.setdefault(host, set()).update(mailbox_ids)
        cert = check_certificate(host, port, security, limits.cert_days, at, expiry)
        results.append(cert)
        if cert.status in (HealthStatus.WARN, HealthStatus.FAIL):
            certs.append(alerts.Condition(f"{host}: {cert.detail}", target=host))
        try:
            for ip in lookup(host, "A"):
                ip_mailboxes.setdefault(ip, set()).update(mailbox_ids)
        except LookupFailed as e:
            log.warning("couldn't resolve %s: %s", host, e)

    for ip, mailbox_ids in sorted(ip_mailboxes.items()):
        affects.setdefault(ip, set()).update(mailbox_ids)
        result = check_blacklists(ip, ip_queries(ip, spamhaus_key), lookup)
        results.append(result)
        if result.status == HealthStatus.FAIL:
            listed.append(
                alerts.Condition(
                    f"The sending server {ip} is {result.detail[0].lower()}{result.detail[1:]}.",
                    target=ip,
                    pause=sorted(mailbox_ids),
                )
            )

    for r in results:
        r.data["mailbox_ids"] = sorted(affects.get(r.target, ()))
        db.add(
            HealthCheck(
                kind=r.kind,
                target=r.target,
                status=r.status,
                detail=r.detail[:2000],
                data=r.data,
                checked_at=at,
            )
        )
    # A target whose check couldn't run keeps its alert as it was.
    unknown = {r.target for r in results if r.status == HealthStatus.UNKNOWN}
    checked = {r.target for r in results} - unknown
    new = alerts.sync(db, "dns_broken", broken, at, scope=checked)
    new += alerts.sync(db, "blacklisted", listed, at, scope=checked)
    new += alerts.sync(db, "cert_expiring", certs, at, scope=checked)
    db.commit()
    alerts.email_admins(db, new, at)
    return results


def latest(db: Session) -> list[HealthCheck]:
    """The results of the latest run (every run checks everything, at one time)."""
    last = db.scalar(select(func.max(HealthCheck.checked_at)))
    if last is None:
        return []
    return list(
        db.scalars(
            select(HealthCheck)
            .where(HealthCheck.checked_at == last)
            .order_by(HealthCheck.target, HealthCheck.kind)
        )
    )


def history(db: Session, kind: HealthKind, target: str, days: int = 30) -> list[HealthCheck]:
    since = datetime.now(UTC) - timedelta(days=days)
    return list(
        db.scalars(
            select(HealthCheck)
            .where(HealthCheck.kind == kind, HealthCheck.target == target, HealthCheck.checked_at >= since)
            .order_by(HealthCheck.checked_at.desc())
        )
    )


def development_note() -> str | None:
    if get_settings().is_development:
        return "Development mailboxes use test domains and the local mail server, which aren't checked."
    return None
