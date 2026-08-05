# Deployment & Release Runbook

## 1. What runs where

| Component | Location | Holds |
|---|---|---|
| Desktop client | Customer PC | PUBLIC verification key only |
| API + admin | Managed host | PRIVATE signing key, DB credentials |
| PostgreSQL | Managed database | All authoritative state |

The client can *verify* a grant. It can never *mint* one. That asymmetry is the
whole security model — everything else supports it.

## 2. First deploy

```bash
# 1. Provision managed PostgreSQL. Note the connection URL.
#    SQLite will NOT start in production and that is deliberate:
#    it cannot enforce SELECT ... FOR UPDATE.

# 2. Generate the signing keypair. Run ONCE. Store the output in the
#    host's secret manager, never in a file, never in the repo.
python -m app.keygen

# 3. Set environment variables on the host
STOPLOSS_ENV=production
STOPLOSS_DEBUG=false
STOPLOSS_DATABASE_URL=postgresql+psycopg://USER:PASS@HOST:5432/stoploss
STOPLOSS_SIGNING_PRIVATE_KEY_B64=<from step 2 — SERVER ONLY>
STOPLOSS_SIGNING_PUBLIC_KEY_B64=<from step 2>
STOPLOSS_SIGNING_KEY_ID=k1
STOPLOSS_TOTP_ENCRYPTION_KEY_B64=<from step 2 — SERVER ONLY, independent secret>
#   ^ Protects MFA secrets at rest. Deliberately NOT derived from the signing
#     key above (see app/config.py) — rotating one must never force rotating
#     the other. The app refuses to boot in production if this equals the
#     signing private key or is left empty.

# 4. Migrate
python -m alembic upgrade head

# 5. Create the first admin (interactive, one time)
python -m app.bootstrap_admin
#   No shell on this host (e.g. Render Free)? Set STOPLOSS_ADMIN_BOOTSTRAP_TOKEN
#   to a long random value, redeploy, then visit /admin/bootstrap?token=<value>
#   in a browser — same rules as the CLI (MFA mandatory, refuses once an admin
#   exists, re-checked on every request). Unset the env var again once done.

# 6. Serve behind TLS. Terminate HTTPS at the platform's proxy.
#    --no-access-log: uvicorn's default access log writes client IP + full
#    request line to stdout for every request, with no defined retention —
#    an undocumented second copy of data DATA_INVENTORY.md already scopes
#    to audit_events (1-year retention, security purpose only). Found during
#    the Step 14 privacy audit. If host-level access logs are wanted for ops
#    reasons, add them deliberately with an explicit retention policy and
#    reflect that in DATA_INVENTORY.md — do not rely on the framework default.
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

The app refuses to boot if `ENV=production` and any of: the database is
SQLite, `DEBUG` is on, either signing key is missing, the TOTP encryption key
is missing, or the TOTP encryption key equals the signing private key.
Failing at boot beats failing at 3am under a race condition.

## 3. Building the client

Order matters.

```bash
# 1. Fetch the PUBLIC key from the running server
curl https://api.stoplosspro.in/api/v1/pubkey

# 2. Paste it into lib/licensing.py -> SERVER_PUBLIC_KEY_B64
#    (public half only — if you ever paste the private key here, every
#     licence in circulation becomes forgeable and you must rotate)

# 3. Point the client at the API
#    lib/licensing.py -> API_BASE

# 4. Gate the build
python verify_release.py        # must exit 0

# 5. Build
build.bat
```

`verify_release.py` blocks the build if credentials are present, if any legacy
ntfy/gist/Worker/GPS path has crept back, if the verification key is missing, if
file logging is enabled, or if the risk-engine baseline regresses.

## 4. Code signing — do this early

Unsigned executables trip SmartScreen and antivirus heuristics. For a product
sold to strangers over Reddit and Telegram, that is a direct conversion problem
before it is a security one.

- **EV certificate** — SmartScreen trust is immediate. Ships on a hardware
  token. Recommended given the sales channel.
- **OV certificate** — cheaper; reputation accrues over weeks of downloads.

Certificate issuance takes days to weeks (identity validation). Start now.

```powershell
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
  /a dist\StopLossPro.exe
