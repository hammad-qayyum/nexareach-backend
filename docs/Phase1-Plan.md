# FastNexa Outreach Platform: Phase 1 Plan (End to End)

**Version:** 1.0 · **Date:** 29 October 2026 · **Scope:** Phase 1 (sending, verification, warm-up, basic replies, deliverability)

---

## Contents

1. [Summary](#1-summary)
2. [Background and goals](#2-background-and-goals)
3. [Comparison with existing tools](#3-comparison-with-existing-tools)
4. [All decisions](#4-all-decisions)
5. [Users and roles](#5-users-and-roles)
6. [Architecture and tech stack](#6-architecture-and-tech-stack)
7. [How one email is sent (end to end)](#7-how-one-email-is-sent-end-to-end)
8. [Data model](#8-data-model)
9. [Module 1: Login, users and activity logs](#9-module-1-login-users-and-activity-logs)
10. [Module 2: Mailboxes and settings](#10-module-2-mailboxes-and-settings)
11. [Module 3: Email verification (QuickEmailVerification)](#11-module-3-email-verification-quickemailverification)
12. [Module 4: Lead import](#12-module-4-lead-import)
13. [Module 5: Duplicates](#13-module-5-duplicates)
14. [Module 6: Groups](#14-module-6-groups)
15. [Module 7: Campaigns](#15-module-7-campaigns)
16. [Module 8: Sending engine](#16-module-8-sending-engine)
17. [Module 9: Basic reply detection](#17-module-9-basic-reply-detection)
18. [Module 10: Warm-up](#18-module-10-warm-up)
19. [Module 11: Deliverability and mail-server logs](#19-module-11-deliverability-and-mail-server-logs)
20. [Module 12: Reports and dashboard](#20-module-12-reports-and-dashboard)
21. [Compliance](#21-compliance)
22. [Production setup](#22-production-setup)
23. [Pages](#23-pages)
24. [Build order](#24-build-order)
25. [Testing and launch checklist](#25-testing-and-launch-checklist)
26. [Costs](#26-costs)
27. [Risks and open items](#27-risks-and-open-items)
28. [Phase 2](#28-phase-2)
29. [Glossary](#29-glossary)
30. [Sources](#30-sources)

---

## 1. Summary

FastNexa will build its own internal web app to send sales-pitch emails to clients automatically, from its own fastnexa.com mailboxes.

- Staff **upload lead sheets**. The app stores them in a database, verifies each email address, and asks staff what to do about duplicates without stopping the import.
- Staff **create a campaign**: pick lead groups, write one email with personal fields, choose mailboxes and settings, then click **Start**.
- The app sends **one email at a time**, with a **random 5–10 minute gap**, up to **40 emails per mailbox per day**, only during chosen hours.
- It **warms up** mailboxes, **detects replies and bounces**, **watches deliverability**, and **pauses itself** if something looks wrong.
- **Every action is logged** with who did it, when, and what changed.
- Users never code. Developers build it once.

Phase 1 launches all at once. Phase 2 adds follow-up emails, a shared inbox, A/B testing and multiple domains.

---

## 2. Background and goals

**Today:** employees email clients manually from fastnexa.com mailboxes.

**Goals:**
1. Save staff time by automating the sending.
2. Keep emails landing in the inbox (deliverability).
3. Keep data clean: no duplicate leads, no dead addresses, no repeat pitches.
4. Full accountability: a log of every action.
5. Stay as close to free as possible while being production-grade.

**Domain status (checked 27 Oct 2026):**

| Record | Value | Status |
|---|---|---|
| MX | `mail.fastnexa.com` (priority 0) | OK |
| SPF | `v=spf1 +a +mx +ip4:64.20.37.26 +include:relay.mailbaby.net +include:relay.is.cc ~all` | OK |
| DKIM | `default._domainkey.fastnexa.com` (cPanel) | OK |
| DMARC | `v=DMARC1; p=none; rua=mailto:dmarc@fastnexa.com; fo=1;` | OK (consider `p=quarantine` later) |
| TLS certificate (mail server) | `*.fastnexa.com`, Let's Encrypt, valid until 17 Jan 2027 | OK |
| Mail hosting | InterServer cPanel shared hosting, outgoing relay likely MailBaby | Hourly limit to confirm |

---

## 3. Comparison with existing tools

| Feature | Smartlead | Instantly | **FastNexa app** |
|---|---|---|---|
| Price | From $39/mo (2,000 leads, 6,000 emails/mo) | From $47/mo (1,000 contacts, 5,000 emails/mo) | **Server only (about $5–12/mo)**, no lead limits |
| Mailboxes | Unlimited | Unlimited | Unlimited |
| Separate sending domains | Encouraged, sells mailboxes | Encouraged | Main domain (owner's choice) |
| Warm-up | Large shared network | Large shared network | Ramp-up + own helper accounts (small network) |
| Rotating between mailboxes | Yes | Yes | Yes |
| Random gaps, sending hours | Yes | Yes | Yes |
| Lead timezone sending | Yes | Yes | Yes |
| Spintax | Yes | Yes | Yes |
| Follow-ups | Yes | Yes | Phase 2 |
| Reply detection | Yes (Master Inbox) | Yes (Unibox) | Basic in Phase 1, shared inbox in Phase 2 |
| A/B testing | Yes | Yes | Phase 2 |
| Email verification | Included credits | Paid credits | QEV, 100/day free, queued |
| Placement tests | Paid add-on | — | Free (helper accounts) |
| DNS and blacklist checks | Yes | — | Yes |
| Duplicate handling | Basic | Basic | **Per row: keep new / old / both** |
| Who-did-what logs | Limited | Limited | **Every action, with before/after** |
| Data ownership | Their servers | Their servers | **Our server** |
| Open/click tracking | Optional | Optional | Off on purpose (hurts cold email) |

**Why build instead of buy:** custom duplicate and group workflow, full audit logs, data ownership, no lead limits, and very low running cost. The one thing that can't be copied for free is the paid tools' large warm-up network.

---

## 4. All decisions

| Topic | Decision |
|---|---|
| Users | FastNexa only (one company) |
| Roles | Admin, Member, Viewer |
| Frontend | Next.js (TypeScript), UI only, shadcn/ui, **Impeccable** design skill, Tiptap editor, Recharts |
| Backend | Python FastAPI, SQLAlchemy 2, Alembic, Pydantic |
| Background jobs | Python worker + Procrastinate (queue inside Postgres, no Redis) |
| Database | PostgreSQL |
| Sending | Python `smtplib`, SSL on 465 or STARTTLS on 587 per mailbox. No Nodemailer, no Resend |
| Reading mailboxes | `imapclient` |
| Hosting | Decided later. Docker Compose, works on any VPS |
| Login | Email + password, invites, **2-step login (TOTP) for admins** |
| Lead sources | CSV, Excel (.xlsx), Google Sheets link, pasted CSV |
| Lead fields | Fixed fields + custom fields |
| Duplicate key | Email (trimmed, lowercased) |
| Duplicate choices | Keep new / Keep old / Keep both, per row or all; never blocks the import |
| Groups | One group per upload; campaigns pick groups and can leave out leads |
| Campaign content | One email per lead; rich text (HTML) or plain text; variables with fallbacks; spintax |
| Sending mailboxes | Several fastnexa.com mailboxes, chosen per campaign, rotating |
| Pace | Random 5–10 min gap; 40/day per mailbox (warm-up included); ramp-up for new mailboxes |
| Sending time | Campaign timezone, days and hours; option to send in each lead's timezone |
| Overlap | No lead emailed twice within 30 days across campaigns (editable, admin override) |
| Verification | QuickEmailVerification, 100/day free; queue continues next day; state + tags on every lead |
| Warm-up | Ramp-up + helper accounts (Gmail + Outlook), 5–10/day within the cap, auto-filed to a Warmup folder |
| Replies | Basic detection in Phase 1 |
| Opt-out | No unsubscribe link or header; footer "reply and I won't email again" + address; opt-out replies go to the do-not-contact list automatically |
| Tracking | No open or click tracking |
| Deliverability | SMTP log, cPanel delivery log, bounces, DMARC reports, DNS/cert/blacklist checks, placement tests, provider mix |
| Alerts | Dashboard banner + email to admins + auto-pause |
| Logs | Every user and system action, append-only |
| Production | Nightly encrypted off-server backups, Uptime Kuma, error tracking, admin 2FA |
| Lead regions | UAE/Gulf, Pakistan, UK/EU, USA/Canada |
| Sending domain | **fastnexa.com (main domain)**, risk accepted |
| Launch | Everything at once |

---

## 5. Users and roles

| Action | Admin | Member | Viewer |
|---|---|---|---|
| Add / remove / pause users, change roles | Yes | No | No |
| Mailboxes, helper accounts, settings, API keys | Yes | Yes | View |
| Import leads, decide duplicates, edit leads and groups | Yes | Yes | View |
| Create / start / pause / cancel campaigns | Yes | Yes | View |
| Resume a campaign paused by the system | Yes | No | No |
| Forget a person (data erasure) | Yes | No | No |
| View reports, logs, deliverability | Yes | Yes | Yes |
| Export CSV | Yes | Yes | Yes (logged) |

Every permission is checked in the backend (FastAPI) on every request. The frontend only hides buttons.

---

## 6. Architecture and tech stack

```
Browser ──► Caddy (HTTPS, automatic certificates)
              ├─ /        ──► Next.js frontend (pages only)
              └─ /api/*   ──► FastAPI backend (auth, permissions, all logic, logs)
                                   │
                                   ▼
                               PostgreSQL  ◄──── Python worker (Procrastinate tasks)
                                                  ├ SMTP 465/587 ─► mail.fastnexa.com ─► clients and helper accounts
                                                  ├ IMAP 993 ─► fastnexa mailboxes (Sent copies, replies, bounces, Warmup folder, DMARC reports)
                                                  ├ IMAP / OAuth ─► helper accounts (Gmail app password, Outlook sign-in)
                                                  ├ HTTPS ─► QuickEmailVerification API, cPanel API (delivery log)
                                                  └ DNS ─► MX lookups, SPF/DKIM/DMARC checks, blacklists
```

| Part | Job | Tools |
|---|---|---|
| Caddy | Front door: HTTPS, sends requests to the right container | Caddy |
| Frontend | The screens users click | Next.js, TypeScript, shadcn/ui, Impeccable, Tiptap, Recharts |
| Backend API | Login, roles, imports, duplicates, campaigns, logs | FastAPI, SQLAlchemy, Alembic, Pydantic |
| Worker | Long-running jobs: imports, verification, sending, warm-up, replies, deliverability | Python, Procrastinate, smtplib, imapclient, dnspython |
| Database | All data and the job queue | PostgreSQL |

**Repository layout:**

```
/frontend            Next.js app
/backend/app
    api/             FastAPI routes
    models/          SQLAlchemy models
    services/        Shared logic (audit, verification, rendering, sending…)
    core/            Config, security, encryption
/backend/worker      Procrastinate tasks (reuse services/)
/deploy              docker-compose.yml, Caddyfile, backup scripts
```

Frontend types are generated from the backend's API description (`openapi-typescript`), so both sides stay in sync.

---

## 7. How one email is sent (end to end)

Example: campaign "Dubai Logistics", lead **John Smith, john@acme.com**, mailbox **sales@fastnexa.com**.

| Step | Who | What happens |
|---|---|---|
| 1 | Worker | Every 30 s it checks: is sales@ free (gap passed), under today's limit, and within sending hours (John's local time)? |
| 2 | Worker | Picks John. Re-checks: not on do-not-contact, not emailed in 30 days, hasn't replied or opted out, verified as valid |
| 3 | Worker | Builds the email: spintax first (`{Hi\|Hello}` becomes "Hello"), then fields (`{{first_name}}` becomes "John"), adds the footer, HTML + plain-text versions, a unique Message-ID |
| 4 | Worker | Connects to `mail.fastnexa.com` (port 465 SSL or 587 STARTTLS), checks the server certificate, logs in as sales@ |
| 5 | Worker → server | `MAIL FROM` sales@fastnexa.com, `RCPT TO` john@acme.com, `DATA`; the server replies `250 OK id=…` (queued) |
| 6 | Mail server | Adds the DKIM seal, passes the email to InterServer's relay (MailBaby) |
| 7 | Relay | Looks up acme.com's MX record and delivers to Acme's mail provider over an encrypted connection |
| 8 | Acme's provider | Checks SPF, DKIM, DMARC (all pass), runs spam filters, places it in Inbox / Promotions / Spam |
| 9 | Worker | Saves a copy to sales@'s Sent folder (IMAP), records the server reply and queue ID, marks John SENT |
| 10 | Worker | Sets sales@'s next free time to now + random 5–10 min |
| 11 | Worker (later) | cPanel log sync updates John to DELIVERED / DEFERRED / BOUNCED |
| 12 | Worker (later) | If John replies, reply detection marks him REPLIED and alerts the campaign owner |

---

## 8. Data model

| Table | Main fields |
|---|---|
| `User` | name, email, passwordHash (argon2), role ADMIN/MEMBER/VIEWER, status ACTIVE/PAUSED/REMOVED, totpSecret (encrypted), invitedBy |
| `Mailbox` | email, displayName, smtpHost, smtpPort, smtpSecurity SSL/STARTTLS, imapHost, imapPort, username, passwordEncrypted, dailyCap (40), rampStartDate, currentDailyLimit, warmupEnabled, status, nextAvailableAt |
| `Lead` | email, emailNormalized (indexed, not unique), firstName, lastName, company, title, phone, website, country, timezone, customFields (JSON), emailProvider GOOGLE/MICROSOFT/OTHER, source, verificationState, verificationTags, replyStatus NONE/REPLIED/OPTED_OUT, lastContactedAt, createdBy, deletedAt |
| `LeadGroup` | name, sourceImportId, createdBy |
| `LeadGroupMember` | groupId, leadId |
| `Import` | groupId, sourceType CSV/XLSX/GSHEET/PASTE, fileName, columnMapping, counts, status |
| `ImportRow` | importId, rowNumber, raw, mapped, status INSERTED/INVALID/DUPLICATE_PENDING/KEPT_NEW/KEPT_OLD/KEPT_BOTH, matchedLeadId, decidedBy, decidedAt |
| `DncEntry` | value, type EMAIL/DOMAIN, reason MANUAL/OPT_OUT_REPLY/HARD_BOUNCE/ERASURE, addedBy |
| `EmailVerification` | emailNormalized, source LOCAL/QEV, result, reason, flags (disposable, acceptAll, role, free, safeToSend), didYouMean, raw, checkedAt, expiresAt (90 days) |
| `VerificationQueue` | emailNormalized, leadIds, priority, attempts, nextAttemptAt, status WAITING/PROCESSING/DONE |
| `VerificationUsage` | date, used, remainingCredits, quotaExhaustedAt |
| `Campaign` | name, subject, bodyHtml, bodyText, format, timezone, sendDays, windowStart, windowEnd, sendInLeadTimezone, gapMinSec (300), gapMaxSec (600), overlapDays, include flags (catch_all / unknown / unverified), status, createdBy |
| `CampaignMailbox`, `CampaignGroup`, `CampaignExclusion` | links |
| `CampaignRecipient` | campaignId, leadId, email, status QUEUED/SENDING/SENT/FAILED/SKIPPED, skipReason, mailboxId, messageId, renderedSubject, renderedBody, sentAt, smtpCode, smtpResponse, serverQueueId, deliveryStatus, error, attempts |
| `Reply` | leadId, recipientId, mailboxId, fromEmail, subject, snippet, receivedAt, type REPLY/AUTO_REPLY/OPT_OUT, handledBy |
| `HelperAccount` | email, provider GMAIL/OUTLOOK, authType APP_PASSWORD/OAUTH, credentials (encrypted), status ACTIVE/LOCKED/ERROR, lastUsedAt |
| `WarmupMessage` | mailboxId, helperId, messageId, sentAt, landedIn INBOX/SPAM/MISSING, rescued, opened, starred, important, repliedAt |
| `DeliveryEvent` | recipientId, mailboxId, source SMTP/CPANEL/BOUNCE/PLACEMENT, status, remoteHost, remoteResponse, raw, occurredAt |
| `DmarcReport`, `DmarcRecord` | reporter, period, sourceIp, count, SPF/DKIM results |
| `HealthCheck` | type, target, status OK/WARN/FAIL, detail, checkedAt |
| `PlacementTest`, `PlacementResult` | mailbox, helper, folder INBOX/PROMOTIONS/SPAM/MISSING |
| `Alert` | type, severity, mailboxId, campaignIds, message, status OPEN/ACKNOWLEDGED/RESOLVED |
| `Setting` | QEV key, cPanel host/user/token (encrypted), thresholds, footer, overlap days |
| `ActivityLog` | actor (user or system), action, entityType, entityId, before, after, ip, userAgent, createdAt. Insert-only |

All passwords, tokens and API keys are encrypted (AES-GCM) with a key kept only in the server's environment.

---

## 9. Module 1: Login, users and activity logs

**Login**
- Email + password (argon2 hashes), session in a secure httpOnly cookie.
- Admins must use 2-step login (an authenticator app code).
- Login rate limiting and temporary lockout after repeated failures.

**Users**
- Admin invites by email. The invited person sets their password.
- Admin can pause, remove or change the role of any user.

**Activity logs**
- One function, `log_activity(...)`, is called in the same database transaction as every change, so a change can't happen without its log.
- Covered: logins (success and failure), 2FA, users, mailboxes, helper accounts, imports, duplicate decisions, verification, leads, groups, do-not-contact, campaigns (create, edit, start, pause, resume, cancel), each email sent or failed, replies, warm-up, alerts, settings, exports, erasures.
- The log table can't be edited or deleted (no permission at database level).
- Logs page: filter by user, action, item and date; before/after view; CSV export. Each lead, group, campaign and mailbox has a History tab.

---

## 10. Module 2: Mailboxes and settings

**Adding a mailbox:** email, display name, SMTP host/port/security (465 SSL or 587 STARTTLS), IMAP host/port, username, password.

**Test connection:** logs in with SMTP and IMAP without sending anything, and shows the result.

**Per mailbox:** daily cap (40), ramp-up start date and starting limit, warm-up on/off, status, today's sent count.

**Settings page:** QuickEmailVerification API key, cPanel API token, helper accounts, alert thresholds, footer (business name and postal address), overlap days (30), default gap and hours.

---

## 11. Module 3: Email verification (QuickEmailVerification)

**Layer 1: our own free checks** (every lead, instant):
- Format (syntax) and typo suggestion (e.g. gmial.com → gmail.com)
- The domain exists and accepts email (MX record)
- Disposable (throwaway) domain list, refreshed weekly
- Role address (info@, sales@) is flagged, not rejected

Leads failing Layer 1 are tagged straight away without using credits.

**Layer 2: QuickEmailVerification API**

| Item | Value |
|---|---|
| Endpoint | `GET https://api.quickemailverification.com/v1/verify?email=…&apikey=…` |
| Free limit | 100 per day |
| Credits left | `X-QEV-Remaining-Credits` header on every reply |
| Out of credits | HTTP 402 |
| Too many requests | HTTP 429 (limit not published) |
| Unknown results | Not charged; re-check after 5–10 min |
| Testing | Free sandbox: `/v1/verify/sandbox` |

**The queue (the flow never stops):**
1. After import, each new unique email joins the queue; its leads become **Queued**.
2. The worker checks one email every 2 seconds.
3. When credits reach 0 (or a 402 comes back), it stops for the day and the rest stay waiting.
4. It tests once an hour; when credits return (normally the next day), it continues automatically.
5. Unknown results retry after 10 min, 1 hour and 24 hours.
6. Errors 401/403 (bad key, disabled account) raise an alert.
7. "Verify now" moves chosen leads to the front.
8. Results are cached for 90 days, so the same email is never checked twice.

**Every lead has a state and tags:**

| State | Meaning |
|---|---|
| Unverified | Not checked yet |
| Queued | Waiting in line |
| Verified | Checked |

| Tag | Meaning |
|---|---|
| valid / invalid / unknown | Main result |
| safe_to_send | Valid and high quality |
| catch_all | Domain accepts every address, so the result isn't certain |
| disposable | Throwaway address |
| role | Generic address like info@ |
| free_provider | Gmail, Yahoo etc. |
| no_mx / bad_syntax / bad_domain | Layer 1 failures |
| typo_suggested | A likely typo, with the suggestion shown |

Examples: `john@acme.com` → Verified · valid, safe_to_send. `info@shop.com` → Verified · valid, catch_all, role.

**Speed:** about 100 verifications a day, so 1,000 new leads take about 10 days. Paid credits can be added later and the same queue simply goes faster.

---

## 12. Module 4: Lead import

**Sources:** CSV file, Excel file, Google Sheets link (shared as "anyone with the link"), or CSV pasted into a text box.

**Steps:**
1. Choose the source → preview the first 20 rows.
2. **Map columns** to fields. Columns are auto-matched by name; staff adjust.
3. Name the group and choose a **lead source** label (e.g. "LinkedIn research Oct").
4. The worker processes each row:
   - Normalizes the email (trim, lowercase).
   - Runs Layer 1 checks.
   - Detects the client's **email provider** from MX (Google / Microsoft / other).
   - Sets the **timezone** from a timezone column, or from the country. Countries with several timezones, like the USA, fall back to the campaign timezone unless a timezone column is given.
   - Checks for duplicates.
5. Non-duplicates are added to the group immediately and queued for verification.
6. Duplicates go to **Pending** (see Module 5).
7. A summary is shown: totals, invalid, pending duplicates, verification queue, and the **email provider mix** (e.g. "60% Google · 30% Microsoft · 10% other").

**Fixed vs custom fields (example):**

Sheet:

| Email | First Name | Company | Country | Industry | Last Order |
|---|---|---|---|---|---|
| john@acme.com | John | Acme | UK | Logistics | Shoes |

- **Fixed:** Email, First Name, Company, Country are real columns, the same for every lead, filterable.
- **Custom:** Industry and Last Order are saved as `{"industry": "Logistics", "last_order": "Shoes"}`.
- Both can be used in an email: `Hi {{first_name}}, we help {{industry}} companies like {{company}}… {{last_order|our products}}`. The text after `|` is used when the field is empty.

---

## 13. Module 5: Duplicates

**Duplicate = same email address** (ignoring capitals and spaces), whether the match is in the database or earlier in the same file.

**Never blocks:** the rest of the import continues. Duplicates wait under a **Pending** filter until someone decides.

**Review screen:** existing and new data side by side, differences highlighted.

| Choice | Result |
|---|---|
| Keep new | Existing lead is updated with the new data and added to the new group |
| Keep old | Existing lead stays unchanged and is added to the new group |
| Keep both | A second lead with the same email is created in the new group |

Decide per row, for selected rows, or "apply to all pending". Every decision is logged with before/after. Pending leads can't be used in campaigns until decided.

---

## 14. Module 6: Groups

- List: name, lead count, pending count, source, creator, date.
- Detail: lead table with search and filters (verification state and tags, email provider, country, reply status), edit or remove leads, add leads manually.
- A lead can belong to several groups.

---

## 15. Module 7: Campaigns

**Wizard:**
1. Name.
2. Choose groups.
3. Review leads and untick any to leave out.
4. Choose sending mailboxes.
5. Write the email:
   - Rich text (HTML) or plain text.
   - Variable picker (fixed and custom fields, with fallbacks).
   - Spintax, e.g. `{Hi|Hello|Hey} {{first_name}}`.
   - Preview of **3 random versions** using a real lead.
6. Settings: timezone or "each lead's timezone", days, hours, gap (default 5–10 min), whether to include catch_all / unknown / unverified leads.
7. **Test send** to your own address.

**Start:** the recipient list is fixed. Leads are skipped, with the reason recorded, when:
- On the do-not-contact list
- Invalid or disposable
- catch_all / unknown / unverified (unless included)
- Emailed by any campaign in the last 30 days
- Already replied or opted out

"Keep both" copies each get their own email.

**Controls:** Pause, Resume, Cancel at any time. Editing content while paused only affects emails not yet sent, and is logged.

---

## 16. Module 8: Sending engine

The **mailbox** is the unit of pacing, so two campaigns sharing a mailbox still respect its gap and cap.

A task runs every 30 seconds:
1. Find mailboxes that are active, past their next free time, and under today's limit. Today's limit is the lower of the daily cap and the ramp-up limit, counting warm-up emails. Each mailbox is locked so it can never send twice at once.
2. Pick the next waiting recipient from a running campaign that uses that mailbox and is within its sending hours (the lead's local time if that option is on). Campaigns take turns.
3. Re-check do-not-contact, the 30-day overlap, and reply/opt-out status.
4. Build the email: spintax, then variables, then the footer, HTML + plain text, Message-ID at fastnexa.com. Save the exact text sent.
5. Send with `smtplib`. Record the server reply and queue ID → SENT. Save a copy to Sent.
6. Temporary error (4xx or network) → retry in 15 min, up to 3 times. Permanent error (5xx) → FAILED.
7. Set the mailbox's next free time to now + a random 5–10 min. Update the lead's last-contacted date. Log it.
8. When no recipients are waiting, the campaign is COMPLETED.

**Footer on every email:**

```
--
<Sender name> · FastNexa
<Business postal address>
If this isn't relevant, just reply and I won't email again.
```

---

## 17. Module 9: Basic reply detection

- Every 5 minutes the worker reads new inbox mail for each mailbox, skipping warm-up mail and bounces.
- A reply is matched to the original email by its reference headers, or by the sender being a contacted lead.
- Each reply is classified:

| Type | How it's recognised | Effect |
|---|---|---|
| Reply | A normal answer | Lead marked Replied; owner alerted |
| Auto-reply | Out-of-office or automatic headers | Recorded only |
| Opt-out | "stop", "unsubscribe", "remove me" etc. in the new text (quoted text and our footer removed) | Lead marked Opted out, added to do-not-contact automatically, shown for review |

- Replied and opted-out leads are skipped by all future campaigns.
- A **Replies page** lists them, and campaign reports show reply counts.
- Staff answer from their normal mail app. A shared inbox comes in Phase 2.

---

## 18. Module 10: Warm-up

**What it is:** Gmail and Outlook trust a mailbox that starts slowly and whose emails get opened and answered. Warm-up builds that trust on purpose.

**Part A: Ramp-up (automatic)**

| Day | Max emails |
|---|---|
| 1 | 5 |
| 3 | 8 |
| 5 | 11 |
| … | +3 every 2 working days |
| About week 4 | 40 |

Admins can set a higher starting point for mailboxes staff already use daily.

**Part B: Helper accounts**
- Staff create free accounts once: about **5 Gmail** and **3 Outlook.com**.
  - Gmail: turn on 2-step verification, create an app password, paste it into the app.
  - Outlook: click "Connect with Microsoft". Developers set this up once with a free Microsoft app registration.
- Each mailbox sends **5–10 warm-up emails a day**, at random times, counted inside the 40 cap, to random helpers.
- Content comes from a library of natural business messages with spintax.
- Warm-up emails are recognised by a hidden code inside the Message-ID, never a visible tag.

**What the app does in the helper account (10–40 min later):**
1. Finds the email.
2. If it's in Spam/Junk, moves it to the Inbox (this tells the provider it's "not spam").
3. Marks it read and important, and stars it.
4. Moves about 20% to a folder.
5. Replies to about 30% with short natural replies.

**Filing:** warm-up emails and helper replies in fastnexa.com mailboxes are moved to a **Warmup** folder within 10 minutes. Staff never see them.

**Warm-up page:** per mailbox, on/off, sent today, and the **inbox rate per provider** (e.g. "Gmail 92% inbox, Outlook 70%") with a trend.

**Problems:** a helper that can't log in is marked as an error and an alert says to replace it. Warm-up continues with the others. An inbox rate below 50% for 3 days raises a warning.

**Honest limits:** 5–10 helpers is a much smaller network than paid tools use, and automating free personal accounts goes against their terms, so a helper may occasionally be locked and need replacing.

---

## 19. Module 11: Deliverability and mail-server logs

| Source | What it gives | How often |
|---|---|---|
| Send log | Server reply code, reply text and queue ID for every email | Every send |
| cPanel delivery log | Delivered / deferred / bounced per email, from the mail server's own log (cPanel API `EmailTrack/search`, user-level API token) | Every 10 min |
| Bounce emails | Hard bounces (dead addresses) go to do-not-contact automatically; soft bounces are recorded; bounce emails move to a Bounces folder | Every 10 min |
| DMARC reports | SPF/DKIM pass rates and anyone faking fastnexa.com | Daily |
| Health checks | SPF, DKIM, DMARC, MX, certificate expiry, blacklists (Spamhaus via a free key, Barracuda, SpamCop) | Daily + "Run now" |
| Placement tests | Inbox / Promotions / Spam at the helper accounts | Weekly + on demand |
| Provider mix | Share of leads on Google / Microsoft / other | After each import |

**Alerts:**

| Condition | Action |
|---|---|
| Hard bounces over 3% (last 100 sends) | Auto-pause the mailbox's campaigns + email admins + banner |
| Blacklisted | Auto-pause + email + banner |
| SPF/DKIM/DMARC/MX broken | Auto-pause + email + banner |
| Mailbox login failing | Auto-pause + email + banner |
| Deferred over 10% in 24 h | Email + banner |
| DMARC pass under 95% | Email + banner |
| Warm-up inbox rate under 50% for 3 days | Email + banner |
| Placement test mostly spam | Email + banner |
| Certificate expires within 14 days | Email + banner |
| Helper account broken, QEV key invalid | Email + banner |

Only an admin can resume an auto-paused campaign, after acknowledging the alert. Every alert, pause and resume is logged.

**Deliverability page:** a card per mailbox (sent, delivered, deferred, bounced, bounce rate, DMARC pass rate, blacklist status, latest placement), alerts, health history, the **mail-server log viewer** (filters, raw server replies, CSV export), the verification card (credits used today / 100, queue size, expected finish), and the provider mix. Each lead in a campaign report has a **delivery timeline**.

---

## 20. Module 12: Reports and dashboard

- **Live campaign progress** (updates every 10 s): sent, waiting, failed, skipped, delivered, bounced, replied, % done, next send time, estimated finish.
- **Per-lead table:** status, delivery status, sent time, mailbox, server reply, error, timeline. CSV export.
- **Charts:** emails per day, per mailbox, per campaign.
- **Dashboard home:** today's sends vs limits per mailbox, running campaigns, pending duplicates, verification queue, new replies, open alerts, recent activity.

---

## 21. Compliance

Lead regions: UAE/Gulf, Pakistan, UK/EU, USA/Canada.

| Rule | How the app meets it |
|---|---|
| Honest sender and subject (US CAN-SPAM, Canada CASL) | Real sender name, honest subjects |
| Postal address in the email (CAN-SPAM, CASL) | Business address in the footer |
| Easy opt-out, honoured quickly (CAN-SPAM 10 business days; GDPR without delay) | "Reply and I won't email again"; opt-out replies go to do-not-contact automatically |
| Know where data came from (GDPR) | Lead source saved per import |
| Right to erasure (GDPR) | "Forget this person": deletes personal data, keeps a hashed email on do-not-contact so they're never re-imported |
| Right of access (GDPR) | Export one person's data |
| Data processors (GDPR) | QuickEmailVerification mentioned in the privacy notice |

**Notes for the owner:** in the UK/EU, emailing sole traders and in Canada (CASL) needs more care with consent. Pitch only business addresses relevant to the person's role. This isn't legal advice; check with a lawyer for your markets.

---

## 22. Production setup

All free tools:

| Area | Setup |
|---|---|
| Backups | Nightly database dump, encrypted with `restic`, stored off-server (Cloudflare R2 or Backblaze B2 free tier), 30 days kept, **restore test every month** |
| Uptime monitoring | Uptime Kuma (self-hosted) checks the app, API and worker heartbeat; emails admins |
| Error tracking | GlitchTip (self-hosted) or the Sentry free tier |
| Security | Admin 2FA, login rate limiting, secrets only in environment variables, all credentials encrypted, HTTPS + security headers via Caddy, database not exposed to the internet |
| Reliability | Health endpoints, Docker restart policies, log rotation |
| Hosting | Any VPS (decided later). If port 465 is blocked, mailboxes use 587 |

---

## 23. Pages

| Page | Purpose |
|---|---|
| `/login` | Login + 2FA |
| `/` | Dashboard home |
| `/leads/import` | Import wizard |
| `/leads/groups`, `/leads/groups/[id]` | Groups and their leads |
| `/leads/duplicates` | Pending duplicates review |
| `/campaigns`, `/campaigns/new`, `/campaigns/[id]` | Campaign list, wizard, detail (progress, recipients, content, settings, history) |
| `/replies` | Replies list |
| `/mailboxes` | Mailboxes, test connection, ramp-up |
| `/warmup` | Warm-up status and helper accounts |
| `/deliverability` | Health, alerts, server log, verification, placement tests |
| `/dnc` | Do-not-contact list |
| `/logs` | Activity log |
| `/settings` | Keys, tokens, thresholds, footer, defaults |
| `/users` | Users and roles (admin only) |

---

## 24. Build order

Launched all at once, at the end.

| # | Milestone | Contents |
|---|---|---|
| 1 | Foundation | Repo, Docker Compose, database models + migrations, login, roles, invites, admin 2FA, activity log, API types, app shell with Impeccable design |
| 2 | Mailboxes | Add, encrypt, 465/587, test connection, ramp-up, do-not-contact, settings |
| 3 | Verification | Layer 1 checks, QEV queue, states and tags |
| 4 | Import + groups | 4 sources, column mapping, provider and timezone detection |
| 5 | Duplicates | Review screen, keep new / old / both, bulk actions |
| 6 | Campaign wizard | Editor, variables, spintax, preview, test send |
| 7 | Sending engine | Rotation, gaps, caps, lead timezone, overlap block, retries, pause/resume |
| 8 | Replies | Detection, opt-out handling, Replies page |
| 9 | Warm-up | Helper accounts (Gmail, Outlook), send/act/file tasks, Warm-up page |
| 10 | Deliverability | cPanel sync, bounces, DMARC, health checks, placement tests, alerts, log viewer |
| 11 | Reports + compliance | Progress, charts, logs UI, exports, forget/export a person |
| 12 | Hardening + launch | Backups + restore test, monitoring, error tracking, security review, Impeccable polish, deploy |

---

## 25. Testing and launch checklist

**Local testing (no real emails):**
- Mailpit as a fake mail server.
- QuickEmailVerification sandbox.
- Recorded samples of cPanel logs, bounce emails and DMARC reports.

**Automated tests (pytest):**
- Duplicate detection and the three choices.
- Verification mapping; quota runs out → continues next day.
- Spintax (nested, escaping) and variables with fallbacks.
- Lead-timezone windows, 30-day overlap, ramp-up limits, caps counting warm-up, gaps within range.
- Reply classification (reply / auto-reply / opt-out), bounce parsing, alert rules and auto-pause.
- Warm-up recognition, role permissions on every endpoint, a log entry for every change, erasure keeps the hashed do-not-contact entry.

**End-to-end (Playwright):** admin with 2FA invites a member → member imports with duplicates → verification → campaign with spintax → test send → start / pause / resume → viewer can't edit → everything appears in the logs.

**Before launch on the real server:**
- [ ] cPanel API token works and the delivery log shows our test sends
- [ ] Ask InterServer: hourly sending limit, log retention, MailBaby delivery logs
- [ ] Port 465 or 587 works from the chosen server
- [ ] 2 Gmail + 1 Outlook helper connected; one full warm-up round works
- [ ] QuickEmailVerification live check on 10 known addresses
- [ ] Backup restored into a fresh database
- [ ] Footer has the real business address

**Pilot:** one campaign to 3–5 of our own and friendly addresses (Gmail + Outlook). Check inbox placement, Sent copy, SPF/DKIM/DMARC pass ("Show original"), reply detection, and opt-out → do-not-contact.

---

## 26. Costs

| Item | Cost |
|---|---|
| Software (all open source) | Free |
| QuickEmailVerification | Free (100/day); optional paid credits later |
| Helper accounts (Gmail, Outlook.com) | Free |
| Microsoft app registration (Outlook sign-in) | Free |
| Spamhaus DQS key | Free tier is meant for non-commercial, low-volume use. Confirm FastNexa qualifies; otherwise use only the free Barracuda/SpamCop checks |
| Backups storage (R2 / B2 free tier) | Free within limits |
| Monitoring and error tracking | Free (self-hosted / free tier) |
| Mail hosting | Existing InterServer plan |
| Server (VPS) | About $5–12/month (to be decided) |

---

## 27. Risks and open items

| Risk / item | Mitigation / next step |
|---|---|
| Sending from the main domain fastnexa.com | Low caps, verification, warm-up, 30-day block, auto-pause on bounces/blacklists. Revisit a second domain in Phase 2 |
| InterServer hourly limit unknown | Ask support; our pace (max about 12/hour per mailbox) is far below typical limits (100–500/hour) |
| MailBaby relay may hide final delivery in cPanel logs | Ask InterServer; bounce detection still catches failures |
| Small warm-up network | Combine with ramp-up and careful lists; monitor inbox rate |
| Helper accounts may get locked | Alert + replace in 5 minutes |
| Verification speed (100/day) | Queue continues daily; paid credits if needed |
| VPS may block port 465 | Use 587 STARTTLS |
| Legal differences by region | Footer, opt-out, source, erasure; get legal advice for UK/EU and Canada |

---

## 28. Phase 2

- Follow-up emails in the same thread, stopping when someone replies
- Shared inbox to answer replies inside the app
- A/B testing
- Multiple domains with rotation
- Yahoo helper accounts
- Optional unsubscribe header if volume grows

---

## 29. Glossary

| Term | Meaning |
|---|---|
| SMTP | Simple Mail Transfer Protocol: how email is **sent** |
| IMAP | Internet Message Access Protocol: how a mailbox is **read** |
| MX record | DNS record saying which server receives a domain's email |
| SPF | Sender Policy Framework: list of servers allowed to send for a domain |
| DKIM | DomainKeys Identified Mail: a digital seal proving an email is genuine and unchanged |
| DMARC | Rule telling receivers what to do with emails failing SPF/DKIM, plus daily reports |
| Bounce | A "delivery failed" message. Hard = address doesn't exist; soft = temporary problem |
| Deferred | The receiving server said "try later" |
| Catch-all | A domain that accepts every address, so verification can't be sure |
| Spintax | `{Hi\|Hello}`: picks one option at random so emails differ |
| Warm-up | Building trust for a mailbox by starting slowly and getting positive engagement |
| Placement test | Checking whether emails land in Inbox, Promotions or Spam |
| DNC | Do-not-contact list |
| Caddy | Web server in front of the app that handles HTTPS |
| Procrastinate | Python job queue stored in PostgreSQL |
| Boilerplate | Standard setup code every app needs |

---

## 30. Sources

- [QuickEmailVerification: verify an email address](https://docs.quickemailverification.com/email-verification-api/verify-an-email-address)
- [QuickEmailVerification: understanding results](https://docs.quickemailverification.com/getting-started/understanding-email-verification-result)
- [QuickEmailVerification: sandbox mode](https://docs.quickemailverification.com/email-verification-api/sandbox-mode)
- [QuickEmailVerification: HTTP codes](https://docs.quickemailverification.com/email-verification-api/kick-start-with-email-validation-api)
- [QuickEmailVerification features](https://quickemailverification.com/features)
- [Smartlead pricing](https://www.smartlead.ai/pricing)
- [Instantly pricing](https://instantly.ai/pricing)
- [Gmail & Yahoo sender requirements (Bird)](https://bird.com/docs/knowledge-base/deliverability/gmail-yahoo-requirements)
- [Microsoft Outlook high-volume sender requirements](https://techcommunity.microsoft.com/blog/outlook/strengthening-email-security-outlook-new-requirements-for-high-volume-senders/4399730)
- [Postmaster Tools v1 retirement (Postmastery)](https://www.postmastery.com/gmail-postmaster-tools-v1-is-retiring-what-you-need-to-know/)
- [Cold email volume per day (LeadHaste)](https://leadhaste.com/blog/how-many-cold-emails-can-you-send-per-day)
- [Free warm-up options (LeadHaste)](https://leadhaste.com/blog/email-warmup-free-email-warmup-options)
- [Port 25/465 blocked on VPS (Findstack)](https://findstack.com/resources/port-25-blocked-vps)
- [Lightreach (open-source reference)](https://github.com/ammysharma11/lightreach)
- [Reacher check-if-email-exists](https://github.com/reacherhq/check-if-email-exists)
