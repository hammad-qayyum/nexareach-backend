"""Talking to Deals (deals.fastnexa.com): signed JSON requests to /api/integrations/nexareach.

The contract is fixed by the Deals team (integration guide, 24 Nov 2026): every request carries
`X-Fastnexa-Signature: t=<unix seconds>,v1=<HMAC-SHA256 hex>` over "<t>\\n<METHOD>\\n<path?query>\\n<body>",
every POST an Idempotency-Key. Outside production every call is a test call ("test": true / ?test=1), so a
development or staging copy can never create anything real in Deals.

Only NexaReach calls Deals; Deals never calls NexaReach.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings

PREFIX = "/api/integrations/nexareach"
VERSION = "0.1.0"
USER_AGENT = f"NexaReach/{VERSION}"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Tests swap in a fake Deals server here (httpx.MockTransport); None = the real network.
transport: httpx.BaseTransport | None = None


def email_hash(email: str) -> str:
    """How Deals names a person: sha256 of the trimmed, lower-case address, as 64 lower-case hex."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def sign(secret: str, t: int, method: str, path: str, body: bytes) -> str:
    """The v1 signature. `path` is the path with its query string exactly as sent; `body` the exact bytes."""
    message = f"{t}\n{method.upper()}\n{path}\n".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class DealsError(Exception):
    """A call to Deals that didn't work. `kind` says what to do about it:

    - invalid (400): our data is wrong; don't retry. `fields` lists what Deals didn't like.
    - signature (401): bad or old signature; check the clock and the secret. Don't retry.
    - conflict (409): same Idempotency-Key with a different body: a bug. Don't retry.
    - gone (410): the blocklist cursor is too old; take a full snapshot.
    - rate_limited (429): wait `retry_after` seconds.
    - unavailable (5xx, network, nonsense answer): Deals' problem; retry later.
    - not_configured: no secret saved yet.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status: int | None = None,
        fields: dict[str, str] | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.fields = fields or {}
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in ("unavailable", "rate_limited")


def _error(response: httpx.Response) -> DealsError:
    code = response.status_code
    if code == 400:
        try:
            fields = response.json().get("fields") or {}
        except ValueError:
            fields = {}
        detail = "; ".join(f"{k}: {v}" for k, v in fields.items()) or "no details"
        return DealsError("invalid", f"Deals refused the data ({detail}).", status=code, fields=fields)
    if code == 401:
        return DealsError(
            "signature", "Deals rejected our signature: check the clock and the secret.", status=code
        )
    if code == 409:
        return DealsError(
            "conflict", "Deals saw the same Idempotency-Key with a different body (a bug).", status=code
        )
    if code == 410:
        return DealsError("gone", "Deals no longer has changes from that far back.", status=code)
    if code == 429:
        try:
            wait = int(response.headers.get("Retry-After", "60"))
        except ValueError:
            wait = 60
        return DealsError(
            "rate_limited", f"Deals asked us to slow down ({wait} s).", status=code, retry_after=wait
        )
    if code >= 500:
        return DealsError("unavailable", f"Deals had a problem (HTTP {code}).", status=code)
    return DealsError("invalid", f"Deals answered HTTP {code}.", status=code)


@dataclass
class Client:
    url: str
    secret: str
    # Test calls: Deals checks everything and answers, but saves nothing. Forced on outside production.
    test: bool = False
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        if get_settings().app_env != "production":
            self.test = True
        self.url = self.url.rstrip("/")

    def get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        query = list(params or [])
        if self.test:
            query.append(("test", "1"))
        return self._send("GET", path, query, b"", {})

    def post(self, path: str, body: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        # Serialised once: these exact bytes are signed and sent. Sorted keys, so the same data always gives
        # the same bytes (a retry must repeat the body exactly, and Postgres JSON doesn't keep key order).
        raw = json.dumps(
            {**body, "test": self.test}, separators=(",", ":"), ensure_ascii=False, sort_keys=True
        ).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key[:200]}
        return self._send("POST", path, [], raw, headers)

    def _send(
        self, method: str, path: str, query: list[tuple[str, str]], body: bytes, headers: dict[str, str]
    ) -> dict[str, Any]:
        url = f"{self.url}{PREFIX}{path}"
        if query:
            url += "?" + urlencode(query)
        with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False) as http:
            request = http.build_request(
                method, url, content=body or None, headers={"User-Agent": USER_AGENT, **headers}
            )
            t = int(self.clock())
            # Signed over the path and query exactly as they go on the wire.
            signature = sign(self.secret, t, method, request.url.raw_path.decode(), body)
            request.headers["X-Fastnexa-Signature"] = f"t={t},v1={signature}"
            try:
                response = http.send(request)
            except httpx.HTTPError as e:
                raise DealsError("unavailable", f"Couldn't reach Deals ({type(e).__name__}).") from e
        if response.status_code != 200:
            raise _error(response)
        try:
            data = response.json()
        except ValueError as e:
            raise DealsError("unavailable", "Deals answered with something that isn't JSON.") from e
        if not isinstance(data, dict):
            raise DealsError("unavailable", "Deals answered with something unexpected.")
        return data
