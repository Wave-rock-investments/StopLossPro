# StopLossPro — Security MVP Architecture & Implementation Plan

**Document status:** Design and analysis only. No application code was modified in producing this document.
**Date produced:** 2026-08-05
**Scope:** Replace the current Gist-based licensing/session mechanism with a minimal, server-authoritative backend that satisfies one-customer / controlled-licence / multiple-devices / one-active-session / MFA device switching / remote revocation / signed authorization / short offline grace / protected client / simple admin control — cleanly enough to scale toward 2,000+ customers, without building enterprise infrastructure prematurely.
**Out of scope:** the risk engine. It is not touched. Security wraps around the product.

> **Legal notice.** Every clause of user-facing legal text referenced in this document is marked
> `[PLACEHOLDER — LAWYER REVIEW REQUIRED]`. Nothing in this document is legal advice and no wording
> here has been reviewed by a lawyer. The author of this document is not a lawyer and not a financial adviser.

---

## Executive summary

The current licensing system is not a weak licensing system — it is an **open write endpoint**. Three separate,
independently sufficient bypasses were confirmed by reading the shipped source, and each takes minutes rather
than skill to exploit. A fourth issue publishes live customer financial and location data to a public URL.
Separately, the architecture has a hard throughput ceiling at roughly *one concurrent customer* because of
GitHub's unauthenticated API rate limit, so it cannot reach even 50 customers, let alone 2,000, regardless of
security.

The verdict is therefore **BUILD NOW**, but a deliberately small build: a single FastAPI + PostgreSQL modular
monolith on one managed host, ~7 tables, Ed25519-signed short-lived authorization, and a server-rendered admin
page. Estimated recurring cost is roughly **$10–30/month flat at any customer count in the 50–2,000 range**,
versus $100–$299+/month for the closest hosted vendor tier — and, more decisively, none of the three vendors
evaluated implements the *specific* control this product needs (one active session per **customer** with
**MFA-gated device switching**) as a first-class primitive.

Phase 1 alone (kill the open write endpoint) is a same-day change and is the single highest-value action in
this entire document.

---

## STEP 1 — Repository state (verified 2026-08-05)

### 1.1 What was inspected

Repository root: `C:\Users\trish\OneDrive\Desktop\StoplossApk-mt5`
Product source of truth: `Working\StopLossPro_OfflineSale`

| Path | Lines | Notes |
|---|---:|---|
| `Product Sell.py` | 523 | entry point; log lockdown present at top |
| `lib/activation.py` | 1,179 | licensing, fingerprint, session, payment, telemetry, activation UI |
| `lib/constants.py` | 153 | all endpoint URLs and licence file paths |
| `lib/calc.py` | 175 | risk engine — untouched |
| `lib/mixin_calculator.py` | 480 | risk engine — untouched |
| `lib/mixin_trading.py` | 745 | order construction |
| `lib/mixin_orders.py` | 502 | order management |
| `lib/mixin_settings.py` | 220 | formula text hidden at lines 127–128 |
| `lib/mixin_lifecycle.py` | 351 | calls the updater |
| `lib/mixin_mt5.py` | 293 | MT5 UI bridge |
| `lib/mt5_api.py` | 590 | MetaTrader5 bridge |
| `lib/updater.py` | 312 | auto-updater, `UPDATE_URL = ""` |
| `lib/layout.kv` | — | 78 KB UI |
| **Total Python** | **6,786** | |
| `build.bat` / `StopLossPro.spec` / `version_info.txt` | — | PyInstaller onefile, `--version-file` present |
| `../cf_worker/gist_proxy.js` | — | Cloudflare Worker write proxy |

`docs/` did not exist before this run and was created to hold this file. No database, no server-side code, and
no authenticated admin panel exist anywhere in the repository.

### 1.2 Confirmed against the brief

Everything the brief asserted was verified true:

- The entire backend is public Gist `8a8b52dc14c0ecca38121df01557ec99`, shared with retired P1/P2
  (`lib/constants.py:133`, `:142`; `lib/activation.py:568`, `:742`; `Product Sell.py:354`).
- `approved_ids.txt` is a plaintext machine-ID allowlist and is the *entire* licensing system. No user accounts,
  no passwords, no MFA, no per-user concept.
- `active_sessions.txt` enforces "one active session" keyed by machine ID, written through the Cloudflare Worker.
- The `SLP_{machine_id}` session-key namespacing fix from 2026-08-04 is present (`lib/activation.py:566`).
- `_get_machine_id()` (`lib/activation.py:53`) = `sha256(uuid.getnode() : hostname : Windows user SID)[:16]`.
  Hardware fingerprint as primary trust.
- The log lockdown is written to source (`Product Sell.py:15–19`, `KIVY_NO_FILELOG` / `KIVY_NO_CONSOLELOG`
  set before any kivy import, gated by `STOPLOSSPRO_DEBUG=1`).
- The SL-multiplier / TP-ratio formula string is no longer reconstructed in the Settings screen
  (`lib/mixin_settings.py:127–129`).
- `version_info.txt` exists and `build.bat` passes `--version-file`; `numpy` is explicitly included in both
  `requirements.txt` and the build, so the MT5 import fix is in place.

### 1.3 Confirmed: the exe is stale

`dist/StopLossPro.exe` is dated **2026-08-04 23:44**. `Product Sell.py` is dated **23:53** and `lib/layout.kv`
**23:58**. The shipped binary therefore predates the log-lockdown and formula-hiding source fixes. **The rebuild
remains pending and was deliberately not performed by this task.**

### 1.4 NEW findings — material, not in the brief

These were found by reading the current code and are the reason the verdict is BUILD NOW rather than BUILD LATER.

---

#### F-1 — CRITICAL: the Cloudflare Worker is an unauthenticated write oracle

`Working/cf_worker/gist_proxy.js` gates inbound requests on exactly two things:

1. `request.method === 'POST'`
2. `request.headers.get('User-Agent').startsWith('StopLossCalc/')`

Its own comment concedes the second is "noise reduction, not a security gate." There is no shared secret, no
signature, no nonce, no rate limit, no IP restriction. The allow-list on filenames restricts *which* files can be
written but not *who* may write them, and `approved_ids.txt` is on the allow-list.

**Consequence.** Anyone who knows the Worker URL — a plaintext string constant in the distributed exe
(`lib/constants.py:140`) — can issue one HTTP POST that replaces the entire contents of `approved_ids.txt`.
That yields, at the attacker's choice:

- **free lifetime activation** for any machine ID, bypassing payment entirely; or
- **instant denial of service against 100% of paying customers simultaneously**, by writing an empty file —
  every running client's `_bg_check_revoked` would confirm "not approved" within 5 minutes and call `self.stop()`.

The same primitive also allows writing `used_txns.txt` (invalidating legitimate payments) and
`active_sessions.txt` (evicting any customer at will).

**Severity: critical. Exploitable remotely, unauthenticated, by a single request, against all customers at once.**
This is the most urgent item in this document and is addressed in Phase 1 as a same-day fix.

---

#### F-2 — CRITICAL: public ntfy.sh topics are a second, independent activation bypass

Both the revoke listener and the activation-screen poller subscribe to `https://ntfy.sh/slcalc_{mid[:10].lower()}`
(`lib/activation.py:493`, `:1136`). ntfy.sh topics are **public by default** — anyone may publish to any topic
name without authentication.

The topic name is a deterministic, unsalted function of the machine ID, and the machine ID is displayed to the
customer in the activation dialog and included in every notification the developer receives.

- `_start_revoke_listener()` terminates the app on any message whose **body** contains `REVOKE`.
  → Anyone with a customer's machine ID can remotely kill that customer's application, repeatedly.
- `_instant_approval_poll()` treats any message whose **title** contains `APPROVED` as a valid activation: it
  writes `_LIC_CACHE` and closes the activation blocker (`lib/activation.py:1153–1170`).
  → **A one-line curl to a public URL activates the product for free.** No Worker, no payment, no reverse
  engineering.

**Severity: critical.**

---

#### F-3 — HIGH: live customer financial and location data is published to a public URL

`_HB_URL = https://ntfy.sh/stoploss_hb_h7zltndg` (`lib/constants.py:132`) is a public topic. `_send_heartbeat()`
posts to it every 5 minutes per client, carrying:

- machine ID, MT5 **broker server name and account login number**
- account **balance, equity, currency**
- **every open position**: symbol, direction, volume, floating P/L, entry price, current price
- **coordinates** — GPS if available, otherwise IP geolocation — plus a source tag

`_NOTIFY_URL = https://ntfy.sh/stoploss_dev_h7zltndg` similarly publishes, on every new install, the customer's
Windows username, hostname, MAC address, machine make/model, CPU, RAM, screen resolution, OS build, public IP,
ISP, city/region/country and coordinates.

Anyone who opens those two URLs in a browser gets a live, continuously updating feed of every customer's trading
account and physical whereabouts. For a product sold to strangers on Reddit and Telegram this is a severe
customer-safety issue before it is a compliance issue, and it is also a competitive-intelligence leak about the
business itself (exact customer count, activity, geography).

**Severity: high. Must be removed, not merely restricted.**

---

#### F-4 — HIGH: GPS is collected without a working consent gate, and the consent text that exists is false

`_require_location()` (`lib/activation.py:264`) is a hard gate that refuses to run the app unless Windows
Location Services is on, and tells the user this is "required by our service agreement for identity verification
and compliance."

**It is never called.** A repository-wide search finds exactly two references: the definition itself, and a
comment on line 651. Meanwhile `_register_if_new()` calls `_collect_system_info(gps_ready=True)`
(`lib/activation.py:651`), which *does* run the PowerShell `GeoCoordinateWatcher` query and ships the resulting
latitude/longitude to a public topic.

So the current shipping behaviour is the worst of both arrangements: **precise GPS location is collected with no
prompt, no gate and no consent record**, while the only consent-adjacent text in the codebase is dead code
asserting a "service agreement" that does not exist in the repository.

This directly contradicts requirement 14 ("no covert surveillance … NO secret GPS"). It also creates concrete
regulatory exposure — precise geolocation is treated as sensitive personal data under most modern regimes, and
the product is marketed from an Indian domain to an international audience.
`[PLACEHOLDER — LAWYER REVIEW REQUIRED]`

**Severity: high. Recommendation: delete GPS collection outright.** It serves no licensing purpose that
coarse country-level IP data does not serve better, and it is the largest privacy liability in the product.

---

#### F-5 — HIGH: the local licence cache is authoritative when the network is unavailable

`_check_approved()` (`lib/activation.py:61`) reads `~/.slcalc_cache`, a plaintext user-writable file of the form
`{unix_ts}:{full approved list}`. If the cached timestamp is within `_CACHE_TTL` (1800 s) it returns immediately
with no network call. On network failure it explicitly falls back to cached content — the comment says
"stay approved if network is temporarily down."

A customer who writes `9999999999:<THEIR_MACHINE_ID>` into that file and blocks `api.github.com` at the firewall
is permanently activated with no network, no payment and no reverse engineering. This is precisely the
`isLicensed = true`-in-a-local-file anti-pattern the core principle forbids.

**Severity: high.**

---

#### F-6 — HIGH: payment verification runs on the client, and the client writes its own approval

The P2 path (`lib/activation.py:742+`) hardcodes the receiving wallet, the USDT contract address and the
minimum amount (`AMOUNT_MIN = 250_000_000`, i.e. 250 USDT) into the exe, queries TronGrid from the client,
sets `verified = True` in client memory, and then instructs the Worker to append its own machine ID to
`approved_ids.txt`.

Every one of those steps is under the attacker's control. The on-chain check is decoration; the client is both
judge and beneficiary.

Two secondary observations:
- If TronGrid is unreachable the code falls back to the pending queue — a correct failure mode, and the only
  part of this flow worth keeping conceptually.
