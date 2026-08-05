# Data Inventory

Every field the system stores, why it exists, and how long it is kept.
If a field is not on this list, the system does not collect it.

| Field | Purpose | Source | Retention |
|---|---|---|---|
| email | account identity, support contact | customer | life of account |
| full_name (optional) | support personalisation | customer | life of account |
| password_hash (Argon2id) | authentication | derived | life of account |
| account status | access control | admin | life of account |
| failed_login_count, locked_until | brute-force defence | derived | rolling |
| licence status / plan / expiry | entitlement | admin | life of account + 7y (accounting) |
| activation_note | proof of manual payment | admin | 7y (accounting) |
| device public key | device identity | client | until device removed |
| device label (non-identifying, derived from device public key — NOT the Windows hostname) / OS name+version / app version | support and admin display | client | until device removed |
| session timestamps, end_reason | single-session enforcement | derived | 90 days |
| session token HASH | session auth | derived | until session ends |
| TOTP secret (encrypted) | MFA | generated | until MFA reset |
| recovery code hashes | MFA fallback | generated | until used or reset |
| consent records | legal proof of acceptance | customer | life of account + 7y |
| audit events | security forensics | derived | 1 year |
| ip_address (audit only) | security anomaly detection | request | 1 year |

## Explicitly NOT collected

Removed in Phase 12 and enforced by an automated test
(`test_no_location_or_financial_fields_in_schema`), which fails the build if a
column with any of these names is ever added:

- GPS / precise location / coordinates
- MAC address, hostname, Windows username, CPU, RAM, screen resolution
- Public IP geolocation (city, region, country, ISP)
- MT5 account login, broker server, balance, equity, currency
- Open positions, entry prices, floating P/L, trade history

The previous build published most of the above to a public URL every five
minutes. None of it served a licensing purpose.
