# StopLoss Pro — Complete Feature Inventory

**Product:** StopLoss Pro (MT5 edition)
**Client version:** 1.0.0 (`StopLossPro_OfflineSale/version.txt`)
**Repository root:** `StoplossApk-mt5/Working/`
**Document generated:** 2026-08-16 — *read-only audit, no source files were modified*

---

## 0. What the product is

A Windows desktop trading terminal that sits **next to MetaTrader 5** and does the one thing MT5 does badly: turning a volatility reading (ATR) into a fully-sized, risk-controlled trade — and then placing, managing and trailing that trade without the user touching the MT5 order dialog.

It ships as a single signed-shape PyInstaller EXE (`dist/StopLossPro.exe`, ~87 MB) built from a Kivy/KivyMD front end, backed by a FastAPI licensing service that is authoritative over who may run the app.

Two halves make up the system:

| Half | Stack | Role |
|---|---|---|
| **Client** — `StopLossPro_OfflineSale/` | Python 3.10 · Kivy 2.3 · KivyMD 1.2 · MetaTrader5 bridge · Tkinter (auth dialogs) | Risk engine, MT5 execution, position management, UI |
| **Server** — `backend/` | FastAPI · SQLAlchemy 2.0 · Alembic · PostgreSQL (prod) / SQLite (dev) · Argon2id · Ed25519 · TOTP | Accounts, licences, devices, sessions, MFA, consent, admin console, audit |

---

## 1. Risk & position-sizing engine

The mathematical core. Pure Python, zero UI imports, callable from any thread (`lib/calc.py`).

### 1.1 Unified risk model

TP ratios are **fixed by design** — the product deliberately refuses to let users fiddle with reward ratios, which is where most retail risk models fall apart:

```
SL  = ATR × SL-multiplier          (default multiplier 1.50×)
TP1 = SL × 2                       → 1:2 R:R
TP2 = SL × 3                       → 1:3 R:R
TP3 = SL × 4                       → 1:4 R:R
```

### 1.2 Outputs of a single calculation (`calc_setup`)

Returns a `TradeSetup` dataclass carrying:

- **Distances** — `sl_dist`, `tp1_dist`, `tp2_dist`, `tp3_dist`, rounded to the instrument's true decimal precision
- **Cash P&L** — `loss`, `profit1`, `profit2`, `profit3` (= distance × lots × contract size)
- **Blended profit** — equal-weight (⅓ each) average across TP1/TP2/TP3, modelling a scale-out exit
- **Expected Value** — `EV = winRate × blended − (1 − winRate) × loss`, signed and displayed with `+`/`−`
- **Full BUY ladder** — entry, SL, TP1, TP2, TP3 as absolute prices
- **Full SELL ladder** — the mirror image of the above
- **R:R labels** and a timestamp

### 1.3 Auto lot sizing (`calc_auto_lot`)

```
lots = (account_balance × risk_% / 100) ÷ (ATR × SL-multiplier × contract_size)
```

Rejects non-finite inputs, zero/negative balance, zero/negative risk %, and unknown symbols with explicit error messages rather than silently producing a garbage lot size.

### 1.4 Input validation guarantees

Every entry point raises a typed, human-readable error instead of propagating NaN:

- ATR must be finite and > 0
- SL multiplier must be finite and > 0
- Lots must be finite and > 0
- Symbol must resolve to a known contract
- Auto-lot denominator is re-checked for ≤ 0 before division

### 1.5 Cluster lot splitting (`_cluster_lots`)

When required volume exceeds the broker's `volume_max`, the engine splits it into N broker-legal chunks — each ≤ `volume_max`, ≥ `volume_min`, rounded down to `volume_step`, with the remainder folded into the final chunk.

> Example from the source: 10 M capital, 500 k at risk → 350 lots needed, broker max 100 → `[87.5, 87.5, 87.5, 87.5]`.

### 1.6 Order-type recommendation (`recommend_order_type`)

Given side, intended entry, live bid/ask and digit precision, the engine picks the correct MT5 order type automatically, with a pip-scaled tolerance band:

| Side | Condition | Order type |
|---|---|---|
| BUY | entry ≈ ask (within tolerance) | `MARKET_BUY` |
| BUY | entry < ask | `BUY_LIMIT` |
| BUY | entry > ask | `BUY_STOP` |
| SELL | entry ≈ bid (within tolerance) | `MARKET_SELL` |
| SELL | entry > bid | `SELL_LIMIT` |
| SELL | entry < bid | `SELL_STOP` |

