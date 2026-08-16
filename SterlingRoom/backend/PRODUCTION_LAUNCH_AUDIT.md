# Sterling_Room — Production Launch Audit

> **UPDATE (Phase 9, same day, commit `f83d503`):** the "no rate limiting on
> public-facing routes" blocker called out below (§ readiness score, §
> remaining blockers, § final launch checklist) is now closed. Every
> externally reachable route has a scoped, tested rate limiter — see
> `STERLING_ROOM_FINAL_LAUNCH_REPORT.md` for the full re-audit and current
> readiness score. The rest of this document is preserved as the original
> Phase 8 audit record and intentionally NOT edited below this notice,
> except for the two checklist/blocker lines struck through where Phase 9
> closed them.

**Release candidate audited:** `1f56193`
**Date:** 2026-08-16
**Scope:** verification and launch-preparation only. Feature set frozen per
instruction — no product features were added in this pass. Only
launch-critical artifacts were created (this audit, `STAGING.md`,
`TELEGRAM_PRODUCTION_SETUP.md`, `PAYMENT_PROVIDER_DECISION.md`). No file
outside `SterlingRoom/` was touched — confirmed with `git diff --stat`
against `Working/`, `Historical/`, and `Dead/`: empty. **No deployment was
performed.**

---

## STEP 1 — Release audit of `1f56193`

Every check below was executed against the actual committed code (`git
log` confirms `HEAD` is `1f56193`, working tree clean), not just read.

| Check | Result | Evidence |
|---|---|---|
| 107 tests pass | **PASS** | `pytest -q` → `107 passed` |
| Production config separated | **PASS** | Live boot test: a process started with `ENV=production`, SQLite, empty `ADAPTER_API_KEYS`, empty `ADMIN_SESSION_SECRET`, `DEBUG=true` refused to start — `RuntimeError` listing all four problems, process exited before serving any traffic |
| No secrets committed | **PASS** | Regex scan of all committed `.py`/`.md`/`.example`/`.ini` files for API-key/token/private-key/hardcoded-password patterns: zero hits. `.gitignore` covers `.env`, `.env.*`, `*.env`. No `.env` file is tracked. |
| Migrations reproducible | **PASS** | Fresh SQLite DB → `alembic upgrade head` runs all 3 migrations cleanly in order (`bdd096501e58` → `66aee44b02d3` → `8ce0bde5280b`) → `alembic check` reports "No new upgrade operations detected" → `alembic history` shows a single linear chain, no branch points |
| Telegram config is environment-based | **PASS** | Every `TELEGRAM_*` value is a `Settings` field read from `STERLING_TELEGRAM_*` env vars (`app/config.py`); nothing hardcoded |
| Payment provider abstracted | **PASS** | `app/payments.py`: `PaymentProvider` ABC, `get_provider()` factory, only `ManualPaymentProvider` registered; `NotImplementedError` for anything else (fails loud, not silent) |
| Logging works | **PASS** | Live boot test: structured JSON log line emitted on startup with `env`, `telegram_configured`, masked chat IDs, `payment_provider` |
| Health endpoint works | **PASS** | Live boot test: `GET /health` → 200, `GET /health/ready` → 200 (checks real DB connectivity) |
| Admin authentication works | **PASS** | Live boot test: `GET /admin/login` → 200; `GET /admin` unauthenticated → 303 redirect. Test suite: bootstrap, login success/failure, 5-attempt lockout, logout, session cookie all covered (`tests/test_admin.py`) |
| Role permissions work | **PASS** | Test suite: a VIEWER-role admin attempting to confirm a payment → 403 (`test_payments_confirm_requires_write_role`) |
| StopLossPro integration remains functional | **PASS** | `Historical/main_dispatcher/main.py` and `sterling_adapter.py` both `py_compile` clean on the actual device checkout; adapter hook (`_post_to_sterling`/`sterling_adapter` reference count: 13) present and unchanged; `git diff --stat` between the pre-Phase-4 baseline and `1f56193` restricted to `Historical/main_dispatcher` is empty — this pass did not touch it at all |

**Additional check run beyond the requested list:** `GET /api/v1/monitoring`
without a bearer token → 401 (confirmed fail-closed); with a valid token →
200 with real counts. Not required by Step 1's list but directly relevant
to "production configuration is separated" and worth having as evidence.

