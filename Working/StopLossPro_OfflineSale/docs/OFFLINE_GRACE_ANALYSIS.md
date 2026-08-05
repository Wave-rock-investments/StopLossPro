# Offline Grace Window — Behavior Analysis & Recommendation

**Status: DECIDED AND APPLIED, 2026-08-05.** Per the recommendation in §3 below,
`STOPLOSS_OFFLINE_GRACE_SECONDS` / `DEFAULT_GRACE` were changed from 72h (259200s)
to **24h (86400s)** in `app/config.py`, `lib/licensing.py`, and `.env.example`,
with a pinned regression test (`test_offline_grace_is_24_hours`) so the value
can't silently drift back. The analysis below is left as originally written —
it's the record of *why* 24h, not just a historical note.

**Where the value lives:** server `app/config.py` `OFFLINE_GRACE_SECONDS`
(sent to the client on every login/heartbeat as `offline_grace_seconds`,
so the client always uses the server's current value, not a hardcoded
one) and client `lib/licensing.py` `DEFAULT_GRACE` (used only until the
first successful server response overwrites it).

---

## 1. What the code actually does (traced, not assumed)

Relevant functions: `LicensingProvider._heartbeat_once`, `_enter_grace`,
`login`, `_save_state`/`_load_state` in `lib/licensing.py`; `heartbeat()` in
`backend/app/services.py`.

Two states are tracked per local install: `last_ok` (wall-clock time of the
last server response that said yes) and `clock_hwm` (a monotonic high-water
mark — the highest wall-clock time ever observed locally). Both are DPAPI-
sealed on disk, not plaintext.

### Scenario 1 — Valid authorization, everything normal
Heartbeat every `HEARTBEAT_INTERVAL_SECONDS` (90s default). Each success
refreshes `last_ok`, `clock_hwm`, and the cached signed grant. `authorised
=True, reason="OK"`.

### Scenario 2 — Customer loses internet
`_post()` raises `ConnectionError` → `_enter_grace()` runs. This is the
**only** path that distinguishes "can't reach the server" from "server said
no" — that distinction is the core design principle of this module, and it
only applies once you've entered this branch. Elapsed = `now - last_ok`;
remaining = `grace - elapsed`. If `remaining > 0`, the app stays fully
functional (`authorised=True, reason="OFFLINE_GRACE"`), showing "Working
offline. Xh of grace remaining."

### Scenario 3 — Administrator revokes licence while customer is offline
**This is the important gap, and it is structural, not a bug.** There is no
push channel (deliberately — the old ntfy/Worker push paths were the
security incident). Revocation only changes a database row. The client has
zero way to learn about it until its *next successful* heartbeat.

Consequence: if a customer is offline when revoked, they keep full,
unmodified access to the app — including the risk engine — for however long
they remain offline, **up to the full grace window**, before the next
successful heartbeat finally delivers the revocation. The 72h value is not
just "how long a legitimate customer can work offline" — it is also **the
worst-case window an admin's revocation can be silently ignored for.**
Those are the same number by construction. This is the central tradeoff
Step 1.2 exists to make explicit.

Once connectivity returns and a heartbeat succeeds, revocation is
immediate and authoritative (server-said-no path: `_clear_state()` runs,
`authorised=False`, cached credentials wiped) — there is no further grace
on the "reachable but revoked" path. Grace only ever applies to
unreachability, never to an explicit no.

### Scenario 4 — Offline duration: 1h / 6h / 12h / 24h / 72h
At every point up to 259200s elapsed, behavior is identical: fully
authorised, offline banner with a shrinking countdown, entitlements held
from the last cached grant (see note below on grant TTL). At exactly 72h
elapsed: `remaining <= 0` → `authorised=False, reason="OFFLINE_GRACE_
EXPIRED"`, app locks with "Offline period exceeded. Connect to the internet
to continue." The local state file is **not** deleted at this point (only
explicit server revocation or logout clears it) — so if connectivity
returns even after expiry, the next heartbeat attempt reuses the same
session token, and if the server-side session was never revoked or reaped,
re-authorization is immediate and transparent to the customer.

Note on grant TTL vs. grace: the signed grant itself is short-lived
(`GRANT_TTL_SECONDS=180s`) — that is a replay/theft control, unrelated to
offline tolerance. `_enter_grace()` explicitly tolerates a locally-expired
cached grant ("normal, they are short-lived") and continues authorising
purely on the grace-window arithmetic. Offline duration is governed only by
wall-clock elapsed time since `last_ok`, never by grant expiry.

### Scenario 5 — System clock moved backward
`_enter_grace()` checks `now < hwm` **first, before anything else**. Any
backward movement at all — one second or one year — immediately ends grace:
`reason="CLOCK_ROLLBACK", grace_remaining=0`, regardless of true elapsed
offline time. This is deliberately conservative: it closes the obvious
attack (wind the clock back to stay inside a grace window forever) at the
cost of a real false-positive path — a laptop resuming from sleep/hibernate
with a momentarily stale RTC, a DST edge case, or a restored VM snapshot can
all trigger this on an honest customer with zero warning. There is currently
no tolerance band (e.g. "ignore rollbacks under 60s") — this is a known,
accepted usability cost of the current implementation, not an oversight.

### Scenario 6 — System clock moved forward
While online: irrelevant, heartbeats keep succeeding and both `last_ok` and
`clock_hwm` advance with it — no special handling needed.