---

## 2. Instrument coverage & broker symbol intelligence

### 2.1 Built-in contract table (15 instruments)

| Class | Symbols | Contract size | Decimals |
|---|---|---|---|
| Metals | XAUUSD, XAGUSD | 100 / 5 000 | 2 / 4 |
| Energy | USOIL, UKOIL, NGAS | 1 000 / 1 000 / 10 000 | 2 / 2 / 3 |
| Crypto | BTCUSD, ETHUSD, BNBUSD | 1 | 2 |
| FX majors | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD, USDCHF | 100 000 | 5 (3 for JPY) |

### 2.2 Three-tier symbol resolution (`_base_sym`)

Brokers rename everything. The client resolves any broker symbol to a canonical contract key using a strict priority order:

1. **Live broker map** (`_dynamic_aliases`) — rebuilt from real MT5 data on every connect, always wins
2. **Static alias table** (`_BROKER_ALIASES`) — GOLD→XAUUSD, SILVER→XAGUSD, WTI/CRUDEOIL→USOIL, BRENT→UKOIL, NATGAS/NG→NGAS, BTC/BITCOIN→BTCUSD, ETH, BNB, XAUUSDM→XAUUSD
3. **Suffix stripping** (`_strip_broker_suffix`) — splits on `-`, `.`, `/` and strips trailing `M + # _`

Handles `XAUUSDm`, `XAUUSD-std`, `EURUSD.pro`, `GOLD#` and similar without user configuration.

### 2.3 Broker symbol resolution on connect

- `mt5_resolve_symbols()` queries the terminal for the broker's real names for all 15 instruments
- The instrument dropdown is **rebuilt live** on MT5 connect/disconnect (`rebuild_instrument_menu`)
- A caption under the instrument field shows the resolved broker symbol so the user always knows what will actually be traded (`_update_broker_sym_label`)
- Thread-safe alias swap: `clear()` + `update()` under the GIL, so `_base_sym()` never observes a half-written table

---

## 3. MetaTrader 5 integration (`lib/mt5_api.py`, `lib/mixin_mt5.py`)

### 3.1 Terminal discovery & auto-launch

- Probes standard install paths (`C:\Program Files\MetaTrader 5\terminal64.exe`, and the x86 variant)
- `_mt5_ensure_init()` initialises the bridge and **launches MT5 automatically if it isn't running**, waiting up to 45 s
- Every API call is guarded by a hard watchdog (default 50 s) so a hung terminal can never freeze the UI

### 3.2 Threading model

Every MT5 call runs on a daemon thread and delivers results back to the Kivy main thread via `_deliver()` / `Clock.schedule_once`. The UI never blocks on a broker round-trip. OS thread-exhaustion is caught explicitly and surfaced as an error rather than a silent hang.

### 3.3 Connection lifecycle

- **On by default on first install** — a new customer auto-connects rather than hunting for a toggle; the saved preference governs every later launch
- Manual `CONNECT` / test button with in-flight state guard (`_mt5_testing`)
- Keep-alive ping every **30 s** (`_MT5_PING_INTERVAL`), skipped if a previous ping is still in flight
- Live status label + MT5 BUY/SELL buttons that only appear when connected **and** levels are calculated
- Full teardown of every timer on disconnect

### 3.4 Supported order types (8)

`MARKET_BUY`, `MARKET_SELL`, `BUY_LIMIT`, `SELL_LIMIT`, `BUY_STOP`, `SELL_STOP`, `BUY_STOP_LIMIT`, `SELL_STOP_LIMIT` — each with a plain-English display label.

### 3.5 Pre-flight order safety (the part most tools get wrong)

Before dispatch, the order is re-validated against the **current** live price:

- **Stops-level guard** — if the pending entry falls inside the broker's minimum-distance zone (`trade_stops_level × point`, floored at 2× the live spread), the order is silently converted to a market fill instead of being rejected with "invalid price"
- **Auto-correction of wrong-side pendings** — `SELL_LIMIT`→`SELL_STOP`, `SELL_STOP`→`SELL_LIMIT`, `BUY_LIMIT`→`BUY_STOP`, `BUY_STOP`→`BUY_LIMIT` when the market has moved through the level
- **Pending → market escalation is blocked, not silent** — if the correction would change the risk profile from pending to market, the user is warned and chooses; only pending-to-pending corrections happen silently
- Fresh tick snapshot taken per order; `ORDER_TIME_GTC` + `ORDER_FILLING_IOC`
- A posting lock (`_mt5_posting`) with timestamp prevents double-submission, and is released on *every* exit path including exceptions raised during symbol lookup

