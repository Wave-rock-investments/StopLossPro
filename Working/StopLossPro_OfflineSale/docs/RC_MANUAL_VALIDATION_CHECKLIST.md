# RC Manual Validation Checklist — Steps 7, 8, 12, 13

Everything in this document requires either real Windows hardware, a real deployed backend, or a
purchased code-signing certificate — none of which exist inside the sandboxed environment this
assistant runs in. Steps 1–6, 9, 14, 15 have already been executed and verified against real
PostgreSQL, real HTTP requests, and real cryptography where "real" was achievable; this document is
the honest handoff for the parts that are not.

Do not check a box here from inference or "it should work." Every box is a thing to actually do and
actually watch happen.

---

## Prerequisite: the backend must be actually deployed first

None of Steps 7/8/13 are meaningful against a backend that only exists as code. Before starting this
checklist:

1. Provision PostgreSQL (managed host — see `backend/DEPLOYMENT.md` §2).
2. `python -m app.keygen` **on the server**, twice if needed — once conceptually for the signing pair,
   once for the TOTP key (the script now prints both in one run). Put both directly into the host's
   secret manager. Do not paste them into a chat, a ticket, a file in this repo, or anywhere they'd be
   logged.
3. `alembic upgrade head`.
4. `python -m app.bootstrap_admin` — create the first admin, with MFA, interactively, on the server.
5. Confirm `GET https://<your-domain>/health/ready` returns `{"status":"ready"}`.
6. `GET /api/v1/pubkey` → copy the **public** key only into `lib/licensing.py` →
   `SERVER_PUBLIC_KEY_B64`. Set `API_BASE` to the real domain.
7. `python verify_release.py` from `Working/StopLossPro_OfflineSale/` → must exit 0.
8. `build.bat` → produces the unsigned RC candidate exe. This is what Steps 7, 8, and 13 below test —
   not a dev build, not the old P1/P2 exe.

---

## STEP 7 — Real Windows client E2E

Run the actual GUI on a real Windows machine against the real deployed backend above. Not a
simulation, not TestClient — click through it.

