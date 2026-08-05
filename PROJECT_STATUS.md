# StopLossPro — Project Status Briefing
*Self-contained handover for an AI assistant or engineer with no prior context.*
*Accurate as of 2026-08-05.*

---

## 1. What the product is

**StopLossPro** — a Windows desktop risk-management / position-sizing calculator for
retail forex and CFD traders. Built by a solo developer.

- **Stack:** Python 3.13, Kivy/KivyMD GUI, packaged with PyInstaller (`--onefile --windowed`)
- **Integration:** MetaTrader 5 via the official `MetaTrader5` Python package (direct Windows
  API bridge, not an EA or ZeroMQ). Reads live prices/account state and can place orders the
  user explicitly confirms.
- **Core IP:** the risk engine (`lib/calc.py`, `lib/mixin_calculator.py`) — a fixed unified
  risk model: `SL = ATR × multiplier`, then `TP1/TP2/TP3 = SL × 2/3/4` (1:2, 1:3, 1:4 R:R),
  with position sizing and expected-value calculation.
- **Business model:** offline/direct sales (Reddit, Telegram, direct outreach). Cash and
  crypto, manually verified. Admin manually activates each customer. No public sales site.
- **Stage:** pre-revenue. Effectively zero real paying customers to date.
- **Scale target:** must comfortably support 2,000+ customers later without a rewrite.

Two earlier products, **P1** and **P2**, have been **permanently retired** (decision
2026-08-05). Ignore them except where noted in the security incident below.

---

## 2. Where the project stands

A full security overhaul was completed in one working session. The product went from
"licensing via a public GitHub Gist" to a real server-authoritative licensing platform.

**Three git commits, each a rollback point:**

| Commit | Contents |
|---|---|
| `9e51f40` | PHASE 0 — credential containment + clean baseline |
| `a6aaaaa` | PHASE 1 — backend + database foundation |
| `8d22c29` | PHASES 2–15 — auth, licensing, sessions, MFA, admin, client cutover, hardening |

Branch: `phase0-clean-baseline` (an **orphan** branch — deliberately has no parent, so the
old credential-contaminated history is unreachable from it).

**Test status: 44 backend tests passing, 65 risk-engine checks passing, 0 failures.**

---

## 3. CRITICAL — the security incident that reframed the project

A pre-commit secret scan uncovered problems far more serious than the licensing weaknesses
the work had originally targeted.

### 3.1 Three leaked GitHub tokens

| Token | Where it was | Exposure |
|---|---|---|
| PAT-A | `net_verify.py`, P1/P2 source (compiled into shipped binaries), pushed git history | Public if repo was public; in customer hands via binaries |
| PAT-B | `deploy_clean.ps1`, and **XOR-obfuscated (key=11) inside a GitHub Pages site** | Served on the public web; trivially decodable |
| PAT-C | `push_to_github.bat`, `.git/config` remote URL | Local + history |

All three had **gist write scope** — meaning possession allowed arbitrary rewriting of the
licensing allowlist: free activation for anyone, or wiping it to disable every paying
customer simultaneously.

**Status: the user has deleted all tokens and all gists.** Exposure closed.

### 3.2 Live customer data on a public URL

The shipped client posted to `ntfy.sh` public topics every 5 minutes:
MT5 broker name, **account login number**, balance, equity, currency, **every open position**
(symbol, direction, volume, floating P/L, entry price, current price), plus **GPS coordinates**.

Install notifications additionally published Windows username, hostname, MAC address,
CPU/RAM/screen, OS build, public IP, ISP and city/region/country.

ntfy topics are public by default. Anyone who opened those URLs saw a live feed of every
customer's trading account and physical location.

### 3.3 Three independent remote bypasses

1. **Cloudflare Worker** authenticated writes on a `User-Agent` prefix — its own comment
   admitted this was "noise reduction, not a security gate." One unauthenticated POST could
   grant a licence or disable all customers.
2. **ntfy `APPROVED`** — publishing a message titled `APPROVED` to a public topic activated
   the product. A one-line curl = free licence.
3. **ntfy `REVOKE`** — publishing `REVOKE` terminated a customer's app, repeatedly.

### 3.4 GPS collected with no consent gate

`_require_location()` — the function that told users location was "required by our service
agreement" — was **dead code, never called**. But `_collect_system_info(gps_ready=True)` *was*
called during registration and did collect GPS. So precise location was gathered with no
prompt and no consent record, while the only consent-adjacent text asserted an agreement that
did not exist.

**All of the above has been removed from the codebase.** Details in
`Working/StopLossPro_OfflineSale/docs/CREDENTIAL_INCIDENT.md`.

---

## 4. What was built (Phases 0–15)

### Backend — `Working/backend/` (FastAPI + SQLAlchemy + PostgreSQL)

Modular monolith, deliberately not microservices.