### 3.6 Instant cluster execution

For volumes above the broker's max lot:

- One **shared price snapshot** taken before dispatch, so all legs price identically
- All sub-orders submitted **simultaneously** through a `ThreadPoolExecutor` (up to 512 workers)
- `pool.shutdown(wait=False)` — the function returns in under 50 ms; legs complete on background daemon threads
- The UI unlocks on the **first** confirmation; a thread-safe one-shot `Event` guarantees exactly one success or error callback
- Each leg is comment-tagged `[i/N]` for reconciliation in the MT5 journal

### 3.7 Candle & ATR data

- `mt5_fetch_candle()` returns the **last closed** candle plus ATR for the chosen period
- Timeframes: **M1, M5, M15, M30, H1, H4, D1, W1** (validated against a whitelist on load; H1 default)
- Duplicate-candle suppression via `_last_candle_time` keyed per symbol+timeframe

---

## 4. Live trading workflow (`lib/mixin_trading.py`, `lib/mixin_orders.py`)

### 4.1 FETCH — one-tap market snapshot

Pulls the last closed candle and pushes ATR, entry price and lots into the calculator, then auto-calculates. Protected by a UI-level timeout, an in-flight guard, and an always-runs `_fetch_done()` reset so the button can never stick.

### 4.2 Candle-close auto-fetch

Rather than polling on a dumb interval, the client computes the **exact seconds until the next candle close** for the active timeframe and arms a single timer; on fire it fetches and re-arms for the following candle. ATR, entry and lots refresh silently, without stealing focus or resetting the user's input.

### 4.3 The order popup

A single unified dialog covering the whole decision:

- Header with symbol, entry, SL and lot size
- **Live bid/ask context** plus a written reason why the order type was auto-selected
- Two plain-English execution buttons — *fill now at market* vs. *place pending* — with a dynamic hint line
- **Take-Profit selector**: `No TP`, `TP1 (1:2)`, `TP2 (1:3)`, `TP3 (1:4)` — each button showing the absolute price, the cash profit and the ratio
- A `PLACE ORDER` button whose label updates live to state exactly what is about to happen: `PLACE ORDER · TP2: 2412.50 +$1,240`

### 4.4 Position Manager

A dedicated tab built on a `RecycleView` (only visible cards are rendered) with per-position controls:

| Control | Behaviour |
|---|---|
| **CLOSE** | Full close with confirmation dialog |
| **PARTIAL** | Volume-entry dialog for partial close |
| **B/E** | One-tap move SL to entry (break-even) |
| **MODIFY** | Edit SL and TP together in one dialog |
| **TRAIL** | Per-position ATR trailing-stop toggle |

Plus:

- **CLOSE ALL** — confirmed, then executed in parallel with a closed/failed/total result report (`mt5_close_all_bulk`)
- **Account summary bar** — Balance, Equity, floating P&L, position count
- **ATR trail configuration bar** — period (default 14) and multiplier (default 1.5) applied to newly-armed trails

### 4.5 ATR trailing stop

Runs on a 30 s tick. For every armed ticket it re-fetches ATR, computes the new stop, and only issues `mt5_modify_sl_tp` when the level actually improves — direction-aware, per-position multiplier and period, with candle-time deduplication so one candle never triggers two modifications.

### 4.6 Refresh architecture (four independent clocks)

| Clock | Interval | Purpose |
|---|---|---|
| Fast price tick | 2 s | Symbol ticks only — mutates *only* the NOW-price label on visible cards, via weak refs. No `positions_get`, no widget rebuild |
| Positions refresh | periodic | Full position list — **only when the Positions tab is actually visible** |
| Account refresh | 30 s | Balance/equity in the background |
| ATR trail tick | 30 s | Trailing-stop evaluation |

The positions list uses **diff-based updates** (the MT5 "dirty cell" principle) — the RecycleView data list is patched, not rebuilt, so scroll position and card state survive refreshes.

---

## 5. Input system — the custom numpad (`lib/mixin_numpad.py`)

Built because mobile/desktop IME behaviour is unreliable for fast numeric entry under a trading clock.