## STEP 5 — Deployment readiness (components verified, not deployed)

- **API**: boots cleanly with valid config, refuses to boot with unsafe
  production config (see table above). All routes registered and
  reachable in a live smoke test (`/health`, `/health/ready`, `/docs`,
  `/admin/login`, `/admin`, `/api/v1/monitoring`).
- **Database**: SQLite migration path verified end-to-end (see Step 1).
  **Caveat, stated plainly:** this has not been run against a real
  PostgreSQL instance in this pass — no Postgres server is available in
  this audit environment. SQLAlchemy's types used throughout
  (`Uuid`, `Numeric`, `DateTime(timezone=True)`, `SAEnum`) are all
  dialect-portable and the migrations contain no SQLite-specific DDL, so
  a clean run against Postgres is expected — but "expected" is not the
  same as "verified," and this is called out explicitly as a remaining
  gap rather than asserted as done.
- **Workers**: **none exist.** `mark_for_retry()`
  (`app/telegram_bot.py`), `mark_expiring_soon()`/`expire_subscriptions()`
  (`app/subscriptions.py`) are implemented and unit-tested but nothing
  invokes them on a schedule — confirmed by grep (no scheduler/cron
  library, no background-task loop anywhere in `app/`). This is a real,
  unresolved gap, listed as a blocker below, not glossed over.
- **Telegram bot**: `app/bot.py` imports and compiles cleanly, the
  webhook route is registered (`POST <API_PREFIX>/telegram/webhook/<secret>`),
  and the full bot test suite (22 tests) plus the live end-to-end webhook
  test both pass. Not yet tested against the real Telegram network (no
  bot token in this audit environment) — that requires the staging setup
  in `STAGING.md`.
- **Admin dashboard**: every section (`/admin`, `/admin/calls`,
  `/admin/performance`, `/admin/subscribers`, `/admin/payments`,
  `/admin/plans`, `/admin/telegram`, `/admin/audit`, `/admin/health`,
  `/admin/settings`) is covered by the admin test suite (24 tests) and
  the login page/redirect were additionally verified live.

## STEP 6 — End-to-end staging-style test run

Re-ran explicitly and independently (not just as part of the full suite):

```
tests/test_end_to_end.py::test_full_call_lifecycle_end_to_end PASSED
tests/test_end_to_end.py::test_full_subscriber_lifecycle_end_to_end PASSED
```

`test_full_call_lifecycle_end_to_end` drives: adapter `POST /calls` →
Trade ID assigned → idempotent retry confirmed → TP1 update → close with
a result → `GET /performance` reflects it → admin call-detail page shows
the full lifecycle and Telegram delivery status → admin dashboard shows
the closed-call count → `/monitoring` reports healthy.

`test_full_subscriber_lifecycle_end_to_end` drives: `/start` webhook →
PREMIUM → plan selection → payment instructions → "I'VE PAID" (ticket
filed, no auto-activation) → admin confirms via `/admin/payments` →
subscription ACTIVE → premium access grant (mocked Telegram invite-link
creation) → forced expiry → `expire_subscriptions()` → access revoked →
Telegram-side revocation call exercised (mocked ban/unban) → admin
subscribers page reflects final state.

**Caveat, stated plainly:** both tests run against SQLite with a mocked
Telegram transport (real network calls are never made in the test suite,
by design — no test should depend on external services). This proves the
application logic is correct; it does not prove the real Telegram Bot API
integration works end-to-end against a live bot, which is why
`STAGING.md` §4 step 8 calls for re-running this scenario against a real
staging deployment before Telegram migration.

## STEP 7 — Security review