- The code enforces a **250 USDT** minimum. Prior wallet-monitoring runs were checking for inflows of **≥240
  USDT**. Reconcile that deliberately rather than letting it drift.

**Severity: high.**

---

#### F-7 — HIGH: "one active session" is not race-safe, and is not per customer

Two distinct defects:

**(a) Not race-safe.** `_start_session_heartbeat()` (`lib/activation.py:527`) performs a read-modify-write of the
*entire* `active_sessions.txt` with no compare-and-swap, no ETag/`If-Match`, and no transaction. Two clients that
read the same Gist revision and both PATCH produce last-writer-wins. The loser does not discover this for up to
`_SESSION_HB_INTERVAL` = 60 s, plus a deliberate 5 s re-verify delay. **During that window both machines are
fully functional.** Requirement 4 ("two computers logging in simultaneously must never both get a valid session")
is not met.

**(b) Not per customer.** The key is `SLP_{machine_id}`. A customer with two computers has two different machine
IDs and therefore two different keys, and can run both simultaneously forever. What is implemented is "one
*process* per *machine*", a different and much weaker property than "one active session per *customer*". There is
no customer entity in the system to key on.

Additionally the stated tie-break — "a fresh launch always takes precedence" — makes the lock a **takeover**
primitive rather than an **exclusion** primitive. Whoever launches last wins, unconditionally, with no
authentication. That is the opposite of the intended control.

**Severity: high (correctness). This is the requirement the whole redesign exists to satisfy.**

---

#### F-8 — HIGH: hard scaling ceiling at roughly one concurrent customer

GitHub's unauthenticated REST API limit is **60 requests per hour per source IP**. Per running client the current
code issues:

| Source | Reads/hour |
|---|---:|
| Session heartbeat (`_read_sessions`, every 60 s) | 60 |
| Revoke/approval backstop (`_bg_check_revoked`, every 300 s) | 12 |
| Startup approval check + occasional re-verify reads | ~2–4 |
| **Total per client** | **~74–76** |

A **single** client already exceeds the unauthenticated budget within the first hour of continuous use, after
which GitHub returns HTTP 403. The code's failure modes on read error are "return `{}`" (session) and "swallow
the exception" (revoke check) — so in practice session enforcement silently stops working after roughly
45 minutes of uptime and stays broken.

Two customers behind the same NAT, one office, one VPN exit node, or one customer running the app all day all
produce the same outcome. **This architecture cannot support 50 customers, let alone 2,000, for reasons that have
nothing to do with security.**

**Severity: high. This alone justifies replacement.**

---

#### F-9 — MEDIUM: the update channel is unsigned and currently disabled

`lib/updater.py:37` has `UPDATE_URL = ""`, so auto-update is inert; `lib/mixin_lifecycle.py:34` imports and calls
it regardless. When enabled as documented, the manifest is fetched from a Gist and the SHA-256 is verified only
*if the manifest supplies one* — manifest and hash come from the same source. Combined with F-1, anyone who can
write the Gist can serve an arbitrary payload with a matching hash and achieve **remote code execution on every
customer machine**.

An update channel must be signed with a key the client verifies and the server never sends. Until that exists,
leaving `UPDATE_URL` empty is the correct posture.

**Severity: medium now, critical the moment it is switched on without signing.**

---

#### F-10 — MEDIUM: unsigned + UPX-compressed PyInstaller onefile is worst case for SmartScreen and AV

`StopLossPro.spec` sets `upx=True`. UPX-packed, unsigned, single-file PyInstaller binaries are among the
highest-false-positive shapes in consumer AV heuristics, and an unsigned exe with no Authenticode reputation
triggers a full-screen SmartScreen "Windows protected your PC" block on first run for every new customer.

For a product sold to strangers who paid in cash or crypto and are already nervous, that is a direct conversion
and support cost. `version_info.txt` improves the properties dialog but does nothing for SmartScreen — only a
real Authenticode signature does.

**Severity: medium (commercial), and cheap to fix. This is why code signing is pulled early in the roadmap.**

---

#### F-11 — LOW: dead constants and dead endpoints

`_LINK_URL` (`lib/constants.py:136`, pointing at `stoplosspro.in/.netlify/functions/link`) and `_SESSIONS_URL`
(`:142`) are both imported into `activation.py` and never used — session reads go through `api.github.com`
directly. `stoplosspro.in` has been DNS-down for an extended period. Delete both so nobody reintroduces a
dependency on a domain that does not resolve.

---

### 1.5 Threat model in one line

The client is fully untrusted, **and so is the current server**, because the current server accepts writes from
anyone. Fixing the client without fixing the write path accomplishes nothing.

---

## STEP 2 — Security MVP architecture

### 2.1 Shape

```
┌──────────────────────────────────────────────────────────────┐
│  WINDOWS DESKTOP CLIENT  (PyInstaller onefile, untrusted)    │
│                                                              │
│  UI (Kivy/KivyMD)                                            │
│  RISK ENGINE  calc.py / mixin_calculator.py   ← UNCHANGED    │
│  MT5 BRIDGE   mt5_api.py                      ← UNCHANGED    │
│                                                              │
│  lib/licensing/   (NEW — replaces lib/activation.py)         │
│    client.py      HTTPS calls, retry, backoff                │
│    auth_token.py  Ed25519 PUBLIC key verify (public only)    │
│    device.py      device keypair, DPAPI-protected            │
│    storage.py     DPAPI blob read/write                      │
│    gate.py        feature gate: is protected UI enabled?     │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS 1.2+ only, cert-pinned CA
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  BACKEND — one FastAPI process (modular monolith)            │
│    /auth      login, password, refresh, logout               │
│    /mfa       TOTP enroll / verify / recovery                │
│    /devices   enroll, list, revoke                           │
│    /session   create, heartbeat, switch-device, end          │
│    /licence   status (server-authoritative)                  │
│    /admin     server-rendered HTML, separate auth + MFA      │
│                                                              │
│  Ed25519 PRIVATE signing key  ← server only, never shipped   │
│  Argon2id password hashing                                   │
│  Rate limiting + lockout                                     │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
              ┌───────────────────────────┐
              │  PostgreSQL (managed)     │
              │  7 tables — see STEP 5    │
              │  daily automated backup   │
              └───────────────────────────┘
```

### 2.2 What runs where — explicit split

**Client (assume fully compromised):**

- All risk/position-sizing calculation. Local, offline, unchanged. This is the product.
- MT5 connection and order placement. Local.
- UI, history, settings. Local.
- Device private key generation and storage (DPAPI, per-user, non-exportable in practice).
- **Verification only** of the Ed25519 authorization token, using the embedded public key.
- Enforcement of the feature gate: when authorization is absent or expired, the protected surface is disabled.

**Server (sole authority):**

- Whether an account exists, its password, and whether it is locked.
- Whether a licence is active, suspended, revoked, or expired — and its expiry date.
- Whether a device is enrolled and authorized.
- Whether a session may be created, and which single session is currently active.
- TOTP secret custody and verification; recovery-code custody.
- Issuing signed authorization. **The private key exists only here.**
- Consent records and audit events.

**Never in the client:** private signing key, database credentials, admin credentials, master secrets, wallet
verification authority, any `is_licensed` boolean treated as authoritative.

### 2.3 Recommended stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | The developer is already fluent. A stack you can debug at 2 a.m. beats a theoretically better one you cannot. |
| Framework | **FastAPI** | Async, Pydantic request/response validation for free, auto OpenAPI docs (useful when writing the client), tiny surface. |
| ORM/migrations | **SQLAlchemy 2.x + Alembic** | Versioned schema migrations matter as soon as there is one paying customer whose row you cannot lose. |
| Database | **PostgreSQL 15+ (managed)** | The session design depends on `SELECT … FOR UPDATE` and **partial unique indexes**. Both are first-class in Postgres. SQLite cannot do concurrent row locking safely; MySQL's partial-index story is worse. This is a technical requirement, not a preference. |
| Password hashing | **Argon2id** via `argon2-cffi` | Memory-hard, current OWASP first choice. |
| Signing | **Ed25519** via `cryptography` (PyNaCl acceptable) | Small (64-byte sig, 32-byte key), fast, no padding-mode footguns. Already agreed. |
| TOTP | **`pyotp`** + `qrcode` | RFC 6238, Google Authenticator compatible. Never hand-roll OTP. |
| Admin UI | **Server-rendered Jinja2 templates inside the same FastAPI app** | No separate SPA, no CORS, no second deployment, no JS build. One admin, ten screens. |
| Hosting | **One small VPS or managed container** (Hetzner / Fly.io / Railway / DigitalOcean App Platform) + managed Postgres | ~$10–30/month all-in at this scale. |
| TLS | Managed certificate (Caddy/Traefik auto-TLS or platform-provided) | TLS 1.2+ only. HSTS. |
| Reverse proxy | Caddy | Auto-TLS with near-zero config; one less thing to get wrong. |

**Why a modular monolith and not services.** One developer, one deployment, one log stream, one database
transaction boundary. The one-active-session guarantee in STEP 6 depends on a *single* transactional store;
splitting session management into its own service would introduce exactly the distributed-consensus problem the
design is trying to avoid. The module boundaries (`auth/`, `licensing/`, `sessions/`, `devices/`, `mfa/`,
`admin/`) are enforced at the package level so the pieces *could* be split later, but there is no reason to
until traffic makes it necessary — which, at 2,000 customers heartbeating every 90 seconds, is roughly
**22 requests/second**. A single FastAPI worker handles that with enormous headroom.

### 2.4 Load sizing sanity check

At 2,000 customers, worst case all online simultaneously, 90-second heartbeat:

- 2,000 / 90 ≈ **22 req/s** sustained for heartbeats
- Logins, device switches, admin actions: negligible by comparison
- Each heartbeat is one indexed primary-key-ish lookup plus one short write

A single 2-vCPU instance with connection pooling is over-provisioned for this. There is no need for a queue, a
cache layer, a CDN, or horizontal scaling anywhere in the 50–2,000 range. Adding them now would be the
overengineering the brief explicitly warns against.

---

## STEP 3 — Build vs buy

### 3.1 Method and honesty note

Pricing below was taken from each vendor's **own** website on 2026-08-05. Aggregator sites were deliberately
not used. Where a figure could not be confirmed from the vendor's own site it is marked
**REQUIRES VERIFICATION** rather than guessed.

- **Keygen** publishes tier prices behind a JavaScript volume slider. The slider was rendered and read: at
  10,000 ALUs it shows **Std 2 — $299/mo** (monthly billing; the page states >16% saving on yearly). The Dev tier
  is **free up to 100 active licensed users and 10 releases**. The **Std 1** price — the tier that would actually
  cover 50–2,000 customers — **could not be read at other slider positions and is REQUIRES VERIFICATION.**
  Confirmed add-ons: Whitelabel API **$995/yr per domain**; Premium Support SLA **$995/mo** (Ent only).
  Keygen bills on **ALUs = licences with any API activity in the last 90 days**, charges a flat fee, and takes no
  revenue percentage. Self-hosting: **Keygen CE free**, Keygen EE a flat-rate paid licence
  (**EE price REQUIRES VERIFICATION**).
- **Cryptlex** publishes prices openly: **Starter $100/mo** (1,000 active activations, 100k API req/month,
  3 products, 3 team members), **Growth $300/mo** (5,000 activations, 1M API req/month), **Business $600/mo**
  (10,000 activations, 5M API req/month, audit logs, custom domains), Enterprise custom. Annual billing saves
  one month. Activations are counted **per licence created**, not per month.
- **LicenseSpring** publishes **no pricing at all** on its pricing page — the page is a contact-sales funnel.
  All LicenseSpring costs are **REQUIRES VERIFICATION**. Public statements indicate usage-based pricing tied to
  API call volume, billed monthly in advance; the actual numbers are not obtainable without contacting sales.