- **Explicit state machine**: `IDLE → EDITING → COMMITTING → IDLE`, with an owner ID (`_numpad_owner`) so a commit can never land in the wrong field
- **Single source of truth** — an internal buffer, synced to both the field text and the large display label on every keypress
- **30 ms throttle** against button bounce and key spam; global lock blocks all keys during calculation except DONE
- **18-character cap** to prevent float overflow
- **Centralised teardown** (`_end_input_session`) called from every exit path — DONE, tab switch, back key, focus loss
- **Back-cancel** restores the field's previous value
- **Live risk preview** — a full risk estimate is recomputed and displayed on every relevant keystroke, before the user commits
- **Silent lot recalculation** from current ATR / account / risk % as values change
- **Slide-open / collapse animation** with row heights collapsed to 0 (so the panel cannot intercept touches when hidden), plus an animation-in-flight guard
- **Haptic feedback** — 10 ms vibration per keypress on Android, with the Vibrator service resolved once and cached
- **IME suppression** — the system keyboard is force-defocused on touch-down for numpad fields, and window soft-input mode is restored for fields that do need the real keyboard
- IME and Window references are **pre-warmed at t=2 s** after launch so the first tap is not slow

---

## 6. History & session statistics (`lib/mixin_history.py`)

- **Automatic capture** — every successful calculation is appended (up to `MAX_HIST = 30` entries)
- **Schema-validated rendering** — every field is passed through safe float/string sanitisers before display, so a corrupt store entry degrades to a placeholder rather than crashing the list
- **Cache-guarded rebuilds** — the list only rebuilds when the underlying data actually changed
- **Batched card creation** (8 per batch) with a cancellation token, so clearing history mid-render doesn't leave orphan batches running
- **Expandable cards** — tap to reveal the full BUY/SELL price ladder; animated collapse
- Card shows symbol + timestamp, ATR / lots / entry, loss + TP1/TP2/TP3, and R:R + EV
- **CLEAR ALL** with an empty-state label
- **Monthly win/loss statistics** — in-memory counters with disk as backup only, with automatic month-rollover detection keyed `YYYY_MM`

---

## 7. UI, theming & platform behaviour

### 7.1 Four-tab navigation

**Calculate · History · Settings · Positions**, with the top-bar title always reflecting the active mode.

### 7.2 Calculate tab layout

`INSTRUMENT` (searchable dropdown + broker symbol caption) → `TRADE INPUTS` (ATR, Lots, optional Entry) → `CALCULATE` / `FETCH` / timeframe selector → `AUTO LOT SIZE` (Account, Risk %) → `RISK ESTIMATE` (distance + P&L grid for SL/TP1/TP2/TP3, Blended, Expected Value) → expandable `BUY LEVELS` / `SELL LEVELS` panels with a *Move SL to* break-even row → `BUY via MT5` / `SELL via MT5` → `RESET ALL`.

### 7.3 Settings tab

- **SL Multiplier** slider (ATR ×, default 1.50×)
- **TP Ratios** — displayed as `FIXED` (unified risk model, deliberately not user-editable)
- **Win Rate** slider (default 50%) feeding the EV calculation
- **MT5 Direct Orders** — enable toggle, connection status, `CONNECT` button, explanatory caption
- **RESET TO DEFAULTS**
- Settings are loaded as plain Python attributes first and only applied to widgets when the tab is first visited, so slider `on_value` events can't fire against unregistered widget IDs during startup

### 7.4 Theming

- Full **Dark / Light** toggle with persistence, applying every hardcoded colour across cards, numpad, position cards and history in one pass (`_apply_full_theme`)
- Custom canvas-drawn **iOS-style switch** widget (`SwitchToggle`) with animated pill track and thumb

### 7.5 Stale-result protection

Any change to an input marks the displayed risk estimate as **stale** (`_mark_stale` / `_clear_stale`), so a user can never place an order against numbers that no longer match the fields on screen.

### 7.6 Status & feedback

- Colour-coded status line (`success` / `error` / `secondary`)
- Snackbar notifications on a pre-imported fast path
- Auto-scroll to the RISK ESTIMATE block after a calculation
- Copy-to-clipboard icon on every individual price row

### 7.7 Android / mobile lifecycle support

Retained from the shared codebase and still wired:

- `on_pause` keeps the process alive in background; `on_resume` re-syncs the UI
- `on_low_memory` sheds caches under memory pressure
- `on_stop` flushes state to disk before process death
- **Layered Android back-button logic** — dismiss dialog → collapse numpad → collapse levels → exit confirmation on double-press
- Keep-screen-on, Android Keystore AES-256-GCM helpers (`_keystore_encrypt` / `_keystore_decrypt`) that fail open to plaintext rather than crashing
- Named layout constants with a 48 dp minimum touch target (`_KV_MIN_TOUCH`)

