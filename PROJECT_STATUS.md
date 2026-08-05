# StopLossPro — Project Status Briefing
*Self-contained handover for an AI assistant or engineer with no prior context.*
*Accurate as of 2026-08-05 (updated after the RC production-validation pass).*

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

Two earlier products, **P1** and **P2**, are **permanently retired** (decision reaffirmed
2026-08-05, standing rule now). Do not reference, reuse, or spend effort on them — treat them
as dead/historical only. One item connected to them remains a live, unresolved issue: see §3.1.

---

## 2. Where the project stands

A full security overhaul was completed, followed by a 15-step production-validation ("RC gate")
pass. The product went from "licensing via a public GitHub Gist" to a real server-authoritative
licensing platform, and that platform has now been independently re-verified against real
PostgreSQL and real adversarial testing rather than left on the strength of unit tests alone.

**Six git commits on `phase0-clean-baseline`, each a rollback point:**

| Commit | Contents |
|---|---|
| `9e51f40` | PHASE 0 — credential containment + clean baseline |
| `a6aaaaa` | PHASE 1 — backend + database foundation |
| `8d22c29` | PHASES 2–15 — auth, licensing, sessions, MFA, admin, client cutover, hardening |
| `ce965af` | RC validation Steps 1–6,9,14,15 — key-domain separation, real-Postgres concurrency proof, admin adversarial tests + rate-limit fix, privacy fixes |
| `41391da` | Step 15 legal review brief |
| `08ba8cf` | Release Readiness Report |

Branch: `phase0-clean-baseline` (an **orphan** branch — deliberately has no parent, so the
old credential-contaminated history is unreachable from it). **`main` and `origin/main` are a
separate, still-contaminated branch** — see §3.1, this has not been cleaned up yet.

**Test status: 108 backend tests passing (was 44), 65 risk-engine checks passing, 0 failures.**
The jump from 44→108 is the RC validation pass: real PostgreSQL concurrency tests (25), admin
adversarial security tests (23), boot-guardrail tests (9), MFA/signing-key-separation tests (7).

**Release verdict as of this pass: CONTROLLED PILOT READY, conditioned on closing the legacy
credential incident first.** Technical readiness 78/100, sales readiness 40/100. Full detail in
`Working/StopLossPro_OfflineSale/docs/RELEASE_READINESS_REPORT_2026-08-05.md`.

---

## 3. CRITICAL — the security incident that reframed the project

A pre-commit secret scan uncovered problems far more serious than the licensing weaknesses
the work had originally targeted. **Re-verified independently on 2026-08-05 — do not assume
this is closed just because tokens were revoked.**

### 3.1 Three leaked GitHub tokens — revocation confirmed, cleanup NOT complete

| Token | Where it was | Exposure | Status as of 2026-08-05 |
|---|---|---|---|
| PAT-A | `net_verify.py`, P1/P2 source (compiled into shipped binaries), pushed git history | Public if repo was public; in customer hands via binaries | Revoked (user-confirmed). **Commit `d253b56` on `main`/`origin/main` still contains it** — independently re-verified via `git merge-base --is-ancestor` on this date. Not purged. |
| PAT-B | `deploy_clean.ps1`, and **XOR-obfuscated (key=11) inside a GitHub Pages site** | Served on the public web; trivially decodable | Revoked (user-confirmed). **The deployed page is still live and still serving the encoded token bytes** — independently re-verified on 2026-08-05 by fetching `https://wave-rock-investments.github.io/stoploss-site/p1_admin.html` and inspecting its live source: the `_TX` array is non-empty (40 numeric entries), not `[]`. Only the *local working-tree copy* of this file was ever cleaned; the deployed copy never was. |
| PAT-C | `push_to_github.bat`, `.git/config` remote URL | Local + history | Revoked (user-confirmed). Working tree cleaned. |

All three had **gist write scope** — meaning possession allowed arbitrary rewriting of the
licensing allowlist: free activation for anyone, or wiping it to disable every paying
customer simultaneously.

**Current status: the tokens themselves are revoked, so the encoded/leaked values are believed
unusable — but the exposure artifacts (the git commit, the live page) have not been removed.**
When asked directly on 2026-08-05 whether to take the live page down now, the user's answer was
to permanently deprioritize P1/P2 rather than act on it — this is a deliberate, informed choice,
not an oversight. Full detail: `Working/StopLossPro_OfflineSale/docs/CREDENTIAL_INCIDENT.md` §0.

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

**All of §3.2–3.4 has been removed from the codebase and re-confirmed absent** during the RC
privacy audit (§4 below) via live grep of the shipping client tree, not just historical record.