### 3.2 Requirement-by-requirement fit

Assessments below are about *our* requirements, not feature-list breadth.

| Our requirement | Custom build | Keygen | Cryptlex | LicenseSpring |
|---|---|---|---|---|
| **One active session per CUSTOMER** (not per device, not per key) | Exact. Partial unique index + row lock. | Approximate — floating licences cap *concurrent activations*; expressible as max_concurrent=1 but the semantics are seat-based, and the "who wins the race" behaviour is the vendor's, not ours. | Approximate — hosted floating licensing (Growth tier and above, i.e. **$300/mo minimum**). | Floating licensing exists; race semantics unverifiable without access. |
| **Multiple legitimate computers over time, no permanent hardware lock** | Exact. Devices are rows; enrol/revoke freely. | Good — device activation/deactivation is first-class. | Good — activations can be deactivated and reused. | Good, per docs. |
| **MFA-gated device switching** (the distinctive control) | Exact. This is our own flow. | **Not a native primitive.** Keygen has no TOTP MFA for *end users*; we would build the MFA layer ourselves anyway and then call Keygen for the licence part. | **Not a native primitive.** Same conclusion. | **Not a native primitive** as far as public docs show. REQUIRES VERIFICATION. |
| **Remote revocation** | Exact, immediate. | Yes. | Yes. | Yes. |
| **Heartbeat / periodic validation** | Exact, tunable. | Yes (validation + heartbeat/ping). | Yes. | Yes. |
| **Bounded offline grace with clock-rollback protection** | Exact — we define the rules. | Offline licences supported; the specific rollback protection we want is our own logic regardless. | Offline activations supported; same caveat. | Supported; same caveat. |
| **Manual cash/crypto sales, admin-activates-after-payment** | Exact — this is just an admin button. | Fine (create licence via API/dashboard). | Fine. | Fine. |
| **Custom admin control** (our own suspend/extend/force-logout/reset-MFA screen) | Exact. | Vendor dashboard, plus API if we want our own. | Vendor dashboard. | Vendor dashboard. |
| **Integration with Kivy/PyInstaller Python** | Native — it is our own Python. | HTTP API; Python is straightforward. | C SDK + Python bindings; PyInstaller bundling of a native DLL is an extra build complication. | SDKs incl. Python; same DLL-bundling caveat. |
| **Source/IP protection** | Neutral — vendor choice doesn't change it. | Neutral. | Cryptlex ships native code, marginally better than pure Python for the *licence check* specifically. | Neutral. |
| **Vendor lock-in** | None. | Moderate — data is exportable via API. | Moderate. | Moderate–high (opaque pricing = weak negotiating position at renewal). |
| **Who carries security responsibility** | Us, entirely. | Shared — vendor is SOC 2 (per their site). | Shared — vendor states ISO 27001 / GDPR. | Shared. |
| **Migration difficulty later** | N/A. | Moderate. | Moderate. | Moderate. |

### 3.3 Recurring cost at our three checkpoints

| Customers | Custom build | Keygen | Cryptlex | LicenseSpring |
|---|---|---|---|---|
| ~50 | **~$10–30/mo** (VPS + managed Postgres + domain) | Free Dev tier caps at 100 ALUs — **usable for the first 50**, then Std 1 (**REQUIRES VERIFICATION**) | **$100/mo** (Starter) — but MFA device switching needs floating licensing → realistically **$300/mo** (Growth) | REQUIRES VERIFICATION |
| ~500 | **~$10–30/mo** (unchanged) | Std tier, **REQUIRES VERIFICATION** | **$300/mo** (Growth) | REQUIRES VERIFICATION |
| ~2,000 | **~$20–40/mo** (maybe one size up) | Std tier, **REQUIRES VERIFICATION**; $299/mo is confirmed only at 10,000 ALUs | **$300/mo** (Growth, 5,000 activations) | REQUIRES VERIFICATION |

The custom build's cost curve is essentially **flat** across this entire range because 22 req/s is not a load.
The vendor curve is not flat, and at 2,000 customers a $300/mo licensing bill against a $250-per-seat product is
real margin. Note also that Cryptlex counts activations **per licence created, not per month** — abandoned trials
and churned customers consume the quota permanently unless manually cleaned up.

### 3.4 Verdict: **BUILD**

Grounded in this specific application and business situation, not in general principle:

1. **The distinctive requirement is not a licensing feature.** "One active session per customer, and switching
   devices requires a TOTP second factor" is an *identity and session* control. None of the three vendors offers
   end-user TOTP MFA as a primitive. Buying a licensing vendor means building the identity/MFA/session layer
   anyway and then paying $100–$300/month for the smaller half of the problem.
2. **The requirement set is genuinely small.** 7 tables, ~8 endpoints, one admin page. This is a two-to-three-week
   part-time build for someone already fluent in Python, not a platform project. Keygen's own build-vs-buy
   argument is about maintenance cost — a fair argument in general, and weaker here because the surface is this
   narrow and the security-critical parts (Argon2id, Ed25519, TOTP) are all delegated to established libraries.
3. **Cost is decisively in favour** at every checkpoint we care about, and the gap widens with growth.
4. **No lock-in on the identity data**, which is the asset that matters if the product ever gains a web
   dashboard, a subscription model, or a second product.
5. **The offline-grace, clock-rollback and heartbeat semantics are ours regardless.** Every vendor supports
   "offline licences"; none of them implements our specific grace policy, so that code gets written either way.

**Honest counter-argument, stated fairly.** Buying is the better call if any of the following becomes true:
the developer's time is worth more than ~$300/month of freed-up hours; a customer or auditor demands SOC 2 /
ISO 27001 attestation the solo build cannot provide; or the business needs floating/seat-based licensing, resellers,
or a customer self-serve portal — all of which are large builds and all of which Cryptlex ships today. If the
product pivots to enterprise or team seats, revisit this decision. **For the current single-seat, direct-sale,
manually-activated model, build.**

**Recommended hedge:** keep the licensing module behind a narrow internal interface (`licensing/provider.py` with
`check_licence()`, `create_session()`, `heartbeat()`, `revoke()`), so a future swap to Keygen or Cryptlex is a
provider implementation rather than a rewrite. This costs about an hour now.

---

## STEP 4 — Migration map

Every existing licensing/security component, classified.

| Component | Verdict | Reason |
|---|---|---|
| Gist `approved_ids.txt` | **REPLACE** | Plaintext allowlist with no owner concept; superseded by `licences` table. Server-authoritative status replaces file membership. |
| Gist `active_sessions.txt` | **REPLACE** | Read-modify-write of a text file cannot be made race-safe; superseded by `sessions` table + partial unique index. |
| Gist `pending_txns.txt` / `used_txns.txt` | **REPLACE** | Becomes admin-side payment records; the client must never write payment state. |
| Cloudflare Worker (`gist_proxy.js`) | **REMOVE** (after Phase 1 hardening, deleted at Phase 4 cutover) | F-1: unauthenticated write oracle. It exists only to let clients write server state — an architecture the new design eliminates entirely. Interim: lock it down same-day (see Phase 1); permanent: delete. |
| Public Gist itself | **REMOVE** at cutover | Also delete the GitHub PAT held in the Worker's environment. Note it is shared with retired P1/P2 — confirm neither is still distributed before deleting. |
| `_get_machine_id()` | **MODIFY** | Keep as a *risk signal* only (attach to `devices.hardware_hint`). It must stop being the identity. Identity becomes the enrolled device keypair. |
| `_check_approved()` / `_is_activated()` | **REPLACE** | F-5: local-cache-authoritative. Replaced by signed-authorization verification with bounded grace. |
| `~/.slcalc_cache` (`_LIC_CACHE`) | **REPLACE** | Plaintext, user-writable. Replaced by a DPAPI-protected blob holding a signed token that the client can verify but not forge. |
| `~/.slcalc_reg` (`_REG_FILE`) | **REMOVE** | Install marker for the telemetry flow being deleted. |
| `~/.slcalc_gps` (`_GPS_CACHE`) | **REMOVE** | Supports GPS collection which is being deleted (F-4). |
| `_bg_check_revoked()` | **REPLACE** | Correct instinct (periodic revalidation, cautious about false positives), wrong mechanism. Becomes the heartbeat call, which returns authoritative status. Keep the "confirm before killing the customer's session" discipline — it is good judgement and should survive into the new code. |
| `_start_session_heartbeat()` | **REPLACE** | F-7: not race-safe, not per customer. Becomes `POST /session/heartbeat`. |
| `_start_revoke_listener()` (ntfy) | **REMOVE** | F-2: anyone can publish a REVOKE. Revocation arrives via the heartbeat response instead — slower by up to one interval, but authenticated. |
| `_instant_approval_poll()` (ntfy) | **REMOVE** | F-2: anyone can publish an APPROVED. Replaced by polling `GET /licence/status` on the activation screen. |
| `_send_heartbeat()` → public ntfy | **REMOVE** | F-3: publishes broker login, balance, open positions, coordinates to a public URL. Nothing in it is required for licensing. |
| `_send_registration_notification()` → public ntfy | **REMOVE** | F-3. Admin notification, if wanted, goes over an authenticated channel from the *server*, never from the client to a public topic. |
| `_collect_system_info()` | **MODIFY** | Cut to the minimum: OS version, app version, and a coarse hardware hint. Drop MAC, username, hostname, screen, ISP, city, coordinates. |
| `_gps_check()` / `_require_location()` | **REMOVE** | F-4: covert collection, dead gate, false consent claim. No licensing purpose. |
| P2 TronGrid client-side payment verification | **REPLACE** | F-6: client is judge and beneficiary. Verification moves server-side (or stays fully manual, which is fine at this volume). |
| Wallet address / contract / amount constants in client | **REMOVE** from client | Should not be in a distributed binary at all. |
| `_LINK_URL`, `_SESSIONS_URL` constants | **REMOVE** | F-11: dead, and `stoplosspro.in` does not resolve. |
| `lib/activation.py` **as a whole** | **REPLACE** | 1,179 lines mixing licensing, fingerprinting, session, telemetry, GPS, crypto-payment verification and Tk UI. Replaced by `lib/licensing/` (client-side, thin) + backend. Do not incrementally patch it — the mixing of concerns is itself a defect. |
| **Risk engine** — `calc.py`, `mixin_calculator.py` | **KEEP — DO NOT TOUCH** | This is the product and the IP. Security wraps around it. |
| `mt5_api.py`, `mixin_mt5.py`, `mixin_trading.py`, `mixin_orders.py` | **KEEP** | Unrelated to licensing. |
| `lib/updater.py` | **MODIFY** | Keep the state machine and UX (they are good). Add Ed25519 signature verification of the manifest **and** the payload, using a separate update-signing key. Keep `UPDATE_URL = ""` until signing exists (F-9). |
| `apply_update.bat` | **KEEP** | Atomic-apply-after-exit is the right pattern. |
| `build.bat` / `StopLossPro.spec` / `version_info.txt` | **MODIFY** | Add Authenticode signing step. Turn **UPX off** (`upx=False`) — it buys a little size and costs AV reputation (F-10). Keep `--version-file`. Ensure no `.env`, key, or credential is ever added to `datas`. |
| Settings-screen formula hiding (`mixin_settings.py:127–128`) | **KEEP** | Correct and already done. Cheap, effective IP protection. |
| Kivy log lockdown (`Product Sell.py:15–19`) | **KEEP** | Correct and already done. **Ships only after the pending rebuild.** |
| `encrypt_logs.py` / `decrypt_log.py` / `logs.key` | **KEEP** (developer tooling) | Never ship in the customer package. Verify `logs.key` is not in the repo or the build. |

---

## STEP 5 — Database design

