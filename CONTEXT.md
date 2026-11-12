# Project context: NexaReach backend

Read this before working on the backend. It covers what the project is, where things stand, and the conventions the code follows. The full product plan is in [docs/Phase1-Plan.md](docs/Phase1-Plan.md).

## What we're building

An internal web app for FastNexa that sends cold sales emails from the company's own fastnexa.com mailboxes, replacing manual sending. Staff import lead sheets, verify addresses, resolve duplicates, build campaigns and press Start. The app sends one email at a time (random 5–10 min gap, max 40 per mailbox per day, only within chosen hours), warms up mailboxes, detects replies and bounces, watches deliverability, and pauses itself if something looks wrong. Every action is logged with who did it and the before/after values.

Single company, three roles: **Admin**, **Member**, **Viewer**. Permissions are enforced in the backend on every request; the frontend only hides buttons.

## Repositories

| Repo | Local folder | Contents |
|---|---|---|
| [nexareach-backend](https://github.com/hammad-qayyum/nexareach-backend) | `~/Projects/nexareach/nexareach-backend` | FastAPI API, Procrastinate worker, migrations, dev Docker Compose, the plan |
| [nexareach-frontend](https://github.com/hammad-qayyum/nexareach-frontend) | `~/Projects/nexareach/nexareach-frontend` | Next.js app (UI only) |

Both use the `main` branch. They were split out of an earlier single repo (`fastnexa-outreach`, where the backend lived under `backend/`). That old folder is reference only; don't commit to it.

## Status

Phase 1 has 12 milestones (plan section 24), all launched together at the end.

**Milestone 1 (Foundation), backend part: done.**
- Login with email and password (argon2), sessions in an httpOnly cookie, login rate limiting
- 2-step login (TOTP) required for admins
- Invites: an admin invites a user, who sets their own password from the invite link
- User management: list, change role or status, resend invite, reset 2FA (admin only)
- Activity log: append-only (database triggers block update, delete and truncate), list with filters, CSV export
- `/api/health` with a database check and a worker heartbeat
- Procrastinate worker with a per-minute heartbeat task
- One Alembic migration (`foundation`), pytest suite against a real Postgres

**Milestone 1, frontend part: done** in nexareach-frontend (app shell, login with 2FA, invites, users, logs, generated API types). The Impeccable polish pass is still to do. When an endpoint changes, regenerate the frontend's types with `npm run gen:api` there.

**"Send today" slice: done** (cut-down parts of milestones 2, 4, 6 and 7, built ahead of order so campaigns can go out now):
- Mailboxes: SMTP only (465 SSL / 587 STARTTLS), password encrypted, login test, daily cap, status (ERROR when the server rejects the login). Optional IMAP for Sent copies (see below); no ramp-up or warm-up yet.
- Mailboxes are tested on create and whenever the connection fields or password change (and on a request to make them ACTIVE): they start as ERROR "Connection not tested yet." and only become ACTIVE after a passing SMTP login (IMAP failures only add a note). Delete is allowed only if the mailbox never sent (no `campaign_recipients` row) and isn't in a RUNNING/PAUSED campaign (`mailbox_has_sent` / `mailbox_in_active_campaign`, exposed as `can_delete`). Otherwise `POST /api/mailboxes/{id}/archive` sets `archived_at`, pauses it and drops it from draft campaigns; `/restore` brings it back paused. Archived mailboxes are hidden from `GET /api/mailboxes` unless `include_archived=true`, can't be activated and can't be added to campaigns.
- Settings: email footer (business name, postal address); campaigns can't start without it.
- Do-not-contact list: emails and whole domains.
- Lead groups: the browser parses the CSV and posts mapped rows to `POST /api/groups/import`; fixed fields plus `custom` JSONB.
- Campaigns: plain-text subject and body with `{{variable|fallback}}` and `{spin|tax}` (`app/services/templates.py`), preview, test send, start/pause/resume/cancel. Start fixes the recipient list and skips do-not-contact and anyone emailed in the last 30 days.
- Sending engine (`app/services/sending.py`, worker task `send_tick` every minute): per-mailbox pacing (random gap, daily cap counted in UTC days), sending window per campaign timezone, campaigns take turns, 3 retries 15 min apart for temporary errors, the exact text and server reply saved per recipient, `email.sent` / `email.failed` logged.
- **The worker must run for anything to send.** Sent mail isn't copied to the mailbox's Sent folder yet (needs IMAP), and replies, opt-outs and bounces aren't detected yet: add opt-outs to the do-not-contact list by hand.
- Local testing: set `SMTP_ALLOW_PLAIN=true` and add a mailbox with security "None", host `localhost`, port `1025`; Mailpit at http://localhost:8025 catches everything.

**Milestone 5 (Duplicates): done.** A row whose email already belongs to a lead (in the system, or earlier in the same file) doesn't join the group; it's stored as a `LeadDuplicate` (status PENDING) and isn't emailed. `app/services/duplicates.py` applies a decision: KEEP_NEW (non-blank new values overwrite, extra fields merge, `lead.updated` logged), KEEP_OLD (existing lead joins the group unchanged), KEEP_BOTH (a second lead with the same email). `GET /api/duplicates`, `/count`, `POST /resolve` (ids, or `all_pending` optionally per group); rows are locked so a decision can't be applied twice. Campaigns send once per lead, so "keep both" copies each get their own email.

**Crash safety (sending engine):** all state lives in Postgres, the worker keeps nothing in memory, so a stop, crash or restart loses nothing and running campaigns continue where they stopped. Each send is two-phase: the recipient is committed as `SENDING` (with the exact text, `send_started_at`, and the mailbox's next free time) *before* the mail server is contacted, then set to SENT / WAITING (retry) / FAILED. `recover_interrupted()` runs at the start of every tick and marks rows stuck in SENDING for over 10 minutes as FAILED ("may have been delivered") without resending, logged as `email.uncertain`. `mailer.send` raises `SendFailed(uncertain=True)` when the connection breaks while the message is being handed over, and those are never retried; a failed goodbye after a 250 counts as sent. Dev Postgres and Mailpit have `restart: unless-stopped`. Production still needs the same restart policy for the API and worker (milestone 12).

**Milestones 4, 6 and 7: done** (except what needs milestone 3, verification).
- Import: leads get `timezone` (timezone column, else single-zone country via `app/services/geo.py`; none for the USA, Canada…) and `email_provider` (GOOGLE / MICROSOFT / OTHER / UNKNOWN from MX records, `app/services/providers.py`, looked up in parallel during the import; tests fake DNS in `conftest.py`). The import result includes `provider_mix`. Google Sheets: `POST /api/groups/google-sheet` downloads a sheet shared as "anyone with the link" as CSV (`app/services/sheets.py`; only Google hosts are ever contacted). Excel is parsed in the browser.
- Groups: `PATCH /api/groups/{id}` (rename), `/facets`, lead filters `provider` and `country`, `POST /api/groups/{id}/leads` (add one; an existing email becomes a pending duplicate), `DELETE /api/groups/{id}/leads/{lead_id}`, `PATCH /api/leads/{id}` (edit; email can't change).
- Campaigns: `body_format` TEXT or HTML. HTML is cleaned with nh3 on save and again at send (`app/services/email_html.py`), values inserted into HTML are escaped, and a plain-text copy is derived from the same spun HTML. `use_lead_timezone` applies the days/hours window in each lead's timezone (campaign timezone as fallback). Leads can be left out of a draft (`campaign_exclusions`, `GET /audience`, `PUT /exclusions`).
- Sending: multipart/alternative emails; after a send is recorded, a copy is APPENDed to the mailbox's Sent folder over IMAP (`imap_host`/`imap_port` on the mailbox, same credentials; folder found via LIST `\Sent` or common names and remembered in `sent_folder`; `recipient.sent_copy_saved`). A Sent-copy failure never affects the send.

**Milestone 12 (launch prep): done, apart from the actual server.** `deploy/docker-compose.prod.yml` (db, migrate, api, worker, web, Caddy, backup, Uptime Kuma; everything `restart: unless-stopped`), `deploy/Caddyfile` (HTTPS, HSTS and security headers, 20 MB body limit), `deploy/backup/` (nightly restic backups plus `restore-test.sh`), `deploy/production.env.example`, and the step-by-step `deploy/README.md`. Error tracking (`SENTRY_DSN`, Sentry or GlitchTip, no personal data) in `app/core/errors.py`. Security review fixes: `.env` kept out of the Docker image, `API_DOCS=false` hides `/api/docs`, CSV export guards against formula injection. Still open: choose the VPS and deploy, and the Impeccable design pass.

**Next:** milestone 8 (replies), then 3 (verification), 2 (ramp-up), 9, 10, 11.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2 / pydantic-settings, Procrastinate (job queue stored in Postgres, no Redis), PostgreSQL 16, psycopg 3, argon2-cffi, cryptography (AES-GCM), pyotp. Dev tools: pytest, httpx, ruff. Later milestones add smtplib, imapclient and dnspython.

## Layout

```
app/
  api/        FastAPI routers (auth, users, logs, health), deps.py, schemas.py
  core/       config, db session, security (passwords), crypto (AES-GCM)
  models/     SQLAlchemy models (User, UserSession, UserToken, LoginAttempt, ActivityLog, Setting)
  services/   shared logic used by both API and worker (audit, auth)
  main.py     app setup + CSRF middleware
  cli.py      create-admin, generate-key
worker/       Procrastinate app and tasks (reuse app/services)
migrations/   Alembic; files named YYYYMMDD_<rev>_<slug>.py
tests/        pytest
deploy/       docker-compose.dev.yml (Postgres on 5433, Mailpit), dev SQL init
docs/         Phase1-Plan.md
```

## Conventions

- **Log every change.** Call `log_activity(db, actor, "entity.action", entity_type=..., entity_id=..., before=..., after=...)` from `app/services/audit.py` in the **same transaction** as the change, so a change can't be saved without its log entry. Use `snapshot(obj, [fields])` for before/after and never include secrets. Worker and CLI actions use the `SYSTEM` actor.
- **Permissions through dependencies** in `app/api/deps.py`: `CurrentUser` (any signed-in user who has passed 2FA), `EditorUser` (Admin or Member), `AdminUser` (Admin only), `CurrentActor` for logging. Every new endpoint must use one of these.
- **CSRF:** every non-GET request must send an `X-Requested-With` header, or it gets a 403 `csrf_header_missing`. CORS is never enabled.
- **Session cookie:** `fn_session`, httpOnly, SameSite=Lax. A session that still needs 2FA gets 401 `mfa_required` on protected routes.
- **Errors** are short snake_case codes in `detail` (e.g. `not_authenticated`, `forbidden`), which the frontend maps to messages.
- **Secrets** (mailbox passwords, tokens, API keys, TOTP secrets) are encrypted with `app/core/crypto.py` using `ENCRYPTION_KEY` from the environment.
- **Shared logic** goes in `app/services/` so the API and worker both use it.
- **Schema changes:** edit the models, then `alembic revision --autogenerate -m "..."` and review the generated file before committing.
- **Style:** ruff, line length 110, target py312. Comments are sparse and explain why, not what.
- **Deliberate product choices:** no open or click tracking, no unsubscribe link (opt-out is "reply and I won't email again", handled automatically), sending only from fastnexa.com.

## Local development

Requires Docker Desktop and Python 3.12 (`python3.12`, not conda's `python3`, which is 3.14). Run everything from the repo root.

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli create-admin
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m procrastinate --app=worker.app.app worker
```

API docs: http://localhost:8000/api/docs. Mailpit (catches all outgoing mail): http://localhost:8025.

Tests and lint:
```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
Tests need the dev Postgres running. They use the `fastnexa_test` database on port 5433, rebuild the schema once per run, and wrap each test in a transaction that is rolled back.

**Configuration:** copy `.env.example` to `.env` in the repo root and fill it in; never commit `.env`. Generate `ENCRYPTION_KEY` with `.venv/bin/python -m app.cli generate-key`.

## Known gaps

- **README.md is out of date** after the repo split: it still shows `/frontend` and `/backend/...` paths and `cd backend`. The commands above are correct for this repo.
- **The `.venv` broke when the folder moved** into `~/Projects/nexareach/`; its scripts still point at the old path. Rebuild it: `rm -rf .venv`, then the `venv` and `pip install` lines above.
- `app/core/config.py` still also looks for `../.env` (left over from the old layout). Harmless, but `.env` belongs in this repo's root.
