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
| `TELEGRAM_FREE_CHAT_ID` / `TELEGRAM_PREMIUM_CHAT_ID` / `TELEGRAM_RESULTS_CHAT_ID` | Yes | The three production channels — see §4 |
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

**Backup strategy** (PostgreSQL, once hosted) — **documented here, not yet
configured**; nothing in this repo runs a backup today, and this section
must not be read as a claim that one exists until an operator actually
turns it on:

- **Frequency**: nightly full `pg_dump` (or the hosting platform's managed
  automated snapshot — e.g. Render/RDS/Neon daily backups) as the baseline.
  If/when a real payment provider goes live, increase to at least every 6
  hours — a day of lost payment/subscription state is a materially
  different risk than a day of lost dev data.
- **Retention**: minimum 7 daily backups plus 4 weekly backups (28 days),
  matching the common managed-Postgres default tier — adjust upward if the
  hosting platform's compliance requirements (see
  `COMPLIANCE_REQUIREMENTS.md`) end up requiring longer.
- **Restore procedure**: `pg_restore --clean --if-exists -d <target_db>
  <backup_file>` against a **separate** restore-target database first,
  never directly over a live database. After restoring, run `alembic
  current` against the restored copy and confirm it matches the revision
  the backup was taken at (`alembic history` shows the expected chain) —
  do not assume the backup's schema matches `HEAD` if migrations ran
  between the backup and the incident.
- **Recovery expectations**: state an explicit RPO/RTO once real traffic
  exists (e.g. "RPO ≤ 24h, RTO ≤ 2h" for a nightly-backup setup) — this
  document intentionally does not invent numbers for a service that isn't
  live yet; whoever turns on the real schedule should set these based on
  the actual backup cadence chosen above.
- **Verification**: a backup that has never been restored is unverified.
  At minimum, do one full restore-and-verify drill before the first real
  subscriber payment, and repeat on a recurring schedule (e.g. monthly) —
  verification means confirming row counts on `calls`/`subscriptions`/
  `payments` in the restored copy are plausible, not just that
  `pg_restore` exited 0.
- Performance ledger integrity depends on `calls.result_r` and
  `calls.closed_at` never being silently rewritten — master-prompt §33/§60
  ("never delete losing trades," "every correction must produce an audit
  event") is enforced by convention (nothing in this codebase currently
  exposes a raw "edit a closed call" endpoint), not by a database trigger.
  If a correction workflow is ever added, it must write an `AuditEvent`
  (see `app/services.py::audit`) alongside the correction.

## 4. Telegram channel architecture

Production wants four distinct destinations, three of which the bot
routes to and one of which is human-run:

- **FREE CHANNEL** (`TELEGRAM_FREE_CHAT_ID`) — public, `route_free=True` calls
- **PREMIUM PRIVATE CHANNEL** (`TELEGRAM_PREMIUM_CHAT_ID`) — invite-link-gated
  (`app/telegram_access.py::grant_premium_access`), `route_premium=True` calls
- **RESULTS CHANNEL** (`TELEGRAM_RESULTS_CHAT_ID`) — configured but not yet
  wired to an automatic post-on-close event; currently a manual-posting
  destination until that automation is built
- **Community group** (optional) — kept **separate** from the three above;
  Sterling_Room's bot does not manage membership in a community group and
  should not be pointed at one via any of the three `_CHAT_ID` settings

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
- **Not yet built:** a background worker. `app/telegram_bot.py::mark_for_retry()`
  and `app/subscriptions.py::mark_expiring_soon`/`expire_subscriptions`
  exist as functions a scheduled job would call, but nothing currently
  invokes them on a schedule — see the STOP-CONDITION report's "remaining
  blockers."

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
