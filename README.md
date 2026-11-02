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

## Local development

Requires Docker Desktop and Python 3.12.

```bash
# 1. Database (port 5433) and Mailpit fake mail server (web UI at http://localhost:8025)
docker compose -f deploy/docker-compose.dev.yml up -d

# 2. Backend setup (once)
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli create-admin

# 3. Run the API (docs at http://localhost:8000/api/docs) and the worker, in two terminals
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m procrastinate --app=worker.app.app worker

# Tests and lint
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

New database tables: change the models in `backend/app/models/`, then
`.venv/bin/alembic revision --autogenerate -m "describe change"` and review the generated file.