| Area | Finding |
|---|---|
| Secrets | No secrets committed (Step 1). `.env.example` documents names only. Settings page (`/admin/settings`) never renders a configured secret's value, only whether it's set. |
| Authentication | Admin: Argon2id (tuned: time_cost=3, memory_cost=64MB, parallelism=4), never plaintext/logged. Sessions: itsdangerous-signed, 12h TTL, `httponly`, `samesite=lax`, `secure` tied to `is_production`. Adapter: constant-time bearer comparison (`hmac.compare_digest`), fails closed if unconfigured. |
| Authorization | Role-gated writes confirmed (VIEWER → 403 on payment confirm, test-verified). Read pages require any authenticated admin — no distinct read-role tiering beyond that today; acceptable for current team size, worth revisiting if the admin user base grows. |
| Webhook verification | Telegram webhook uses a secret **path segment**, constant-time compared, 404s (not 401/403) on mismatch — matches Telegram's own recommended pattern, since Telegram webhooks carry no bearer-header mechanism. **Gap:** this is the *only* verification — Telegram does not sign webhook payloads the way Stripe does, so anyone who obtains the URL (e.g. via a leaked log line, a misconfigured proxy) can POST arbitrary "updates." Impact is bounded (the bot logic only reads a handful of fields and never trusts client-supplied identity for anything privileged — Telegram `from.id` is used as the Telegram user ID, which is inherently attacker-controllable in a forged request), but this is worth knowing explicitly rather than assuming the URL alone is sufficient. Mitigation: keep the secret truly secret (never logged — confirmed masked in `_mask_chat_id`, and the webhook secret itself is never logged anywhere), rotate it if ever suspected leaked. |
| Rate limiting | **Gap.** No rate-limiting middleware exists anywhere in the app (confirmed by grep — no `slowapi`/equivalent). Admin login has account-lockout after 5 failures (a narrow, effective mitigation for credential stuffing against one account, but not a general request-rate control), and the adapter/webhook routes have no throttling at all beyond whatever the hosting platform provides at the edge. For a public-facing webhook and API, this is a real pre-launch gap — flagged as a blocker below, not fixed in this pass per the feature freeze (adding rate-limiting middleware is arguably launch-critical hardening rather than a product feature; deliberately left for you to greenlight explicitly rather than assumed). |
| Duplicate events | Verified structurally, not just by convention: `calls.source_call_id` unique constraint, `payments.provider_payment_id` unique, `telegram_update_log.update_id` primary key. All three have passing idempotency tests, including a live end-to-end retry. |
| Telegram permissions | Documented in `TELEGRAM_PRODUCTION_SETUP.md` — minimum bot permissions specified per channel (Post Messages for FREE/RESULTS; Add Users + Ban Users for PREMIUM, nothing more). |
| Database permissions | **Not yet specified.** No documented recommendation for the production DB user's privilege level (e.g. whether it should have `CREATE`/`DROP` beyond what `alembic upgrade` needs, or be scoped to a single schema). Recommendation: the application's runtime DB user should not be a Postgres superuser; grant it `CONNECT`, `USAGE` on its schema, and `SELECT`/`INSERT`/`UPDATE`/`DELETE` on its own tables — migrations can run under a separate, more-privileged user if your hosting platform's workflow prefers that split. Not enforced by this codebase; a hosting-configuration decision. |
| Admin access | Bootstrap fails closed without `ADMIN_BOOTSTRAP_TOKEN`, refuses a second bootstrap once any admin exists. No "invite another admin" UI — additional admins require a direct DB insert today (documented gap, not hidden). |
| Logging | Structured JSON, one line per event, no secret values ever interpolated into a log line (checked: `ADMIN_SESSION_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, `ADAPTER_API_KEYS`, and password/hash values are never passed to any `log.*()` call anywhere in `app/`). |
| Production configuration | `assert_production_ready()` verified live to actually block an unsafe boot (Step 1) — not just a function that exists but is never called. |

**Net assessment:** no critical/blocking vulnerability found in what
exists. Two real, launch-relevant gaps identified and left unfixed by
design (feature freeze + "don't add scope autonomously" instruction):
**no rate limiting**, and **no independent human security review has
happened yet** (this audit was performed by the same model that wrote
the code — useful, not a substitute for a second reviewer before real
money and real user data flow through it).

---

## STEP 8 — STOP. Final report.

### 1. Production readiness score

**7.5 / 10.**

Everything that can be verified through code and live testing passes
cleanly: config separation, migrations, auth, role permissions, logging,
health checks, idempotency, and the full application logic for both
lifecycles. What holds this back from a higher score is not defects
found, but real, unverified, or missing pieces that are honest to call
out rather than round up past: no Postgres-backed run yet, no live
Telegram network test yet, no background worker, no rate limiting, no
independent security review, and two genuinely open business decisions
(payment provider, real Telegram channels) that aren't engineering work
at all.

### 2. Remaining blockers

- No staging deployment has actually been stood up yet (this audit
  prepared the spec — `STAGING.md` — it did not provision infrastructure,
  which requires hosting access this environment doesn't have).
- No background worker scheduling `mark_for_retry`/`mark_expiring_soon`/`expire_subscriptions`.
- ~~No rate limiting on public-facing routes.~~ **Closed in Phase 9** (`app/rate_limit.py`, commit `f83d503`) — see the final launch report.
- No independent (human, or second-model) security review performed yet.
- Migrations unverified against real PostgreSQL (SQLite-only so far).
- Telegram bot logic unverified against the real Telegram network (mocked in all tests).
- RESULTS channel has no automatic posting (manual for now).
- No "invite another admin" UI (direct DB insert only).

### 3. Exact credentials/configuration required

See `STAGING.md` §3 for the full table. Minimum to go live: `DATABASE_URL`
(Postgres), `ADAPTER_API_KEYS`, `ADMIN_SESSION_SECRET`,
`ADMIN_BOOTSTRAP_TOKEN` (one-time). All independently generated, all set
only in the production secret manager.

### 4. Exact Telegram setup required

See `TELEGRAM_PRODUCTION_SETUP.md` in full. Summary: decide on a
dedicated production bot vs. reusing the existing one (§1), create the
FREE/PREMIUM/RESULTS channels with the exact minimum bot permissions
listed (§2-4), do not set any of `TELEGRAM_FREE_CHAT_ID` /
`TELEGRAM_PREMIUM_CHAT_ID` / `TELEGRAM_RESULTS_CHAT_ID` in production
until `STAGING.md` §5's three conditions are met, then follow the
cutover sequence in §6.

### 5. Exact payment decision required

See `PAYMENT_PROVIDER_DECISION.md` in full. Decision needed: stay
manual-only for launch (Option A, zero additional work) or integrate a
real processor first (Stripe/PayPal/crypto — Options B/C/D, each with
its exact credential/account requirements listed). `PAYMENT_PROVIDER=manual`
remains the default and stays fully functional either way.

### 6. Exact deployment steps

Full sequence in `DEPLOYMENT.md` §9, cross-referenced with `STAGING.md`
for staging specifically. Order: provision staging first → validate
(§6 above) → only then provision production infrastructure → set
production secrets → `alembic upgrade head` → deploy → confirm
`/health`/`/health/ready`/`/monitoring` → register Telegram webhook →
bootstrap admin → point the StopLossPro adapter at the new base URL →
send one clearly-marked test call end to end.

### 7. Final launch checklist

- [ ] Staging environment actually provisioned (per `STAGING.md`) and validated (its §5)
- [ ] Migrations run once against real PostgreSQL and confirmed to match the SQLite-verified schema
- [ ] Payment provider decision made (`PAYMENT_PROVIDER_DECISION.md`) and implemented if not staying manual-only
- [ ] Production Telegram bot + 3 channels created per `TELEGRAM_PRODUCTION_SETUP.md`, cutover sequence followed
- [ ] All production secrets generated fresh and set only in the production secret manager
- [ ] Background worker decision made: implement scheduling for the three existing-but-unscheduled functions, or explicitly accept manual/cron triggering for launch
- [x] Rate limiting added to public-facing routes — **done, Phase 9** (`app/rate_limit.py`; in-memory + Redis-backed shared-state, 30 tests)
- [ ] Independent security review completed (a second reviewer, human or model, on `app/admin.py`'s auth flow and the Telegram webhook auth model specifically)
- [ ] Production deploy executed, `/health`/`/health/ready`/`/monitoring` all green
- [ ] Webhook registered, `/start` confirmed working against the production bot
- [ ] First admin bootstrapped, `ADMIN_BOOTSTRAP_TOKEN` rotated/cleared afterward
- [ ] StopLossPro adapter pointed at production, one clearly-marked test call sent end to end and confirmed correct in every channel
- [ ] Backup schedule confirmed actually running (not just documented) before the first real subscriber payment

**No deployment has been performed. This document is preparation only,
as instructed.**