Seven tables. Postgres. Minimal on purpose — every column below earns its place.

### 5.1 `users`

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK, default `gen_random_uuid()` |
| `email` | `citext` | **UNIQUE NOT NULL** |
| `password_hash` | `text` | NOT NULL — Argon2id encoded string |
| `status` | `text` | NOT NULL, CHECK IN (`active`,`suspended`,`locked`) |
| `failed_login_count` | `int` | NOT NULL DEFAULT 0 |
| `locked_until` | `timestamptz` | NULL |
| `password_changed_at` | `timestamptz` | NOT NULL DEFAULT now() — used to invalidate old tokens |
| `created_at` / `updated_at` | `timestamptz` | NOT NULL DEFAULT now() |

Indexes: `UNIQUE(email)`.
Note: `password_changed_at` is the cheap global "log out everywhere" lever — any token issued before it is rejected.

### 5.2 `licences`

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK |
| `user_id` | `uuid` | **FK → users(id) ON DELETE RESTRICT**, NOT NULL |
| `status` | `text` | NOT NULL, CHECK IN (`active`,`suspended`,`revoked`,`expired`) |
| `plan` | `text` | NOT NULL DEFAULT `'standard'` — placeholder for future tiers, deliberately not a tier system yet |
| `max_devices` | `int` | NOT NULL DEFAULT 3 |
| `expires_at` | `timestamptz` | NULL = perpetual |
| `activated_at` | `timestamptz` | NULL until admin activates |
| `payment_ref` | `text` | NULL — free-text: TX hash, "cash 2026-08-05", etc. Deliberately unstructured at this stage. |
| `notes` | `text` | NULL — admin notes |
| `created_at` / `updated_at` | `timestamptz` | NOT NULL DEFAULT now() |

Indexes: `INDEX(user_id)`, `INDEX(status)`, `INDEX(expires_at) WHERE status='active'`.
**One licence per user for MVP**, enforced by `UNIQUE(user_id)` — drop that constraint later if multi-licence is ever needed. Modelling it as a separate table now costs nothing and avoids a painful migration later.

### 5.3 `devices`

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK |
| `user_id` | `uuid` | **FK → users(id) ON DELETE CASCADE**, NOT NULL |
| `public_key` | `bytea` | NOT NULL — device Ed25519 public key, 32 bytes |
| `name` | `text` | NOT NULL — e.g. "DESKTOP-4KQ21" (display only, never trusted) |
| `hardware_hint` | `text` | NULL — the old machine ID, kept **only** as a risk signal |
| `os_version` | `text` | NULL |
| `app_version` | `text` | NULL |
| `status` | `text` | NOT NULL, CHECK IN (`active`,`revoked`) |
| `enrolled_at` | `timestamptz` | NOT NULL DEFAULT now() |
| `last_seen_at` | `timestamptz` | NULL |

Indexes: `UNIQUE(public_key)`, `INDEX(user_id) WHERE status='active'`.
Enrolment is capped by `licences.max_devices` counted over `status='active'`.

### 5.4 `sessions`

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK |
| `user_id` | `uuid` | **FK → users(id) ON DELETE CASCADE**, NOT NULL |
| `device_id` | `uuid` | **FK → devices(id) ON DELETE CASCADE**, NOT NULL |
| `status` | `text` | NOT NULL, CHECK IN (`active`,`ended`,`superseded`,`expired`) |
| `created_at` | `timestamptz` | NOT NULL DEFAULT now() |
| `last_heartbeat_at` | `timestamptz` | NOT NULL DEFAULT now() |
| `expires_at` | `timestamptz` | NOT NULL — hard ceiling regardless of heartbeats |
| `ended_reason` | `text` | NULL — `logout`,`switch`,`admin_force`,`stale`,`revoked` |
| `ip_hash` | `text` | NULL — salted hash, not the raw IP |

**The constraint that makes the whole design work:**

```sql
CREATE UNIQUE INDEX one_active_session_per_user
  ON sessions (user_id)
  WHERE status = 'active';
```

Indexes: the partial unique index above, plus `INDEX(last_heartbeat_at) WHERE status='active'` for the stale sweeper.

### 5.5 `mfa_credentials`

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK |
| `user_id` | `uuid` | **FK → users(id) ON DELETE CASCADE**, **UNIQUE** NOT NULL |
| `totp_secret_enc` | `bytea` | NOT NULL — encrypted at rest with a server-side key from the environment, **not** stored plaintext |
| `status` | `text` | NOT NULL, CHECK IN (`pending`,`active`,`disabled`) |
| `confirmed_at` | `timestamptz` | NULL — set only after first successful verification |
| `last_used_step` | `bigint` | NULL — **replay guard**: the last accepted TOTP time-step |
| `created_at` | `timestamptz` | NOT NULL DEFAULT now() |

`last_used_step` is what stops an attacker who shoulder-surfs a 6-digit code from reusing it within the same 30-second window.

### 5.6 `recovery_codes`

*(Counted as part of the MFA table group; kept separate because codes are consumed individually.)*

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK |
| `user_id` | `uuid` | **FK → users(id) ON DELETE CASCADE**, NOT NULL |
| `code_hash` | `text` | NOT NULL — **Argon2id hash**, never the plaintext code |
| `used_at` | `timestamptz` | NULL |

Indexes: `INDEX(user_id) WHERE used_at IS NULL`.

### 5.7 `consent_records`

| Column | Type | Constraints |
|---|---|---|
| `id` | `uuid` | PK |
| `user_id` | `uuid` | **FK → users(id) ON DELETE CASCADE**, NOT NULL |
| `document` | `text` | NOT NULL — `tos`,`risk_disclosure`,`privacy` |
| `version` | `text` | NOT NULL — e.g. `2026-08-01` |
| `accepted_at` | `timestamptz` | NOT NULL DEFAULT now() |
| `ip_hash` | `text` | NULL |

Indexes: `UNIQUE(user_id, document, version)`.
The app blocks on first launch until all three current versions are accepted; a version bump re-prompts.

### 5.8 `audit_events`

| Column | Type | Constraints |
|---|---|---|
| `id` | `bigserial` | PK |
| `occurred_at` | `timestamptz` | NOT NULL DEFAULT now() |
| `actor_type` | `text` | NOT NULL — `user`,`admin`,`system` |
| `actor_id` | `uuid` | NULL |
| `event` | `text` | NOT NULL — see list below |
| `target_user_id` | `uuid` | NULL, FK → users(id) ON DELETE SET NULL |
| `metadata` | `jsonb` | NOT NULL DEFAULT `'{}'` |
| `ip_hash` | `text` | NULL |

Indexes: `INDEX(target_user_id, occurred_at DESC)`, `INDEX(event, occurred_at DESC)`.

**Essential events only** (deliberately not everything): `login_success`, `login_failure`, `account_locked`,
`mfa_enrolled`, `mfa_verified`, `mfa_failed`, `mfa_reset_admin`, `recovery_code_used`, `device_enrolled`,
`device_revoked`, `session_created`, `session_switched`, `session_force_ended`, `licence_activated`,
`licence_suspended`, `licence_revoked`, `licence_extended`, `admin_login`, `consent_accepted`.

### 5.9 Relationship summary

```
users 1──1 licences          (UNIQUE(user_id) for MVP; drop to go multi-licence)
users 1──N devices           (capped by licences.max_devices)
users 1──1 sessions[active]  (enforced by partial unique index)
users 1──1 mfa_credentials
users 1──N recovery_codes
users 1──N consent_records
users 1──N audit_events      (as target)
sessions N──1 devices
```

---

## STEP 6 — Session design: transaction-safe one-active-session

### 6.1 The guarantee

**At most one row in `sessions` may have `status='active'` for a given `user_id`, at any instant, under any
interleaving of concurrent requests, on any number of application workers.**

This is enforced by the *database*, not by application logic. That distinction is the whole point: application-level
"check then insert" is a time-of-check-to-time-of-use race, and it is exactly the bug the current Gist
implementation has (F-7a).

### 6.2 The two mechanisms, and why both are needed

**Mechanism 1 — partial unique index (the hard guarantee).**

```sql
CREATE UNIQUE INDEX one_active_session_per_user
  ON sessions (user_id) WHERE status = 'active';
```

Postgres evaluates unique indexes inside the transaction that performs the write. If two transactions both try
to insert an `active` session for the same `user_id`, one commits and the other receives a unique-violation
error. There is no window in which both succeed, because uniqueness is checked at write time by the storage
engine, not by a prior `SELECT`. **Even if the application logic is completely wrong, the database refuses.**

**Mechanism 2 — `SELECT … FOR UPDATE` (orderly behaviour).**

The index alone would work but would produce ugly failures — one client gets an opaque 500 from a constraint
violation. So the normal path serialises on the user row first:

```sql
BEGIN;

-- 1. Serialise all session operations for this user. Any concurrent
--    transaction doing the same blocks here until we commit or roll back.
SELECT id, status FROM users WHERE id = :user_id FOR UPDATE;

-- 2. Now, and only now, inspect existing sessions. We hold the lock,
--    so this read cannot be invalidated before we write.
SELECT id, device_id, last_heartbeat_at
  FROM sessions
 WHERE user_id = :user_id AND status = 'active';

-- 3a. No active session  → insert one.
-- 3b. Active session on THIS device → reuse it (reconnect after a crash).
-- 3c. Active session on ANOTHER device → do NOT insert.
--     Return 409 SESSION_ACTIVE_ELSEWHERE with the other device's name.
--     The client then offers "Switch to this device", which requires TOTP.

-- On an approved switch, both statements run inside THIS SAME transaction:
UPDATE sessions SET status='superseded', ended_reason='switch'
 WHERE user_id = :user_id AND status = 'active';
INSERT INTO sessions (user_id, device_id, status, expires_at)
VALUES (:user_id, :device_id, 'active', now() + interval '24 hours');

COMMIT;
```

### 6.3 Why two simultaneous logins cannot both succeed — walked through

Two computers, A and B, submit `POST /session/create` for the same user at the same millisecond, landing on two
different application workers.

1. Both transactions reach `SELECT … FROM users WHERE id = :user_id FOR UPDATE`.
2. Postgres grants the row lock to exactly one — say A. **B blocks inside the database.** It is not polling, not
   retrying, not racing; it is suspended until A's transaction resolves.
3. A sees no active session, inserts one, commits, releases the lock.
4. B now proceeds and reads *A's committed state* — because B's `SELECT` executes after the lock was released,
   it sees the row A just wrote. B therefore takes branch 3c and returns 409. **B never attempts an insert.**
5. Had B somehow attempted one (a bug, a code path that skipped the lock, a future refactor), the partial unique
   index would reject it at write time. Two independent barriers, one of which is enforced by the storage engine
   and cannot be bypassed by application error.

The failure that destroys naive implementations — both sides read "no session" before either writes — is
impossible, because the read that decides the branch happens *while holding a lock that the other side must
acquire before it can write*.

The switch case is equally safe: `UPDATE … status='superseded'` and `INSERT … status='active'` are in one
transaction, so at no point is the index asked to hold two active rows, and no other transaction can observe an
intermediate state.

### 6.4 Session lifecycle and the stale sweeper

- `expires_at` = `created_at + 24h` — a hard ceiling regardless of heartbeats. A session cannot live forever.
- `last_heartbeat_at` is refreshed on every heartbeat.
- A session is **stale** when `last_heartbeat_at < now() - 3 × heartbeat_interval` (i.e. 4.5 minutes at a
  90-second interval). Three missed beats tolerates a laptop sleeping, a Wi-Fi drop, or a brief server blip
  without evicting a legitimate user.
- A background job (or a lazy check on the next `session/create` for that user) marks stale sessions
  `status='expired'`, freeing the partial unique index for a new login.

