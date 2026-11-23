"""Who hosts an email address (Google, Microsoft, Zoho…), from the email's domain or its MX records.

Used for the provider mix shown after an import and to filter leads. Well-known free-mail domains are
recognised straight away; anything else is looked up in DNS, in parallel with a short timeout. A domain with
no mail server (or one that can't be resolved) is UNKNOWN.
"""

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

import dns.exception
import dns.resolver

from app.models import EmailProvider

P = EmailProvider

# Free-mail domains: known without a DNS lookup.
KNOWN: dict[str, EmailProvider] = {
    **dict.fromkeys(("gmail.com", "googlemail.com"), P.GOOGLE),
    **dict.fromkeys(
        (
            "outlook.com",
            "outlook.fr",
            "outlook.de",
            "outlook.es",
            "outlook.it",
            "hotmail.com",
            "hotmail.co.uk",
            "hotmail.fr",
            "hotmail.de",
            "hotmail.it",
            "hotmail.es",
            "live.com",
            "live.co.uk",
            "live.fr",
            "msn.com",
            "windowslive.com",
            "passport.com",
        ),
        P.MICROSOFT,
    ),
    **dict.fromkeys(
        (
            "yahoo.com",
            "yahoo.co.uk",
            "yahoo.co.in",
            "yahoo.ca",
            "yahoo.com.au",
            "yahoo.fr",
            "yahoo.de",
            "yahoo.it",
            "yahoo.es",
            "yahoo.co.jp",
            "ymail.com",
            "rocketmail.com",
            "aol.com",
            "aim.com",
            "aol.co.uk",
        ),
        P.YAHOO,
    ),
    **dict.fromkeys(("icloud.com", "me.com", "mac.com"), P.ICLOUD),
    **dict.fromkeys(("proton.me", "protonmail.com", "protonmail.ch", "pm.me"), P.PROTON),
    **dict.fromkeys(("zoho.com", "zohomail.com", "zohomail.in", "zohomail.eu"), P.ZOHO),
    **dict.fromkeys(("yandex.com", "yandex.ru", "ya.ru"), P.YANDEX),
}

# MX host name endings, checked in this order (a filter like Mimecast is what the MX shows, so it's named).
MX_ENDINGS: list[tuple[tuple[str, ...], EmailProvider]] = [
    (("google.com", "googlemail.com"), P.GOOGLE),
    (("outlook.com", "office365.com", "hotmail.com"), P.MICROSOFT),
    (("zoho.com", "zoho.eu", "zoho.in", "zoho.com.au", "zoho.jp", "zoho.sa", "zohocloud.ca"), P.ZOHO),
    (("yahoodns.net", "aol.com"), P.YAHOO),
    (("icloud.com", "me.com"), P.ICLOUD),
    (("protonmail.ch", "proton.me"), P.PROTON),
    (("secureserver.net",), P.GODADDY),
    (("titan.email",), P.TITAN),
    (("yandex.net", "yandex.ru"), P.YANDEX),
    (("amazonaws.com", "awsapps.com"), P.AMAZON),
    (("mimecast.com", "mimecast.co.za", "mimecast-offshore.com"), P.MIMECAST),
    (("pphosted.com", "ppe-hosted.com"), P.PROOFPOINT),
]
LOOKUP_TIMEOUT_SECONDS = 4.0
MAX_PARALLEL = 16


def mx_hosts(domain: str) -> list[str]:
    """The domain's MX host names, lowercased. Raises on lookup failure."""
    answer = dns.resolver.resolve(domain, "MX", lifetime=LOOKUP_TIMEOUT_SECONDS)
    # A "null MX" (a single "." record, RFC 7505) means the domain accepts no mail at all.
    return [h for h in (str(r.exchange).rstrip(".").lower() for r in answer) if h]


def _ends(host: str, endings: tuple[str, ...]) -> bool:
    return any(host == e or host.endswith("." + e) for e in endings)


def classify(hosts: list[str], domain: str = "") -> EmailProvider:
    if not hosts:
        return P.UNKNOWN
    for endings, provider in MX_ENDINGS:
        if any(_ends(h, endings) for h in hosts):
            return provider
    # mail.acme.com for acme.com: the company's own server (cPanel and the like).
    if domain and any(_ends(h, (domain.lower(),)) for h in hosts):
        return P.OWN_SERVER
    return P.OTHER


def detect(domain: str) -> EmailProvider:
    domain = domain.lower()
    if domain in KNOWN:
        return KNOWN[domain]
    try:
        return classify(mx_hosts(domain), domain)
    except (dns.exception.DNSException, OSError):
        return P.UNKNOWN


def detect_many(domains: Iterable[str]) -> dict[str, EmailProvider]:
    unique = sorted({d.lower() for d in domains})
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return dict(zip(unique, pool.map(detect, unique), strict=True))


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()