| Phase | Delivered |
|---|---|
| 2 | Argon2id passwords; failed-login lockout; per-IP + per-account rate limits; user enumeration blocked via identical errors and equalised timing |
| 3 | Server-authoritative licences; expiry judged on **server** time so a client clock cannot extend one |
| 4 | Device enrolment by Ed25519 public key; hardware fingerprint demoted to an advisory hint, never an authorization input |
| 5 | One active session per customer |
| 6 | RFC 6238 TOTP (Google Authenticator compatible); secret encrypted at rest; step-replay protection; 10 single-use Argon2id-hashed recovery codes |
| 7 | Device takeover: revoke-old + create-new in **one transaction**, gated on TOTP |
| 8 | Ed25519 signed authorization grants, 180 s TTL |
| 9 | 90 s heartbeat; 72 h bounded offline grace; clock-rollback defence |
| 10 | Server-rendered admin panel with mandatory admin TOTP |
| 11 | Versioned consent — login blocked until all three documents accepted |

**Schema (9 tables, UUID primary keys throughout — no sequential IDs):**
`users`, `licences`, `devices`, `sessions`, `mfa_credentials`, `recovery_codes`,
`consent_records`, `audit_events`, `admin_users`

### The central guarantee

```sql
CREATE UNIQUE INDEX uq_sessions_one_active_per_user
  ON sessions (user_id) WHERE status = 'ACTIVE'
```

A second concurrent active session is **physically unrepresentable** — the losing transaction
fails on a uniqueness violation at the storage layer. Application-level checking alone cannot
achieve this, because another transaction can commit between the check and the insert.
`SELECT … FOR UPDATE` is layered on top to turn a hard integrity error into an orderly handover.

### Key design choices worth knowing

- **Ed25519, not RSA** — 32-byte keys, fast client-side verification, no padding-scheme footguns.
- **Custom compact grant format, not JWT** — JWT's `alg` header is a known downgrade vector
  (`alg:none`, HS/RS confusion). Here the algorithm is fixed by the verifier and not negotiable
  by the token.
- **Windows DPAPI** for client-side session/key storage — sealed to the Windows account, so
  copying the state file to another machine is useless.
- **REVOKED ≠ UNREACHABLE** — a network outage starts a bounded grace window; a server "no"
  locks immediately. These are never conflated.
- **Clock-rollback defence** — a monotonic high-water mark; moving the system clock backwards
  ends the grace window rather than extending it.

### Client — `Working/StopLossPro_OfflineSale/`

**Phase 12 cutover.** `lib/activation.py` (1,179 lines) was replaced by a thin shim over a new
`lib/licensing.py`. The old import surface is preserved so nothing else in the app changed.

**Deleted:** gist allowlist, gist session file, Cloudflare Worker writes, ntfy APPROVED,
ntfy REVOKE, ntfy telemetry, GPS collection, ipinfo lookups, MAC/hostname/username collection,
plaintext `~/.slcalc_cache`, hardware-fingerprint identity.

The client now holds the **public verification key only**. It can verify a grant; it can never
mint one.

**Phase 13/15:** build excludes tests, `--noupx` (UPX packing trips AV heuristics), exe version
metadata, plus `verify_release.py` — a pre-ship gate that fails the build on credentials,
resurrected legacy paths, missing verification key, enabled file logging, or risk-engine
regression.

---

## 5. Testing

**44 backend tests**, including genuinely adversarial ones:

- Threaded **simultaneous-login race** (two clients at a barrier) → exactly one session.
  Deliberately run on SQLite where the row lock is a no-op, proving the index alone holds.
- **Grant forgery** using an attacker-generated keypair → rejected
- **TOTP replay** within the same 30 s window → rejected
- Revocation, lockout, expired licence, suspended account, revoked device
- Audit log scanned for leaked secrets
- Schema scanned for banned column names (GPS, balance, positions, MAC…) — fails the build if
  privacy-invasive collection is ever reintroduced

**65 risk-engine golden-master checks** pin exact SL/TP values, contract sizes, buy/sell levels,
auto-lot, order-type recommendation, input validation and determinism. The risk engine was
**never modified** — a headless Kivy stub was used so `lib/` is tested byte-for-byte unchanged.

### Two real bugs the tests caught

1. **Naive/aware datetime comparison.** Licence-expiry checks would crash on SQLite but work on
   PostgreSQL — invisible in dev, fatal in one deployment and not another. Fixed via
   `services.as_aware()`.
2. **Self-scanning false positive.** The release verifier and one test flagged their own
   detector pattern strings as secrets. Both now self-exclude.

One *non-bug*: TOTP replay protection correctly rejected a code reused inside its window. The
test helper was unrealistic, so the **test** was fixed, not the security.

---

## 6. What remains before shipping

### Blocking

`verify_release.py` currently **fails by design** with:
`SERVER_PUBLIC_KEY_B64 is EMPTY`

Sequence to unblock:

1. Provision managed PostgreSQL + host
2. `python -m app.keygen` → store the **private** key in the host's secret manager only
3. `python -m alembic upgrade head`
4. `python -m app.bootstrap_admin` (interactive; MFA mandatory)
5. Fetch `GET /api/v1/pubkey`, paste the **public** key into `lib/licensing.py`
6. Set `API_BASE` in `lib/licensing.py`
7. `python verify_release.py` until it exits 0
8. `build.bat`
9. Authenticode sign