signtool verify /pa /v dist\StopLossPro.exe
```

Always timestamp. Without it, signatures expire with the certificate and every
previously shipped build silently becomes untrusted.

The signing key must never enter the repository, a build script, or CI logs.

## 5. Key rotation

**Signing key** (Ed25519 — signs grants) and **TOTP encryption key** (Fernet —
protects MFA secrets at rest) are independent secrets. Rotating one never
requires rotating the other; the procedures below are separate.

### 5a. Signing key rotation

**Verified 2026-08-05 (RC gate, Stage 2 crypto-architecture check):** every
grant already carries `kid` (`app/security.py` `issue_grant`/`verify_grant`,
`GrantClaims.key_id`) — the server-side plumbing for rotation is in place.
**However, the shipped client is single-key, not multi-key.**
`lib/licensing.py` bakes in exactly one `SERVER_PUBLIC_KEY_B64` string at
build time and `verify_grant()` there checks the token's signature against
only that one key — it does not read `kid` or consult a set of trusted keys.
There is also no auto-update mechanism; this is a manually distributed EXE
sold over Reddit/Telegram.

**Consequence:** the moment the server starts signing with a new private key,
every already-installed customer EXE (which still has the *old* public key
compiled in) fails every subsequent grant verification and that customer is
locked out — not eventually, immediately, for 100% of the installed base,
until each of them individually downloads and installs a new build. "Ship a
client update that accepts both k1 and k2" is aspirational text describing a
capability the client does not currently have; treat rotation as customer-
impacting until this is built. It is **not** built now because no rotation is
imminent (no production key exists yet) and adding a multi-key keyring to the
client is a scope change, not a bug fix — out of bounds under the current
feature freeze without an explicit go-ahead.

Until the client is updated to look up a public key by `kid`, the real
procedure is:

1. `python -m app.keygen` → new pair
2. Set the new private key on the server, bump `STOPLOSS_SIGNING_KEY_ID` to `k2`
3. **Every customer must reconnect on a rebuilt client carrying the new
   public key before you cut the server over** — there is no overlap window.
   Coordinate this as a release, not a background rotation.
4. Only rotate on suspected compromise (see §8) or as part of a deliberate,
   communicated release — never routinely.

This has no effect on stored TOTP secrets — they are encrypted under the
separate `STOPLOSS_TOTP_ENCRYPTION_KEY_B64`, untouched by this rotation.

### 5b. TOTP encryption key rotation

Unlike the signing key, this is a *symmetric* key — there is no public half to
roll forward, so old blobs become undecryptable the moment the env var
changes. Rotate as a coordinated maintenance pass, not a config flip:

1. `python -m app.keygen` → note the new `STOPLOSS_TOTP_ENCRYPTION_KEY_B64`
2. Temporarily keep the OLD key available (e.g. as `STOPLOSS_TOTP_ENCRYPTION_KEY_B64_OLD`
   in the deploy environment, not in the repo)
3. Run a one-off maintenance script that, for every `mfa_credentials` row:
   decrypts `secret_encrypted` with the OLD key, re-encrypts with the NEW key
   (both via `app.security` — do not hand-roll Fernet calls), writes it back
   in the same transaction as the read
4. Verify a sample of admin/customer TOTP logins succeed post-migration
5. Remove the OLD key from the environment

Do not rotate this key without running that pass — see
`test_totp_key_rotation_without_reencryption_fails_cleanly_not_silently` in
`tests/test_phase16_key_separation.py` for what happens if you skip it (a
clean `ValueError`, not silent data loss, but every affected customer is
locked out of MFA until support resets it).

## 6. Backups

```bash
pg_dump "$STOPLOSS_DATABASE_URL" | gpg --encrypt -r <key> > backup-$(date +%F).sql.gpg
```

Daily, retained 30 days, stored off the application host.

**Restore-test quarterly.** A backup that has never been restored is a
hypothesis, not a backup. Record the date of the last successful restore drill.

## 7. Monitoring

Minimum viable: uptime check on `/health/ready`, alert on sustained 5xx, alert
on authentication failure spikes (credential stuffing), disk and connection-pool
alerts on the database.

Never log: passwords, TOTP secrets, recovery codes, session tokens, grants.
Client IP addresses are recorded in exactly one place — `audit_events`, for
security purposes, 1-year retention (see DATA_INVENTORY.md). Do not let a
second, undocumented copy of client IPs accumulate in host/proxy access
logs without the same deliberate retention decision.

## 8. Incident response

| Situation | Action |
|---|---|
| Signing key suspected leaked | Rotate immediately (§5). Every grant is forgeable until you do. |
| Database compromised | Rotate DB credentials, force-logout all sessions, notify per obligations |
| Customer reports lockout | Admin → customer → Force logout, releases the session lock |
| Customer lost authenticator | Verify identity out of band, then admin → Reset MFA |
| Admin account compromised | Rotate admin password + TOTP, review `audit_events` |

## 9. Cutover from the legacy system

The legacy Gist, Cloudflare Worker and ntfy topics are already deleted or
decommissioned. Any client build older than this release will fail to activate,
which is the intended outcome — those builds contain the bypasses.

Do not restore any part of the old system as a fallback. Restoring it restores
every bypass with it.
