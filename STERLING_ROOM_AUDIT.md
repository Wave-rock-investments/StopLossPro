# Sterling_Room — Initial Workspace Audit

*Audited 2026-08-16. Read-only pass — nothing in the existing project was modified to produce this document.*
*Primary sources: live inspection of `C:\Users\trish\OneDrive\Desktop\StoplossApk-mt5\` (git log, file tree, source reads) plus the project's own `PROJECT_STATUS.md` (2026-08-05) and `FEATURES.md` (2026-08-16, itself a fresh read-only audit).*

---

## 1. Existing repositories

One repo: `StoplossApk-mt5`, git-tracked (`.git` present, remote `origin`). **Two branches matter, and they are not equivalent:**

- **`phase0-clean-baseline`** — an orphan branch (no parent), 6 commits, the *only* clean history. This is where the StopLossPro Pro backend/security work actually lives.
- **`main` / `origin/main`** — a **separate, still-contaminated branch**. Per `PROJECT_STATUS.md` §3.1, commit `d253b56` on this branch still contains a leaked GitHub PAT (revoked, but the commit itself was never purged from this branch's history). A related leaked-token artifact (`p1_admin.html`) is also reported still live on a public GitHub Pages URL as of the last check (2026-08-05).
- The user has **explicitly and deliberately deferred** cleaning this up (standing rule in `PROJECT_STATUS.md` §10.8-9) — this is not something I am touching or re-flagging as urgent; it's noted here only because it determines **which branch Sterling_Room should be built on** (see §14).

`.github/workflows/build.yml` and `build-macos.yml` exist (PyInstaller client builds), not backend deployment.

## 2. StopLossPro — three distinct codebases, not one

The master prompt refers to "StopLossPro Personal" as the upstream call source. There are actually **three** related-but-separate things in this workspace, and they matter differently to Sterling_Room:

| Codebase | What it is | Relevance to Sterling_Room |
|---|---|---|
| `Historical/main_dispatcher/` | The **personal-use build** — no login, no licensing, single-laptop, direct local MT5. This is what "StopLossPro Personal" means. **As of this same working session**, it gained a real Telegram Bot API sender (`signal_dispatcher.py`) that posts BUY/SELL signals to one configured channel. | **This is the upstream call source.** Already generates and sends structured trade calls. Do not rebuild. |
| `Working/StopLossPro_OfflineSale/` | The **commercial, licensed product** ("StopLoss Pro (MT5 edition)") — separate business line, sold offline (Reddit/Telegram/direct, cash/crypto, manual admin activation), pre-revenue. Has its own risk engine, MT5 execution, licensing client. **Zero Telegram or payment code** (confirmed by grep). | Not the call source. Do not touch — it's a separate product with its own live security/legal history (see §3). |
| `Working/backend/` | FastAPI + SQLAlchemy + PostgreSQL licensing API for the *commercial* client above — accounts, licences, devices, sessions, MFA, admin console, audit log. 108 passing tests, Alembic migrations, real-Postgres concurrency proof, a delivered (not final) legal review brief. | **Not Sterling_Room's domain**, but its patterns (FastAPI skeleton, pydantic-settings config, audit_events table, Argon2id/Ed25519/TOTP, server-rendered admin console, "server is authoritative" discipline) are the strongest reusable foundation available for Sterling_Room's own API — see §11. |

## 3. Context worth knowing before touching anything in this repo

`Working/` had a serious, now-largely-remediated security incident (three leaked GitHub PATs with gist-write scope, live telemetry of customer MT5 account/balance/positions/GPS to a public `ntfy.sh` topic, three unauthenticated remote bypasses). All of §3.2–3.4 of that incident has been removed and re-verified absent. Two items remain open by the user's own explicit choice (contaminated `main` branch, one still-live public artifact) — not my task to act on, but the reason Sterling_Room work should not start from `main` (see §14).

This history is *why* the existing backend is unusually disciplined about secrets (env-only, `.env.example` with names not values, a pre-commit secret scanner, `verify_release.py` scanning shipped binaries for credential patterns). Sterling_Room should inherit that discipline rather than reintroduce a softer standard.

## 4. Tech stack actually in use

- **Client apps**: Python 3.10–3.13, Kivy 2.3 / KivyMD 1.2, PyInstaller (`--onefile --windowed`), MetaTrader5 official Python package (direct Windows bridge, not an EA/ZeroMQ).
- **Backend**: FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL (prod) / SQLite (dev/test), Argon2id (`argon2-cffi`), Ed25519 + Fernet (`cryptography`), TOTP (`pyotp`), pydantic-settings.
- **No JS/React/web frontend anywhere.** The one "dashboard" that exists (StopLossPro's admin console) is server-rendered HTML from FastAPI, no SPA.
- **No task queue / worker framework** (no Celery/RQ/arq) anywhere yet.

## 5. Existing Telegram functionality

Real, but narrow: `Historical/main_dispatcher/signal_dispatcher.py` (built earlier in this session) implements `dispatch_signal()`/`dispatch_result()` — plain-text POSTs to `api.telegram.org/bot<token>/sendMessage` for **one** configured `chat_id`, fire-and-forget, background-thread + Kivy-Clock delivery. No bot with `/start` or a menu, no webhook receiver, no multi-channel routing, no subscriber tracking, no message-ID/delivery-status storage, no retry queue. This is a sender, not a bot service — everything in master-prompt §9–§30 (bot menu, plans, payments, subscriber DB, retry/dedup) is genuinely new work.

`Working/StopLossPro_OfflineSale` has **zero** Telegram code of any kind (confirmed by grep across the whole tree).

## 6. Existing MT5 functionality

Both `Historical/main_dispatcher/mt5_dispatcher.py` and `Working/StopLossPro_OfflineSale/lib/mt5_api.py` wrap the `MetaTrader5` package directly: order placement with broker-`volume_max` cluster-splitting, stops-level pre-flight guard, auto pending-type correction, position management (close/partial/break-even/modify/ATR-trail), candle/ATR fetch. This is real, working, direct-broker execution code — not a "call object" system. No Trade ID, no call state machine, no structured lifecycle events exist anywhere. Per master-prompt §66, MT5 execution is explicitly out of scope for Sterling_Room's Phase 1 (Telegram-only) — noted so nothing here gets wired in prematurely.

## 7. Existing database

Only `Working/backend` has one: SQLAlchemy models for `users`, `licences`, `devices`, `sessions`, `mfa_credentials`, `recovery_codes`, `consent_records`, `audit_events`, `admin_users` (9 tables, UUID PKs, timezone-aware timestamps throughout). Alembic is wired up with 2 real migrations. **None of this models calls, subscribers, plans, payments, or performance** — that schema doesn't exist yet anywhere.

## 8. Existing backend

`Working/backend/app` (FastAPI) is a genuinely solid, tested asset: rate-limited auth, server-authoritative licensing, one-active-session DB-level guarantee (proven under real PostgreSQL concurrency, not just SQLite), Ed25519-signed short-lived grants, TOTP MFA with independent encryption key, structured `audit_events`, a server-rendered admin console with its own RBAC-ready `AdminRole`. 108 backend tests currently passing. `Settings.assert_production_ready()` refuses to boot with an unsafe production config. This is the single best-built piece of infrastructure in the workspace and the natural foundation to extend for Sterling_Room's API (see §11) rather than starting a second backend from scratch.

**Deployment target**: `Working/StopLossPro_OfflineSale/lib/licensing.py` hardcodes a default `API_BASE = "https://stoplosspro.onrender.com/api/v1"`, with an in-code comment saying this points at "the real Render production URL... so the client actually works today." I could not independently verify this endpoint is live from this sandbox (a `WebFetch` attempt to `/health` was blocked by a robots.txt fetch timeout, which is inconclusive either way). **This needs the user to confirm**: is there an actual live Render service today, and is it meant to also host Sterling_Room, or does Sterling_Room need its own service?

## 9. Existing frontend

None, beyond the server-rendered admin HTML described above. No dashboard, no analytics UI, no customer portal.

## 10. Existing deployment

No Dockerfile, no docker-compose, no CI deployment step (the two GitHub Actions workflows found build the Windows/macOS *client* binaries, not the backend). `DEPLOYMENT.md` exists under `Working/backend/` (deploy + code-signing + key-rotation + incident runbook) but describes a manual/documented process, not an automated pipeline. No evidence of a queue, scheduler, or background-worker process running anywhere today.

## 11. Existing tests

Real and substantial, but scoped to StopLossPro Pro, not Sterling_Room: 108 backend tests (`Working/backend/tests/`, including a genuine PostgreSQL-concurrency suite) + 65 risk-engine golden-master checks (`Working/StopLossPro_OfflineSale/tests/`). Zero tests exist for `Historical/main_dispatcher` (the personal build) or for anything Sterling_Room would add.

## 12. Existing reusable components

- **FastAPI app skeleton + config pattern** (`Working/backend/app/{main,config,database}.py`) — production boot-guardrails, env-driven secrets, health/readiness endpoints. Directly reusable.
- **`audit_events` table + `services.audit()` helper** — exactly the append-only audit log master-prompt §31/§61 asks for; extend rather than reinvent.
- **Admin console pattern** (`admin.py`) — cookie-auth, RBAC-ready roles, HTML escaping helper, HTTP-bootstrap-with-signed-pending-payload for shell-less hosts. Directly reusable pattern for Sterling_Room's admin dashboard (§35–§37 of the master prompt).
- **`signal_dispatcher.py`'s Telegram Bot API client** (urllib-based POST to `sendMessage`, no extra dependency) — the seed of a fuller bot, not a bot itself.
- **Alembic wiring** already proven against real Postgres — new Sterling_Room tables can piggyback on the same migration chain if the same database is used, or a fresh chain if not (see open question in §14).

## 13. Missing infrastructure (everything Sterling_Room-specific)

Every item in master-prompt §7 is net-new: call/call_events/call_messages tables, Trade ID generator, call state machine + validation, `subscribers`/`plans`/`subscriptions`/`payments` tables and state machine, Telegram bot with `/start` menu and multi-channel routing (free/premium/results), payment provider integration (none chosen), webhook receiver with idempotency, delivery-retry queue, performance engine + R-multiple ledger, admin dashboard extensions, support-ticket system, notification system, referral tracking, background workers, monitoring/alerting, backups, CI for the backend, `docs/` compliance/legal docs specific to Sterling_Room (the existing legal docs are all StopLossPro-Pro-specific and explicitly marked non-final placeholders).

## 14. Integration options

Covered in detail in `STOPLOSSPRO_INTEGRATION.md`. Summary: the cleanest path is a **small, additive change** to `Historical/main_dispatcher/main.py`'s existing signal-send path (`_post_telegram_signal`) so it also (or instead) calls a new Sterling_Room `POST /calls` endpoint — nothing about the risk engine, MT5 execution, or calculator changes.

**Open question this raises**: should Sterling_Room's API live inside `Working/backend` (new routers/models alongside the existing licensing tables, same database, same deploy target) as a modular monolith — consistent with this project's own documented build-vs-buy philosophy (`PROJECT_STATUS.md` §8, "the surface is genuinely small... build") — or as a fully separate service? I'd default to **extending `Working/backend`** unless there's a reason (e.g. wanting Sterling_Room on a different host/billing account, or wanting it fully decoupled from the licensing product) to keep them apart. This is a real architectural decision, not a formality — flagging it for the user rather than assuming.

And regardless of which: **new work should branch from `phase0-clean-baseline`, not `main`**, given §1/§3 above.

## 15. Recommended architecture (see also §14)

```
StopLossPro Personal (Historical/main_dispatcher)
        │  _post_telegram_signal() — existing, unchanged behavior preserved
        ▼