---

## 4. RC production-validation pass (2026-08-05) — what changed and what it found

A 15-step gate (production provisioning → adversarial validation → release candidate → pilot)
was run against the codebase above. Steps 1–6, 9, 14, 15 were fully executable and verified;
Steps 7, 8, 10–13 are genuinely blocked on things that don't exist in a sandboxed environment —
real Windows hardware, a real deployed backend, and a purchased code-signing certificate — and
are reported as pending, not faked.

**Three real defects were found and fixed** (not new features — fixes required to make the
checks honestly pass):

1. **MFA/signing key coupling.** TOTP secrets were encrypted with a key derived from the Ed25519
   signing private key — rotating one would have silently forced re-encrypting the other. Split
   into an independent `STOPLOSS_TOTP_ENCRYPTION_KEY_B64`, with a versioned decrypt path (`v2:`
   prefix) so already-encrypted secrets keep working during migration. 7 new tests prove the two
   domains now rotate independently.
2. **Admin login had zero rate limiting.** `/admin/login` — the highest-value credential in the
   system — had no brute-force protection at all on password or TOTP attempts, unlike customer
   login. Now uses the same limiter (`app/api.py:rate_limit`), keyed by IP and by email.
3. **Client leaked the real Windows hostname.** `device_name` was populated from
   `socket.gethostname()`, directly contradicting `DATA_INVENTORY.md`'s explicit "hostname NOT
   collected" promise (Windows hostnames often embed the owner's name). Replaced with a
   non-identifying label derived from the device's own Ed25519 public key
   (`Device-{sha256(pubkey)[:8]}`). `verify_release.py` now scans for `gethostname`/`getuser()`
   patterns so this can't silently regress.

A fourth, smaller gap was also closed: the deployment runbook ran uvicorn without
`--no-access-log`, creating an undocumented second copy of client IPs outside the one documented,
retention-scoped location (`audit_events`). Fixed in `DEPLOYMENT.md`.

**Real PostgreSQL concurrency was proven, not assumed.** No docker/root access existed in the
validation environment, so a real (not simulated) PostgreSQL server was run via the `pgserver`
pip package, which bundles an actual `postgres` binary and runs it without root. Against that
real server: `alembic upgrade head` executed for real, the partial unique index was confirmed via
direct catalog introspection (not just trusting the model definition), and the single-active-
session invariant was stress-tested across 17 total concurrent-login race rounds (2-way, 15×
4-way, 8-way) — exactly one active session, every round. Test file:
`Working/backend/tests/test_postgres_production.py`.

**Offline grace period fully analyzed, value left unchanged.** All 7 required scenarios (revoke-
while-offline, 1h/6h/12h/24h/72h offline, clock rollback, clock forward, state copy/restore) are
documented in `docs/OFFLINE_GRACE_ANALYSIS.md`. Recommendation: **24h**, down from the current
72h — the current value means a revoked-while-offline customer could retain full access for up to
3 days, which is a meaningful gap given revocation here is a manual admin action (fraud/chargeback
response), not a race against an automated attack. **This is a recommendation only — the value is
still 259200s (72h) in `app/config.py`, pending an explicit decision.**

**Legal brief delivered, not final legal text.** `docs/LEGAL_REVIEW_BRIEF.md` — a from-the-code
description of what the product actually does/collects/promises, plus 8 open questions for
counsel (jurisdiction, AML/KYC on crypto payments, refund policy, broker-specific risk-disclosure
requirements, data-retention gaps, liability caps, governing law, registration status). The three
existing legal docs (`TERMS_OF_SERVICE_v1.0.md`, `RISK_DISCLOSURE_v1.0.md`,
`PRIVACY_NOTICE_v1.0.md`) remain explicit placeholders — nothing here is legal-ready.

**Manual validation checklist delivered for what couldn't be run here.**
`docs/RC_MANUAL_VALIDATION_CHECKLIST.md` — exact, step-by-step procedures for Steps 7 (Windows
client E2E), 8 (DPAPI cross-machine), 12 (Authenticode signing), 13 (clean-install test),
including a prerequisite section for actually deploying the backend first (none of those steps
are meaningful against code that only exists on disk).

---

## 5. What was built (Phases 0–15, plus the RC hardening above)

### Backend — `Working/backend/` (FastAPI + SQLAlchemy + PostgreSQL)

Modular monolith, deliberately not microservices.

