# Deploying NexaReach

Everything runs on one Linux server with Docker: Postgres, the API, the sending worker, the web app,
Caddy (HTTPS), nightly backups and Uptime Kuma. Every service restarts by itself after a crash or a
reboot, and all state lives in the database, so campaigns carry on where they stopped.

## 1. Server

- Any VPS with 2 GB RAM, 2 vCPU and 40 GB disk (Ubuntu 24.04 LTS is assumed below). About $5–12 a month.
- Check outgoing mail ports first. Many hosts block port 25; we need **465** (or 587) to
  `mail.fastnexa.com`:
  ```bash
  nc -vz mail.fastnexa.com 465
  ```
- Install Docker: <https://docs.docker.com/engine/install/ubuntu/>
- Allow only SSH, 80 and 443 in the firewall:
  ```bash
  sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
  ```

## 2. DNS

Create an **A record** for the app's domain (for example `outreach.fastnexa.com`) pointing at the server's
IP. Caddy gets the HTTPS certificate on first start. This doesn't touch the domain's email records
(MX, SPF, DKIM, DMARC).

## 3. Code and settings

```bash
mkdir -p ~/nexareach && cd ~/nexareach
git clone https://github.com/hammad-qayyum/nexareach-backend.git
git clone https://github.com/hammad-qayyum/nexareach-frontend.git
cd nexareach-backend/deploy
cp production.env.example production.env
nano production.env
```

Fill in every value. Generate secrets with `openssl rand -base64 32` (use the same database password
in `POSTGRES_PASSWORD`, `DATABASE_URL` and `PGPASSWORD`). **Keep a copy of `ENCRYPTION_KEY` and
`RESTIC_PASSWORD` somewhere safe and offline**: without them, backups can't be restored and saved
mailbox passwords can't be read.

## 4. Start

```bash
docker compose -f docker-compose.prod.yml --env-file production.env up -d --build
docker compose -f docker-compose.prod.yml --env-file production.env ps
```

Create the first admin:

```bash
docker compose -f docker-compose.prod.yml --env-file production.env exec api python -m app.cli create-admin
```

Open `https://<your domain>`, sign in and set up 2-step sign-in.

To change someone's sign-in email or password later (for example a forgotten password), run the same
command with `change-login` instead of `create-admin`. It signs them out everywhere and keeps their 2-step sign-in.

## 5. Backups

The `backup` service dumps the database every day at `BACKUP_HOUR` (UTC), encrypts it with restic and
uploads it to `RESTIC_REPOSITORY`, keeping `BACKUP_KEEP_DAILY` days.

Set up a bucket first. **Cloudflare R2:** create a bucket and an API token with Object Read & Write,
then set `RESTIC_REPOSITORY=s3:https://<account-id>.r2.cloudflarestorage.com/<bucket>` plus
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. **Backblaze B2** works the same way through its S3 endpoint.

```bash
# Take a backup now (do this once after setting up, and before any upgrade)
docker compose -f docker-compose.prod.yml --env-file production.env exec backup backup.sh once
# List backups
docker compose -f docker-compose.prod.yml --env-file production.env exec backup restic snapshots
```

**Monthly restore test** (plan section 22). It restores the latest backup into a scratch database,
prints row counts and drops it; the live database isn't touched:

```bash
docker compose -f docker-compose.prod.yml --env-file production.env exec backup restore-test.sh
```

**Full restore** (after losing the server): set up a new server with steps 1–3 and the *same*
`production.env`, then:

```bash
docker compose -f docker-compose.prod.yml --env-file production.env up -d db backup
docker compose -f docker-compose.prod.yml --env-file production.env exec backup sh -c \
  'restic dump --tag db latest nexareach.dump | pg_restore --clean --if-exists --no-owner --dbname "$PGDATABASE"'
docker compose -f docker-compose.prod.yml --env-file production.env up -d
```

## 6. Monitoring

**Uptime Kuma** listens only on the server itself. Open it through an SSH tunnel:

```bash
ssh -L 3001:localhost:3001 you@your-server
```

Then browse to <http://localhost:3001>, create the admin account and add:

- **HTTP(s) monitor** on `https://<your domain>/api/health`, every 60 s. It returns 503 when the
  database or the sending worker is down. Add an email notification so admins hear about it.
- **HTTP(s) monitor** on `https://<your domain>/login`.

**Error tracking:** create a project in Sentry (free tier) or a self-hosted GlitchTip, put its DSN in
`SENTRY_DSN` and restart the api and worker. No request bodies, cookies or personal data are sent.

## 7. Updating

```bash
cd ~/nexareach/nexareach-backend && git pull
cd ../nexareach-frontend && git pull
cd ../nexareach-backend/deploy
docker compose -f docker-compose.prod.yml --env-file production.env exec backup backup.sh once
docker compose -f docker-compose.prod.yml --env-file production.env up -d --build
```

Migrations run automatically (the `migrate` service) before the API and worker start. Stopping the
worker waits up to 60 seconds for an email that is mid-send.

## Restarts

If a process crashes or the server reboots, Docker starts it again by itself. A container you stop by
hand (`docker stop` or `docker kill`) is the exception: it stays stopped until you run
`docker compose -f docker-compose.prod.yml --env-file production.env up -d` again.

## Security notes

- Only Caddy is reachable from the internet; Postgres, the API and the web app sit on Docker's internal
  network. Uptime Kuma is bound to 127.0.0.1.
- HTTPS everywhere with HSTS, clickjacking protection and other security headers (see `Caddyfile`).
- `COOKIE_SECURE=true` and `API_DOCS=false` in production.
- Admins must use 2-step sign-in; logins are rate limited; mailbox passwords, TOTP secrets and other
  secrets are AES-GCM encrypted with `ENCRYPTION_KEY`; the activity log can't be edited.
- `production.env` is git-ignored and never copied into an image.