### 7.8 Startup

Splash screen, icon resolution across build layouts, and a **single deferred `_startup_complete`** task that batches all post-load initialisation into one frame rather than scattering timers.

---

## 8. Licensing & activation — client side (`lib/licensing.py`, `lib/activation.py`)

The client is deliberately built so that **it cannot grant itself anything**. The server is authoritative.

### 8.1 Signed grants (Ed25519)

- Server issues short-lived signed authorization grants (**180 s TTL**)
- The client ships only the **public** verification key, pinned at build time (`SERVER_PUBLIC_KEY_B64`, key id `k1`)
- `verify_grant()` validates the signature locally before trusting any entitlement claim
- `GET /api/v1/pubkey` exposes the current key for rotation

### 8.2 Device identity

Each installation generates an **Ed25519 keypair** at enrolment (`get_or_create_device_keypair`). The keypair is the device identity — hardware fingerprints are recorded only as a weak risk signal, never as the trust anchor, precisely because customers legitimately replace SSDs, NICs and motherboards.

### 8.3 Sealed local state

Session state is written through **Windows DPAPI** (`CryptProtectData`) to `~/.stoplosspro/state.bin` — never as plaintext, never in a user-editable file.

### 8.4 Heartbeat & authoritative revocation

- Background daemon thread heartbeats every **90 s** (interval server-controlled)
- **HTTP 200** → new grant, state saved, entitlements refreshed
- **Server says no** → state cleared and the app locks *immediately*, no grace, with the server's reason code and message shown
- **Unreachable** → treated as offline, *not* as revoked — a distinction the code calls out explicitly

### 8.5 Bounded offline grace with clock-rollback defence

- **24 h** offline tolerance (server-controlled, reduced from 72 h — see `docs/OFFLINE_GRACE_ANALYSIS.md`)
- A **monotonic high-water mark** is persisted; if the system clock ever moves backwards relative to it, grace is treated as **exhausted rather than extended** — rewinding the clock is refused, not rewarded
- Inside the window the app keeps working on the last verified grant and shows remaining hours; an expired cached grant is normal and doesn't end the session, the *window* does
- On expiry: `OFFLINE_GRACE_EXPIRED` with a clear reconnect instruction

### 8.6 Live admin actions reach open sessions

An admin suspend/revoke propagates to an **already-running** app instance — the heartbeat thread fires `_on_licence_state_change`, which marshals onto the UI thread and forces sign-out (`_force_signed_out`). The background thread never touches Kivy or Tk widgets directly.

### 8.7 Activation dialogs (Tkinter)

- **Registration dialog** — self-serve signup, lands in `PENDING` until an admin approves
- **Consent dialog** — scrollable, mouse-wheel-bound, listing every outstanding required document
- **Activation blocker** — login gate shown when no valid session exists
- **Silent session resume** — re-authenticates from persisted sealed state with no password prompt
- Enter-key submit and centred, height-corrected dialogs

### 8.8 Entitlements

Licences carry a comma-separated entitlement list (default `risk_engine`) checked client-side via `has_entitlement()`. The provider sits behind a deliberately narrow interface so the whole licensing backend could be swapped for a hosted vendor without touching the risk engine.

---

## 9. Licensing backend — API (`backend/app/api.py`)

Base prefix `/api/v1`.

| Endpoint | Method | Purpose |
|---|---|---|
| `/auth/register` | POST | Self-serve signup → account in `PENDING` |
| `/auth/login` | POST | Password + optional MFA + device enrolment → session token |
| `/session/heartbeat` | POST | Returns a fresh signed grant, heartbeat interval and grace window |
| `/session/logout` | POST | Ends the session server-side |
| `/mfa/enrol` | POST | Begin TOTP enrolment (secret + provisioning URI) |
| `/mfa/confirm` | POST | Confirm TOTP, returns recovery codes |
| `/consent/required` | GET | Outstanding documents for an email |
| `/consent/accept` | POST | Record acceptance of a document version |
| `/pubkey` | GET | Current Ed25519 public verification key |
| `/health` | GET | Liveness — deliberately reveals nothing |
| `/health/ready` | GET | Readiness — verifies the DB is actually reachable |

### 9.1 Rate limiting (per-IP **and** per-identity)

| Action | IP limit | Identity limit |
|---|---|---|
| Register | 5 / hour | 3 / hour per email |
| Login | 10 / 5 min | 8 / 5 min per email |
| MFA | 10 / 5 min | — |
| Admin login | 10 / 5 min | 8 / 5 min per email |
| Admin bootstrap | 10 / 5 min | — |

