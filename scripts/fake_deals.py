"""A fake Deals server on this computer, for trying the Deals integration by hand before the real one is live.

    .venv/bin/python scripts/fake_deals.py            # http://127.0.0.1:8099, secret "local-test-secret"
    .venv/bin/python scripts/fake_deals.py --secret S --port 8099

Then in NexaReach (development or staging only): Settings → Deals → address http://localhost:8099
and the same secret. Open http://127.0.0.1:8099 in a browser to block or unblock emails, pretend Deals
is down, and see every call NexaReach made. It follows the integration contract (signatures, blocklist
full/delta, check, services); the later endpoints (handover, optouts, events) answer 404 until those
steps are built. Nothing is saved: a restart starts empty. It only listens on this computer.
"""

import argparse
import hashlib
import hmac
import html
import json
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PREFIX = "/api/integrations/nexareach"
STAGES = ("contacted", "discovery", "proposal", "negotiation", "won", "lost", "disqualified")
REASONS = ("lead", "client", "lost", "meta_lead", "opted_out", "bounced", "complained", "forgotten")
SERVICES = [
    {"key": "vapt", "name": "VAPT"},
    {"key": "odoo", "name": "Odoo"},
    {"key": "web", "name": "Web development"},
]
STYLE = (
    "body{font:14px system-ui;margin:2rem;max-width:60rem}"
    "table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}"
    "td,th{border-bottom:1px solid #ddd;padding:.35rem;text-align:left}"
    "input,select,button{font:inherit;padding:.25rem .5rem}"
)


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


class State:
    def __init__(self, secret: str):
        self.secret = secret
        self.blocks: dict[str, dict] = {}  # hash -> {"hash", "reason", "until", "email"}
        self.changes: list[dict] = []  # for deltas; a cursor is a position in this list
        self.down = False
        self.calls: list[tuple[str, str, str, int]] = []
        self.optouts: list[dict] = []  # what NexaReach shared (POST /optouts)
        self.keys: dict[str, bytes] = {}  # Idempotency-Key → body: a repeat returns the first answer
        self.answers: dict[str, object] = {}  # Idempotency-Key → the first answer
        self.leads: dict[str, dict] = {}  # lead id → {"email", "reply_id", "stage", "handover"}
        self.events: list[dict] = []  # GET /events; a cursor is a position in this list
        self.handover_answer = "created"  # what the next new handover gets: created, existing or blocked

    def event(self, lead_id: str, **fields: object) -> None:
        lead = self.leads[lead_id]
        self.events.append(
            {
                "id": f"evt_{len(self.events) + 1}",
                "lead_id": lead_id,
                "reply_id": lead["reply_id"],
                "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **fields,
            }
        )

    def block(self, email: str, reason: str, until: str | None) -> None:
        h = email_hash(email)
        self.blocks[h] = {"hash": h, "reason": reason, "until": until, "email": email.strip().lower()}
        self.changes.append({"hash": h, "reason": reason, "until": until, "removed": False})

    def unblock(self, h: str) -> None:
        if self.blocks.pop(h, None):
            self.changes.append({"hash": h, "removed": True})

    def active(self, h: str) -> dict | None:
        b = self.blocks.get(h)
        if b and (b["until"] is None or b["until"] > datetime.now(UTC).isoformat()):
            return {"hash": h, "reason": b["reason"], "until": b["until"]}
        return None


