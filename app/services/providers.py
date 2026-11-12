"""Who hosts an email address (Google, Microsoft or other), from the domain's MX records.

Used for the provider mix shown after an import and to filter leads. Lookups run in parallel with a
short timeout; a domain that can't be resolved is UNKNOWN.
"""

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

import dns.exception
import dns.resolver

from app.models import EmailProvider

KNOWN = {
    "gmail.com": EmailProvider.GOOGLE,
    "googlemail.com": EmailProvider.GOOGLE,
    "outlook.com": EmailProvider.MICROSOFT,
    "hotmail.com": EmailProvider.MICROSOFT,
    "hotmail.co.uk": EmailProvider.MICROSOFT,
    "live.com": EmailProvider.MICROSOFT,
    "msn.com": EmailProvider.MICROSOFT,
}
LOOKUP_TIMEOUT_SECONDS = 4.0
MAX_PARALLEL = 16


def mx_hosts(domain: str) -> list[str]:
    """The domain's MX host names, lowercased. Raises on lookup failure."""
    answer = dns.resolver.resolve(domain, "MX", lifetime=LOOKUP_TIMEOUT_SECONDS)
    # A "null MX" (a single "." record, RFC 7505) means the domain accepts no mail at all.
    return [h for h in (str(r.exchange).rstrip(".").lower() for r in answer) if h]


def classify(hosts: list[str]) -> EmailProvider:
    if any(h.endswith(("google.com", "googlemail.com")) for h in hosts):
        return EmailProvider.GOOGLE
    if any(h.endswith(("outlook.com", "office365.com", "protection.outlook.com")) for h in hosts):
        return EmailProvider.MICROSOFT
    return EmailProvider.OTHER if hosts else EmailProvider.UNKNOWN


def detect(domain: str) -> EmailProvider:
    domain = domain.lower()
    if domain in KNOWN:
        return KNOWN[domain]
    try:
        return classify(mx_hosts(domain))
    except (dns.exception.DNSException, OSError):
        return EmailProvider.UNKNOWN


def detect_many(domains: Iterable[str]) -> dict[str, EmailProvider]:
    unique = sorted({d.lower() for d in domains})
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return dict(zip(unique, pool.map(detect, unique), strict=True))


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()
