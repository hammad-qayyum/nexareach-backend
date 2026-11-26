# NexaReach backend (FastNexa Outreach Platform)

The API and background worker for NexaReach, FastNexa's internal tool for sending sales emails from its own
fastnexa.com mailboxes. Staff import lead lists, review duplicates, build campaigns (with follow-ups) and press
Start; the worker sends one email at a time within each campaign's hours and limits, notices replies, and keeps
an activity log of every change. The web app is the separate
[nexareach-frontend](https://github.com/hammad-qayyum/nexareach-frontend) repo.

The original plan is [docs/Phase1-Plan.md](docs/Phase1-Plan.md); [CONTEXT.md](CONTEXT.md) says what is built
today and how the code is organised.

## Tech stack

| Part | Tools |
|---|---|
| API | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic |
| Worker | Procrastinate (job queue stored in PostgreSQL), smtplib, imaplib, dnspython |
| Database | PostgreSQL 16 |
| Deploy | Docker Compose, Caddy (see [deploy/README.md](deploy/README.md)) |

## Repository layout

```
app/
    api/             FastAPI routes
    models/          SQLAlchemy models
    services/        Shared logic (sending, replies, templates, audit, privacy…)
    core/            Config, security, encryption
    cli.py           Admin commands: create-admin, change-login, reset-2fa, generate-key, seed-dev, reset-dev, simulate-reply
worker/              Procrastinate tasks: send emails, check replies, heartbeat
migrations/          Alembic database migrations
tests/               pytest
deploy/              Docker Compose (development and production), Caddyfile, backups
docs/                The plan
```

## Configuration

Copy `.env.example` to `.env` and fill in the values. Never commit `.env`.
Generate an encryption key with `.venv/bin/python -m app.cli generate-key`.

## Development environment

Two copies of NexaReach exist: **production** (the server, `main` branch, real email) and
**development** (your laptop, `develop` branch, email only goes to Mailpit). See CONTEXT.md for the rules.

Requires Docker Desktop and Python 3.12. Run everything from this folder.

```bash
# 1. Database (port 5433) and Mailpit fake mail server (web UI at http://localhost:8025)
docker compose -f deploy/docker-compose.dev.yml up -d

# 2. Settings (once): copy the template, then put a NEW key in ENCRYPTION_KEY
cp .env.example .env
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m app.cli generate-key

# 3. Database with sample data: users, two Mailpit mailboxes, real-estate leads (also: start over any time)
.venv/bin/python -m app.cli reset-dev

# 4. Run the API (docs at http://localhost:8000/api/docs) and the worker, in two terminals
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
.venv/bin/python -m procrastinate --app=worker.app.app worker

# Tests and lint (GitHub runs the same on every push to develop or main)
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Sign in at http://localhost:3000 as `dev@example.com` / `devpassword123` (admin), or `member@example.com`
/ `viewer@example.com` with the same password. With `APP_ENV=development` the app shows a
**Development** badge and refuses to send through any mailbox that isn't Mailpit (`localhost`).

To try follow-ups quickly, the development copy lets a follow-up wait **0 days**: it goes out a minute or two
after the first email, and Mailpit shows it under the same conversation. Mailpit has no inbox to read (no
IMAP), so on a laptop fake a reply instead:

```bash
.venv/bin/python -m app.cli simulate-reply ahmed@emaar-brokers.example.com --text "Interested, call me."
```

It goes through the same code as a real reply: the person's follow-ups stop and it appears on the **Replies**
page, where an answer goes out through Mailpit.

**Staging (real email, on a laptop):** a third copy with the newest code, its own database (`fastnexa_staging`)
and real sending, next to the development copy. Copy `.env.staging.example` to `.env.staging` with a new key,
then `scripts/staging.sh setup`, `scripts/staging.sh admin`, and run `scripts/staging.sh api`,
`scripts/staging.sh worker` and the frontend's `npm run dev:staging`; it's at http://127.0.0.1:3001 with a red
"Staging · real email" badge. Development commands (`seed-dev`, `reset-dev`) refuse to run against it.

**Warm-up on a laptop:** the dev setup also runs GreenMail, a test mail server with real inboxes. `reset-dev`
adds three helper accounts (`helper1-3@warmup.example.com`) and gives the dev mailboxes inboxes there. Switch
warm-up on for a mailbox on the Warm-up page: during warm-up hours (Mon-Fri 08:00-19:00 Pakistan time) the
worker sends warm-up emails (you'll see them in Mailpit), and 10-40 minutes later the helpers open, star and
answer them, and their answers are filed into the mailbox's Warmup folder.

New database tables: change the models in `app/models/`, then
`.venv/bin/python -m alembic revision --autogenerate -m "describe change"`, review the generated file and
test it with `reset-dev`. Production applies it on the next deploy.