**Crash recovery matters commercially.** If a customer's machine blue-screens, they must not be locked out for
24 hours. The stale sweep handles this: after ~4.5 minutes they can log in again *without* needing MFA, because
branch 3b/stale-cleanup means there is no longer a competing active session. Only a switch to a *different*
device while a session is genuinely live requires TOTP.

### 6.5 What the losing client experiences

Computer A is running. Computer B logs in.

1. B gets `409 SESSION_ACTIVE_ELSEWHERE`, with the other device's display name and last-seen time.
2. B shows: *"StopLossPro is currently active on DESKTOP-4KQ21 (last seen 30 seconds ago). Switch to this
   computer? The other computer will be signed out."*
3. B requests a TOTP code. On success, the switch transaction in §6.2 runs.
4. A's next heartbeat (≤90 s) returns `401 SESSION_SUPERSEDED`. A immediately disables the protected surface and
   shows *"Signed out — StopLossPro was opened on another computer."* **A does not kill the process** — killing
   the app while a trader has open positions on screen is user-hostile. It locks the licensed functionality and
   leaves the window up with a clear explanation and a "Sign in here" button.

That last point is a deliberate departure from the current `app.stop()` behaviour, and it is a product decision as
much as a security one.

---

## STEP 7 — Authorization design

### 7.1 Authentication

`POST /auth/login` with `{email, password, device_public_key, device_name, app_version, os_version}`.

- Password verified with **Argon2id** (`argon2-cffi`, `PasswordHasher()` defaults are a sane starting point;
  target ≥250 ms per verification on the production instance and tune memory cost to that).
- Rate limiting: **per-account** and **per-IP**, both. Per-IP alone is defeated by botnets; per-account alone
  enables account-lockout denial-of-service. Suggested starting policy: 5 failures per account in 15 minutes →
  `locked_until = now() + 15 min`, doubling on repeat, capped at 24 h; 20 failures per IP in 15 minutes → 429.
- Response is generic on failure ("invalid email or password") — never reveal whether the email exists.
- Every attempt writes an `audit_events` row.

If MFA is enrolled and this is a device-switch or a new-device enrolment, the login returns a short-lived
`mfa_pending` challenge token rather than authorization.

### 7.2 Token format and lifetime

Two things, deliberately kept distinct:

**(a) Session token** — an opaque, high-entropy random string (32 bytes, base64url) stored server-side against
the session row. Sent as `Authorization: Bearer`. It is a lookup key, not a claim. Opaque tokens are chosen over
JWT here because **instant revocation matters more than statelessness** at 22 req/s — every heartbeat hits the
database anyway, so there is nothing to gain from a self-contained token and a great deal to lose.

**(b) Authorization grant** — an **Ed25519-signed** payload the client verifies offline:

```json
{
  "v": 1,
  "sub": "<user_id>",
  "lic": "active",
  "dev": "<device_id>",
  "sid": "<session_id>",
  "iat": 1785600000,
  "exp": 1785600180,
  "grace_until": 1785686400,
  "nbf": 1785599940
}
```

signed as `base64url(payload) + "." + base64url(ed25519_sign(payload))`.

- **`exp` = `iat` + 180 s.** Three minutes: long enough that a single dropped heartbeat is invisible to the user,
  short enough that a stolen grant is nearly worthless.
- **`grace_until`** is the server's explicit statement of how long this client may keep operating without
  reaching the server. It is *inside the signed payload*, so the client cannot extend it.
- The client holds **only the Ed25519 public key**, compiled in as a byte constant. The private key exists only
  in the server's environment (secret manager or env var), is never in the repository, and is never transmitted.

### 7.3 Refresh strategy

**Refresh is collapsed into the heartbeat.** There is no separate refresh endpoint for MVP.

`POST /session/heartbeat` with the session bearer token returns either:
- `200` + a **freshly signed authorization grant** (new `exp`, new `grace_until`), or
- `401` with a machine-readable reason: `SESSION_SUPERSEDED`, `SESSION_EXPIRED`, `LICENCE_REVOKED`,
  `LICENCE_SUSPENDED`, `LICENCE_EXPIRED`, `DEVICE_REVOKED`.

One endpoint, one code path, one thing to get right. A separate refresh-token mechanism is a meaningful amount of
extra surface (rotation, reuse detection, family revocation) for zero benefit at this scale. Revisit only if a
web dashboard is added.

### 7.4 Secure local storage on Windows

The session token, the current signed grant, and the **device private key** are stored in a single blob at
`%LOCALAPPDATA%\StopLossPro\state.bin`, encrypted with **DPAPI** (`CryptProtectData`) using
`CRYPTPROTECT_UI_FORBIDDEN` and scoped to the **current user** (not `LOCAL_MACHINE`).

- Ties the secret to the Windows user account: another user on the same PC cannot read it, and copying the file
  to a different machine yields undecryptable bytes.
- An additional application-specific entropy value is passed to DPAPI so that another program running as the same
  user cannot decrypt it merely by calling `CryptUnprotectData` on the file.

**Honest limitation:** DPAPI protects against file copying and other local users. It does **not** protect against
malware or a debugger running as that same user — nothing on a desktop does. That is accepted, and it is why the
grant is short-lived and the session is server-controlled.

### 7.5 Heartbeat interval — and the justification

**Recommended: 90 seconds.**

The brief suggests a 60–120 s range. Reasoning for landing at 90:

- **Detection latency.** Worst-case time for a revoked or superseded customer to lose access is one interval
  plus network time: **≤ ~95 seconds**. Acceptable for a licence-enforcement control (this is not fraud
  prevention on a payments rail).
- **Server load.** At 2,000 customers: 2,000/90 ≈ **22 req/s**. At 60 s it would be 33 req/s — still fine, but 90
  gives 50% more headroom for free.
- **Sharing abuse economics.** The realistic abuse is two people sharing one licence. A 90-second window means a
  freeloader is interrupted constantly; the control does not need to be tighter than "makes sharing annoying."
- **Battery/network politeness.** A laptop on mobile tethering waking the radio every 60 s is worse than every 90.
- **Staleness tolerance.** Three missed beats = 4.5 minutes before a session is reclaimed — comfortably longer
  than a sleep/resume cycle or a Wi-Fi handover, so legitimate users are not evicted.

Jitter of ±10% is added to each interval so 2,000 clients do not synchronise into a thundering herd after a
server restart.

### 7.6 Offline grace — precise behaviour

The controlling principle: **"cannot reach the server" and "server said no" are completely different, and the
client must never confuse them.**

| Condition | Client behaviour |
|---|---|
| Heartbeat `200` | Store new grant. Full functionality. Reset grace anchor. |
| Heartbeat `401 LICENCE_REVOKED` / `SUSPENDED` / `EXPIRED` / `DEVICE_REVOKED` | **Immediate hard lock.** Wipe the stored grant. Show the specific reason. No grace whatsoever — the server has spoken. |
| Heartbeat `401 SESSION_SUPERSEDED` | Immediate lock of the protected surface, with a "signed out on another computer" message and a sign-in button. |
| Network failure / DNS failure / timeout / 5xx | **Grace mode.** Keep working while `now() < grant.grace_until`. Show a persistent, non-blocking banner: *"Offline — licence re-check needed by 14:30."* Retry with exponential backoff (30 s → 60 s → 120 s → capped at 300 s). |
| `now() >= grant.grace_until` | Lock the protected surface. Message: *"Cannot verify your licence. Connect to the internet to continue."* This is explicitly **not** an accusation of wrongdoing. |

**Grace duration: 72 hours**, carried in `grace_until`. Rationale: covers a weekend of travel or a router failure —
the realistic honest-customer outage — without creating a meaningful "just stay offline" bypass. It is a server-side
policy value and can be extended per-licence by the admin for a customer with a genuine connectivity problem.

**Clock-rollback protection.** A client that sets its system clock backwards must not extend grace. Three layers:

1. **Monotonic clock as primary.** Grace is measured with `time.monotonic()` from the moment of the last
   successful heartbeat, not with wall-clock time. `monotonic()` is unaffected by any user clock change while the
   process runs.
2. **High-water mark.** The DPAPI blob stores `max_seen_server_time`, updated from the server's timestamp on
   every successful heartbeat and **never decreased**. If wall-clock `now()` is earlier than
   `max_seen_server_time`, the clock has moved backwards: the client treats grace as **already expired** and
   locks. A rollback therefore makes the situation strictly worse for the attacker, not better — the correct
   incentive.
3. **Server-side `nbf`.** The grant carries a not-before timestamp; a client whose clock is far in the past will
   fail its own verification of a freshly issued grant, producing a clear "check your system clock" message
   rather than silent misbehaviour.

Restarting the process resets `monotonic()`, which is why layer 2 exists and is the one that actually carries the
guarantee across restarts.

---

## STEP 8 — Device and MFA design

### 8.1 Device identity — cryptographic, not hardware

Hardware fingerprinting is **not** the identity. Requirement 19, and also simple reality: RAM upgrades, NIC
changes, dock swaps and Windows reinstalls all change `uuid.getnode()`, and every one of those would currently
lock out a paying customer.

**Enrolment (first run on a new computer):**

1. Client generates an **Ed25519 keypair** locally. The private key is written into the DPAPI blob and never
   leaves the machine.
2. Client sends `{email, password, device_public_key, device_name, os_version, app_version, hardware_hint}`.
3. If MFA is already enrolled for the account, the server requires a **TOTP code** before accepting a *new*
   device. (The very first device, enrolled during initial setup, is accepted with password alone, immediately
   followed by mandatory MFA enrolment.)
4. Server checks `count(active devices) < licences.max_devices` (default 3), inserts the row, writes
   `device_enrolled` to the audit log.
5. Subsequent requests are bound to that device: the client signs a challenge with the device private key, proving
   possession. Possession of the key — not the shape of the hardware — is the trust anchor.

`hardware_hint` is stored purely as a **risk signal**: "this device's hardware fingerprint changed" is useful
information on an admin screen. It never gates access on its own.

### 8.2 Device switching — the flagship flow

```
Computer B: user enters email + password
      ↓
Server: credentials OK; active session exists on Computer A
      ↓
409 SESSION_ACTIVE_ELSEWHERE { other_device: "DESKTOP-4KQ21", last_seen: "30s ago" }
      ↓
Computer B shows: "Active on DESKTOP-4KQ21. Switch to this computer?"
      ↓
User confirms  →  Server issues short-lived mfa_challenge (valid 5 min, single use)
      ↓
Computer B prompts for the 6-digit TOTP code
      ↓
Server verifies TOTP (±1 step window, replay-guarded via last_used_step)
      ↓
ONE TRANSACTION (see §6.2):
   supersede A's session  +  create B's session  +  audit session_switched
      ↓
Computer B receives a signed authorization grant → unlocked
      ↓
Computer A's next heartbeat (≤90 s) → 401 SESSION_SUPERSEDED → locks the protected surface
```

The second factor is what makes this safe: a stolen password alone cannot evict a working session and take over
the licence. Possession of the enrolled authenticator is required.

### 8.3 TOTP specifics

- **RFC 6238**, SHA-1, 6 digits, 30-second step — these exact parameters because that is what Google
  Authenticator, Authy, 1Password and Microsoft Authenticator all implement interoperably. Deviating (SHA-256,
  8 digits) breaks compatibility with the apps customers actually have.
- Secret: 160 bits from `secrets.token_bytes(20)`, base32-encoded, generated **server-side**.
- Enrolment: server returns an `otpauth://totp/StopLossPro:{email}?secret=…&issuer=StopLossPro` URI rendered as a
  QR code. Row is created with `status='pending'` and **only becomes `active` after the user submits one valid
  code** — this prevents locking a user out of an authenticator they never actually scanned.
- Verification window: **±1 step** (±30 s) to tolerate clock drift. Not wider — each extra step is a linear
  increase in brute-force surface.
