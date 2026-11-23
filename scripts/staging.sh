#!/usr/bin/env bash
# Staging on this laptop: the newest code with its own database (fastnexa_staging), sending REAL email.
# Settings come from .env.staging (copy .env.staging.example). Runs next to the development copy:
# API on port 8001, the website on http://127.0.0.1:3001 (npm run dev:staging in the frontend).
#
#   scripts/staging.sh setup     create the database (first time) and bring it up to date
#   scripts/staging.sh admin     create the first admin
#   scripts/staging.sh api       run the API (leave it open)
#   scripts/staging.sh worker    run the worker that sends (leave it open)
set -euo pipefail
cd "$(dirname "$0")/.."
export NEXAREACH_ENV_FILE=.env.staging

if [ ! -f .env.staging ]; then
  echo "No .env.staging yet. Run: cp .env.staging.example .env.staging, then put a new key in ENCRYPTION_KEY."
  exit 1
fi
if ! grep -q '^ENCRYPTION_KEY=.\+' .env.staging; then
  echo "ENCRYPTION_KEY is empty in .env.staging. Run .venv/bin/python -m app.cli generate-key and paste it there."
  exit 1
fi

case "${1:-}" in
  setup)
    if ! docker exec fastnexa-dev-db-1 psql -U fastnexa -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='fastnexa_staging'" | grep -q 1; then
      docker exec fastnexa-dev-db-1 psql -U fastnexa -d postgres -c "CREATE DATABASE fastnexa_staging OWNER fastnexa"
    fi
    .venv/bin/python -m alembic upgrade head
    echo "Staging database is ready. Next: scripts/staging.sh admin"
    ;;
  admin) .venv/bin/python -m app.cli create-admin ;;
  api) .venv/bin/python -m uvicorn app.main:app --reload --port 8001 ;;
  worker) .venv/bin/python -m procrastinate --app=worker.app.app worker ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