---

## 10. Backend data model (`backend/app/models.py`)

Eight tables, all UUID-keyed, all timezone-aware, all with cascade rules.

| Table | Contents |
|---|---|
| `users` | email, full name, Argon2id hash, status, failed-login count, lockout timestamp, last login |
| `licences` | product, plan, status, `max_concurrent_sessions`, entitlements, activation/expiry, activation note |
| `devices` | keypair-based identity, status, hardware fingerprint as a **weak signal only** |
| `sessions` | hashed session token, status, activity timestamps |
| `mfa_credentials` | encrypted TOTP secret, last-used step (replay defence) |
| `recovery_codes` | hashed single-use codes |
| `consent_records` | which document version each user accepted, and when |
| `audit_events` | every security-relevant action |

### 10.1 State machines

- **AccountStatus** — `PENDING → ACTIVE → SUSPENDED / REVOKED / CLOSED`
- **LicenceStatus** — `PENDING → ACTIVE → EXPIRED / SUSPENDED / REVOKED`
- **DeviceStatus** — `ACTIVE / REVOKED / REMOVED`
- **SessionStatus** — `ACTIVE / EXPIRED / REVOKED / LOGGED_OUT`
- **ConsentDocument** — `TERMS_OF_SERVICE / RISK_DISCLOSURE / PRIVACY_NOTICE`

### 10.2 Data minimisation by design

The `activation_note` field is free text (cash / crypto / manual) and **deliberately not** a wallet address or transaction hash — the schema comment states plainly that the business keeps no more payment data than it genuinely needs.

---

## 11. Backend security (`backend/app/security.py`, `services.py`)

### 11.1 Credentials

- **Argon2id** password hashing with automatic `needs_rehash()` upgrade on login
- Weak-password rejection at registration
- Account lockout after **5** failed logins for **15 minutes**

### 11.2 MFA

- **TOTP** with encrypted-at-rest secrets (Fernet)
- **Replay defence** — the last-used time step is recorded and refused on re-use
- **10 single-use recovery codes**, hashed, normalised on entry
- Admin-triggered MFA reset with audit trail

### 11.3 Key separation (a deliberate architectural decision)

The Ed25519 **signing key** and the TOTP **encryption key** are independent secrets, and production boot **refuses to start** if they are identical. Rationale is documented in-source: rotating the signing key after a suspected leak must not force every customer's TOTP secret to be re-encrypted, and vice versa. A dedicated test suite (`test_phase16_key_separation.py`) enforces this.

### 11.4 One-active-session guarantee

Enforced with `SELECT ... FOR UPDATE` row locking (`_lock_user_row`), plus stale-session reaping (`reap_stale_sessions`) against a **15-minute idle timeout**. Session tokens are stored **hashed**, never raw.

### 11.5 Production boot guardrails (`main.py::_startup_guard`)

Production refuses to start if any of the following is true:

- `DATABASE_URL` is SQLite — SQLite cannot enforce `SELECT ... FOR UPDATE`, so the one-session guarantee would be unsafe under concurrent logins
- `DEBUG` is enabled
- Signing private/public key is unset
- TOTP encryption key is unset, or identical to the signing key

Interactive API docs (`/docs`, `/openapi.json`) are disabled in production as an information-disclosure surface. CORS is deny-by-default (empty origin list = nothing allowed).

### 11.6 Typed error taxonomy

`InvalidCredentials`, `AccountLocked`, `AccountNotActive`, `LicenceProblem`, `SessionActiveElsewhere`, `MfaRequired`, `MfaInvalid`, `DeviceRevoked`, `SessionInvalid`, `ConsentRequired`, `EmailAlreadyRegistered`, `WeakPassword` — each mapping to a stable client-facing code.

---

## 12. Admin console (`backend/app/admin.py`)

A server-rendered HTML console with its own auth, roles (`AdminRole`) and audit trail — no separate SPA, no extra attack surface.

| Capability | Route |
|---|---|
| Admin login / logout | `/admin/login`, `/admin/logout` |
| Customer list with search | `/admin` |
| Customer detail | `/admin/customer/{id}` |
| Create customer directly | `/admin/customer/create` |
| **Approve signup** (with licence duration, default 365 days) | `/admin/signup/{id}/approve` |
| **Reject signup** | `/admin/signup/{id}/reject` |
| Licence actions (activate / suspend / revoke / extend) | `/admin/licence/{id}/action` |
| **Force logout** — kills live sessions instantly | `/admin/user/{id}/force-logout` |
| Reset a customer's MFA | `/admin/user/{id}/reset-mfa` |
| Revoke a device | `/admin/device/{id}/revoke` |
| Audit log viewer | `/admin/audit` |