- **Replay guard:** `mfa_credentials.last_used_step` records the accepted time step; a code from a step
  `<= last_used_step` is rejected even if arithmetically valid.
- Rate limit: 5 verification attempts per 15 minutes per user, then lock.
- At-rest: `totp_secret_enc` is encrypted with a server key held in the environment. A database dump alone must
  not hand over every customer's second factor.
- **Never invent OTP logic.** `pyotp` implements the RFC; use it.

### 8.4 Recovery codes — the self-service fallback

Generated **at MFA enrolment**, before the user leaves the screen:

- **10 codes**, each 10 characters from an unambiguous alphabet (no `0`/`O`, no `1`/`I`/`l`), formatted `XXXXX-XXXXX`.
- Shown **exactly once**, with a "Download / Copy" button and a plain warning.
- Stored **Argon2id-hashed** in `recovery_codes.code_hash`. The plaintext is never persisted server-side.
- **Single use** — `used_at` is stamped on redemption, and `recovery_code_used` is written to the audit log.
- Redeeming a code is accepted anywhere a TOTP code is accepted, including device switching.
- When fewer than 3 remain unused, the app nags the user to regenerate. Regenerating invalidates all previous codes.

### 8.5 Admin-assisted MFA reset — and the honest caveat

Recovery codes handle the common case. For the customer who has lost both the phone and the codes, there must be
a documented manual path — but it must be documented as what it is.

**Stated plainly: there is no formal KYC in this business.** Sales are direct, often cash or crypto, with no
identity verification at purchase. Any "identity verification" at reset time is therefore *best-effort
correlation*, not proof of identity, and it should never be described to a customer as more than that.

Recommended procedure, to be followed and recorded consistently:

1. Request arrives from the **email address on the account** (a request from any other address is refused outright).
2. Admin asks for corroborating detail the legitimate purchaser would plausibly know: approximate purchase date,
   the payment method and amount, the transaction hash or the circumstances of the cash payment, and the name of a
   previously enrolled device.
3. Admin checks these against `licences.payment_ref`, `devices`, and `audit_events`.
4. **A 24-hour cooling-off period before the reset takes effect**, with a notification email sent to the account
   address immediately. If the real owner did not request it, they have a day and a clear channel to object. This
   single measure does more against social engineering than any question-and-answer script.
5. Admin performs `Reset MFA` in the admin panel. This: disables the MFA credential, invalidates all recovery
   codes, **revokes all devices**, ends any active session, and writes `mfa_reset_admin` to the audit log with the
   admin's justification text as a required field.
6. The customer re-enrols MFA and re-enrols devices on next login.

Recording the justification as a mandatory field is what makes this auditable later, when there are two admins
and nobody remembers August.

---

## STEP 9 — Admin design

### 9.1 Principle

One administrator today. **Design so a second admin and real roles can be added without rework** — which in
practice means exactly three cheap decisions taken now, not an RBAC system:

1. Admins live in their **own `admin_users` table**, never as a flag on `users`. Customer identity and operator
   identity are different problems and must not share a table.
2. Every admin table carries a `role` column, `NOT NULL DEFAULT 'owner'`, with a CHECK constraint listing the
   allowed values. Today the list has one value. Adding `'support'` later is a migration that adds a value and a
   handful of permission checks — not a redesign.
3. Every admin action is written to `audit_events` with `actor_type='admin'` and `actor_id` **from day one**.
   Retrofitting attribution after the fact is the part that is genuinely painful, and it costs nothing to do now.

That is the whole of "don't paint into a corner." No permission matrix, no role editor, no policy engine.

### 9.2 Admin authentication

- Separate login at `/admin`, separate session cookie (`HttpOnly`, `Secure`, `SameSite=Strict`), separate and
  shorter idle timeout (30 minutes).
- Password: Argon2id, with a materially higher minimum length requirement than customer accounts (≥16 chars).
- **TOTP MFA is mandatory for the admin account — not optional, not skippable.** The admin account can activate,
  revoke and reset every customer; it is the highest-value credential in the system.
- Optional and recommended while there is exactly one admin: **IP allow-list** on `/admin`, or put the admin
  routes behind a private network / VPN / Cloudflare Access. Cheap, and it removes the admin login from the
  public internet entirely.
- Admin login failures are rate-limited and alerted on.

### 9.3 Screens

| Screen | Actions |
|---|---|
| **Dashboard** | Counts: active licences, active sessions now, devices enrolled, logins today, failed logins today. Recent audit events. |
| **Customers (list)** | Search by email; filter by licence status. Columns: email, status, expiry, devices, session-now, last seen. |
| **Customer (detail)** | The main working screen. Shows the user, their licence, their devices, their current session, their MFA state, their consent records, and their audit trail. |
| — Licence actions | **Create customer** (email + temp password + licence) · **Activate** · **Suspend** · **Revoke** · **Extend expiration** · edit `payment_ref` and notes |
| — Device actions | List devices (name, enrolled, last seen, hardware-hint drift flag) · **Revoke device** |
| — Session actions | Show current active session (device, started, last heartbeat) · **Force logout** |
| — MFA actions | Show MFA status and unused-recovery-code count · **Reset MFA** (requires a justification field) |
| **Audit log** | Filter by event type, user, date range. Read-only. Export CSV. |

### 9.4 Deliberately excluded from MVP

Bulk operations, email templating, a metrics/BI layer, webhooks, an admin API, self-serve customer portal,
multi-tenant anything. Each is a real feature; none is needed to sell to the first hundred customers, and each
would be built on guesses about workflows that do not exist yet.

### 9.5 Creating a customer after a manual payment — the actual workflow

1. Payment arrives (cash / crypto / transfer) and is verified **by a human, out of band**.
2. Admin → *Create customer*: email, generated temporary password, licence with `status='active'`,
   `expires_at`, and `payment_ref` recording how it was paid.
3. System emails the customer their temporary password and the download link.
4. Customer installs, logs in, is forced to change the password, accepts ToS / Risk Disclosure / Privacy
   (written to `consent_records`), enrols MFA, saves recovery codes, and the first device enrols automatically.
5. Done. **The client never verifies a payment and never writes its own approval** — the defect at the heart of
   F-6 simply has no equivalent in this design.

---

## STEP 10 — Security hardening for a PyInstaller-packaged Kivy app

### 10.1 The honest statement, first

**A desktop application cannot be made uncrackable.** The customer's machine executes the code and therefore
controls it. Any check that runs locally can be found and patched, and Python is a comparatively soft target: a
PyInstaller onefile is a self-extracting archive of `.pyc` files, and tools to unpack it and decompile the
bytecode are freely available and take minutes to run.

What client hardening actually buys is **cost and time**. The realistic goal is that cracking StopLossPro is more
work than paying for it — the buyers are traders, not reverse engineers — and that a crack does not scale, because
the parts that matter are not on the client at all.

Real protection is layered, and the layers are not equally valuable:

| Layer | Real value |
|---|---|
| **Server-authoritative licensing** | **Highest.** A cracked binary still cannot create a session, and a shared licence still collides on the one-active-session constraint. |
| **Signed short-lived authorization** | **High.** The grant cannot be forged without the private key, and it expires in 3 minutes. |
| **Session control + remote revocation** | **High.** A leaked account is killable within 90 seconds. |
| **Code signing** | **High commercially** (SmartScreen, AV, trust); modest as an anti-tamper measure — it does mean a patched exe loses the signature, which is detectable. |
| **Client hardening / obfuscation** | **Low–moderate.** Raises effort. Does not prevent. |

### 10.2 Do now — high value, low cost

1. **Authenticode code signing.** Pulled early per the brief, and correctly so. An OV certificate is the entry
   point; an **EV certificate grants immediate SmartScreen reputation** and is worth the extra cost when selling
   an unknown exe to strangers. The key must live on the issued hardware token or in a cloud HSM signing service —
   **never in the repository, never in `datas`, never in the build script as a file path to a `.pfx` with a
   password beside it.** Sign both the exe and any installer.
2. **Turn UPX off** (`upx=False` in `StopLossPro.spec`). It saves some megabytes and costs AV reputation (F-10).
   Wrong trade for a product sold to strangers.
3. **Ship no source, no secrets.** Verify the built exe contains no `.env`, no `.pem`, no `.pfx`, no database
   URL, no admin credential, no GitHub PAT, no wallet address. Add a build-time grep over the spec's `datas` and
   a post-build string scan of the exe for high-risk patterns; fail the build on a hit.
4. **Compile with `optimize=2`** so docstrings are stripped from the bundled bytecode. Small, free.
5. **Keep the log lockdown, and ship it.** Already written; the rebuild is pending.
6. **Keep the formula hiding.** Already done. The risk model is the IP; do not print it into the UI or the logs.
7. **Embed only the Ed25519 public key.** Trivially true if the design is followed, but worth an explicit
   build-time assertion that no 64-byte private key material appears in the binary.

### 10.3 Worth doing — moderate value

8. **Verify the grant properly.** Constant-time signature verification via `cryptography`; validate `exp`,
   `nbf`, `sub`, `dev` and `sid` — not just the signature. A signature check that ignores the claims is theatre.
9. **Self-integrity check.** On startup, compute a hash of the running executable and include it in the heartbeat.
   The server can flag mismatches for investigation. Note honestly: an attacker who patches the binary can also
   patch this check. Its value is *detection of casual tampering at scale*, not prevention.
10. **Fail closed on ambiguity, fail open only on network error.** Any state that is not a positive, verified,
    unexpired grant → locked. The only exception is the explicit bounded grace window.
11. **Keep licensing logic out of a single obvious function.** Not obfuscation for its own sake — just don't ship
    a function named `is_licensed()` returning a bare boolean at one call site, which is a one-byte patch.
    Distribute the gate across the protected surface so a bypass requires understanding the app, not finding one
    `if`.
12. **Rate-limit and alert server-side on impossible patterns**: one account heartbeating from many IPs, rapid
    device-enrolment churn, sustained 401s. This is where crack detection actually works, because the server is
    the part the attacker does not control.

### 10.4 Consider later — low value now

13. **Cython-compile the risk engine** (`calc.py`, `mixin_calculator.py`) into a native `.pyd`. This is the one
    obfuscation step with a genuinely favourable cost/benefit ratio here, because it protects **the actual IP**
    — the formulas — rather than the licence check, and native code is meaningfully harder to read than
    decompiled bytecode. Defer until there is revenue worth protecting; it complicates the build.
14. **PyArmor or equivalent bytecode obfuscation.** Raises effort against casual decompilation. Known to cause
    PyInstaller packaging friction and occasional AV false positives. Only if piracy is *observed*, not
    pre-emptively.
15. **Anti-debug / anti-VM checks.** Not recommended. High false-positive rate against legitimate users (many
    traders run in VMs or on VPS), trivially bypassed by anyone competent, and they make the app look like
    malware to AV heuristics. Net negative.

### 10.5 What is explicitly not achievable

- Preventing a determined reverse engineer from removing the licence check from a local binary.
- Preventing a customer from sharing their credentials — **though the one-active-session constraint makes shared
  credentials actively painful, which is the realistic mitigation.**
- Preventing screen recording, screenshots, or a human copying the formula's outputs.
- Keeping any secret confidential inside a binary that is distributed to the person you are keeping it from.

Design accordingly: the valuable secrets live on the server, and the licence is enforced by something the
attacker does not control.

---

## STEP 11 — Implementation roadmap

Phases are small and independently shippable. **"Commercial value"** marks a phase that could plausibly ship and
be sold before the rest of the plan is finished.

---

### PHASE 0 — Emergency containment (same day) 🔴 **COMMERCIAL VALUE: protects existing revenue**

**Objective.** Close F-1 and F-2 — the two unauthenticated remote bypasses — without waiting for the new backend.
Nothing else in this roadmap matters if anyone can activate for free or wipe every customer today.

