"""Fetch a Google Sheet shared as "anyone with the link" as CSV.

Only Google's own hosts are ever contacted, so a pasted link can't make the server fetch arbitrary
URLs (SSRF). The browser can't do this itself because Google doesn't allow cross-site requests.
"""

import re
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT_SECONDS = 20
SHEET_URL = re.compile(r"^/spreadsheets/d/([A-Za-z0-9_-]{20,})")
ALLOWED_HOSTS = ("docs.google.com",)
ALLOWED_REDIRECT_SUFFIX = ".googleusercontent.com"


class SheetError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def export_url(link: str) -> str:
    parsed = urllib.parse.urlparse(link.strip())
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SheetError("sheet_link_invalid")
    match = SHEET_URL.match(parsed.path)
    if not match:
        raise SheetError("sheet_link_invalid")
    gid = urllib.parse.parse_qs(parsed.query).get("gid", [None])[0]
    gid = gid or urllib.parse.parse_qs(parsed.fragment).get("gid", [None])[0]
    query = {"format": "csv", **({"gid": gid} if gid and gid.isdigit() else {})}
    return f"https://docs.google.com/spreadsheets/d/{match[1]}/export?{urllib.parse.urlencode(query)}"


class _GoogleOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        host = urllib.parse.urlparse(newurl).hostname or ""
        if not (host in ALLOWED_HOSTS or host.endswith(ALLOWED_REDIRECT_SUFFIX)):
            raise SheetError("sheet_not_public")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_csv(link: str) -> str:
    opener = urllib.request.build_opener(_GoogleOnlyRedirects)
    request = urllib.request.Request(export_url(link), headers={"User-Agent": "NexaReach"})  # noqa: S310
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_BYTES + 1)
    except SheetError:
        raise
    except urllib.error.HTTPError as e:
        raise SheetError("sheet_not_public" if e.code in (401, 403, 404) else "sheet_fetch_failed") from e
    except (urllib.error.URLError, OSError) as e:
        raise SheetError("sheet_fetch_failed") from e
    if len(data) > MAX_BYTES:
        raise SheetError("sheet_too_large")
    # A private sheet answers with Google's sign-in page instead of CSV.
    if "text/csv" not in content_type:
        raise SheetError("sheet_not_public")
    return data.decode("utf-8-sig", errors="replace")