### User action still outstanding

- **Contaminated `main` branch** — commit `d253b56` still contains PAT-A and was pushed.
  Recommendation: **delete and recreate the remote repo** rather than history-rewrite. The repo
  holds only two commits of retired P1/P2 code; `Working/` was never committed, so nothing of
  value is lost, and deletion is definitive where rewriting leaves objects recoverable via
  cached views and forks.
- **`p1_admin.html`** on the public `stoploss-site` GitHub Pages repo still serves the
  XOR-obfuscated PAT-B. Revoking the token already defused it; deleting the file stops it being
  served.

### Known limitations (documented, not hidden)

- **Concurrency proven on SQLite only.** The index holds either way, but `SELECT … FOR UPDATE`
  is a no-op there. Re-run the race test against PostgreSQL before launch. The app refuses to
  boot in production on SQLite, so this cannot silently reach customers.
- **Rate limiting is in-process.** Correct for a single worker; multi-instance needs Redis.
- **Legal text is placeholder.** Framework complete and versioned; wording needs a lawyer.
  Marked `[PLACEHOLDER — LAWYER REVIEW REQUIRED]` throughout.
- **Client GUI not yet run end-to-end.** Everything compiles and imports cleanly, but the Tk
  sign-in dialog needs a real run against a live server.
- **Code-signing certificate not yet purchased.** Issuance takes days to weeks. EV gives
  immediate SmartScreen trust — worth it for an exe sold to strangers.

### Deliberately deferred until there is traction

Multi-role RBAC (one admin today, but `admin_users.role` already exists so adding it is
additive), enterprise audit/SIEM infrastructure, APM, formal multi-region disaster recovery,
complex entitlement tiers, CI security automation, fraud analytics, customer self-serve portal.

---

## 7. Build-vs-buy decision (already made)

Evaluated a custom backend against **Keygen**, **Cryptlex** and **LicenseSpring**.

**Verdict: BUILD.** Reasoning specific to this product, not general principle:

1. The distinguishing requirement is **identity and session control**, not a licensing feature.
   None of the three vendors offers end-user TOTP MFA, so buying means building the identity
   layer anyway *and* paying $100–300/month for the smaller half of the problem.
2. The surface is genuinely small — 9 tables, ~10 endpoints, one admin page.
3. Cost favours building 3–10× at every scale checkpoint considered (50 / 500 / 2,000 customers).
4. No lock-in on identity data.

**Hedge in place:** licensing sits behind a narrow provider interface, so swapping to a hosted
vendor later is a provider implementation rather than a rewrite.

**Revisit BUY if:** enterprise/team seats, floating licences, resellers, or a customer
self-serve portal become requirements — Cryptlex ships those today.

---

## 8. Key files

```
Working/backend/
  app/config.py          env-driven settings + production guard rails
  app/models.py          9 tables
  app/security.py        Argon2id, TOTP, Fernet, Ed25519 — all delegated to libraries
  app/services.py        business logic (all phases)
  app/api.py             client-facing HTTP API
  app/admin.py           admin panel
  app/keygen.py          Ed25519 keypair generator (stdout only, never writes a file)
  app/bootstrap_admin.py first-admin creation, MFA mandatory
  tests/                 44 tests
  DEPLOYMENT.md          deploy + code-signing + rotation + incident runbook
  DATA_INVENTORY.md      every field, why it exists, how long it is kept
  legal/                 versioned consent placeholders

Working/StopLossPro_OfflineSale/
  lib/calc.py            RISK ENGINE — core IP, do not modify
  lib/licensing.py       new client licensing provider
  lib/activation.py      shim over licensing.py (old surface preserved)
  verify_release.py      pre-ship gate
  tests/                 65-check risk-engine golden master
  docs/SECURITY_MVP_ARCHITECTURE.md   1,632-line architecture + audit
  docs/CREDENTIAL_INCIDENT.md         full incident record
```

---

## 9. Standing rules for anyone working on this

1. **Never modify the risk engine** (`calc.py`, `mixin_calculator.py`) without explicit
   approval. Run the 65-check baseline after any change that could touch it.
2. **Never hardcode a credential** in source, config, build scripts or client artefacts.
3. **Never invent cryptography.** Use `cryptography`, `argon2-cffi`, `pyotp`.
   Obfuscation is not encryption — XOR with a fixed key was already broken here once.
4. **The client is untrusted.** The server is authoritative for account status, licence status,
   expiry, sessions, device authorization and revocation.
5. **No private signing key, DB credential, admin credential or master secret** in the desktop
   application, ever.
6. **Run the pre-commit secret gate** before every checkpoint, and `verify_release.py` before
   every build.
7. **Do not restore any part of the old Gist/Worker/ntfy system as a fallback** — restoring it
   restores every bypass with it.