**Files/components.** `Working/cf_worker/gist_proxy.js`; Cloudflare Worker environment; `lib/constants.py`;
`lib/activation.py` (`_start_revoke_listener`, `_instant_approval_poll`).

**Actions.**
1. Add a **shared secret header** to the Worker (`X-SLP-Auth`, compared with `crypto.subtle.timingSafeEqual`
   against a CF secret) and reject everything else. Accepts that the secret is extractable from the exe — this is
   a speed bump, not a fix, and it is explicitly temporary.
2. **Remove `approved_ids.txt` and `used_txns.txt` from the Worker's `ALLOWED_FILES` set entirely.** Clients have
   no legitimate reason to write licence state. This single line change removes the free-activation and mass-DoS
   primitives even if the secret leaks. **Do this first; it is the highest-value five minutes in the document.**
3. Add per-IP rate limiting on the Worker.
4. Rotate the GitHub PAT held in the Worker's environment (it has been reachable through an open write path).
5. Regard `stoploss_dev_h7zltndg` and `stoploss_hb_h7zltndg` as **already compromised**; plan to retire them
   (Phase 4) rather than rename them.

**Dependencies.** Cloudflare account access. No client rebuild required for steps 1–4 except the header, which
does require a rebuild — so if a rebuild is not possible today, **do step 2 alone**, which needs no client change
because the auto-approve write path breaking is a *feature*, not a regression (it was the bypass).

**Tests.** Confirm a POST without the header is rejected. Confirm a POST attempting to write `approved_ids.txt` is
rejected with 403. Confirm a legitimate client still starts and reads its approval status.

**Risks.** Removing `approved_ids.txt` from the allow-list disables the crypto auto-approval flow → new P2 sales
must be activated manually until Phase 4. **Given that the auto-approval flow was itself the bypass, this is a
correct trade, not a regression.**

**Rollback.** Revert `gist_proxy.js` and redeploy (one `wrangler deploy`). Under a minute.

**Expected result.** No unauthenticated party can grant themselves a licence or revoke everyone else's.

---

### PHASE 1 — Backend skeleton + schema (foundational)

**Objective.** A running FastAPI service with the seven tables, migrations, and health checks. No client changes.

**Files/components.** New repository or `server/` directory: `app/main.py`, `app/db.py`, `app/models/`,
`alembic/`, `app/config.py`, `Dockerfile`, `docker-compose.yml` (local dev).

**Dependencies.** Hosting account, managed Postgres, domain + TLS certificate.

**Tests.** Alembic up/down migrations run cleanly. **A dedicated test asserting the partial unique index rejects
a second active session** — write this test before writing the session code; it is the load-bearing invariant.
FK cascade behaviour tested. `/health` returns 200.

**Risks.** Low. Nothing customer-facing exists yet.

**Rollback.** Tear down the environment. No customer impact.

**Expected result.** A deployed, empty, migrating backend.

---

### PHASE 2 — Auth + licence status + signed authorization (foundational)

**Objective.** Register/login with Argon2id, licence status lookup, Ed25519 grant issuance, rate limiting, audit
logging.

**Files/components.** `app/auth/`, `app/licensing/`, `app/crypto/signing.py`, `app/audit.py`; key generation
runbook; secret storage configuration.

**Dependencies.** Phase 1. Ed25519 keypair generated **on the server**; private key into the secret store; public
key exported for the client.

**Tests.** Password hash/verify round-trip. Lockout after N failures. Grant verifies against the public key and
**fails** after `exp`. A tampered grant fails verification. Audit rows written for every auth event. Load test:
50 concurrent logins.

**Risks.** **Key management is the sharp edge.** If the private key leaks, every grant is forgeable. Mitigation:
generate on the server, never commit, never log, document the rotation procedure (bump the grant `v` field, ship a
client with both public keys, retire the old key) *before* going live.

**Rollback.** Backend only; no client depends on it yet.

**Expected result.** A working identity and licensing API.

---

### PHASE 3 — Sessions + devices + MFA (foundational)

**Objective.** The distinctive controls: one active session per customer, device enrolment, TOTP, recovery codes,
MFA-gated switching.

**Files/components.** `app/sessions/`, `app/devices/`, `app/mfa/`; the stale-session sweeper.

**Dependencies.** Phase 2.

**Tests.** **The concurrency test is the important one:** 20 threads calling `POST /session/create` for the same
user simultaneously → exactly one 200, nineteen 409, and exactly one `active` row. Repeat 100 times.
Switch flow requires TOTP and supersedes atomically. Stale sweeper reclaims after 3 missed beats. TOTP replay
within the same step is rejected. Recovery code is single-use. Device cap enforced.

**Risks.** Concurrency bugs are the classic failure mode here — hence testing the invariant directly rather than
testing the happy path. Second risk: a too-aggressive stale threshold evicting legitimate users; mitigate by
starting at 3 intervals and watching the audit log.

**Rollback.** Backend only.

**Expected result.** The full server-side control set, verified under concurrency.

---

### PHASE 4 — Admin panel (foundational, unblocks selling)

**Objective.** The screens in STEP 9, with admin auth + mandatory admin MFA.

**Files/components.** `app/admin/` routes + Jinja2 templates; `admin_users` table.

**Dependencies.** Phase 3.

**Tests.** Every admin action writes an attributed audit row. Admin MFA cannot be skipped. Session cookie flags
correct. Force-logout genuinely ends the customer session (verified by the customer's next heartbeat returning
401). Authorisation test: unauthenticated access to every `/admin/*` route returns 302/401.

**Risks.** The admin panel is the highest-value target in the system. Mitigate with mandatory MFA and an IP
allow-list or private-network deployment.

**Rollback.** Disable the `/admin` router; manage via `psql` in the interim.

**Expected result.** Customers can be created, activated, suspended, revoked and supported without touching SQL.

---

### PHASE 5 — Client licensing module (the cutover) 🟢 **COMMERCIAL VALUE: this is the sellable product**

**Objective.** Replace `lib/activation.py` with `lib/licensing/`. Remove all Gist, ntfy, GPS and telemetry code.
Add login/MFA/device-switch UI. Gate the protected surface on a verified grant.

**Files/components.** New `lib/licensing/{client,auth_token,device,storage,gate}.py`; **delete**
`lib/activation.py`; edit `Product Sell.py` (startup flow), `lib/constants.py` (strip dead endpoints),
`lib/layout.kv` (login / MFA / switch-device screens), `lib/mixin_lifecycle.py`.
**`calc.py` and `mixin_calculator.py` are not opened.**

**Dependencies.** Phases 2–4.

**Tests.** Full manual matrix: fresh install → enrol → MFA → trade. Second computer → 409 → TOTP → switch → first
computer locks within 90 s. Pull the network cable → grace banner → still works → past `grace_until` → locks.
Set the system clock back 3 days → **locks immediately** (rollback protection). Admin revokes → client locks
within 90 s. Recovery-code path. DPAPI blob copied to another machine → unusable. **Regression test the risk
engine: identical inputs must produce byte-identical position sizes before and after.**

**Risks.** **Highest-risk phase — it touches the customer's startup path.** A bug here locks out paying customers.
Mitigations: ship to one internal machine first; keep the old exe downloadable; add a documented server-side
"grace extension" lever so a stuck customer can be unblocked in seconds without a rebuild.

**Rollback.** Customers keep running the previous exe; the old Gist path remains live until Phase 6. **Do not
delete the Gist until Phase 5 has been stable in the field for at least two weeks.**

**Expected result.** A product with real server-authoritative licensing that can be sold to strangers.

---

### PHASE 6 — Code signing + build hardening 🟢 **COMMERCIAL VALUE: directly improves conversion**

**Objective.** Signed binaries, no SmartScreen block, no UPX, verified-clean package.

**Files/components.** `build.bat`, `StopLossPro.spec`, signing runbook.

**Dependencies.** Purchased code-signing certificate (OV or, preferably, EV) and its hardware token / cloud HSM.

**Tests.** `signtool verify /pa /v` passes. Download on a clean Windows VM → no SmartScreen block (EV) or
diminishing warnings as reputation accrues (OV). AV scan across major engines. Post-build string scan finds no
secrets. Confirm `upx=False` did not break the build.

**Risks.** Certificate issuance takes days to weeks (identity validation) — **start this in parallel with
Phase 1, not at Phase 6.** Losing the token means re-issuance.

**Rollback.** Ship unsigned as before.

**Expected result.** A customer double-clicks and it just runs.

---

### PHASE 7 — Consent framework + privacy cleanup 🟢 **COMMERCIAL VALUE: removes a real liability**

**Objective.** Versioned ToS / Risk Disclosure / Privacy acceptance recorded server-side; all covert collection gone.

**Files/components.** `app/consent/`, `consent_records`; client first-run acceptance screen; final removal of
`_collect_system_info` remnants; `docs/legal/` holding the placeholder documents.

**Dependencies.** Phase 5. **A lawyer for the actual text.** `[PLACEHOLDER — LAWYER REVIEW REQUIRED]`

**Tests.** Cannot proceed past first run without accepting all three. Version bump re-prompts. Row written with
document, version and timestamp. **Network capture confirms no coordinates, no MAC, no username, no hostname, no
broker login, no balance and no position data leave the machine.**

**Risks.** Placeholder legal text must be visibly marked as such internally and must be replaced before any
serious volume. Never present unreviewed text to customers as though it were reviewed.

**Rollback.** Feature-flag the consent gate off.

**Expected result.** The product collects only what it needs, says what it collects, and can prove when each
customer agreed to what.

---

### PHASE 8 — Signed auto-update (foundational, enables everything after)

**Objective.** Turn `UPDATE_URL` back on, safely.

**Files/components.** `lib/updater.py`; release-signing key (**separate from the authorization key**); server
release endpoint.

**Dependencies.** Phase 6. A second Ed25519 keypair for releases.

**Tests.** Unsigned manifest rejected. Valid signature + wrong SHA-256 rejected. Downgrade attempt rejected.
Interrupted download resumes or fails cleanly. Rollback-to-backup works.

**Risks.** **This phase can achieve remote code execution on every customer machine if done wrong.** Both the
manifest *and* the payload must be signature-verified before anything is executed. Keep `UPDATE_URL = ""` until
every test above passes.

**Rollback.** Set `UPDATE_URL = ""`.

**Expected result.** Security fixes can reach customers without asking them to re-download.

---

### PHASE 9 — Operational baseline (foundational)

**Objective.** Not being blind, and not losing the database.

**Files/components.** Automated daily Postgres backups with **tested restore**; uptime monitoring on `/health`;
error alerting; log retention policy; a one-page incident runbook.

**Dependencies.** Phase 5 in production.

**Tests.** **Restore a backup into a scratch database and verify row counts.** An untested backup is not a backup.
Alerts actually fire.

**Risks.** The realistic catastrophe at this stage is not a hacker — it is losing the customer database with no
working restore. That single risk justifies this whole phase.

**Rollback.** N/A — additive.

**Expected result.** Failures are noticed, and data loss is recoverable.

---

### Sequencing note

Phase 0 is today. Phases 1–4 are backend-only and carry no customer risk. Phase 5 is the risky one and should be
attempted only when 1–4 are genuinely finished. The code-signing certificate purchase (Phase 6) should be
**started at the same time as Phase 1**, because issuance latency, not implementation, is its critical path.

---

## STEP 12 — Final recommendation

### 12.1 Verdicts

