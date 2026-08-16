# Sterling_Room API

A separate FastAPI service from `Working/backend` (StopLossPro Pro's licensing API) —
own database, own deploy target, per the 2026-08-16 hosting decision. See
`STERLING_ROOM_AUDIT.md` and `STOPLOSSPRO_INTEGRATION.md` (repo root) for the
full audit and integration design this was built from.

## What's actually implemented (Phase 1-8)

**Phase 1-3 — core pipeline:**
- Full call schema (`app/models.py`): calls, call_events, call_messages,
  telegram_chats, telegram_users, subscribers, plans, subscriptions,
  payments, support_tickets, audit_events, admin_users, telegram_update_log.
  Alembic migrations generated and verified (`alembic/versions/`).
- Trade ID generation (`SR-YYMMDD-NNN`, `app/trade_id.py`) — collision-safe
  under concurrency via a unique-constraint + bounded retry, not row locking.
- Call validation + idempotent creation (`app/services.py`) — a retried
  `POST /calls` with the same `source_call_id` returns the existing call
  instead of creating a duplicate Trade ID.
- Call state machine — explicit legal-transition table, `CLOSED -> ACTIVE`
  (and every other illegal move) rejected with HTTP 409.
- Telegram distribution (`app/telegram_bot.py`) — five message templates,
  per-call Free/Premium routing flags, per-(call, chat, type, content) dedup,
  synchronous retry with backoff.
- `PaymentProvider` interface + `ManualPaymentProvider` (cash/crypto,
  admin-verified) — no live processor wired in, per the explicit
  "abstraction first" decision (still open — see `DEPLOYMENT.md` §6).

**Phase 4 — interactive Telegram bot (`app/bot.py`):**
- `/start` main menu (FREE ACCESS / PREMIUM / PERFORMANCE / HOW IT WORKS /
  MY SUBSCRIPTION / SUPPORT) via inline keyboards, webhook-driven
  (`POST <API_PREFIX>/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>`, fails
  closed without the secret).
- FREE ACCESS tracks the Telegram user idempotently (no duplicate rows on
  repeat taps). PREMIUM walks plan selection → payment instructions → "I'VE
  PAID" → a support ticket, ending there by design (see `app/bot.py`'s
  docstring) — actual activation is an admin action (Phase 7).
- Duplicate webhook deliveries are no-ops (`telegram_update_log`).

**Phase 5 — subscription lifecycle (`app/subscriptions.py`,
`app/telegram_access.py`):**
- Full state machine (PENDING_PAYMENT/ACTIVE/EXPIRING_SOON/EXPIRED/
  REVOKED/CANCELLED), idempotent payment confirmation, renewal, batch
  expiry/expiring-soon marking.
- Telegram premium access grant/revoke via real Bot API mechanisms
  (single-use invite links; ban-then-unban).

**Phase 6 — performance ledger (`app/performance.py`):**
- Every stat (win rate, net R, expectancy, profit factor, max drawdown,
  consecutive streaks) computed live from `calls.result_r`/`status`/
  `closed_at` — nothing hand-entered. Daily/weekly/monthly aggregation.
  `GET <API_PREFIX>/performance[/daily|/weekly|/monthly]`.

**Phase 7 — admin dashboard (`app/admin.py`, server-rendered HTML,
cookie auth):**
- Dashboard, Calls (list + full lifecycle/Telegram-delivery detail),
  Performance, Subscribers, Payments (the only place that confirms a
  manual payment and activates a subscription), Plans (create/toggle),
  Telegram (config status + delivery failures), Audit Logs, System Health,
  Settings (never renders secret values). Role-gated writes
  (SUPER_ADMIN/ADMIN only), Argon2id passwords, login lockout after 5
  failed attempts.

**Phase 8 — production readiness:**
- Structured JSON logging (`app/logging_config.py`), a machine-readable
  `GET <API_PREFIX>/monitoring` endpoint, `assert_production_ready()` boot
  guardrails, `DEPLOYMENT.md` (environment separation, secrets, backups,
  webhook registration, rollback).

**Tests:** 97 passing (`tests/`) — trade ID, idempotency, validation, state
machine, Telegram dedup/retry, subscription lifecycle, Telegram access
grant/revoke, performance calculations, the interactive bot (including
duplicate-update-id idempotency), the admin dashboard end-to-end (including
role gating, lockout, and payment confirmation), and full HTTP end-to-end
tests of the actual FastAPI app.

## What's NOT implemented yet

A real payment processor (deliberately deferred — abstraction exists, see
`DEPLOYMENT.md` §6), a background worker to call the existing
`mark_for_retry()` / `mark_expiring_soon()` / `expire_subscriptions()` on a
schedule, automatic RESULTS-channel posting on call close, a UI for
inviting additional admins (direct DB insert today), CI for this service,
deployment automation (`DEPLOYMENT.md` documents a manual process, not a
pipeline).

## Running locally

```bash
cd SterlingRoom/backend
pip install -r requirements.txt
cp .env.example .env   # fill in ADAPTER_API_KEYS at minimum
alembic upgrade head
uvicorn app.main:app --reload
```

## Running tests

```bash
pytest tests/ -v
```

## Deploying

See `DEPLOYMENT.md` for the full guide (environment separation, required
secrets, database backups, Telegram webhook registration, admin bootstrap,
monitoring, rollback). Not yet automated — that document describes a
manual sequence, not a CI/CD pipeline.
