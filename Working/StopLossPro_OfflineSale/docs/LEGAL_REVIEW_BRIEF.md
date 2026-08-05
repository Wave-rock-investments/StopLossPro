# StopLossPro — Legal Review Briefing Packet
**Prepared for:** outside/retained counsel, pre-production release readiness (Step 15 of 15)
**Prepared by:** solo operator (no company/LLC structure implied unless counsel is told otherwise)
**Status of existing legal docs:** `TERMS_OF_SERVICE_v1.0.md`, `RISK_DISCLOSURE_v1.0.md`, and `PRIVACY_NOTICE_v1.0.md` are all explicitly marked **PLACEHOLDER — LAWYER REVIEW REQUIRED** in the repo. None of them contain finished legal language. This brief describes what the product and code actually do, so counsel can draft real terms instead of guessing.

---

## 1. What StopLossPro Does

A Windows desktop **position-sizing and risk-management calculator** for retail traders using MetaTrader 5 (MT5). It computes lot size / stop-loss / risk parameters from user-supplied inputs and can optionally place the resulting order into the user's own MT5 terminal at the user's explicit initiation.

It is explicitly **not**:
- a broker
- a trading platform
- an investment or financial advisor
- a signal or "copy trading" service
- a custodian of client funds — the software never holds money; all trades execute in the user's own MT5 account via the user's own broker connection

## 2. What It Does NOT Promise

- No guarantee of profit or prevention of loss
- No trading advice, recommendation, or signal — calculations are mechanical outputs of user-entered numbers
- No fiduciary relationship with the user
- **Flag for counsel:** none of this is currently stated in a customer-facing disclaimer with binding force — the current Risk Disclosure doc is a placeholder outline only. A strong, prominent disclaimer is needed before release.

## 3. Trading-Risk Disclaimer Requirement

Standard retail-trading risk language is needed: leverage risk, potential total loss of capital, past performance not indicative of future results, no outcome guarantee, user is solely responsible for verifying all values before placing any trade, MT5 order placement is user-initiated/user-confirmed (software makes no autonomous trading decisions).

**Open regulatory question:** several regimes (notably EU/UK) impose specific mandatory CFD/leveraged-product risk-warning formats and content requirements (e.g., ESMA-style warnings, UK FCA rules) that go beyond generic disclaimers. **Sales channel is Reddit and Telegram — actual customer geography is unknown/uncontrolled.** Counsel needs to weigh in on whether jurisdiction-specific risk-warning language is required given this uncontrolled distribution, and whether geographic sales restrictions are advisable.

## 4. Account / Licensing Model

- One active session per user, enforced server-side (`services.py` — a new device login revokes the prior session; "device switching permitted" per TOS placeholder notes)
- Devices are bound via a public-key credential the client generates locally (`Device.public_key`); re-enrolling the same key on reinstall does not create a duplicate; a revoked device (`DeviceStatus.REVOKED`) is permanently locked out
- Licences are **admin-activated** — there is no public self-serve signup/checkout flow; an `activation_note` field records proof of manual payment
- Licence lifecycle is server-authoritative: `LicenceStatus` = ACTIVE / PENDING / SUSPENDED / REVOKED / EXPIRED, evaluated against server time (not client clock)

## 5. Data Collected (per `DATA_INVENTORY.md`)

| Category | Field(s) |
|---|---|
| Identity | email, optional full name |
| Credentials | Argon2id password hash, encrypted TOTP secret, recovery code hashes |
| Account state | account status, licence status/plan/expiry, activation_note (payment proof) |
| Security/anti-abuse | failed_login_count, locked_until |
| Device | device public key, device name (non-identifying label, not raw hostname), OS, app version |
| Session | session timestamps + end_reason, session token hash |
| Legal | consent records (per document, per version, timestamped) |
| Security forensics | audit events, IP address (audit-only) |

**Explicitly NOT collected** (enforced by an automated schema test, `test_no_location_or_financial_fields_in_schema`, that fails the build if violated): GPS/precise location, MAC address, hostname, Windows username, hardware fingerprints (CPU/RAM/screen res), IP geolocation (city/region/country/ISP), MT5 account number/broker/balance/equity, open positions, entry prices, floating P/L, trade history. Note: an earlier build did publish most of this to a public URL every 5 minutes — that behavior has been removed and is now actively guarded against by an automated test. A separate Step 14 audit (2026-08-05) also found and fixed a case where the client was sending the real Windows hostname under a differently-named field (`device_name`); it now sends a non-identifying label derived from the device's own public key instead.

## 6. IP-Address Processing

IP addresses are logged **only** as part of security audit events — not as a standalone profile field. Confirmed logging points in `services.py`: `LOGIN_SUCCESS`, `LOGIN_FAILED`, `LOGIN_BLOCKED_LOCKED`, `SESSION_STARTED`, and `TAKEOVER_MFA_FAILED` all record the requesting IP via `audit(..., ip=ip)`. Stated purpose: security anomaly detection. Retention: **1 year** (same as audit events generally, per `DATA_INVENTORY.md`). No IP geolocation is performed or stored (explicitly excluded, see Section 5). Note: the Step 14 audit also flagged that the deployment runbook previously left the web server's own default access log enabled, which would have captured client IPs a second time with no defined retention; the runbook now disables it (`--no-access-log`) so IP capture happens in exactly the one documented, retention-scoped place.

## 7. Retention Periods — Documented vs. Undefined