STATE: State


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: object = None, content_type: str = "application/json") -> None:
        if isinstance(body, str):
            raw = body.encode()
        else:
            raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _signed(self, body: bytes) -> bool:
        try:
            parts = dict(p.split("=", 1) for p in self.headers["X-Fastnexa-Signature"].split(","))
            t = int(parts["t"])
        except (AttributeError, KeyError, ValueError):
            return False
        message = f"{t}\n{self.command}\n{self.path}\n".encode() + body
        good = hmac.new(STATE.secret.encode(), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, parts.get("v1", "")) and abs(time.time() - t) <= 300

    def _api(self, body: bytes) -> tuple[int, object]:
        if not self._signed(body):
            return 401, None
        if STATE.down:
            return 503, None
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.removeprefix(PREFIX)
        if self.command == "GET" and path == "/services":
            return 200, {"services": SERVICES}
        if self.command == "GET" and path == "/blocklist":
            if query.get("mode") == "full" or "since" not in query:
                items = [{k: b[k] for k in ("hash", "reason", "until")} for b in STATE.blocks.values()]
                cursor = str(len(STATE.changes))
                return 200, {"full": True, "items": items, "next_page_token": None, "cursor": cursor}
            since = query["since"]
            if not since.isdigit() or int(since) > len(STATE.changes):
                return 410, None
            items = STATE.changes[int(since) :]
            return 200, {"full": False, "items": items, "cursor": str(len(STATE.changes)), "has_more": False}
        if self.command == "POST":
            key = self.headers.get("Idempotency-Key", "")
            if not key:
                return 400, {"error": "invalid", "fields": {"Idempotency-Key": "required"}}
            if key in STATE.keys and STATE.keys[key] != body:
                return 409, None
            repeat = key in STATE.keys
            STATE.keys[key] = body
            if repeat and key in STATE.answers:
                return 200, STATE.answers[key]
        if self.command == "GET" and path == "/events":
            start = int(query.get("after") or 0)
            items = STATE.events[start : start + int(query.get("limit") or 200)]
            end = start + len(items)
            return 200, {"events": items, "cursor": str(end), "has_more": end < len(STATE.events)}
        if self.command == "POST" and path == "/handover":
            payload = json.loads(body or b"{}")
            email = payload.get("prospect", {}).get("email", "")
            existing = next((i for i, lead in STATE.leads.items() if lead["email"] == email), None)
            if STATE.active(email_hash(email)) and STATE.active(email_hash(email))["reason"] in (
                "opted_out",
                "bounced",
                "complained",
                "forgotten",
            ):
                answer: dict = {"status": "blocked", "lead_id": None, "lead_url": None}
            elif existing or STATE.handover_answer == "existing":
                lead_id = existing or f"lead_{len(STATE.leads) + 1}"
                STATE.leads.setdefault(
                    lead_id,
                    {
                        "email": email,
                        "reply_id": payload.get("reply_id"),
                        "stage": "new",
                        "handover": payload,
                    },
                )
                answer = {
                    "status": "existing",
                    "lead_id": lead_id,
                    "lead_url": f"http://localhost:{PORT}/#{lead_id}",
                }
            else:
                lead_id = f"lead_{len(STATE.leads) + 1}"
                STATE.leads[lead_id] = {
                    "email": email,
                    "reply_id": payload.get("reply_id"),
                    "stage": "new",
                    "handover": payload,
                }
                # A Deals lead is on the do-not-email list, as with the real Deals.
                STATE.block(email, "lead", None)
                answer = {
                    "status": "created",
                    "lead_id": lead_id,
                    "lead_url": f"http://localhost:{PORT}/#{lead_id}",
                }
            STATE.answers[key] = answer
            return 200, answer
        if self.command == "POST" and path == "/optouts":
            items = json.loads(body or b"{}").get("items") or []
            if not 1 <= len(items) <= 500:
                return 400, {"error": "invalid", "fields": {"items": "1 to 500 items"}}
            if not repeat:
                STATE.optouts.extend(items)
                # Shared opt-outs come back on the do-not-email list, as with the real Deals.
                for item in items:
                    reason = {"unsubscribed": "opted_out", "manual": "opted_out"}.get(
                        item["reason"], item["reason"]
                    )
                    STATE.blocks[item["hash"]] = {
                        "hash": item["hash"],
                        "reason": reason,
                        "until": None,
                        "email": item.get("email") or "(hash only)",
                    }
                    STATE.changes.append(
                        {"hash": item["hash"], "reason": reason, "until": None, "removed": False}
                    )
            return 200, {"accepted": len(items)}
        if self.command == "POST" and path == "/check":
            hashes = json.loads(body or b"{}").get("hashes") or []
            return 200, {"blocked": [a for a in (STATE.active(h) for h in hashes) if a]}
        return 404, None

    def _page(self) -> str:
        rows = (
            "".join(
                f"<tr><td>{html.escape(b['email'])}</td><td>{b['reason']}</td><td>{b['until'] or ''}</td>"
                f"<td><form method=post action=/control/unblock>"
                f"<input type=hidden name=hash value={b['hash']}><button>Remove</button></form></td></tr>"
                for b in STATE.blocks.values()
            )
            or "<tr><td colspan=4><i>Nobody is blocked.</i></td></tr>"
        )
        calls = (
            "".join(
                f"<tr><td>{at}</td><td>{m}</td><td>{html.escape(p)}</td><td>{code}</td></tr>"
                for at, m, p, code in reversed(STATE.calls[-25:])
            )
            or "<tr><td colspan=4><i>No calls yet.</i></td></tr>"
        )
        options = "".join(f"<option>{r}</option>" for r in REASONS)
        shared = (
            "".join(
                f"<tr><td>{html.escape(o.get('email') or '(hash only)')}</td>"
                f"<td>{o['reason']}</td><td>{o['at']}</td></tr>"
                for o in reversed(STATE.optouts[-50:])
            )
            or "<tr><td colspan=3><i>None yet.</i></td></tr>"
        )
        down = "on: every call gets 503" if STATE.down else "off"
        secret = html.escape(STATE.secret)
        stages = "".join(f"<option>{s}</option>" for s in STAGES)
        leads = (
            "".join(
                f"<tr id={i}><td>{i}</td><td>{html.escape(lead['email'])}</td><td>{lead['stage']}</td>"
                f"<td>{lead['handover'].get('score', {}).get('value', '')} "
                f"{lead['handover'].get('score', {}).get('band', '')}</td>"
                f"<td><form method=post action=/control/stage><input type=hidden name=lead value={i}>"
                f"<select name=stage>{stages}</select> <input name=value placeholder='Won $' size=7> "
                f"<input name=reason placeholder='Lost reason' size=12> <button>Move</button></form>"
                f"<form method=post action=/control/delete><input type=hidden name=lead value={i}>"
                f"<button>Delete lead</button></form></td></tr>"
                for i, lead in STATE.leads.items()
            )
            or "<tr><td colspan=5><i>No handovers yet.</i></td></tr>"
        )
        answer_options = "".join(
            f"<option{' selected' if a == STATE.handover_answer else ''}>{a}</option>"
            for a in ("created", "existing")
        )
        return f"""<!doctype html><meta charset=utf-8><title>Fake Deals</title><style>{STYLE}</style>
<h1>Fake Deals</h1>
<p>Secret: <code>{secret}</code> · In NexaReach use the address <code>http://localhost:{PORT}</code>.</p>
<h2>Do-not-email list</h2>
<form method=post action=/control/block>
<input name=email type=email required placeholder="someone@example.com" size=32>
<select name=reason>{options}</select>
<input name=until type=date title="Only for lost: may be emailed again from this day">
<button>Block</button></form>
<table><tr><th>Email</th><th>Reason</th><th>Until</th><th></th></tr>{rows}</table>
<h2>Leads NexaReach handed over</h2>
<form method=post action=/control/answer><p>A new handover gets
<select name=answer>{answer_options}</select> <button>Set</button>
(someone on the list as opted out, bounced or complained always gets "blocked").</p></form>
<table><tr><th>Lead</th><th>Email</th><th>Stage</th><th>Score</th>
<th>Change it (NexaReach reads it within 5 min)</th></tr>
{leads}</table>
<h2>Opt-outs NexaReach shared (newest first)</h2>
<table><tr><th>Email</th><th>Reason</th><th>At</th></tr>{shared}</table>
<h2>Pretend Deals is down</h2>
<form method=post action=/control/down><p>Now {down}. <button>Switch</button></p></form>
<h2>Calls from NexaReach (newest first)</h2>
<table><tr><th>Time (UTC)</th><th>Method</th><th>Path</th><th>Answer</th></tr>{calls}</table>
<p><a href=/>Refresh</a></p>"""  # noqa: S608 (HTML, not SQL)

    def do_GET(self) -> None:
        if not self.path.startswith(PREFIX):
            return self._send(200, self._page(), "text/html; charset=utf-8")
        self._handle_api(b"")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/control/"):
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if self.path == "/control/block" and form.get("email"):
                until = f"{form['until']}T00:00:00Z" if form.get("until") else None
                STATE.block(form["email"], form.get("reason", "lead"), until)
            elif self.path == "/control/unblock":
                STATE.unblock(form.get("hash", ""))
            elif self.path == "/control/down":
                STATE.down = not STATE.down
            elif self.path == "/control/answer":
                STATE.handover_answer = form.get("answer", "created")
            elif self.path == "/control/stage" and form.get("lead") in STATE.leads:
                lead = STATE.leads[form["lead"]]
                new = form.get("stage", "new")
                value = int(form["value"]) if form.get("value", "").isdigit() else None
                STATE.event(
                    form["lead"],
                    type="stage_changed",
                    **{"from": lead["stage"], "to": new},
                    value_usd=value if new == "won" else None,
                    lost_reason=form.get("reason") or None if new == "lost" else None,
                )
                lead["stage"] = new
            elif self.path == "/control/delete" and form.get("lead") in STATE.leads:
                STATE.event(form["lead"], type="lead_deleted")
                STATE.leads[form["lead"]]["stage"] = "deleted"
            return self._redirect()
        self._handle_api(body)

    def _handle_api(self, body: bytes) -> None:
        code, answer = self._api(body)
        STATE.calls.append((datetime.now(UTC).strftime("%H:%M:%S"), self.command, self.path, code))
        self._send(code, answer)


PORT = 8099

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--secret", default="local-test-secret")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--block", action="append", default=[], help="email:reason, can repeat")
    args = parser.parse_args()
    PORT = args.port
    STATE = State(args.secret)
    for item in args.block:
        email, _, reason = item.partition(":")
        STATE.block(email, reason or "lead", None)
    print(f"Fake Deals on http://127.0.0.1:{PORT} (secret {args.secret!r}). Ctrl+C to stop.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