| Phase | Delivered |
|---|---|
| 2 | Argon2id passwords; failed-login lockout; per-IP + per-account rate limits; user enumeration blocked via identical errors and equalised timing |
| 3 | Server-authoritative licences; expiry judged on **server** time so a client clock cannot extend one |
| 4 | Device enrolment by Ed25519 public key; hardware fingerprint demoted to an advisory hint, never an authorization input |
| 5 | One active session per customer |
| 6 | RFC 6238 TOTP (Google Authenticator compatible); secret now encrypted with an **independent** key (RC fix, see §4); step-replay protection; 10 single-use Argon2id-hashed recovery codes |
| 7 | Device takeover: revoke-old + create-new in **one transaction**, gated on TOTP |
| 8 | Ed25519 signed authorization grants, 180 s TTL, `kid` field present for future key rotation |
| 9 | 90 s heartbeat; 72 h bounded offline grace (under review, see §4); clock-rollback defence |
| 10 | Server-rendered admin panel with mandatory admin TOTP; **now rate-limited** (RC fix) |
| 11 | Versioned consent — login blocked until all three documents accepted |

**Schema (9 tables, UUID primary keys throughout — no sequential IDs):**
`users`, `licences`, `devices`, `sessions`, `mfa_credentials`, `recovery_codes`,
`consent_records`, `audit_events`, `admin_users`

### The central guarantee — now proven on real PostgreSQL, not just SQLite

```sql
CREATE UNIQUE INDEX uq_sessions_one_active_per_user
  ON sessions (user_id) WHERE status = 'ACTIVE'
```

A second concurrent active session is **physically unrepresentable** — the losing transaction
fails on a uniqueness violation at the storage layer. `SELECT … FOR UPDATE` layers on top to turn
that hard integrity error into an orderly handover. As of the RC pass this has been verified
against a real PostgreSQL server under real concurrent load (§4), not inferred from SQLite alone.

### Key design choices worth knowing

- **Ed25519, not RSA** — 32-byte keys, fast client-side verification, no padding-scheme footguns.
- **Custom compact grant format, not JWT** — JWT's `alg` header is a known downgrade vector
  (`alg:none`, HS/RS confusion). Here the algorithm is fixed by the verifier and not negotiable
  by the token.
- **Signing key and TOTP-encryption key are independent secrets** (RC fix, §4) — compromising or
  rotating one must never force rotating the other.
- **Windows DPAPI** for client-side session/key storage — sealed to the Windows account, so
  copying the state file to another machine is useless (documented residual risk: DPAPI is not
  unbreakable against an attacker with full access to the *original* machine — see
  `docs/OFFLINE_GRACE_ANALYSIS.md` §1 Scenario 7).
- **REVOKED ≠ UNREACHABLE** — a network outage starts a bounded grace window; a server "no"
  locks immediately. These are never conflated. Full scenario-by-scenario writeup in
  `docs/OFFLINE_GRACE_ANALYSIS.md`.
- **Clock-rollback defence** — a monotonic high-water mark; moving the system clock backwards
  ends the grace window rather than extending it (known tradeoff: this can false-positive on a
  laptop resuming from sleep with a momentarily stale clock — documented, not hidden).

### Client — `Working/StopLossPro_OfflineSale/`

**Phase 12 cutover.** `lib/activation.py` was replaced by a thin shim over `lib/licensing.py`.
The old import surface is preserved so nothing else in the app changed. **RC fix:** no longer
sends the real Windows hostname (see §4 item 3).

**Deleted:** gist allowlist, gist session file, Cloudflare Worker writes, ntfy APPROVED,
ntfy REVOKE, ntfy telemetry, GPS collection, ipinfo lookups, MAC/hostname/username collection,
plaintext `~/.slcalc_cache`, hardware-fingerprint identity.

The client now holds the **public verification key only**. It can verify a grant; it can never
mint one.

**Phase 13/15:** build excludes tests, `--noupx` (UPX packing trips AV heuristics), exe version
metadata, plus `verify_release.py` — a pre-ship gate that fails the build on credentials,
resurrected legacy paths, missing verification key, enabled file logging, hostname/username
collection (RC addition), or risk-engine regression.

---

## 6. Testing

**108 backend tests** (was 44), including genuinely adversarial ones:

- Threaded **simultaneous-login race**, now proven on **real PostgreSQL** (not just SQLite) —
  17 total rounds up to 8-way contention, exactly one active session every time
- **Grant forgery** using an attacker-generated keypair → rejected
- **TOTP replay** within the same 30 s window → rejected, for both customer and admin logins
- Revocation, lockout, expired licence, suspended account, revoked device
- **23 admin-panel adversarial tests** (new, RC pass): unauthenticated access blocked, customer
  credentials inert against admin routes, forged/expired admin sessions blocked, MFA mandatory
  with replay protection, password AND TOTP brute force rate-limited (previously unlimited — see
  §4), licence mutation/MFA reset/force-logout all independently confirmed audited, admin cookie
  confirmed `HttpOnly` + `SameSite=Strict` + `Secure` by inspecting the actual response header