**New customer journey**
- [ ] Admin panel: create a customer, activate their licence
- [ ] Client: sign in with that customer's credentials
- [ ] Required consent screens appear and must be accepted before proceeding
- [ ] TOTP setup: QR/secret shown once, authenticator app scans it, confirmation code accepted
- [ ] Device enrolment happens transparently (Ed25519 device keypair generated, no user action needed)
- [ ] Signed authorization grant is issued; client shows "authorised" / no error banner
- [ ] StopLossPro's main window opens
- [ ] Risk engine: run a calculation, confirm SL/TP/lot-size output looks sane (compare one example
      by hand against `tests/test_risk_engine_baseline.py`'s known-good values if in doubt)
- [ ] Heartbeat succeeds — leave it running 2+ minutes, confirm no re-auth prompt, no error

**Device takeover**
- [ ] Computer A: signed in, active
- [ ] Computer B: attempt sign-in with the same account → blocked, "active on another device" message
- [ ] Computer B: enter the correct TOTP code → takeover proceeds
- [ ] Computer A: within ~90s (one heartbeat interval), loses authorization — confirm it actually
      shows a locked/signed-out state, not just a stale window
- [ ] Computer B: now active
- [ ] Admin panel → customer detail page → confirm exactly ONE session shows ACTIVE, on Computer B's
      device entry

**Admin revocation while connected**
- [ ] With a client actively connected and heartbeating, admin panel → Revoke the licence
- [ ] Within one heartbeat interval, the connected client loses authorization and shows why
      (not a generic error — should say the licence is no longer valid)

**Offline behavior**
- [ ] Disconnect the test machine's network entirely
- [ ] Client continues working, shows an "offline / grace period" indicator with a countdown
- [ ] Reconnect network before grace expires
- [ ] Confirm the client re-syncs promptly and the offline banner clears — server state should win
      immediately (if something was revoked while offline, it takes effect now, not after a delay)

Record every deviation from the above, even minor UI ones. A blocker for sales-readiness is not just
"security failed" — a confusing or broken screen at any point in this flow is a real defect for a
product with no live support desk to catch a confused customer.

---

## STEP 8 — DPAPI cross-machine test

Needs two distinct Windows environments (two physical machines, or two separate VMs — NOT two user
accounts on the same Windows install, since DPAPI's protection is tied to the Windows installation's
key material in ways that can behave differently on the same OS instance vs. genuinely separate ones).

**On Computer A**
- [ ] Sign in normally, let it fully authenticate (device enrolled, grant issued, state saved)
- [ ] Locate the local state file: `%USERPROFILE%\.stoplosspro\state.bin`
- [ ] Copy that exact file (do not modify it)

**On Computer B**
- [ ] Ensure StopLossPro has never been signed in here (fresh state, or delete any existing
      `%USERPROFILE%\.stoplosspro\state.bin` first)
- [ ] Place the copied `state.bin` from Computer A into `%USERPROFILE%\.stoplosspro\` on Computer B
- [ ] Launch the app
- [ ] **Expected: the app does NOT come up authenticated.** It should behave exactly as if no state
      file existed — sign-in screen, no cached identity, no entitlements.
- [ ] Record PASS if it demanded fresh sign-in. Record FAIL — and treat as a release blocker — if it
      opened authorised on Computer B's copied file.

**Corruption / deletion resilience**
- [ ] On a machine that's already validly signed in, close the app, then either delete `state.bin` or
      truncate/corrupt it (e.g. open in a text editor, save garbage over it)
- [ ] Relaunch the app
- [ ] Expected: clean re-authentication prompt, no crash, no bypass. Record PASS/FAIL.

**What this test does NOT prove** (so it isn't over-claimed in the final signoff): it does not prove
DPAPI is unbreakable against an attacker with full disk + credential access to the *original* machine
— that's a known, accepted limitation of DPAPI as a primitive, documented in
`docs/OFFLINE_GRACE_ANALYSIS.md` §1 Scenario 7. This test only proves the naive "copy the file
somewhere else" attack doesn't work, which is the realistic threat for a cash/crypto-sold consumer
product.

---

## STEP 12 — Authenticode signing

Full procedure already lives in `backend/DEPLOYMENT.md` §4 — repeated here as a checklist against the
*specific* RC binary from the prerequisite section above, not a hypothetical build.

- [ ] Confirm you have a valid code-signing certificate (EV recommended — SmartScreen trust is
      immediate; OV is cheaper but accrues reputation over weeks of real-world downloads).
      Certificate issuance takes days to weeks — this should have been started well before this point.
- [ ] `signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /a dist\StopLossPro.exe`
- [ ] `signtool verify /pa /v dist\StopLossPro.exe` — confirm it reports a valid signature
- [ ] Compute and record the SHA-256 of the **signed** file (this is the value that goes in the final
      release manifest, not the pre-signing hash — they will differ)
- [ ] Do not claim SmartScreen reputation is guaranteed by signing alone — a brand-new EV-signed binary
      can still take time to build reputation with some antivirus heuristics; monitor early downloads.

---

## STEP 13 — Clean Windows installation test

**Must be a genuinely clean environment** — a fresh VM snapshot or a machine that has never had any
StopLossPro version (including old P1/P2) installed. The developer's own machine, having run every
prior build and test in this project, is not a valid substitute no matter how convenient.

Test the **exact signed artifact** from Step 12 (verify the SHA-256 matches before starting).

- [ ] Installation completes without errors, no missing-DLL or "Windows protected your PC" dead-end
      (SmartScreen may still show an interstitial even when signed — click through and confirm it's
      the expected first-run experience, not a crash)
- [ ] First launch
- [ ] Sign-in with a real test customer
- [ ] Consent screens
- [ ] MFA setup
- [ ] Device registration
- [ ] Licence recognized correctly
- [ ] MT5 integration: with a real or demo MT5 terminal running, confirm connection status shows
      correctly (this was previously a bug — "Disconnected" due to numpy packaging — confirm it's
      genuinely fixed on a clean install, not just on the dev machine where numpy happened to already
      be present some other way)
- [ ] Risk calculation produces sane output
- [ ] Order confirmation flow (placing a trade via the calculator's BUY/SELL flow) works end-to-end
- [ ] Logout
- [ ] Restart the machine, relaunch — session restoration behaves correctly (re-authenticates or
      resumes as designed, does not crash, does not silently bypass auth)
- [ ] Revocation (admin revokes from the panel) takes effect
- [ ] Device takeover from a second machine works exactly as in Step 7
- [ ] Network interruption mid-session → offline grace behavior as in Step 7
- [ ] Uninstall — confirm it removes what it should; note if `%USERPROFILE%\.stoplosspro\` is left
      behind (that may be intentional — licence state persisting across reinstall — but it should be a
      deliberate decision, not an oversight, and should be documented either way)
- [ ] Reinstall — confirm the app comes back up cleanly, whether or not local state survived

Record every failure with exact repro steps. This is the last checkpoint before the binary is
considered fit to hand to a stranger over Reddit or Telegram with no support team standing by.