| Item | Verdict | Reasoning |
|---|---|---|
| **Emergency containment of the Worker + ntfy bypasses (Phase 0)** | **BUILD NOW — today** | Three unauthenticated remote bypasses are live in a product being actively sold. Everything else is secondary. |
| **Custom minimal backend (FastAPI + Postgres)** | **BUILD NOW** | The distinctive requirement is identity/session control, which no vendor supplies; cost is 3–10× lower and flat; the surface is genuinely small. |
| **Server-authoritative licensing + Ed25519 signed grants** | **BUILD NOW** | Directly replaces the local-file-authoritative anti-pattern (F-5). |
| **One-active-session via partial unique index + row lock** | **BUILD NOW** | The current mechanism does not satisfy the requirement (F-7) and cannot be patched into satisfying it. |
| **Device enrolment via keypair, not fingerprint** | **BUILD NOW** | Current fingerprinting locks out customers who upgrade hardware; keypairs cost no more to implement. |
| **TOTP MFA + hashed recovery codes** | **BUILD NOW** | Required for the device-switch flow; `pyotp` makes it small. |
| **Minimal admin panel with mandatory admin MFA** | **BUILD NOW** | Without it, every support action is manual SQL — which does not scale past a handful of customers and is itself a risk. |
| **Removal of GPS, MAC, hostname, broker-login and position telemetry** | **BUILD NOW** | F-3 and F-4 are the most serious *customer-facing* risks in the product. Deletion is cheaper than any alternative. |
| **Authenticode code signing** | **BUILD NOW (start procurement immediately)** | Weeks of issuance latency; direct conversion and support impact. |
| **Versioned consent framework (placeholder text)** | **BUILD NOW** | The scaffolding is cheap. The text needs a lawyer. `[PLACEHOLDER — LAWYER REVIEW REQUIRED]` |
| **Signed auto-update** | **BUILD NOW — but strictly after code signing** | It is the mechanism for shipping every future security fix; unsigned, it is a remote-code-execution channel (F-9). |
| **Backups with a tested restore** | **BUILD NOW** | The most probable catastrophe is data loss, not intrusion. |
| **Cython-compiling the risk engine** | **BUILD LATER** | The only obfuscation with a good cost/benefit ratio, but it protects revenue that does not exist yet and complicates the build. |
| **Multi-role RBAC** | **BUILD LATER** | One admin. The three cheap decisions in §9.1 keep the door open. |
| **Enterprise audit/SIEM, dashboards, log aggregation** | **BUILD LATER** | `audit_events` plus a filterable admin page is sufficient below ~500 customers. |
| **Advanced monitoring / APM / distributed tracing** | **BUILD LATER** | Uptime check + error alerting covers a 22 req/s monolith. |
| **Formal disaster-recovery infrastructure (multi-region, hot standby)** | **BUILD LATER** | A tested daily backup is the right level of investment at this stage. |
| **Complex entitlement tiers / feature flags per plan** | **BUILD LATER** | The `plan` column exists; there is one plan. Do not build a tier system for hypothetical tiers. |
| **Extensive CI security automation (SAST/DAST/dependency gates)** | **BUILD LATER** | Dependabot-equivalent alerts and `pip-audit` in a pre-release check is proportionate now. |
| **Advanced fraud detection / behavioural analytics** | **BUILD LATER** | Needs baseline data that does not exist. Simple server-side anomaly alerts (§10.3 item 12) cover the realistic cases. |
| **Customer self-serve portal, resellers, floating/seat licences** | **DO NOT BUILD (now)** | No demand signal. If these become requirements, **revisit BUY** — Cryptlex ships them today. |
| **Custom cryptography of any kind** | **DO NOT BUILD — ever** | Use `cryptography`, `argon2-cffi`, `pyotp`. Never invent OTP or signature schemes. |
| **Anti-debug / anti-VM client checks** | **DO NOT BUILD** | High false-positive rate against legitimate users, trivially bypassed, makes the app look like malware. Net negative (§10.4). |
| **Keeping the shared Gist as a fallback after cutover** | **DO NOT BUILD / DO NOT KEEP** | Retaining it retains every bypass. Delete it — and the PAT — two weeks after Phase 5 is stable. |
| **Legal text authorship** | **BUY / OUTSOURCE** | ToS, Risk Disclosure and Privacy Notice for a trading-adjacent product sold internationally need a lawyer. Build the *framework*; buy the *text*. `[PLACEHOLDER — LAWYER REVIEW REQUIRED]` |
| **Code-signing certificate** | **BUY** | Not buildable. |
| **Hosting, managed Postgres, TLS, backups** | **BUY (managed)** | Running your own Postgres and cert renewal is unpaid operational work with no product value. |
| **Email delivery (password resets, MFA-reset notices)** | **BUY** (transactional provider) | Deliverability is a specialist problem. |

### 12.2 BUILD NOW

1. Phase 0 emergency containment — remove `approved_ids.txt` from the Worker allow-list, add auth, rotate the PAT
2. FastAPI + PostgreSQL modular monolith on one managed host
3. The seven tables, with Alembic migrations
4. Argon2id passwords, rate limiting, account lockout
5. Ed25519-signed 3-minute authorization grants; private key server-only
6. One-active-session: partial unique index + `SELECT … FOR UPDATE`
7. Cryptographic device enrolment (fingerprint demoted to a risk hint)
8. TOTP MFA + 10 hashed single-use recovery codes
9. MFA-gated device switching
10. 90-second heartbeat; 72-hour bounded offline grace; monotonic + high-water-mark clock-rollback protection
11. Remote revocation via the heartbeat response
12. Minimal admin panel, mandatory admin MFA, `admin_users` table with a `role` column
13. Deletion of all ntfy telemetry, GPS collection, MAC/hostname/username collection and client-side payment verification
14. Versioned consent capture with placeholder legal documents
15. Authenticode code signing; `upx=False`; secret-free build verification
16. Signed auto-update (after signing exists)
17. Daily backups with a **tested** restore, uptime monitoring, error alerting

### 12.3 BUILD AFTER TRACTION

1. Multi-role RBAC and a second admin account
2. Enterprise audit infrastructure / log aggregation / SIEM
3. Advanced monitoring, APM, distributed tracing
4. Formal disaster recovery (multi-region, hot standby, RTO/RPO targets)
5. Complex entitlement tiers and per-plan feature flags
6. Extensive CI security automation (SAST, DAST, dependency gating)
7. Advanced fraud and licence-abuse detection
8. Larger operational tooling — bulk admin actions, reporting, BI
9. Customer self-serve portal, resellers, floating/seat licensing (**and at that point, re-run build-vs-buy**)
10. Cython-compiled risk engine
11. Third-party penetration test and a formal security review

### 12.4 The one-paragraph answer

Build it, but build the small version. The product does not need a licensing platform; it needs one Postgres
database with a unique index on it, one FastAPI process, one signing key that never leaves the server, and one
admin page. That is a few weeks of focused work and roughly $10–30 a month to run at any scale this business will
plausibly reach in the next two years. What it must not keep doing is treating a public Gist and a public
notification topic as a security boundary — because right now anyone with a browser can activate the product for
free, and anyone who is annoyed can switch off every paying customer at once. Fix that today; build the rest
deliberately.

---

## Appendix A — Consent and legal framework

> **Every item below is `[PLACEHOLDER — LAWYER REVIEW REQUIRED]`. None of it has been reviewed by a lawyer, and
> none of it should be presented to a customer as though it had been.**

Three documents, each independently versioned by date string (e.g. `2026-08-01`):

| Document | Purpose | Must state `[PLACEHOLDER — LAWYER REVIEW REQUIRED]` |
|---|---|---|
| **Terms of Service** | Licence grant, one-active-session rule, device limits, revocation grounds, refund policy, governing law | That the licence is per-customer and non-transferable; that a breach may result in revocation |
| **Risk Disclosure** | Trading risk | **That the software is a calculation and order-entry tool only; that it does not provide investment advice; that trading involves substantial risk of loss; that no profit, loss prevention or trading outcome is guaranteed or implied** |
| **Privacy Notice** | What is collected, why, how long it is kept, how to request deletion | The exact list in Appendix B; that no location data is collected |

**Mechanics.** On first launch after login, the client fetches the current version of each document, displays it,
and requires explicit acceptance. Acceptance writes a `consent_records` row with document, version, timestamp and
hashed IP. A version bump re-prompts at next launch. The customer can re-read all three from Settings at any time.

**Non-negotiable product constraint.** The application, its marketing copy and its UI must not represent that it
guarantees profits, prevents losses, or produces trading success. This is both a legal exposure and a
customer-wellbeing issue: the buyers are retail traders who can lose real money. The existing decision to hide the
formula from the UI does not conflict with this — hiding the *implementation* is IP protection; the *risk* must be
stated plainly and prominently.

---

## Appendix B — Data inventory (target state)

**Collected and retained:**

| Data | Why | Where |
|---|---|---|
| Email address | Account identity, support, security notices | `users` |
| Password (Argon2id hash) | Authentication | `users` |
| Licence status, expiry, payment reference | Entitlement | `licences` |
| Device public key, display name, OS version, app version | Device authorization | `devices` |
| Hardware hint (existing machine ID) | Risk signal only — never gates access | `devices` |
| Session timestamps, device link | One-active-session enforcement | `sessions` |
| TOTP secret (encrypted), recovery-code hashes | MFA | `mfa_credentials`, `recovery_codes` |
| Consent document + version + timestamp | Proof of acceptance | `consent_records` |
| Security events (login, MFA, device, licence, admin) | Security and support | `audit_events` |
| Salted IP hash | Rate limiting, anomaly detection | several |

**Explicitly NOT collected (removed from the current build):**

- GPS coordinates or any precise location
- IP-derived city / region / coordinates (**coarse country may be retained for licensing geography; the ISP,
  city and lat/long must not be**)
- MAC address, Windows username, hostname, screen resolution, CPU model, RAM size, machine make/model
- **MT5 broker server, account login number, balance, equity, or any open-position data** — none of this has any
  licensing purpose and its collection was the most serious privacy defect found (F-3)
- Any trading activity, calculation inputs, or calculation outputs

**Retention.** Audit events 12 months. Sessions 90 days after ending. Everything else for the life of the account
plus a short wind-down window. Deletion on request removes the account and cascades; audit rows retain only a
pseudonymous reference. `[PLACEHOLDER — LAWYER REVIEW REQUIRED]`

---

## Appendix C — Open items requiring a decision

1. **Keygen Std 1 pricing** and **Keygen EE self-host pricing** — REQUIRES VERIFICATION from keygen.sh directly.
   Only the Std 2 figure ($299/mo at 10,000 ALUs) and the free Dev tier (100 ALUs) were confirmable.
2. **All LicenseSpring pricing** — REQUIRES VERIFICATION; the vendor publishes none.
3. **250 vs 240 USDT** — the client enforces a 250 USDT minimum while prior wallet monitoring used ≥240 USDT.
   Pick one deliberately.
4. **P1/P2 retirement status** — confirm neither retired product is still distributed before deleting the shared
   Gist, or their clients will hard-fail.
5. **Code-signing certificate type** — OV (cheaper, reputation accrues over time) vs EV (immediate SmartScreen
   trust). Recommendation: EV, given the sales channel.
6. **Whether to keep any crypto auto-approval at all.** Manual activation at current volume is a few minutes per
   sale and removes an entire class of risk. Recommendation: manual until volume justifies otherwise, then
   server-side verification only.
7. **Hosting region** — has privacy and latency implications. `[PLACEHOLDER — LAWYER REVIEW REQUIRED]`

---

## Appendix D — Pending work explicitly not performed by this task

- **The exe rebuild remains pending.** `dist/StopLossPro.exe` (2026-08-04 23:44) predates the log-lockdown and
  formula-hiding source fixes and does not contain them. That rebuild is tracked as a separate, currently-paused
  task and was deliberately not performed here.
- No application code (`.py`, `.kv`, `.bat`, config) was modified in producing this document.
- No credentials were rotated, no infrastructure was deleted, and no migration was performed.