- **9 boot-guardrail tests** (new, RC pass): the FastAPI app is actually booted end-to-end (not
  just the config function in isolation) and proven to refuse starting under every unsafe
  production condition individually and combined, and to start cleanly under a valid one
- Audit log scanned for leaked secrets
- Schema scanned for banned column names (GPS, balance, positions, MAC…) — fails the build if
  privacy-invasive collection is ever reintroduced

**65 risk-engine golden-master checks** pin exact SL/TP values, contract sizes, buy/sell levels,
auto-lot, order-type recommendation, input validation and determinism. The risk engine was
**never modified** — a headless Kivy stub was used so `lib/` is tested byte-for-byte unchanged,
and the RC pass re-confirmed all 65 still pass unchanged.

### Real bugs the tests caught (across both passes)

1. **Naive/aware datetime comparison.** Licence-expiry checks would crash on SQLite but work on
   PostgreSQL — invisible in dev, fatal in one deployment and not another. Fixed via
   `services.as_aware()`.
2. **Self-scanning false positive.** The release verifier and one test flagged their own
   detector pattern strings as secrets. Both now self-exclude.
3. **Admin login had no rate limiting** (RC pass) — see §4.
4. **Client hostname leak via `device_name`** (RC pass) — see §4.

One *non-bug* each pass: TOTP replay protection correctly rejecting a genuinely reused code (the
test helper was unrealistic, so the test was fixed, not the security); and a SQLite in-memory
connection-pooling gotcha in the new admin test fixtures (each session was getting its own
private empty database) that looked like a missing-table bug but was a test-harness bug, fixed
with `StaticPool`.

---

## 7. What remains before shipping

### Blocking

`verify_release.py` currently **fails by design** with:
`SERVER_PUBLIC_KEY_B64 is EMPTY`

This is correct, expected behavior — it cannot pass until a real backend exists to pull the key
from. Sequence to unblock (full detail in
`docs/RC_MANUAL_VALIDATION_CHECKLIST.md`, prerequisite section):

1. Provision managed PostgreSQL + host
2. `python -m app.keygen` → store the **private** signing key AND the **independent** TOTP
   encryption key in the host's secret manager only (never in a file, never in this repo)
3. `python -m alembic upgrade head`
4. `python -m app.bootstrap_admin` (interactive; MFA mandatory)
5. Fetch `GET /api/v1/pubkey`, paste the **public** key into `lib/licensing.py`
6. Set `API_BASE` in `lib/licensing.py`
7. `python verify_release.py` until it exits 0
8. `build.bat`
9. Authenticode sign (needs a purchased certificate — not yet started as of this writing)

### User action still outstanding (unchanged from the incident; user has deferred, not forgotten)

- **Contaminated `main` branch** — commit `d253b56` still contains PAT-A and was pushed.
  Independently re-confirmed still true on 2026-08-05. Recommendation: delete and recreate the
  remote repo rather than history-rewrite (see `CREDENTIAL_INCIDENT.md` §5 for exact commands).
- **`p1_admin.html`** on the public `stoploss-site` GitHub Pages repo still serves the
  XOR-obfuscated PAT-B — **confirmed still live and still populated on 2026-08-05**, not just
  "presumed." Revoking the token already defused the value itself; deleting the file/deployment
  stops it being served at all. User has explicitly deprioritized this for now.

### Known limitations (documented, not hidden)

- **Concurrency is now proven on real PostgreSQL** (RC pass, §4) — this used to be a limitation
  ("SQLite only"), it no longer is, though the test environment was a single-host instance, not a
  production-scale managed deployment under real network conditions.
- **No human has ever run the actual GUI against the new backend.** Everything verified this pass
  was backend/API-level (real HTTP requests, real Postgres, real crypto) — genuinely strong, but
  it is not the same as Step 7's real-hardware click-through, which has not happened yet.
- **No RC binary exists.** Nothing has been built or signed.
- **Rate limiting is in-process.** Correct for a single worker; multi-instance needs Redis.
- **Legal text is placeholder**, though a complete from-the-code brief now exists for counsel
  (`docs/LEGAL_REVIEW_BRIEF.md`) — wording still needs a lawyer, explicitly not marked ready.
- **Code-signing certificate not yet purchased.** Issuance takes days to weeks. EV gives
  immediate SmartScreen trust — worth it for an exe sold to strangers. User declined to start
  this "for now" as of 2026-08-05.