### 12.1 First-admin HTTP bootstrap

Free-tier hosts often have no shell, so the interactive CLI (`python -m app.bootstrap_admin`) can't run. A doubly-gated HTTP route exists instead:

- Disabled by default — with no `ADMIN_BOOTSTRAP_TOKEN` set, the routes **404**
- Refuses to act once a single `AdminUser` row exists, matching the CLI behaviour
- Two-step flow with an **HMAC-signed pending payload** (`_sign_pending` / `_verify_pending`) so step 2 cannot be forged
- Rate-limited, and intended to be unset immediately after use

### 12.2 Output safety

All rendered values pass through an HTML escaper (`_h`), and a shared `_page()` shell keeps markup consistent.

---

## 13. Auto-update system (`lib/updater.py`)

Six documented capabilities:

1. **Silent background version check** on every startup
2. **Background download** with real byte-level progress percentage
3. **In-app progress notification** that never blocks the UI
4. **"Restart & Update" dialog** when the download completes
5. **Atomic apply** via an external `apply_update.bat` that runs *after* the app exits
6. **Auto-backup of the previous version** for rollback

Plus: semantic version comparison (`_parse_ver` / `_is_newer`), ZIP validity check, **optional SHA-256 integrity verification** against the manifest, an explicit `UpdateState` machine, all callbacks marshalled to the Kivy main thread, and errors that are logged silently rather than crashing the app.

---

## 14. Build, release & operations

### 14.1 Client build

- PyInstaller one-file build (`StopLossPro.spec`, `build.bat`) → `dist/StopLossPro.exe`
- Windows version resource (`version_info.txt`) and app icon
- `numpy` explicitly declared because it is a MetaTrader5 dependency PyInstaller would otherwise miss — the root cause of a previously-shipped "Disconnected" bug
- Kivy Windows graphics backends (`angle`, `glew`, `sdl2`) conditionally required on `win32` only
- `launch.bat` installs requirements and runs for non-frozen use
- **Production log lockdown** at the top of the entry point

### 14.2 Release verification

`verify_release.py` — an automated pre-release gate. `DATA_INVENTORY.md` documents what data the product holds.

### 14.3 Encrypted diagnostics

`encrypt_logs.py` / `decrypt_log.py` (+ `run_encrypt_logs.bat`) — customer logs can be encrypted before transmission and decrypted only by the operator.

### 14.4 Backend operations

- **Alembic migrations** — `9ad2a56f4b99` (core schema) and `7aa03f28bee7` (token hash, entitlements, admin)
- `net_verify.py` connectivity checker
- `deploy_clean.ps1` / `.bat`, `push_to_github.bat`
- `DEPLOYMENT.md`, `.env.example`

### 14.5 Test suites

**Backend (`backend/tests/`):**

- `test_phase1_foundation.py` — schema and foundation
- `test_phase14_security.py` — security controls
- `test_phase16_key_separation.py` — signing vs. TOTP key independence
- `test_phase17_registration.py` — self-serve registration flow
- `test_phase5_boot_guardrails.py` — production boot refusal conditions
- `test_phase6_admin_security.py` — admin console security
- `test_postgres_production.py` — real-PostgreSQL concurrency, proving the one-session guarantee
- `test_admin_bootstrap_http.py` — HTTP bootstrap gating

**Client (`StopLossPro_OfflineSale/tests/`):**

- `test_risk_engine_baseline.py` — risk-engine regression baseline

### 14.6 Legal & compliance documents

Versioned and consent-tracked: `TERMS_OF_SERVICE_v1.0.md`, `RISK_DISCLOSURE_v1.0.md`, `PRIVACY_NOTICE_v1.0.md`. Supporting analyses live in `docs/`: `SECURITY_MVP_ARCHITECTURE.md` (~102 KB), `LEGAL_REVIEW_BRIEF.md`, `OFFLINE_GRACE_ANALYSIS.md`, `RC_MANUAL_VALIDATION_CHECKLIST.md`, `RELEASE_READINESS_REPORT_2026-08-05.md`, `CREDENTIAL_INCIDENT.md`.

---

## 15. Architecture notes

### 15.1 Client composition