While offline: a forward jump makes `elapsed` (and therefore the apparent
consumption of the grace window) grow faster than real time, so grace can
appear to expire *early*, never late. `clock_hwm` is also pulled forward to
`max(hwm, now)`. Consequence worth flagging: if the clock is later
*corrected back* to the true time (e.g. NTP resync undoing a bad forward
jump), that correction itself now looks like a backward rollback relative
to the inflated high-water mark, and triggers Scenario 5's lockout. Net
effect: forward clock manipulation can only ever shrink a customer's
effective grace window or force an early reconnect — it is not exploitable
to extend access. The design fails closed in both clock-tamper directions,
which is correct, but the backward-rollback false-positive path (Scenario 5)
is the one with a real customer-facing cost.

### Scenario 7 — Cached state copied/restored to another machine
`state.bin` is sealed with Windows DPAPI (`CryptProtectData`, current-user
scope, no machine-only flag set explicitly — see the residual-risk note
below). On a different Windows user account or a different machine, the
same bytes fail to decrypt: `_dpapi(False, sealed)` returns `None`,
`_load_state()`'s broad `except Exception` catches the resulting error and
returns `{}`. `_heartbeat_once()` then sees no `session_token` and falls
back to `authorised=False, reason="NOT_AUTHENTICATED"` — the app looks
logged out and demands a fresh login. **Copying the file does not grant
access; it produces a clean re-authentication prompt.** The same fail-closed
path handles simple corruption/truncation of the file (any decode exception
→ empty state → re-auth), so a customer who bricks their local cache is
inconvenienced, not locked out permanently, and the failure mode is never a
silent bypass.

Residual risk, stated plainly: DPAPI's guarantee is "tied to this Windows
user's protection key," not "cryptographically impossible to extract under
any circumstance." An attacker with full disk access to the *original*
machine (not a copied file alone) and knowledge of the user's Windows
logon credentials, or access to a domain-joined profile's roaming DPAPI
master key, has a theoretical path to decrypt the blob in place. This is a
limitation of DPAPI as a primitive, not of this implementation, and is the
same tradeoff every Windows credential-manager-based product accepts. It is
not testable from a "copy the file to another PC" experiment (Step 8) and
should not be reported as such — Step 8 proves the file doesn't travel; it
does not prove the machine can't be attacked directly.

---

## 2. Tradeoff of 6h / 12h / 24h / 72h

The number being chosen is simultaneously: (a) how much offline flexibility
a paying, honest customer gets, and (b) the worst-case delay before an
admin's revocation actually takes effect on an offline machine. There is no
value that improves both — every hour added to (a) is an hour added to (b).

Two things shape which end of that tradeoff matters more for *this*
product specifically, not in the abstract:

- **What revocation is actually for here.** There is no automated fraud
  detection or abuse pipeline — revocation is a manual action by a single
  admin (you), triggered by things like a chargeback, a support dispute, or
  suspected credential sharing. It is not defending against an active,
  ongoing attack that needs a fast kill switch; single-session enforcement
  already closes the "many people use one licence simultaneously" case the
  moment any device is online. The offline-revocation gap only matters for
  the narrower case of a revoked customer who *stays offline on purpose* to
  keep using the app — which is possible at any grace length, just bounded
  by it.
- **What offline actually looks like for the customer base.** Retail
  traders on Reddit/Telegram-sourced sales, plausibly trading from mobile
  connections, closing a laptop overnight, or traveling. A locked-out risk
  tool is worst precisely when someone is mid-position and least able to
  deal with a support ticket — and this is a solo-developer product with no
  live support desk to triage false lockouts quickly.

| Grace | Usability | Security (worst-case silent-revocation window) |
|---|---|---|
| **6h** | Fails a large share of ordinary overnight use (laptop closed 8h+ is common and exceeds this). High false-lockout rate for honest customers for no strong offsetting benefit — a determined licence-sharer isn't meaningfully deterred by 6h vs. 24h, they just reconnect periodically like everyone else. | Tightest: ≤6h of continued access after a revocation on an offline machine. |
| **12h** | Covers a single overnight, not a weekend or a short trip. Still a plausible lockout source. | ≤12h exposure. |
| **24h** | Covers a full offline day — a weekend day, a travel day, a bad-ISP day. The natural unit ("reconnect within a day") is easy to put in an error message and easy for a customer to reason about without contacting support. | ≤24h exposure — caps worst case at "one day," a meaningful tightening from 72h. |
| **72h (current)** | Covers a full offline long weekend with zero lockout risk — the best usability of the four. | Loosest: a revoked account can retain full functional access for up to **3 days** after an explicit admin revocation, on a product whose primary enforcement mechanism against abuse *is* manual revocation. |

## 3. Recommendation

**24h.** This is where the curve bends: going from 24h to 72h buys
relatively little additional real-world usability (most genuine offline
gaps — an overnight, a flight, a bad ISP day — fit inside 24h) while nearly
tripling the worst-case window a revocation can be silently ignored.
Going the other direction, from 24h down to 6h or 12h, meaningfully raises
the false-lockout rate for honest offline use on a product with no live
support triage, while barely improving security — revocation here is a
slow, manual, deliberate admin action, not something racing against an
active exploit that a few extra hours meaningfully worsens.

This is a recommendation only. `OFFLINE_GRACE_SECONDS` stays at 259200 (72h)
until you decide. Changing it is a one-line config edit
(`STOPLOSS_OFFLINE_GRACE_SECONDS` server-side); no client code change is
required since the client always adopts whatever value the server last sent.
