# Sterling_Room — Deployment Guide

This document covers everything needed to take Sterling_Room from "runs in
a dev sandbox" to "serving real Telegram traffic in production." It does
**not** authorize production deployment by itself — see the STOP-CONDITION
report delivered alongside Phase 4-8 for the decisions that still need a
human (payment provider selection, real credentials, production Telegram
channel provisioning).

## 1. Environment separation

Sterling_Room recognizes three environments via `STERLING_ENV`:
`development`, `staging`, `production`. Each environment MUST have its own,
completely separate `.env` (or hosting-platform secret set):

- **Own database.** Never point staging or development at the production
  `DATABASE_URL`.
- **Own Telegram bot token and chat IDs**, if at all possible. A single bot
  token shared between dev and prod is the single biggest way a test
  message ends up in a real channel. If a second bot isn't available yet,
  the second-best mitigation is: dev/staging point `TELEGRAM_PREMIUM_CHAT_ID`
  / `TELEGRAM_FREE_CHAT_ID` at the existing Sterling_Room dev/test Telegram
  group (per the explicit instruction to keep that group available as the
  non-production destination until production channels are verified) and
  **never** at the real FREE CHANNEL / PREMIUM PRIVATE CHANNEL / RESULTS
  CHANNEL.
- **Own `ADMIN_SESSION_SECRET`, `ADAPTER_API_KEYS`, `ADMIN_BOOTSTRAP_TOKEN`,
  `TELEGRAM_WEBHOOK_SECRET`.** None of these should ever be shared across
  environments — a leaked staging secret should not be able to touch
  production.

