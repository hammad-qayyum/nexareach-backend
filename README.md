# FastNexa Outreach Platform

An internal web app that sends sales-pitch emails from FastNexa's own fastnexa.com mailboxes. It handles lead import, email verification, duplicate review, campaigns, paced sending, warm-up, reply detection and deliverability monitoring, and keeps an activity log of every action.

The full Phase 1 plan is in [docs/Phase1-Plan.md](docs/Phase1-Plan.md).

## Tech stack

| Part | Tools |
|---|---|
| Frontend | Next.js (TypeScript), shadcn/ui, Tiptap, Recharts |
| Backend API | Python FastAPI, SQLAlchemy 2, Alembic, Pydantic |
| Worker | Python, Procrastinate, smtplib, imapclient, dnspython |
| Database | PostgreSQL (also holds the job queue) |
| Deploy | Docker Compose, Caddy |

## Repository layout

```
/frontend            Next.js app
/backend/app
    api/             FastAPI routes
    models/          SQLAlchemy models
    services/        Shared logic (audit, verification, rendering, sending…)
    core/            Config, security, encryption
/backend/worker      Procrastinate tasks (reuse services/)
/deploy              docker-compose.yml, Caddyfile, backup scripts
/docs                Plans and design notes
```

## Build order

1. Foundation
2. Mailboxes
3. Verification
4. Import and groups
5. Duplicates
6. Campaign wizard
7. Sending engine
8. Replies
9. Warm-up
10. Deliverability
11. Reports and compliance
12. Hardening and launch

See section 24 of the plan for what each milestone contains.

## Configuration

Copy `.env.example` to `.env` and fill in the values. Never commit `.env`.
Generate an encryption key with `python -m app.cli generate-key` (from `backend/`).

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

New database tables: change the models in `app/models/`, then
`.venv/bin/python -m alembic revision --autogenerate -m "describe change"`, review the generated file and
test it with `reset-dev`. Production applies it on the next deploy.