`Root` is composed from eight mixins in a deliberate MRO — `LifecycleMixin`, `SettingsMixin`, `HistoryMixin`, `NumpadMixin`, `CalculatorMixin`, `MT5Mixin`, `TradingMixin`, `OrdersMixin` — over `MDBoxLayout`. `constants.py` imports nothing from the project, so circular-import risk is structurally zero.

### 15.2 Performance engineering

- `dp()` values cached lazily (`_DP`) after window init
- Module-level caches for the Android Vibrator, InputMethodManager and Window, shared across instances
- Weak references from ticket → position card, so fast ticks mutate labels directly with no lookup and no leak
- Diff-based RecycleView updates; batched, cancellable history rendering
- Snackbar imported once at module level for the fast path
- Settings loaded as plain attributes before any widget access

### 15.3 Retired subsystems (explicitly documented in-source)

The `constants.py` header records exactly what was deleted and why — public `ntfy` topics that broadcast customer MT5 login, balance, equity, open positions and GPS to a world-readable URL and accepted APPROVED/REVOKE from anyone who knew the topic name; a Gist-based client-side allowlist; a shared Gist session file; a Cloudflare Worker that authenticated writes on a User-Agent prefix; and plaintext user-writable licence/GPS caches. The Cloudflare Worker carries its own `DECOMMISSIONED.md`.

Keeping this record in the source is itself a feature: it prevents a future contributor from re-introducing a removed insecure path.

---

## Appendix A — Defaults reference

| Setting | Default | Source |
|---|---|---|
| SL multiplier | 1.50× ATR | `DEFAULT_SL` |
| Win rate (EV) | 50% | `DEFAULT_WR` |
| Max lot | 100.0 | `MAX_LOT` |
| History depth | 30 entries | `MAX_HIST` |
| ATR timeframe | H1 | `_atr_timeframe` |
| ATR trail period / multiplier | 14 / 1.5 | `trail_period_field`, `trail_mult_field` |
| MT5 ping interval | 30 s | `_MT5_PING_INTERVAL` |
| MT5 enabled on first run | Yes | `_mt5_enabled` |
| Fast price tick | 2 s | `_fast_price_tick_ev` |
| Theme | Dark | `_theme_style` |
| Numpad throttle / input cap | 30 ms / 18 chars | `_last_key_ts`, `_MAX_INPUT_LEN` |
| Grant TTL | 180 s | `GRANT_TTL_SECONDS` |
| Heartbeat interval | 90 s | `HEARTBEAT_INTERVAL_SECONDS` |
| Offline grace | 24 h | `OFFLINE_GRACE_SECONDS` |
| Session idle timeout | 15 min | `SESSION_IDLE_TIMEOUT_SECONDS` |
| Max failed logins / lockout | 5 / 15 min | `MAX_FAILED_LOGINS`, `LOCKOUT_SECONDS` |
| Recovery codes | 10 | `RECOVERY_CODE_COUNT` |
| Max concurrent sessions | 1 | `Licence.max_concurrent_sessions` |
| Default entitlements | `risk_engine` | `Licence.entitlements` |
| Signing key id | `k1` | `SIGNING_KEY_ID` |

## Appendix B — Source map

```
Working/
├── StopLossPro_OfflineSale/          # Windows client
│   ├── Product Sell.py               # entry point, Root composition, app lifecycle
│   ├── StopLossPro.spec / build.bat  # PyInstaller build
│   ├── verify_release.py             # pre-release gate
│   ├── lib/
│   │   ├── constants.py              # contracts, decimals, aliases, cluster split
│   │   ├── calc.py                   # risk engine (pure, thread-safe)
│   │   ├── mt5_api.py                # MT5 bridge — orders, candles, positions
│   │   ├── licensing.py              # grants, heartbeat, DPAPI state, grace
│   │   ├── activation.py             # Tkinter auth / consent / registration
│   │   ├── updater.py                # auto-update pipeline
│   │   ├── widgets.py                # SwitchToggle, PositionCardRV
│   │   ├── layout.kv                 # full UI definition (~78 KB)
│   │   └── mixin_*.py                # lifecycle, settings, history, numpad,
│   │                                 #   calculator, mt5, trading, orders
│   ├── docs/                         # security, legal, release documents
│   └── tests/test_risk_engine_baseline.py
└── backend/                          # FastAPI licensing service
    ├── app/{main,api,admin,services,models,security,keygen,config,
    │        database,bootstrap_admin}.py
    ├── alembic/versions/             # 2 migrations
    ├── legal/                        # ToS, Risk Disclosure, Privacy Notice v1.0
    └── tests/                        # 8 backend suites
```
