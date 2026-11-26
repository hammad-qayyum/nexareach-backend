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
- Settings: business name and postal address, optional, usable in emails as `{{business_name}}` / `{{postal_address}}`. No footer is added: an email is exactly the subject and message written (owner's decision, 13 Nov 2026; for US/Canada recipients the address and an opt-out line must be in the message itself).
- Do-not-contact list: emails and whole domains.
- Lead groups: the browser parses the CSV and posts mapped rows to `POST /api/groups/import`; fixed fields plus `custom` JSONB.
- Campaigns: plain-text subject and body with `{{variable|fallback}}` and `{spin|tax}` (`app/services/templates.py`), preview, test send, start/pause/resume/cancel. Start fixes the recipient list and skips do-not-contact and anyone emailed in the last 30 days.
- Sending engine (`app/services/sending.py`, worker task `send_tick` every minute): per-mailbox pacing (random gap, daily cap counted in UTC days), sending window per campaign timezone, campaigns take turns, 3 retries 15 min apart for temporary errors, the exact text and server reply saved per recipient, `email.sent` / `email.failed` logged.
- **The worker must run for anything to send.** Replies, opt-outs and bounces are noticed only for mailboxes with IMAP (see "Follow-ups and reply checking" below).
- Local testing: set `SMTP_ALLOW_PLAIN=true` and add a mailbox with security "None", host `localhost`, port `1025`; Mailpit at http://localhost:8025 catches everything.

**Milestone 5 (Duplicates): done.** A row whose email already belongs to a lead (in the system, or earlier in the same file) doesn't join the group; it's stored as a `LeadDuplicate` (status PENDING) and isn't emailed. `app/services/duplicates.py` applies a decision: KEEP_NEW (non-blank new values overwrite, extra fields merge, `lead.updated` logged), KEEP_OLD (existing lead joins the group unchanged), KEEP_BOTH (a second lead with the same email). `GET /api/duplicates`, `/count`, `POST /resolve` (ids, or `all_pending` optionally per group); rows are locked so a decision can't be applied twice. Campaigns send once per lead, so "keep both" copies each get their own email.

**Crash safety (sending engine):** all state lives in Postgres, the worker keeps nothing in memory, so a stop, crash or restart loses nothing and running campaigns continue where they stopped. Each send is two-phase: the recipient is committed as `SENDING` (with the exact text, `send_started_at`, and the mailbox's next free time) *before* the mail server is contacted, then set to SENT / WAITING (retry) / FAILED. `recover_interrupted()` runs at the start of every tick and marks rows stuck in SENDING for over 10 minutes as FAILED ("may have been delivered") without resending, logged as `email.uncertain`. `mailer.send` raises `SendFailed(uncertain=True)` when the connection breaks while the message is being handed over, and those are never retried; a failed goodbye after a 250 counts as sent. Dev Postgres and Mailpit have `restart: unless-stopped`. Production still needs the same restart policy for the API and worker (milestone 12).

**Milestones 4, 6 and 7: done** (except what needs milestone 3, verification).
- Import: leads get `timezone` (timezone column, else single-zone country via `app/services/geo.py`; none for the USA, Canada…) and `email_provider` (GOOGLE / MICROSOFT / OTHER / UNKNOWN from MX records, `app/services/providers.py`, looked up in parallel during the import; tests fake DNS in `conftest.py`). The import result includes `provider_mix`. Google Sheets: `POST /api/groups/google-sheet` downloads a sheet shared as "anyone with the link" as CSV (`app/services/sheets.py`; only Google hosts are ever contacted). Excel is parsed in the browser.
- Groups: `PATCH /api/groups/{id}` (rename), `/facets`, lead filters `provider` and `country`, `POST /api/groups/{id}/leads` (add one; an existing email becomes a pending duplicate), `DELETE /api/groups/{id}/leads/{lead_id}`, `PATCH /api/leads/{id}` (edit; email can't change).
- Campaigns: `body_format` TEXT or HTML. HTML is cleaned with nh3 on save and again at send (`app/services/email_html.py`), values inserted into HTML are escaped, and a plain-text copy is derived from the same spun HTML. `use_lead_timezone` applies the days/hours window in each lead's timezone (campaign timezone as fallback). Leads can be left out of a draft (`campaign_exclusions`, `GET /audience`, `PUT /exclusions`).
- Sending: multipart/alternative emails; after a send is recorded, a copy is APPENDed to the mailbox's Sent folder over IMAP (`imap_host`/`imap_port` on the mailbox, same credentials; folder found via LIST `\Sent` or common names and remembered in `sent_folder`; `recipient.sent_copy_saved`). A Sent-copy failure never affects the send.

**Milestone 12 (launch prep): done, apart from the actual server.** `deploy/docker-compose.prod.yml` (db, migrate, api, worker, web, Caddy, backup, Uptime Kuma; everything `restart: unless-stopped`), `deploy/Caddyfile` (HTTPS, HSTS and security headers, 20 MB body limit), `deploy/backup/` (nightly restic backups plus `restore-test.sh`), `deploy/production.env.example`, and the step-by-step `deploy/README.md`. Error tracking (`SENTRY_DSN`, Sentry or GlitchTip, no personal data) in `app/core/errors.py`. Security review fixes: `.env` kept out of the Docker image, `API_DOCS=false` hides `/api/docs`, CSV export guards against formula injection. Still open: choose the VPS and deploy, and the Impeccable design pass.

**Milestone 11 (Reports + compliance): done** (delivered / bounced / replied numbers come with milestones 8 and 10).
- `GET /api/reports/summary?days=&tz=`: sent and failed per day (days cut in the browser's timezone), per mailbox, per campaign (period counts plus overall progress), and totals (sent, failed, skipped, distinct people reached).
- `GET /api/campaigns/{id}/recipients/export`: the per-lead report as CSV (same status/search filters), logged as `campaign.recipients_exported`. CSV helpers live in `app/services/csv_export.py` (formula-injection guard).
- GDPR, in `app/services/privacy.py`, by email: `POST /api/people/export` (Admin/Member) returns a JSON file of every lead copy, group and source, campaign email (with its text), waiting duplicate and do-not-contact entry, logged `person.exported`. `POST /api/people/forget` (Admin only) deletes the leads (memberships and exclusions cascade) and duplicate rows, turns waiting recipients into SKIPPED `forgotten` (finishing the campaign if that was the last one), keeps campaign rows but sets `lead_id` NULL and replaces the address and sent text with nothing but a hash, and puts `forgotten:<HMAC-SHA256 of the address, keyed with ENCRYPTION_KEY>` on do-not-contact (kind EMAIL). Sending, imports (row reason `forgotten`) and add-lead (`person_forgotten`) check that hash; an exact email search of the DNC list finds it. Returns 409 `person_being_emailed` if a send is in flight. The activity log is append-only and keeps its entries; they refer to leads by id, though a few older entries (`lead.created`, `group.lead_pending`, `dnc.added`) include the address.

**Follow-ups and reply checking: done** (Phase 2 follow-ups, plus the basic part of milestone 8, 16 Nov 2026).
- Follow-ups are optional: `campaigns.follow_ups_enabled` switches them off without losing the steps (the form starts with it off; the API default stays on). Switching it while paused re-plans everyone; a campaign left with nothing to send finishes when it is resumed.
- `campaigns.follow_ups` (JSONB, up to 5): `[{"delay_days", "body"}]`, plain text with the same `{{variables}}` and spintax. Editable in DRAFT and PAUSED; a change while paused re-plans everyone already emailed (`sending.reschedule_follow_ups`, timed from the last email they got). `delay_days` is 1–60, or 0 only with `APP_ENV=development`. Start refuses `follow_up_empty`.
- Each recipient has `follow_ups_sent`, `next_follow_up_at` (None = nothing planned), `replied_at`, and `follow_up_stop` (why they stopped early: replied, opted_out, bounced, do_not_contact, failed, cancelled, forgotten). Each follow-up sent is a `follow_up_emails` row (step, status SENDING/WAITING/SENT/FAILED, exact text, Message-ID), committed as SENDING before the mail server is contacted, like opening emails; `recover_interrupted` settles stuck ones.
- Sending (`app/services/sending.py`): due follow-ups go before new opening emails, from the mailbox that sent the opening email (even if it was taken off the campaign), inside the campaign's days/hours (lead timezone too), with `In-Reply-To`/`References` and "Re: <subject as sent>" so mail apps thread them. They count towards the daily cap and the 30-day rule. Temporary errors retry (3 × 15 min); anything else stops that person's follow-ups. A campaign finishes only when no opening email and no follow-up is left. Cancel stops all follow-ups.
- Reply checking (`app/services/replies.py`, worker task `reply_check` every 5 minutes): for each non-archived mailbox with `imap_host` and someone emailed in the last 60 days who hasn't replied, reads new INBOX messages read-only (`mailer.read_inbox`, continues from `inbox_last_uid`, starts over if UIDVALIDITY changes, 200 per run). Matches by our Message-IDs in `In-Reply-To`/`References`, else by sender address (only messages after our email). Auto-replies (Auto-Submitted, X-Autoreply, Precedence, "Out of office"… subjects) are ignored. Bounces (mailer-daemon/postmaster or multipart/report, not "Action: delayed") stop follow-ups (`bounced`). A real reply sets `replied_at` and stops follow-ups; if what they wrote (quoted text removed) says unsubscribe / remove me / stop emailing / not interested…, the address also goes on do-not-contact (`opted_out`). Logged as `email.replied`, `email.bounced`, `dnc.added`. Mailboxes show `inbox_checked_at` / `inbox_error`. Skipped in development for non-Mailpit hosts; Mailpit has no IMAP.
- `POST /api/campaigns/{id}/recipients/{rid}/replied` marks a reply by hand (`email.marked_replied`). Recipients list/export filter `replied=true|false`. `Counts` adds follow_ups_sent, follow_ups_planned, replied, opted_out, bounced; reports count follow-ups as emails and add `follow_ups_sent` and `replied` totals. Test send can include the follow-ups (`include_follow_ups`), sent at once in one thread. Forget/export-a-person cover follow-up emails.
- Not yet: a Replies page (reading and answering replies in the app), reply text stored, follow-up subjects of their own.

**Invite emails: done.** `PUT /api/settings/invites {mailbox_id}` (admin) picks a mailbox (setting `invites.mailbox_id`); inviting or "new invite link" then also emails the link (`app/services/invites.py`), after the invite is saved, so a mail failure never loses it. Responses add `email_sent` (None = not set up) and `email_detail`; logged `user.invite_emailed` / `user.invite_email_failed`. Invites don't count towards the daily cap.

**Replies page (shared inbox): done** (milestone 8 and Phase 2's shared inbox, 16 Nov 2026).
- Every reply found in an inbox is stored as a `Reply` (`replies` table): who, subject, `text` (what they wrote, quoted part removed) and `full_text`, `has_attachments`, their Message-ID and References, `received_at`, kind REPLY or OPT_OUT, and inbox state `read_at` / `done_at`. Unique per (mailbox, Message-ID), so reading an inbox again never doubles anything. Inboxes are now read up to 300 KB per message.
- The conversation stays watched after the first reply: later messages from them (matched by our Message-IDs, theirs, our answers', or their address) become new unread replies. Only the first reply sets `replied_at`; an opt-out in any of them adds do-not-contact.
- `app/api/replies.py`: `GET /api/replies?state=open|unread|done|all&q=&campaign_id=`, `GET /count` (unread, open, done, all, plus `checked_mailboxes` and `checked_at` for "Inboxes checked …"), `POST /check` (Admin/Member: read the inboxes now, in the background), `GET /{id}` (the reply plus the whole thread: campaign email, follow-ups, their replies, our answers, oldest first), `POST /{id}/read {read}` (not logged), `POST /{id}/done {done}` (`reply.done` / `reply.reopened`), `POST /{id}/answer {body}`. Reading is for everyone; changes are Admin/Member.
- Answers (`replies.answer`, `reply_answers` table): plain text, sent at once from the mailbox that got the reply ("Re: their subject", In-Reply-To their Message-ID, References the thread), copied to its Sent folder, no sending window or daily cap. A failure is stored and shown, not raised. A sent answer marks the reply read and done. Logged `reply.answered` / `reply.answer_failed`.
- Recipients have `last_reply_id`. Forget-a-person deletes their replies and our answers (`replies` count in the result); export includes them.
- `python -m app.cli simulate-reply <email> [--text …]` (development only): pretends the lead answered their last email, through the same code as a real reply, since Mailpit has no inbox to read.
- `GET /api/replies/{id}` also returns `details` (the lead's fields plus business fields and `sender_name`, campaign status, follow-ups sent and why they stopped) for the details panel and quick replies.
- Quick replies (`app/api/quick_replies.py`, `quick_replies` table): shared saved answers, CRUD for Admin/Member (read for everyone), logged `quick_reply.created/updated/deleted`, at most 50. The migration adds 4 starters.
- Not yet: attachments (only flagged), HTML replies shown as text, assigning replies to a person, notifications.

**Tests never touch the network:** `tests/conftest.py` refuses any connection to a host other than this computer. Without it, tests that save Sent copies tried to log in to the real mail.fastnexa.com.

**Milestone 9 (Warm-up) and the ramp-up part of milestone 2: done** (17 Nov 2026). Decisions: ramp-up for new mailboxes only; warm-up off until switched on per mailbox; helpers may be any IMAP/SMTP account (Gmail, Outlook, Yahoo, company domains) with a password or app password ("Connect with Microsoft" later); admins and members manage helpers; a low inbox rate pauses campaign sending.
- Ramp-up (`app/services/warmup.py: daily_limit`): `mailboxes.ramp_up_enabled/ramp_start_date/ramp_start_limit`. Day 1 = start limit (5), +3 every 2 working days, up to `daily_cap`. On by default for mailboxes created via the API, off for older ones (column default). Turning it on (again) starts from today. `send_next` stops at `daily_limit` (campaign + follow-up + warm-up emails, UTC days). `MailboxOut` adds `daily_limit_today`, `ramp_day`.
- Warm-up sending: `mailboxes.warmup_enabled`. Each day `plan_for_day` picks 5-10 moments in the warm-up hours (setting `warmup.schedule`: default Mon-Fri 08-19 Asia/Karachi, `GET/PUT /api/warmup/schedule`), seeded by mailbox and date so nothing is stored. `send_next` sends a due warm-up email before campaign emails, to the active helper this mailbox used least in the last week, two-phase like campaigns, then waits 5-10 min. Texts: `warmup_content.py` (40 business messages, 12 replies, spintax). Each Message-ID carries a hidden HMAC code (`nxw…`, `warmup.is_warmup`); reply checking ignores such mail. Logged `warmup.sent` / `warmup.failed`.
- Helpers (`warmup_helpers`, same field names as Mailbox so `mailer` signs in to both): worker task `warmup_tick` (every 5 min) runs `act_for_helper`: 10-40 min after sending, find the email by its code in INBOX, else in the spam folder (found via LIST \\Junk or common names) and move it to INBOX (`rescued`); mark \\Seen \\Flagged (+ \\Important on Gmail); reply to ~30% from the helper's SMTP in the same thread; move ~20% to a "Work" folder. Not found 3 h after sending = MISSING. A sign-in failure marks the helper ERROR (`warmup.helper_failed`); the others carry on. `mailer.Inbox` is the read-write IMAP session.
- Filing: the same task moves helper answers out of each mailbox's INBOX into its "Warmup" folder (`INBOX.Warmup` on cPanel), found by IMAP header search for the code and checked with the HMAC.
- Inbox rate: `warmup_messages.landed_in` INBOX/SPAM/MISSING per provider. Task `warmup_health` (hourly) pauses campaign emails (`mailboxes.campaigns_paused_at/_reason = low_inbox_rate`, logged `mailbox.campaigns_paused`) when each of the last 3 days with 2+ results was under 50%; warm-up continues. `POST /api/mailboxes/{id}/resume-campaigns` lifts it and sets `campaigns_resumed_at`; from then on the check only counts warm-up results sent after the resume, so the same bad days can't pause it again within the hour.
- Filing helper answers (`warmup_tick`, every 5 min) only opens the inbox of mailboxes with warm-up on or a warm-up email in the last 7 days, never an ERROR one (`warmup.filing_mailbox_ids`): repeated logins with a wrong password can get the server's IP blocked by cPanel's cPHulk.
- API: `GET /api/warmup` (per mailbox: today's limit, ramp-up day, warm-up sent/planned today, 7-day rates per provider, 14-day trend, low-rate flag, pause), `/api/warmup/helpers` CRUD + `/test` (Admin/Member; tested on save, ERROR until a test passes; `guess_provider` from the domain and MX). In development helpers must be on localhost (`dev_real_helper`).
- Development: `deploy/docker-compose.dev.yml` adds GreenMail (SMTP 3025, IMAP 3143, any `@warmup.example.com` or `@example.com` account, password `mailpit`), and Mailpit relays mail for those addresses to it, so the whole loop runs locally and still shows in Mailpit. `mailer` uses plain IMAP only for local hosts with `SMTP_ALLOW_PLAIN`. `seed-dev` adds 3 helpers and gives the dev mailboxes GreenMail inboxes (IMAP localhost:3143) with Sent and Spam folders. GreenMail keeps mail in memory: after it restarts, run `reset-dev`.
- Not yet: "Connect with Microsoft" (OAuth) for Outlook.com helpers, email alerts (warnings show on the Warm-up page and the dashboard).

**Milestone 10 (Deliverability): done** (19 Nov 2026). Decisions: cPanel API token for the delivery log; a DMARC reports mailbox in Settings; free blacklists plus Spamhaus only with a DQS key; alerts emailed to all admins from the invite mailbox; admins and members may resume; placement tests use a campaign's real message; bounce emails move to a "Bounces" folder; alert limits editable by admins.
- `delivery_events` (`app/services/delivery.py`): one row per send result (SMTP: SENT / DEFERRED / REJECTED, with the Exim queue ID from "250 OK id=..."), cPanel log line and bounce email; the strongest status is kept in `delivery_status` on `campaign_recipients` / `follow_up_emails` (a bounce beats "delivered"). `bounce_kind()`: HARD only for dead addresses (5.1.x, 5.2.1, "user unknown"...); spam blocks, full mailboxes and 4.x.x are SOFT. A hard bounce puts the address on do-not-contact (`dnc.added`, reason "hard bounce"); any bounce stops follow-ups.
- Bounce emails (`replies.process` → `bounce_details` reads the DSN's Status / Diagnostic-Code / Remote-MTA) are recorded once per message (`external_id`) and then moved from INBOX to "Bounces" (`_file_bounces`, best effort). `python -m app.cli simulate-bounce EMAIL [--soft]` (development only) fakes one.
- cPanel (`app/services/cpanel.py`, worker `deliverability_check` every 10 min): UAPI `EmailTrack/search` with `Authorization: cpanel user:token` over the last 3 days; lines match our emails by queue ID, else address + mailbox within 15 minutes. success → DELIVERED, defer → DEFERRED (once), failure → bounce. A failing token raises `cpanel_failing`. With MailBaby relaying, "delivered" may mean "handed to the relay".
- Alerts (`app/services/alerts.py`, table `alerts`): `sync(kind, conditions)` opens / refreshes / resolves; one active alert per (kind, mailbox, target). Pausing kinds (`hard_bounces`, `blacklisted`, `dns_broken`, `warmup_low`) set `campaigns_paused_at` with the kind as reason and record `paused_mailbox_ids`, but only while `deliverability.thresholds.auto_pause` is on (Settings → Alert limits → "Pause sending on serious problems"; off by default since 25 Nov 2026, so alerts only warn). Switched on while an alert is open, the next check pauses (not if a person already resumed). `resume_mailbox()` (Mailboxes, Warm-up, Deliverability page) acknowledges them and sets `campaigns_resumed_at`; bounce and warm-up checks only count what happened after that. Resolving never resumes. New alerts are emailed to active admins from the invite mailbox (`emailed_at` / `email_error`). Limits in setting `deliverability.thresholds` (hard bounces > 3% of the last 100 opening emails, counted out of at least 100; delayed > 10% of 24 h with 10+ emails; DMARC < 95% with 20+ emails; certificate within 14 days; placement > 50% spam/missing).
- Health checks (`app/services/domain_health.py`, worker daily 01:20 UTC, "Run checks now"): MX, SPF, DKIM (selector from Settings, default `default`), DMARC per sending domain; certificate per mail server (TLS on 465, STARTTLS on 587; 30 s timeout, mail.fastnexa.com is slow); blacklists (SpamCop, Barracuda, PSBL; Spamhaus ZEN/DBL only with a DQS key) for the servers' A records and SPF `ip4:` addresses. UNKNOWN (a lookup that didn't answer) never alerts or resolves. Test domains (example.com, .test...) and local hosts are skipped. Every result is kept (`health_checks`), with `data.mailbox_ids`.
- DMARC (`app/services/dmarc.py`, worker every 6 h, "Read now"): reads the reports mailbox read-only (UID tracking in `deliverability.dmarc_state`), unpacks .zip / .gz / .xml (refuses DOCTYPE/ENTITY), stores `dmarc_reports` / `dmarc_records` once per (reporter, report_id). Pass = aligned DKIM or SPF pass. Development only reads local hosts.
- Placement tests (`app/services/placement.py`, worker `placement_tick` every minute, `placement_weekly` Mondays 06:00 UTC for mailboxes that sent in the last 14 days): the campaign's real message (or a warm-up sample) to every active helper, looked for after 3 minutes (Inbox, Gmail Promotions via `X-GM-RAW`, Spam), missing after an hour. Helpers don't act on them. They count towards the daily limit (`sending.sent_today`).
- API `app/api/deliverability.py`: `GET /api/deliverability` (mailbox cards, active alerts, setup state, provider mix, limits), `/alerts`, `/alerts/count` (also `paused_mailboxes`, for the banner on every page), `POST /alerts/{id}/acknowledge|resume`, `/health`, `POST /health/run`, `/health/history`, `/log` (+ `/log/export` CSV, `POST /log/sync`), `/placement` (GET, POST), `/dmarc` (+ `POST /dmarc/check`), `/settings` (GET for everyone, no secrets) and admin-only `PUT /settings/cpanel|dmarc|checks|thresholds`, `POST /settings/cpanel/test|dmarc/test`. Recipients carry `delivery_status` and `delivery` (their log lines); campaign counts add `delivered` and `hard_bounced`. Forget-a-person deletes their delivery events; export includes them.
- Not done: the verification card (milestone 3 isn't built).

**Deals integration, step 1 (do-not-email list): built** (25 Nov 2026). Contract and plan: the "NexaReach + Deals" integration guide and prompt (Deals CRM at deals.fastnexa.com builds the other side; NexaReach always calls Deals, never the reverse). Off until an admin switches it on; with it off nothing changes (the existing tests pass unchanged).
- Client (`app/services/deals_client.py`, httpx): every request signed `X-Fastnexa-Signature: t=…,v1=HMAC-SHA256(secret, "t\nMETHOD\npath?query\nbody")` over the bytes actually sent; POSTs carry `Idempotency-Key`; `User-Agent: NexaReach/<version>`; 10 s connect / 30 s read. Outside production every call is a test call (`"test": true`, `?test=1`). Errors become `DealsError(kind)`: invalid, signature, conflict, gone, rate_limited, unavailable, not_configured. Tests swap `deals_client.transport` for a fake Deals server (`tests/test_deals.py`).
- Settings (`deals.settings` JSON: url, encrypted secret + set date, `blocklist_enabled`, `stale_minutes`), admin page Settings → Deals (`/api/settings/deals`, `/test`, `/api/deals/status`, `/api/deals/blocklist/sync`). The secret is write-only.
- `leads.email_hash`: sha256 of the trimmed lower-case email, a generated column (`nexareach_email_hash()` SQL function), so it's always right. `deals_blocklist` (hash, reason, until; active while until is empty or ahead) and `deals_sync_state`. Worker: `deals_blocklist_delta` every 10 min (a full fetch if none ever succeeded; 410 → full), `deals_blocklist_full` daily 02:40 UTC; one at a time (Procrastinate lock + an advisory lock on its own connection).
- Three checks: import (live join, so badges never go stale: `deals_block` on leads, `deals_blocked` filter and facet, `blocked_by_deals` in the import result); Start (`POST /check` in batches of 1,000 plus the local copy; blocked people get recipient status `BLOCKED_BY_DEALS` with Deals' reason in `skip_reason`; Deals unreachable → 503 `deals_check_failed`, nothing saved); every send (`send_next`: an opening email → `BLOCKED_BY_DEALS`; a follow-up → stays SENT, follow-ups stop `blocked_by_deals`; logged `email.blocked_by_deals`; the turn moves on to the next person, up to 25).
- Fail closed: switched on and the copy older than `stale_minutes` (default 30) or never fetched → `send_next` returns `deals_stale` for campaign and follow-up emails (warm-up carries on); `check_stale` (every tick and after each sync) opens one critical `deals_stale` alert, emails admins once, logs `deals.sending_held`, and closes it / logs `deals.sending_resumed` after a good sync. Also alerts `deals_signature` (401, critical) and `deals_error` (400/409, warning).
- Step 2, opt-outs (`app/services/deals_optouts.py`, switch `optouts_enabled`): every new do-not-contact address (opt-out reply → unsubscribed, hard bounce → bounced, spam complaint → complained, by hand → manual) goes into `deals_optout_outbox` in the same transaction; a GDPR forget is queued as hash only even while off, and scrubs the address from unsent rows (an unsent batch holding it is superseded). Older entries catch up when switched on. Worker `deals_optouts_push` every 2 min sends batches of ≤500 (`deals_optout_batches` keeps the exact items; retries 1/5/15/60 min then hourly for 3 days; 400/409/401 give up). Entries from Deals itself ("Opted out in Deals") are never sent back. Spam complaints (ARF `multipart/report; report-type=feedback-report`) are now detected in reply checking. Request bodies are JSON with sorted keys so a retry repeats the bytes exactly.
- Opt-out line and footer (`email.footer` setting, Settings → Opt-out line and footer, off by default): the line and the postal address (Business details) end every campaign and follow-up email (text and HTML, previews and test sends too), plus `List-Unsubscribe: <mailto:mailbox?subject=unsubscribe>`; a reply with the subject "unsubscribe" is then an opt-out; Start needs the address. The import form's List source is required (API stays optional; groups without one show "unknown").
- Step 3, reply score (`app/services/scoring.py` pure word rules, never AI; `reply_scores.py`; `reply_score_rules` seeded with the 12 starting rules; setting `score.settings` {enabled, auto_threshold 80, review_threshold 40, free_mail_domains}): new replies are scored in reply checking; auto → handover (or Needs review while handover is off), review → Needs review, low, zero (opt-out / hard stop). Switching on scores earlier replies for display only. Admin page Settings → Reply score (rules editor, cut-offs, Try it; members can Try it). Regex rules run with a 50 ms limit and slow patterns are refused on save. Replies keep `cc`.
- Step 4, handover (`deals_handover.py`, `deals_handovers`, switch `handover_enabled`): queued once per reply (auto, approved, manual, repeat); worker `deals_handovers` every minute, Send to Deals also tries at once. Payload per contract 4.5 (saved with the first try), `nexareach_url` = /replies?open=<id>, ids `rpl_<id>` / `cmp_<id>`. created/existing → recipient `HANDED_TO_DEALS` (counts as emailed everywhere via `models.EMAILED`), follow-ups stop in every campaign, waiting copies elsewhere become BLOCKED_BY_DEALS, the lead is marked (`handed_to_deals_at`, link) and never joins a campaign again; blocked → do-not-contact, not sent back. Later real replies from a handed person go as `repeat`. Campaign field `deals_service_key` (dropdown from `/api/deals/services`, cached 1 h).
- Step 5, results (`deals_events.py`, switch `events_enabled`, worker every 5 min): events stored once (`deals_lead_events`), current state per lead in `deals_lead_results` (merge re-points, delete → "deleted"). Reports `GET /api/reports/deals?dimension=campaign|mailbox|version|lead_list` (+ `/export` CSV): MQLs (not repeat), auto/approved/manual, reached proposal, won + USD, lost by reason, revenue per 1,000 emails. Version = `campaign_recipients.subject_version` (the subject after spintax, before names).
- Hand testing: `scripts/fake_deals.py` is a local fake Deals (signatures, blocklist, check, optouts, handover, events, services) with a control page to block people, move leads through stages and pretend Deals is down.

**Next:** 3 (verification).

## Stack

Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2 / pydantic-settings, Procrastinate (job queue stored in Postgres, no Redis), PostgreSQL 16, psycopg 3, argon2-cffi, cryptography (AES-GCM), pyotp. Dev tools: pytest, httpx, ruff. Later milestones add smtplib, imapclient and dnspython.

## Layout

```
app/
  api/        FastAPI routers (auth, users, logs, health), deps.py, schemas.py
  core/       config, db session, security (passwords), crypto (AES-GCM)
  models/     SQLAlchemy models (users and sessions, mailboxes, leads, campaigns and recipients, follow-ups, activity log, settings)
  services/   shared logic used by both API and worker (sending, replies, mailer, templates, audit, privacy, invites…)
  main.py     app setup + CSRF middleware
  cli.py      create-admin, change-login (email/password, logged, signs out), reset-2fa (lost phone or a secret saved under an old key), generate-key, seed-dev, reset-dev, simulate-reply
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

## Environments

Production is live (deployed Nov 2026). Two copies, kept apart:

| | Development (laptop) | Production (server) |
|---|---|---|
| Branch | `develop` (and short branches off it) | `main` only; the server runs `git pull` of `main` |
| Settings | `.env` from `.env.example`, `APP_ENV=development` | `deploy/production.env`, `APP_ENV=production` |
| Database | Docker on port 5433, sample data from `reset-dev` | Docker on the server, real data |
| Email | Mailpit only: `mailer.development_block` refuses any non-local SMTP host, and starting a campaign with one returns 400 `dev_real_mailbox` | real mailboxes |
| Secrets | its own `ENCRYPTION_KEY` | production key, never on a laptop |

Rules: never copy the production database to a laptop; merge `develop` into `main` only when GitHub CI
(`.github/workflows/ci.yml`: ruff + pytest against Postgres) is green; take a backup before each deploy.
`APP_ENV` defaults to `production`, so a server without it set behaves as before. `/api/auth/me` returns
`environment`; the frontend shows a Development badge. Sentry events are tagged with the environment.
Development-only CLI: `seed-dev` (empty database → dev@example.com / member@ / viewer@, password
`devpassword123`; two Mailpit mailboxes; real-estate sample leads on example.com; two do-not-contact
entries) and `reset-dev` (drops the schema, runs migrations, seeds; asks you to type the database name).
Both refuse unless `APP_ENV=development` and the database host is localhost.

## Local development

Requires Docker Desktop and Python 3.12 (`python3.12`, not conda's `python3`, which is 3.14). Run everything from the repo root.

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
cp .env.example .env            # then put a new key from `app.cli generate-key` in ENCRYPTION_KEY
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m app.cli reset-dev
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
.venv/bin/python -m procrastinate --app=worker.app.app worker
```

API docs: http://localhost:8000/api/docs. Mailpit (catches all outgoing mail): http://localhost:8025.

Tests and lint:
```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
Tests need the dev Postgres running. They use the `fastnexa_test` database on port 5433, rebuild the schema once per run, and wrap each test in a transaction that is rolled back.

**Staging on a laptop:** `APP_ENV=staging` from `.env.staging` (`NEXAREACH_ENV_FILE` picks the settings file; `scripts/staging.sh` sets it), database `fastnexa_staging` in the dev Postgres, API :8001, frontend `npm run dev:staging` on 127.0.0.1:3001 (own `.next-staging` build folder, own cookie host). Real email is allowed; `is_test_copy` (development or staging) allows 0-day follow-ups. Warm-up never takes more than half of a mailbox's daily limit, so ramp-up days still leave room for campaigns.

**Configuration:** `.env.example` is the development template (copy to `.env`, never commit it). Server settings live in `deploy/production.env` (template `deploy/production.env.example`).

## Known gaps

- Nothing open from the repo split: the README, the `.venv` and the old `../.env` lookup were fixed on 16 Nov 2026.