**What the code enforces vs. what's operational discipline:** every boot
logs the configured environment and a masked tail of each Telegram chat ID
(`app/main.py::_startup_guard`) specifically so a misconfigured environment
is visible in the boot log immediately, not discovered later by a message
landing in the wrong channel. `assert_production_ready()`
(`app/config.py`) refuses to start a `production` process with SQLite, an
empty `ADAPTER_API_KEYS`, an empty `ADMIN_SESSION_SECRET`, or a configured
bot token with no `TELEGRAM_WEBHOOK_SECRET`. Beyond that, cross-environment
isolation is a **secrets-management** control (separate `.env` files /
separate entries in the host's secret manager), not something application
code can fully enforce without knowing in advance which chat IDs are "the
real ones" — that's why this section exists.

## 2. Required secrets checklist

| Variable | Required in prod? | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL in production — see `assert_production_ready` |
| `ADAPTER_API_KEYS` | Yes | Comma-separated; rotate by adding new before removing old |
| `TELEGRAM_BOT_TOKEN` | Yes (once Telegram is live) | From BotFather |
| `TELEGRAM_FREE_CHAT_ID` / `TELEGRAM_PREMIUM_CHAT_ID` | Yes | The two production channels — see §4. No separate results variable: verified results post to `TELEGRAM_FREE_CHAT_ID` itself (2026-08-16 architecture decision). |
| `TELEGRAM_WEBHOOK_SECRET` | Yes (once bot token is set) | Unguessable path segment — see §5 |
| `TELEGRAM_FREE_CHANNEL_LINK` | Recommended | Shown by the bot's FREE ACCESS button |
| `ADMIN_SESSION_SECRET` | Yes | Independent value, not reused from `ADAPTER_API_KEYS` |
| `ADMIN_BOOTSTRAP_TOKEN` | Once, then optional | Used only to create the first admin account |
| `PAYMENT_PROVIDER` | Yes | `manual` today — see §6, this is a real open decision |
| `REDIS_URL` | Recommended (required if >1 worker) | See §3a — rate limiting falls back to a single-process in-memory counter without it |
| `TRUST_PROXY_HEADERS` | Only if behind a reverse proxy/load balancer | `false` by default; enabling it without a proxy that strips inbound `X-Forwarded-For` lets a client spoof its own rate-limit identity — see `app/rate_limit.py` |

Generate high-entropy secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Never commit real values — `.env` is gitignored; `.env.example` documents
names only.

## 3. Database & migrations

```bash
alembic upgrade head
```

Run this on every deploy, before starting the app process. Sterling_Room's
migrations are additive so far (new tables), so there is no destructive
migration in the current history — `alembic downgrade -1` is safe if a
rollback is ever needed, but confirm against the specific revision's
`downgrade()` before relying on it in production.

**Backup strategy** — **the tooling and procedure are built and tested in
this repo (Phase 10); a real, scheduled, provider-managed backup is
NOT configured.** Nothing runs automatically today. This section must not
be read as a claim that automated backups exist in production until an
operator actually turns on a real schedule (either the hosting platform's
managed snapshot product, or `scripts/backup_db.sh` on a cron trigger) —
see "What's actually configured today" below for the honest current state.

**Scripts** (`scripts/`, provider-agnostic — none of them hard-code or
assume a specific hosting platform's backup product; they work against
any standard `postgresql://` connection string or local SQLite file):

- `scripts/backup_db.sh [output_dir]` — reads `DATABASE_URL`, writes a
  timestamped backup to `output_dir` (default `./backups`). PostgreSQL:
  `pg_dump --format=plain` (a portable, human-diffable SQL script).
  SQLite (dev only — production requires PostgreSQL, see
  `assert_production_ready()`): a `VACUUM INTO` online atomic snapshot via
  `scripts/_sqlite_backup.py`, not a raw file copy, so it's safe to run
  against a database the app/worker are concurrently writing to. Exits
  nonzero on any failure — safe to wire into cron without extra error
  handling.
- `scripts/restore_db.sh <backup_file> <target_database_url>` — restores
  into an **explicit target** that is never the ambient `DATABASE_URL` and
  never overwrites an existing SQLite file; there is no "restore in
  place" default, specifically so a fat-fingered invocation cannot
  overwrite a live database. PostgreSQL restore uses `psql -f` (matching
  `backup_db.sh`'s plain-SQL dump format) with `ON_ERROR_STOP=1` so a
  mid-restore SQL error fails loudly instead of leaving a half-restored
  target.
- `scripts/verify_backup.sh <backup_file>` — the actual restore-and-verify
  drill: restores the backup into a scratch database (a throwaway temp
  file for SQLite; `VERIFY_TARGET_URL` — required, must be a dedicated
  scratch DB, never live — for Postgres), runs `alembic current` against
  the restored copy to confirm its migration state, then queries
  `calls`/`subscriptions`/`payments`/`audit_events` to confirm the
  restored schema is real and queryable (not just that the restore
  command exited 0).

All three were exercised end-to-end against a freshly-migrated SQLite
database with seeded data during this hardening pass (backup → restore
into a scratch file → `alembic current` reports the correct head revision
→ row counts confirmed) — the mechanics are proven; what has not been
exercised is the PostgreSQL path against a real managed Postgres instance,
because no production database exists yet (a CREDENTIALS/INFRASTRUCTURE
gap, not an ENGINEERING one — see the final report).

- **Frequency**: nightly full backup as the baseline (`scripts/backup_db.sh`
  on a cron trigger, or the hosting platform's managed automated snapshot
  — e.g. Render/RDS/Neon daily backups — either is a legitimate choice).
  If/when a real payment provider goes live, increase to at least every 6
  hours — a day of lost payment/subscription state is a materially
  different risk than a day of lost dev data.
- **Retention**: minimum 7 daily backups plus 4 weekly backups (28 days),
  matching the common managed-Postgres default tier — adjust upward if the
  hosting platform's compliance requirements (see
  `COMPLIANCE_REQUIREMENTS.md`) end up requiring longer.
- **Restore procedure**: `scripts/restore_db.sh <backup_file> <target_url>`
  against a **separate** restore-target database first, never directly
  over a live database. After restoring, `scripts/verify_backup.sh` runs
  `alembic current` against the restored copy automatically and confirms
  it matches the revision the backup was taken at — do not assume the
  backup's schema matches `HEAD` if migrations ran between the backup and
  the incident.
- **Recovery expectations (RPO/RTO) — placeholders, not yet set**: state
  an explicit RPO/RTO once real traffic exists (e.g. "RPO ≤ 24h, RTO ≤ 2h"
  for a nightly-backup setup) — this document intentionally does not
  invent numbers for a service that isn't live yet; whoever turns on the
  real schedule should set these based on the actual backup cadence
  chosen above and record them here.
- **Verification**: a backup that has never been restored is unverified.
  At minimum, run `scripts/verify_backup.sh` once against a real backup
  before the first real subscriber payment, and repeat on a recurring
  schedule (e.g. monthly) — verification means the restored copy's
  migration state matches and its core tables are queryable with
  plausible row counts, not just that the backup command exited 0.
- **Backup health checklist** (run through this before declaring backups
  "production ready" — none of these are checked automatically):
  1. A real backup schedule is actually configured on the production host
     (managed snapshot enabled, or `scripts/backup_db.sh` on a working
     cron entry) — confirm by checking for a backup file/snapshot newer
     than the configured frequency, not by trusting the config was applied.
  2. `scripts/verify_backup.sh` has been run at least once against a real
     production-shaped backup (not just this session's SQLite smoke test)
     and passed.
  3. Retention matches the policy above and old backups are actually being
     pruned (unbounded retention is a cost/compliance surprise waiting to
     happen, not a safety feature).
  4. The RPO/RTO placeholders above have been filled in with real numbers
     and someone (not just "the system") owns confirming they're still met
     after any change to backup frequency.
  5. Whoever holds production access knows where backups live and how to
     run `restore_db.sh` under pressure — an untested runbook read for the
     first time during an actual incident is not a runbook.
- **What's actually configured today**: nothing. The scripts above are
  built, tested, and ready to point at a real database; no cron entry, no
  managed-snapshot toggle, and no real `DATABASE_URL` exist yet because no
  production database has been provisioned. This is a CREDENTIALS /
  INFRASTRUCTURE gap (needs a real hosting decision + provisioned DB), not
  something further engineering in this repo can close.
- Performance ledger integrity depends on `calls.result_r` and
  `calls.closed_at` never being silently rewritten — master-prompt §33/§60
  ("never delete losing trades," "every correction must produce an audit
  event") is enforced by convention (nothing in this codebase currently
  exposes a raw "edit a closed call" endpoint), not by a database trigger.
  If a correction workflow is ever added, it must write an `AuditEvent`
  (see `app/services.py::audit`) alongside the correction.

## 4. Telegram channel architecture

**Production configuration (2026-08-16 — finalized):**

| Channel | Name | Chat ID | Setting |
|---|---|---|---|
| FREE | Sterling_Room | `-1004319935784` | `TELEGRAM_FREE_CHAT_ID` |
| PREMIUM | SterlingRoom_Premium | `-1004292117841` | `TELEGRAM_PREMIUM_CHAT_ID` |

Bot: `@SterlingroomBot`, already an administrator in both channels.

Two distinct destinations, both bot-routed — **there is no separate
Results channel**:

- **FREE CHANNEL** (`TELEGRAM_FREE_CHAT_ID`) — public, `route_free=True`
  calls, market content, premium-conversion content, **and** every
  verified CLOSED/STOPPED result (see below). This is a deliberate
  architecture decision, not a gap: Sterling_Room's own free channel is
  where results are published, by design.
- **PREMIUM PRIVATE CHANNEL** (`TELEGRAM_PREMIUM_CHAT_ID`) — invite-link-gated
  (`app/telegram_access.py::grant_premium_access`), `route_premium=True` calls

**Results automation** — automatically posted to `TELEGRAM_FREE_CHAT_ID`
on every CALL CLOSED / STOPPED event carrying a `result_r`
(`app/api.py::transition_call` → `telegram_bot.distribute_call(...,
MessageType.RESULTS, chat_ids=[settings.TELEGRAM_FREE_CHAT_ID])`, Phase
10, re-pointed from a since-retired `TELEGRAM_RESULTS_CHAT_ID` setting on
2026-08-16). Fires regardless of the individual call's own
`route_free`/`route_premium` flags — a premium-only call's result still
gets a verified result post in the free channel. The result text is
rendered exclusively from `call.result_r` — the same authoritative field
the performance ledger sums over — never recomputed separately for
Telegram. Retrying the same close event is safe: `distribute_call`'s
content-hash dedup on `(call_id, message_type, chat_id)` means a retried
request that produces identical result text reuses the existing
`CallMessage` row instead of posting a duplicate.

- **Community group** (optional) — kept **separate** from the two above;
  Sterling_Room's bot does not manage membership in a community group and
  should not be pointed at one via either `_CHAT_ID` setting.

## 5. Registering the Telegram webhook

Once `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` are set and the app
is deployed at a public HTTPS URL:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-domain><API_PREFIX>/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>"
```

Verify with `getWebhookInfo` and confirm `last_error_message` is empty
after sending `/start` to the bot. The route 404s on a missing/wrong
secret by design (`app/api.py::telegram_webhook`) — a 404 from Telegram's
side after registering means the secret in the URL doesn't match
`TELEGRAM_WEBHOOK_SECRET` on the server.

## 5a. Rate limiting & Redis (Phase 9)

Every externally reachable route is rate-limited (`app/rate_limit.py`) —
admin login/bootstrap and the Telegram webhook per-IP, admin
reads/writes per authenticated admin, StopLossPro adapter traffic per API
key (generous limits — a safety net against a runaway caller, not an
abuse defense, since it's authenticated trusted traffic).

**Single-worker launch**: no additional infrastructure required. The
in-memory backend is correct as long as the app runs as exactly one
process (e.g. `uvicorn app.main:app` with no `--workers` flag, or a
platform that runs a single instance).

**Multi-worker / multi-instance**: set `REDIS_URL` before scaling past one
worker. Without it, each worker keeps its own independent counters and the
effective rate limit silently multiplies by worker count — the app still
boots and runs fine, it just isn't actually enforcing the limit you think
it is. `app/main.py`'s boot log and `Settings.production_warnings()` log a
loud warning in production if `REDIS_URL` is unset, specifically so this
doesn't go unnoticed. A managed Redis instance (Render Key Value, AWS
ElastiCache, Upstash, etc.) is sufficient — Sterling_Room only uses
`INCR`/`EXPIRE`, no pub/sub, no persistence requirement (`--save ""` /
ephemeral cache tier is fine).

If Redis is configured but becomes unreachable at runtime, requests are
allowed through (fail open, loudly logged) rather than the API going down
— admin login keeps its own separate DB-backed lockout regardless, so
brute-force protection on the highest-value target doesn't disappear
during a Redis outage.

## 5b. Background worker (Phase 10)

`app/worker.py` runs the two jobs the deployment audit previously flagged
as "functions with nothing invoking them": Telegram delivery retry and
subscription lifecycle (expire + revoke access). Deliberately a
lightweight polling script, not Celery/RQ/Kafka — see the module
docstring for the reasoning; at Sterling_Room's call volume a broker
would be pure operational overhead, and every job is idempotent by
construction (unique constraints, status-filtered queries, a persisted
"already done" marker column per job), which is what actually makes a
simple poll-and-retry loop safe, not a distributed queue.

Two supported ways to run it — pick whichever fits the host:

- **Scheduled/cron invocation** (recommended if the host has a native
  scheduled-job feature, e.g. Render Cron Jobs, a Kubernetes CronJob):
  ```bash
  python -m app.worker --once
  ```
  Runs every job exactly one time, logs one `worker_tick_complete` line,
  exits 0. Schedule this every `WORKER_INTERVAL_SECONDS` (default 60s;
  override via `STERLING_WORKER_INTERVAL_SECONDS`) — the actual interval
  chosen is a host-scheduler config value, not something the app enforces.

- **Long-lived background process** (if the host runs a persistent
  "worker" process type alongside the web process, e.g. Render Background
  Worker, a second container/dyno):
  ```bash
  python -m app.worker
  ```
  Loops forever, sleeping `WORKER_INTERVAL_SECONDS` between ticks, and
  exits cleanly on `SIGTERM`/`SIGINT` (logs `worker_shutdown_requested`
  then `worker_stopped` — no mid-tick work is left half-done, since each
  job commits its own progress incrementally, not once at the very end;
  see the idempotency notes in `app/telegram_bot.py`'s `RetryRunStats` and
  `app/subscriptions.py`'s `LifecycleStats` docstrings).

**Concurrency**: running more than one worker instance (either mode)
simultaneously is safe, not just tolerated — there is intentionally no
distributed lock, because every job re-derives its work list from
database state on each run (a `FAILED`-status filter, an
`expiry_date`-threshold filter, a `revoked_at IS NULL` filter), so two
overlapping ticks do redundant work, not incorrect work. Do not add a
lock unless a real operational reason requires it later.

**Process restart safety**: nothing the worker does is held only in
memory. A crash mid-batch (partway through retrying 50 failed messages,
say) loses at most the DB commit for whichever single message was
in-flight — `process_telegram_retries` and `revoke_lapsed_telegram_access`
both commit after each row, not once per batch — so the next tick simply
picks up wherever the last one stopped, exactly as if it had run to
completion. Verified in `tests/test_worker.py`'s worker-restart tests
(independent `SessionLocal()`s against a persisted file DB, simulating a
real process restart).

## 6. Payment provider — still an open decision

`app/payments.py`'s `PaymentProvider` abstraction is real and tested; only
`ManualPaymentProvider` (cash/crypto, admin-confirmed via
`/admin/payments`) is implemented. **This is not a gap to silently work
around** — per the 2026-08-16 decision, a real processor (Stripe, etc.)
is a deliberate later choice, not an oversight. Before production launch,
a human needs to decide: continue with manual-only for launch, or select
and integrate a real processor first. Either is a legitimate choice; this
document doesn't make it.

## 7. Admin dashboard bootstrap

1. Set `ADMIN_BOOTSTRAP_TOKEN` to a fresh high-entropy value.
2. `curl -X POST https://<host>/admin/bootstrap -H "X-Bootstrap-Token: <token>" -d "email=you@example.com" -d "password=<strong password, 12+ chars>"`
   — this only succeeds once (refuses if any admin account already exists).
3. Log in at `/admin/login`.
4. Consider rotating/clearing `ADMIN_BOOTSTRAP_TOKEN` afterward — the
   endpoint is already inert once an admin exists, but rotating removes any
   doubt.
5. Additional admins are created by direct DB insert today (no
   "invite another admin" UI exists yet) — use `app/security.py::hash_password`
   to generate the `password_hash`.

## 8. Monitoring

- `GET /health` — liveness, no auth, no DB access.
- `GET /health/ready` — readiness, checks DB connectivity, no auth.
- `GET <API_PREFIX>/monitoring` — adapter-key-authenticated JSON: DB
  reachability, active call count, failed Telegram delivery count, open
  support ticket count, pending-payment count, and
  `assert_production_ready()`'s current problem list. Point an external
  uptime/monitoring tool at this with the same bearer token used for the
  adapter.
- `/admin/health` — the human-facing equivalent, same underlying counts,
  rendered in the dashboard.
- Logs are structured JSON in non-DEBUG environments
  (`app/logging_config.py`) — one JSON object per line on stdout, ready for
  any log aggregator that reads stdout (Render, CloudWatch, etc.) without a
  separate shipping agent. Every log line carries the request's
  correlation ID (`request_id`, also echoed as the `X-Request-ID` response
  header) when one is available, so every log line touched by a single
  inbound request can be grep'd together.
- Rate-limit events (`rate_limit_exceeded`, scope/identity/limit/
  retry_after/path) are logged at WARNING from `app/rate_limit.py` —
  search logs for this message to see who is hitting limits and where.
- **Background worker** (`app/worker.py`, Phase 10) runs the Telegram
  retry job (`telegram_bot.process_telegram_retries`) and the
  subscription lifecycle job (`subscriptions.run_lifecycle_job` — marks
  EXPIRING_SOON, expires, revokes lapsed premium Telegram access) on a
  schedule. See §5b below for how to run it in production. Every worker
  tick logs a single `worker_tick_complete` structured line with counts
  for both jobs — grep for it to confirm the worker is actually running
  and to see retry/expiry volume.

## 9. Deploying (manual sequence — no CI/CD pipeline exists for this
service yet)

1. Provision a host + PostgreSQL — a **separate** database from
   StopLossPro Pro's licensing DB (`Working/backend`) and from any other
   service in this repo. Provision Redis too if running more than one
   worker (§5a).
2. Set every secret in §2 in the host's secret manager, never in a
   committed file.
3. `alembic upgrade head`
4. Start the app: `uvicorn app.main:app --host 0.0.0.0 --port <port>`
   (or the platform's equivalent process command).
5. Confirm `GET /health` and `GET /health/ready` both return 200, and
   `GET <API_PREFIX>/monitoring` (with a valid adapter key) reports
   `production_ready: true` with an empty `production_problems` list.
6. Register the Telegram webhook (§5) and send `/start` to confirm the
   bot responds.
7. Bootstrap the first admin account (§7) and confirm `/admin/login` works.
8. Configure the StopLossPro adapter
   (`Historical/main_dispatcher/sterling_adapter.py`'s settings) with this
   service's base URL and one of the `ADAPTER_API_KEYS`.
9. Send one real (or clearly-marked test) call through the full pipeline
   end to end and confirm it lands correctly before declaring the
   migration complete.

## 9a. Domain & HTTPS — no production domain has been supplied

No domain name has been provided for Sterling_Room as of this audit, and
none is invented here. What's required once one exists:

- **DNS**: an `A`/`AAAA` record (or a `CNAME` if the host issues one, e.g.
  `sterling-api.yourdomain.com CNAME your-app.onrender.com`) pointing the
  chosen subdomain at the hosting platform. Most PaaS hosts (Render, Fly,
  Railway, etc.) issue and auto-renew a TLS certificate once the DNS record
  resolves to them — check the specific host's docs for the exact record
  they expect before creating it.
- **HTTPS is not optional**: the Telegram webhook (§5) and the admin
  session cookie's `secure` flag (`app/admin.py::login_submit`, tied to
  `settings.is_production`) both require it — the admin cookie is not
  marked secure in non-production environments specifically so local
  HTTP development still works, which means a production deploy served
  over plain HTTP would silently transmit the session cookie in the
  clear. Do not put a production Sterling_Room instance behind anything
  but HTTPS.
- **Recommended split**: `api.<domain>` for the adapter/monitoring API and
  `admin.<domain>` (or a path prefix on the same host) for the dashboard —
  not required by the code (both are served by the same FastAPI app,
  `app/main.py`), but keeps the admin surface off a subdomain a public
  API consumer would think to probe.
- Until a domain is chosen, staging/local traffic uses the hosting
  platform's default HTTPS subdomain (e.g. `*.onrender.com`), which is
  sufficient for the Telegram webhook requirement (Telegram only requires
  valid HTTPS, not a custom domain).

## 10. Rollback

- **App**: redeploy the previous container/release; migrations run so far
  are additive, so an old app version against a newer schema should still
  work (it simply won't use the new columns/tables) — verify this
  assumption against the specific migration before relying on it.
- **Database**: `alembic downgrade <previous revision>` only after
  confirming the specific migration's `downgrade()` is safe to run against
  real data — read it, don't assume.
- **Telegram**: `deleteWebhook` immediately stops the bot from receiving
  updates if something is actively misbehaving in production, without
  needing to redeploy anything.
