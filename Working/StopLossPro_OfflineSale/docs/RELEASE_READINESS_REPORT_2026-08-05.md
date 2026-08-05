# STOPLOSSPRO RELEASE READINESS REPORT — 2026-08-05

Covers Steps 1–15 of the Production Validation phase. Steps 7, 8, 10–13 could not be executed inside
this environment (no Windows hardware, no deployed backend, no purchased code-signing certificate) —
those are reported as PENDING USER with exact procedures handed off, not guessed at or assumed passing.

---

## PASS/FAIL GRID

| Item | Result |
|---|---|
| SECURITY MVP | **PASS** — key-domain separation implemented & tested; offline-grace behavior fully documented and analyzed |
| POSTGRESQL | **PASS** — real PostgreSQL server, real `alembic upgrade head`, 25/25 tests including concurrency |
| SINGLE SESSION | **PASS** — proven on SQLite AND real Postgres, up to 8-way concurrent contention, 15 repeated stress rounds |
| MFA/TOTP | **PASS** — enrollment, replay protection, wrong-code rejection tested for both customer and admin |
| DEVICE TAKEOVER | **PASS** — correct revoke-then-activate sequence proven on SQLite and real Postgres |
| SIGNED GRANTS | **PASS** — tamper-evident, forged-keypair rejection proven, `kid` present for rotation |
| REVOCATION | **PASS** — licence/device/account revocation all tested and audited; propagation timing is heartbeat-bound by design (documented, not a defect) |
| OFFLINE GRACE | **ANALYZED, NOT YET DECIDED** — all 7 required scenarios documented; 24h recommended over current 72h; value intentionally left unchanged pending your decision |
| DPAPI CROSS-MACHINE | **PENDING USER** — requires two real Windows machines; exact procedure in `RC_MANUAL_VALIDATION_CHECKLIST.md` |
| ADMIN SECURITY | **PASS** — 23 adversarial tests; found and fixed a real gap (admin login had zero rate limiting) before marking pass |
| PRIVACY | **PASS, WITH FIXES APPLIED** — found and fixed two real gaps (see §1 below) before marking pass |
| SECRET SCAN | **PASS** (current shipping codebase) — see LEGACY INCIDENT below for a separate, still-open item |
| RISK ENGINE 65/65 | **PASS** — unchanged, no golden values modified |
| BACKEND TESTS | **PASS** — 108/108 (44 pre-existing + 64 added this pass) |
| VERIFY_RELEASE | **FAIL — one expected, structural failure only**: `SERVER_PUBLIC_KEY_B64` empty. Cannot pass until a real backend exists to pull the key from. All 6 other checks pass. |
| WINDOWS E2E | **PENDING USER** — no Windows hardware available here; full click-through checklist provided |
| AUTHENTICODE | **PENDING USER** — needs a purchased certificate and a built RC binary, neither exists yet |
| LEGAL | **PENDING REVIEW** — brief delivered to counsel-ready state; placeholders remain by design |
| LEGACY INCIDENT | **OPEN — confirmed, not assumed.** Independently re-verified today: `p1_admin.html` is still live at `wave-rock-investments.github.io/stoploss-site/p1_admin.html`; git `main`/`origin/main` still contain the leaked-PAT commit. Both need your GitHub access. |

---

## 1. What changed this pass (beyond verification)

Three real defects were found and fixed while executing the checks above — not new features, fixes
required to make the checks honestly pass:

- **MFA/signing key coupling removed** (Step 1.1): TOTP secrets were encrypted with a key derived
  from the Ed25519 signing key. Split into an independent `STOPLOSS_TOTP_ENCRYPTION_KEY_B64`, with a
  versioned decrypt path so already-encrypted secrets keep working. 7 new tests prove the two domains
  rotate independently.
- **Admin login had no rate limiting** (Step 6): password and TOTP brute force were both unlimited on
  `/admin/login`, despite the customer-facing login already having it. Now uses the same limiter.