**Documented (per `DATA_INVENTORY.md`):**
- Identity/credentials/licence/consent records: life of account (licence, activation_note, and consent records additionally retained **+7 years** for accounting/legal proof purposes)
- Device records: until device removed
- Session timestamps: 90 days
- Session token hash: until session ends
- TOTP secret: until MFA reset
- Recovery code hashes: until used or reset
- Audit events / IP address: 1 year

**Gaps counsel should flag:** no documented end-to-end account-deletion or data-erasure procedure (what actually happens to the "life of account" fields when an account is closed or a deletion request is received); no documented process or SLA for honoring data-subject access/deletion requests; no defined process for the 7-year accounting-retention fields once that period lapses.

## 8. Consent Mechanism

Versioned, per-user, per-document consent is implemented in code (`services.py`):
- `REQUIRED_CONSENTS` maps each of `ConsentDocument.TERMS_OF_SERVICE`, `RISK_DISCLOSURE`, `PRIVACY_NOTICE` to a version string (currently `"1.0"` for all three)
- `outstanding_consents()` compares each user's recorded `ConsentRecord` acceptances against the current required versions
- `record_consent()` stores acceptance with a timestamp and the app version, and writes an audit event (`CONSENT_RECORDED`)
- Bumping a version string in `REQUIRED_CONSENTS` automatically re-prompts every customer at next sign-in — so once counsel finalizes real document text, redeployment plus a version bump is sufficient to require re-acceptance from the existing user base

## 9. Cash/Crypto/Manual Payment Model

There is no integrated payment processor and no stored card/payment-instrument data anywhere in the schema (confirmed absent from `DATA_INVENTORY.md`). Payment is handled entirely outside the app: the admin manually reconciles payment (cash or crypto) and activates the licence, recording only an `activation_note` as proof, retained 7 years for accounting.

**Open question for counsel:** accepting crypto payments directly to a solo operator, with no payment processor and no identity verification of payers, raises **AML/KYC exposure** that needs an explicit risk assessment — including whether any threshold/reporting obligations apply in the operator's jurisdiction, and whether a written no-refund/manual-reconciliation policy is legally sufficient without one.

## 10. Account Suspension / Revocation

Enforcement is **admin-triggered and takes effect on the client's next successful heartbeat**, not instantly server-push:
- `heartbeat()` checks `user.status` on every poll (interval configurable, see Section 11); if the account is not `ACTIVE`, the session is immediately marked `REVOKED` server-side, the session token hash is cleared, and an audit event records the reason (`ACCOUNT_<status>`)
- Licence-level checks (`effective_licence()`) independently block `REVOKED`, `SUSPENDED`, and `PENDING` licence states, and auto-transition to `EXPIRED` when the server-side expiry timestamp passes (evaluated against server time, so a client clock cannot extend a licence)
- Device revocation (`DeviceStatus.REVOKED`) is also enforced and independently locks a specific device out even if the account itself is active
- All of the above actions write to the audit log

## 11. Offline Grace Period

The client tolerates a bounded period without a successful heartbeat before functionality is affected. Currently configured as `OFFLINE_GRACE_SECONDS = 259200` (72 hours) in `config.py`, described in-code as a "bounded offline tolerance." **This value is currently under internal review and may be reduced to 24 hours before release** — counsel should treat this as "a bounded offline grace period, currently 72 hours, under review," not a fixed permanent figure, when drafting any terms that reference offline usability. See `docs/OFFLINE_GRACE_ANALYSIS.md` for the full tradeoff analysis.

## 12. Support / Recovery Procedures

- MFA recovery uses one-time recovery codes (`RecoveryCode` model, hashed at rest; `consume_recovery_code()` marks a code used on successful redemption)
- If a user loses both their MFA device and all recovery codes, the only path back in is an **admin-assisted MFA reset** (`reset_mfa()`), which also force-terminates the user's active session (audited as `MFA_RESET`)
- **There is no live support team** — this is a solo-operator product. All support and account-recovery actions are manual, asynchronous, and performed by the same individual who administers licensing. Counsel should account for this when drafting SLA-adjacent language (e.g., avoid implying guaranteed response times).

---

## OPEN QUESTIONS FOR COUNSEL

1. **Jurisdiction / consumer-protection regime** — sales occur via Reddit/Telegram with no controlled customer geography; seller is based in India. Which jurisdiction's consumer-protection and trading-risk-disclosure rules apply, and should sales be geo-restricted?
2. **AML/KYC exposure** — crypto payments received directly by a solo operator with no processor and no payer identity verification. Is any registration, reporting, or threshold-monitoring obligation triggered?
3. **Refund policy** — none currently exists for this manually-activated, offline-sold product. What refund/cancellation terms are legally required or advisable?
4. **Broker-specific risk disclosure** — given the direct MT5 order-placement integration, does the risk disclosure need to go beyond generic trading-risk language into broker/platform-specific warnings (e.g., execution risk, connectivity risk, third-party broker terms)?
5. **Data retention gaps** — no documented deletion procedure for "life of account" fields, no documented data-subject request handling process, and no defined disposition for 7-year accounting-retention fields after that period lapses.
6. **Limitation-of-liability language** — cap amount, carve-outs (if any), and enforceability across the unknown customer-jurisdiction mix noted in item 1.
7. **Governing law and dispute resolution** — seller in India, customers international; venue and arbitration/litigation clause need to be chosen deliberately, not defaulted.
8. **Registration status** — whether providing a position-sizing calculator with direct MT5 order-placement triggers any financial-services or software-as-advice registration requirement in any jurisdiction the product is actually sold into.

---

**LEGAL: PENDING REVIEW**
