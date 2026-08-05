# PRIVACY NOTICE — version 1.0

**[PLACEHOLDER — LAWYER REVIEW REQUIRED]**

This file is a versioned container, not legal advice and not final wording.
Do not ship it to customers as-is.

The technical framework around it is complete: acceptance is recorded per user
per document per version in `consent_records`, with a timestamp and the app
version. Bumping the version string in `services.REQUIRED_CONSENTS` will
re-prompt every customer at next sign-in.

## Points counsel should address

- Data actually collected (see backend/DATA_INVENTORY.md): email, optional
  name, Argon2id password hash, encrypted TOTP secret, device public key,
  device name/OS/app version, session timestamps, IP for security events,
  consent records, audit events
- Data explicitly NOT collected: GPS or precise location, MAC address, hardware
  fingerprints, MT5 account numbers, balances, equity, open positions, P/L,
  browsing or file activity
- Purpose limitation: identity, licensing, session control, security, support
- Retention periods and deletion process
- Sub-processors: hosting provider, managed database, email provider
- Cross-border transfer basis (seller in India, customers international)
- Data-subject rights and how to exercise them