- **Client leaked the real Windows hostname** (Step 14): `device_name` was populated from
  `socket.gethostname()`, contradicting `DATA_INVENTORY.md`'s explicit "hostname NOT collected"
  promise (Windows hostnames often embed the owner's name). Replaced with a non-identifying label
  derived from the device's own public key. `verify_release.py` now scans for this pattern so it can't
  silently come back.
- **Undocumented second copy of IP logs** (Step 14): the deployment runbook ran uvicorn without
  `--no-access-log`, so IPs were captured a second time outside the one documented, retention-scoped
  location (`audit_events`). Fixed in `DEPLOYMENT.md`.

## 2. Exact remaining blockers

1. `SERVER_PUBLIC_KEY_B64` empty in the client — blocks `verify_release.py` and therefore any build.
   Resolves automatically once a real backend is deployed (see prerequisite section of
   `RC_MANUAL_VALIDATION_CHECKLIST.md`).
2. No RC binary exists — cannot exist until (1) is resolved.
3. No Authenticode signature — needs (2) plus a purchased certificate.
4. Legacy incident still open — see below.
5. Offline grace value undecided — 72h vs. 24h, your call, analysis in `OFFLINE_GRACE_ANALYSIS.md`.
6. Legal docs still placeholders — brief is ready for counsel, nothing binding exists yet.

## 3. Exact manual actions required from you

1. Delete `p1_admin.html` (or take down the whole `stoploss-site` Pages deployment) — **confirmed
   still live** as of today via a direct fetch, not assumed.
2. Decide and execute the git remote cleanup in `CREDENTIAL_INCIDENT.md` §5 (delete+recreate
   recommended) — `main`/`origin/main` still contain the leaked-PAT commit.
3. Provision the real production backend (Postgres host, deploy, `keygen`, `bootstrap_admin`) —
   step-by-step in `DEPLOYMENT.md` and the prerequisite section of `RC_MANUAL_VALIDATION_CHECKLIST.md`.
4. Run Steps 7, 8, 13 (Windows E2E, DPAPI cross-machine, clean install) on real hardware against that
   real backend — checklists provided, nothing to improvise.
5. Purchase and apply a code-signing certificate (Step 12) — start now, EV issuance takes days-weeks.
6. Decide the offline-grace value (72h vs. 24h vs. something else) — engineering has a recommendation,
   not a decision.
7. Send `docs/LEGAL_REVIEW_BRIEF.md` to counsel; do not ship the three placeholder legal docs as-is.

## 4. Security assumptions still unproven

- **DPAPI's real-world attack resistance** beyond the "copy the file" test — a known, accepted
  limitation of the primitive itself, not something Step 8 as specified can prove either way.
- **SameSite=Strict as the sole CSRF defense** on the admin panel — correctly configured and verified
  by inspecting the actual `Set-Cookie` header, but its enforcement is a browser behavior no
  automated test in this environment can simulate. Adequate for a single-operator admin panel; a CSRF
  token would be defense-in-depth, not currently present.
- **Whether the deployed copy of the old `p1_admin.html`'s token array was ever neutralized** — the
  local working-tree copy is confirmed `_TX=[]`; the live deployed page was fetched as rendered text,
  not raw source, so this specific detail wasn't independently confirmed either way. Doesn't change
  the required action (take it down).
- **PostgreSQL behavior under real production load** (this pass used a real but small, single-host,
  low-latency PostgreSQL instance — genuine MVCC and locking semantics, not a simulation, but not the
  same as a managed host under production network conditions and connection pooling).

## 5. RC build identifier

**None.** No exe has been built or signed this pass — `verify_release.py` correctly blocked it on the
one expected condition (empty production key), exactly as designed.

## 6. Scores

**Technical readiness: 78/100.** The parts that could be verified were verified to an unusually high
standard for a solo project — real PostgreSQL concurrency proof under contention, adversarial admin
testing that found and fixed a real gap, a privacy audit that found and fixed two real gaps, 108/108
backend tests, 65/65 risk-engine baseline untouched. What holds this back from higher: zero human has
clicked through the actual GUI against the actual new backend yet (all verification this pass was
backend/API-level), no production deployment exists, and one credential-exposure incident from before
this pass is still measurably open on the public internet.

**Sales readiness: 40/100.** Legal is entirely unreviewed for a trading-adjacent product sold across
unknown jurisdictions via crypto with no refund policy and an open AML/KYC question. No signed binary
exists. No customer has ever completed the real end-to-end journey on real hardware. The legacy
incident being open means there is arguably still a live artifact on the public internet bearing the
shape of the original exposure, independent of whether the token itself still works.

## 7. Recommendation

**CONTROLLED PILOT READY — conditioned on closing the legacy incident first.**

Not DO NOT SHIP: the engineering is genuinely sound and has been tested to a standard most solo
projects never reach, including the one test (real Postgres concurrency) that's usually just assumed.

Not PRODUCTION SALES READY: no signed binary, no real-hardware E2E proof, no legal review, and a
still-open credential incident are not things to wave past for a product sold to strangers with no
support desk.

A controlled pilot — a small number of known, trusted testers, after the legacy incident is closed
and Steps 7/8/13 are run on real hardware — is the right next gate. General sale should wait for
Authenticode signing and at least a first pass from counsel on the open questions in
`LEGAL_REVIEW_BRIEF.md`.
