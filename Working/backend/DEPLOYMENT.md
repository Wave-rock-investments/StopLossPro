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

# 4. Migrate
python -m alembic upgrade head

# 5. Create the first admin (interactive, one time)
python -m app.bootstrap_admin

# 6. Serve behind TLS. Terminate HTTPS at the platform's proxy.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The app refuses to boot if `ENV=production` and any of: the database is SQLite,
`DEBUG` is on, or either signing key is missing. Failing at boot beats failing
at 3am under a race condition.

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

1. `python -m app.keygen` → new pair
2. Set the new private key on the server, bump `STOPLOSS_SIGNING_KEY_ID` to `k2`
3. Ship a client update that accepts **both** `k1` and `k2`
4. Once adoption is sufficient, drop `k1`

TOTP secrets are encrypted with a key derived from the signing private key, so
rotation requires re-encrypting them. Do not rotate without that migration.

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