- **DPAPI cross-machine test not run.** Needs two distinct Windows environments; user currently
  has access to only one machine.

### Deliberately deferred until there is traction

Multi-role RBAC (one admin today, but `admin_users.role` already exists so adding it is
additive), enterprise audit/SIEM infrastructure, APM, formal multi-region disaster recovery,
complex entitlement tiers, CI security automation, fraud analytics, customer self-serve portal.

---

## 8. Build-vs-buy decision (already made)

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

## 9. Key files

```
Working/backend/
  app/config.py          env-driven settings + production guard rails (now checks TOTP key too)
  app/models.py          9 tables
  app/security.py        Argon2id, TOTP (independent key), Fernet, Ed25519 — all delegated to libraries
  app/services.py        business logic (all phases)
  app/api.py             client-facing HTTP API; rate_limit() reused by admin.py
  app/admin.py           admin panel — now rate-limited
  app/keygen.py          Ed25519 keypair + independent TOTP key generator (stdout only, never writes a file)
  app/bootstrap_admin.py first-admin creation, MFA mandatory
  run_pg_validation.py   orchestrates a real disposable PostgreSQL server for test_postgres_production.py
  tests/                 108 tests total:
    test_phase1_foundation.py           schema/FK/invariant sanity (17)
    test_phase14_security.py            adversarial concurrency/MFA/revocation (27)
    test_phase16_key_separation.py      signing-key/TOTP-key independence (7, new)
    test_phase5_boot_guardrails.py      real ASGI boot refusal under every unsafe config (9, new)
    test_phase6_admin_security.py       admin adversarial suite (23, new)
    test_postgres_production.py         real-Postgres concurrency + schema proof (25, new — skips
                                         cleanly if STOPLOSS_PG_TEST_URL is unset)
  DEPLOYMENT.md           deploy + code-signing + key rotation (both keys) + incident runbook
  DATA_INVENTORY.md       every field, why it exists, how long it is kept
  legal/                  versioned consent placeholders

Working/StopLossPro_OfflineSale/
  lib/calc.py             RISK ENGINE — core IP, do not modify
  lib/licensing.py        client licensing provider
  lib/activation.py       shim over licensing.py; no longer sends the real hostname
  verify_release.py       pre-ship gate; now also scans for gethostname()/getuser()
  tests/                  65-check risk-engine golden master
  docs/SECURITY_MVP_ARCHITECTURE.md      1,632-line architecture + audit
  docs/CREDENTIAL_INCIDENT.md            full incident record, §0 = 2026-08-05 re-verification
  docs/OFFLINE_GRACE_ANALYSIS.md         all 7 scenarios, 6h/12h/24h/72h tradeoff, 24h recommended (new)
  docs/RC_MANUAL_VALIDATION_CHECKLIST.md exact procedures for Steps 7/8/12/13 (new)
  docs/LEGAL_REVIEW_BRIEF.md             from-the-code brief for counsel (new)
  docs/RELEASE_READINESS_REPORT_2026-08-05.md   full PASS/FAIL grid, scores, recommendation (new)
```

---

## 10. Standing rules for anyone working on this

1. **Never modify the risk engine** (`calc.py`, `mixin_calculator.py`) without explicit
   approval. Run the 65-check baseline after any change that could touch it.
2. **Never hardcode a credential** in source, config, build scripts or client artefacts.
3. **Never invent cryptography.** Use `cryptography`, `argon2-cffi`, `pyotp`.
   Obfuscation is not encryption — XOR with a fixed key was already broken here once.
4. **The client is untrusted.** The server is authoritative for account status, licence status,
   expiry, sessions, device authorization and revocation.
5. **No private signing key, TOTP-encryption key, DB credential, admin credential or master
   secret** in the desktop application, ever. The two crypto secrets (signing, TOTP) must stay
   independent — do not re-derive one from the other again.
6. **Run the pre-commit secret gate** before every checkpoint, and `verify_release.py` before
   every build.
7. **Do not restore any part of the old Gist/Worker/ntfy system as a fallback** — restoring it
   restores every bypass with it.
8. **P1 and P2 are permanently retired.** Do not reference, reuse, or prioritize fixing them.
   The one still-open item connected to them (`p1_admin.html` still live, §3.1) is a deliberate,
   user-acknowledged deferral, not something to chase without being asked.
9. **Do not report "LEGACY INCIDENT: CLOSED"** in any future status or readiness report until
   both the git-history cleanup and the `p1_admin.html` takedown are actually done and
   re-verified — both are still open as of this document.