NEW: POST /sterling/calls  (adapter — new endpoint, new module)
        │
        ▼
Sterling_Room call engine (new SQLAlchemy models + services, same
FastAPI app as Working/backend if that's the chosen target — see §14)
        │
        ├─► calls / call_events tables (Trade ID, state machine, audit)
        ├─► Telegram bot (new module) → Free / Premium / Results channels
        ├─► performance ledger (R-multiple)
        └─► admin console (extends the existing admin.py pattern)
```

Subscriptions/payments/access-control sit alongside the call engine, reusing the existing `audit_events` pattern and the "server is authoritative" discipline already established in this codebase.

## 16. Risks

- **No confirmed 24/7 hosting for Sterling_Room specifically.** This cloud sandbox is ephemeral and cannot run a persistent bot/webhook/worker process — Sterling_Room's backend must run somewhere real (Render, if that's genuinely where `Working/backend` already lives, or elsewhere). This is a hard blocker for anything beyond writing code (§17).
- **Zero payment provider chosen anywhere in the project.** Cannot build §13/§14/§30 of the master prompt without one.
- **No Telegram bot or channels confirmed to exist yet** for Sterling_Room specifically (the bot token/chat_id in Settings today, if any, belongs to the personal app's existing single-channel setup, not a Sterling_Room bot with free/premium/results channels).
- **Legal/compliance**: the existing legal docs in this repo are explicitly StopLossPro-Pro-specific placeholders, not Sterling_Room-ready, and per the master prompt's own §62 I should not assume "educational purposes only" makes a paid-call service compliant.
- **Contaminated `main` branch** (§1) — building from it would carry forward a known credential-exposed commit into new history.
- **No tests exist yet for anything Sterling_Room touches** — the discipline (108 tests, golden-master checks) exists elsewhere in this repo and should be matched, not skipped, for new code.

## 17. Blockers requiring a human decision before implementation proceeds

These are financially, security-, and legally consequential per the master prompt's own §86 escalation rule — flagging rather than guessing:

1. **Hosting/deployment target** for Sterling_Room's backend + always-on Telegram bot. Does the Render service referenced in the client code actually exist and run today, and is it the intended home for Sterling_Room too?
2. **Payment provider.** Which one (Stripe, PayPal, crypto-manual like the existing StopLossPro Pro "activation_note" pattern, something else)? This determines the entire payment abstraction and webhook design.
3. **Telegram surface.** Do the Free/Premium/Results channels and a Sterling_Room bot (with its own bot token) already exist, or do they need to be created? I cannot fabricate bot tokens or chat IDs.

## 18. Implementation plan (adapted from master-prompt §78)

| Phase | Work | Blocked? |
|---|---|---|
| 0 | This audit | Done |
| 1 | Schema design + Alembic migration for calls/call_events (extends `Working/backend` per §14, pending confirmation) | Can start now |
| 2 | StopLossPro adapter (`POST /calls` + minimal `_post_telegram_signal` hook in `main.py`) | Can start now |
| 3 | Call lifecycle engine (Trade ID, state machine, validation) | Can start now |
| 4 | Telegram bot + channel distribution | **Blocked on §17.3** |
| 5 | Subscriber management | Can start (schema/logic), full flow blocked on §17.3 |
| 6 | Payments | **Blocked on §17.2** |
| 7 | Subscription/access automation | Blocked on §17.2/§17.3 |
| 8 | Performance engine | Can start now |
| 9 | Admin dashboard | Can start now (extends existing admin.py pattern) |
| 10 | Security + monitoring | Ongoing, matches existing project discipline |
| 11 | Testing | Ongoing, alongside each phase — this project's own standard is tests-with-every-change, not after |
| 12–13 | Staging / production deployment | **Blocked on §17.1** |

## 19. First workstream to execute

Phases 1–3 (schema + adapter + call engine) require none of the three blocking decisions and touch nothing in `Working/StopLossPro_OfflineSale` (the commercial product, explicitly off-limits per master-prompt §3/§80). Recommend starting there while §17 is being decided, on a branch off `phase0-clean-baseline`.
