# Sterling_Room — Staging Environment

Purpose: a real deployment of this exact codebase, wired to real-enough
infrastructure to validate the full pipeline, that is structurally
incapable of reaching a real subscriber or a real Telegram channel. This
document is what Step 2 of the pre-production launch process asked for —
it does not itself provision anything (no hosting access from this
environment); it is the spec to hand to whoever provisions the host.

## 1. The one rule that matters

**Staging must never be able to send a message to a production Telegram
destination.** Two ways to guarantee that, in order of preference:

1. **Separate bot token.** Create a second bot via @BotFather
   (`@sterling_room_staging_bot` or similar) used *only* by staging.
   Even if every chat ID were somehow copied from production by mistake,
   a staging-only bot was never added to the real channels, so sends
   simply fail (403 "bot is not a member").
2. **If a second bot isn't available yet**, staging's
   `TELEGRAM_FREE_CHAT_ID` / `TELEGRAM_PREMIUM_CHAT_ID` /
   `TELEGRAM_RESULTS_CHAT_ID` must point at the existing Sterling_Room
   dev/test group (the one already in use throughout Phase 1-8
   development) and must **never** be set to the real production channel
   IDs once those exist. This is a manual discipline, not something the
   app enforces by itself — see the "what code can and can't enforce"
   note below.

Whichever option is used, `app/main.py`'s boot log
(`_startup_guard`) prints `env`, `telegram_configured`, and a masked tail
of each configured chat ID on every start — check that log immediately
after any staging deploy and confirm the values are what you expect
before sending a single test message.

### What code can and can't enforce here

`assert_production_ready()` (`app/config.py`) blocks a `STERLING_ENV=production`
process from booting with an unsafe configuration (SQLite, DEBUG on,
missing `ADAPTER_API_KEYS`/`ADMIN_SESSION_SECRET`, a bot token with no
webhook secret) — verified live in this pass (see the release audit). It
does **not** and structurally **cannot** know "chat ID `-1009876543210` is
the real production Premium channel" — that knowledge doesn't exist
anywhere in the codebase, by design (per the standing rule against
fabricating configuration). Isolation between staging and production
Telegram destinations is therefore a **secrets-management** control:
separate `.env` files, separate entries in the host's secret manager,
ideally a separate bot token. Document, don't assume.

## 2. Staging vs. production — what must differ

| | Staging | Production |
|---|---|---|
| `STERLING_ENV` | `staging` | `production` |
| `DATABASE_URL` | its own Postgres instance/database | its own Postgres instance/database — **never shared with staging** |
| `TELEGRAM_BOT_TOKEN` | staging bot (preferred) or unset | real production bot |
| `TELEGRAM_*_CHAT_ID` | the existing dev/test group, or unset | real FREE/PREMIUM/RESULTS channels |
| `ADAPTER_API_KEYS` | staging-only value | production-only value |
| `ADMIN_SESSION_SECRET` | staging-only value | production-only value |
| `ADMIN_BOOTSTRAP_TOKEN` | staging-only value | production-only value |
| `TELEGRAM_WEBHOOK_SECRET` | staging-only value | production-only value |

No value in this table should ever be copy-pasted from one environment
to the other. Each is independently generated
(`python -c "import secrets; print(secrets.token_urlsafe(32))"`).

## 3. Full required environment variable list

Every variable Sterling_Room reads, from `app/config.py` (`STERLING_`
prefix on all of them):

| Variable | Purpose | Required for staging to be *useful* |
|---|---|---|
| `ENV` | `development` / `staging` / `production` — gates `assert_production_ready()` | Yes — set `staging` |
| `DEBUG` | verbose logging, `/docs` enabled | Yes — `false` recommended even in staging, to mirror prod behavior |
| `DATABASE_URL` | Postgres connection string | Yes |
| `ADAPTER_API_KEYS` | bearer token(s) for `POST /calls` etc. | Yes |
| `TELEGRAM_BOT_TOKEN` | bot API token | Only if testing Telegram flows — see §1 |
| `TELEGRAM_FREE_CHAT_ID` / `TELEGRAM_PREMIUM_CHAT_ID` / `TELEGRAM_RESULTS_CHAT_ID` | destination channels | Only if testing Telegram flows — see §1 |
| `TELEGRAM_CHANNEL_LINK` | legacy/general channel link | Optional |
| `TELEGRAM_FREE_CHANNEL_LINK` | shown by the bot's FREE ACCESS button | If testing the bot |
| `TELEGRAM_SUPPORT_CONTACT` | shown by SUPPORT | Optional |
| `TELEGRAM_WEBHOOK_SECRET` | webhook path secret | Required if `TELEGRAM_BOT_TOKEN` is set |
| `ADMIN_BOOTSTRAP_TOKEN` | one-time first-admin creation | Yes, once |
| `ADMIN_SESSION_SECRET` | signs admin cookies | Yes |
| `PAYMENT_PROVIDER` | which provider `get_provider()` returns | Yes — `manual` (only implementation) |
| `API_PREFIX` | API route prefix | Optional, defaults to `/api/v1` |
| `CORS_ORIGINS` | comma-separated allowed origins | If a browser client calls the API directly |

## 4. Standing up staging

1. Provision a host + its own PostgreSQL database (not shared with dev,
   staging-only, never production's).
2. Set every "Yes" row in §3, using staging-only values throughout.
3. `alembic upgrade head`.
4. Start the app, confirm the boot log shows `env: staging` and the
   Telegram destination values you expect (§1).
5. Confirm `GET /health`, `GET /health/ready`, and
   `GET <API_PREFIX>/monitoring` (with a staging adapter key) all report
   healthy.
6. If testing Telegram flows, register the webhook against the staging
   bot token (see `DEPLOYMENT.md` §5) and confirm `/start` only ever
   reaches the dev/test group or the staging-only bot — never a real
   channel.
7. Bootstrap a staging admin account (`DEPLOYMENT.md` §7) and confirm
   `/admin/login` works end to end.
8. Run the Step 6 end-to-end scenario (call lifecycle + subscriber
   lifecycle) against staging before treating it as validated — see the
   release audit report for what "validated" means here and its current
   caveats (SQLite-based test suite vs. a real Postgres staging run).

## 5. What staging validation must confirm before Telegram migration

Per the explicit instruction not to migrate off the current test group
until staging validation passes: staging validation is complete when (a)
`alembic upgrade head` has been run against a real, empty Postgres
database and produced the identical schema the SQLite test suite
produces, (b) the full end-to-end scenario (§4 step 8) has been run
against that Postgres-backed staging deployment and passed, and (c) the
boot log has been checked to confirm no staging chat ID resolves to a
production channel. Only after all three should production Telegram
channel IDs be set anywhere.
