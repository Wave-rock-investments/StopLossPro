# cf_worker — DECOMMISSIONED 2026-08-05

`gist_proxy.js` authenticated inbound writes on two things: the request being a
POST, and the `User-Agent` header starting with `StopLossCalc/`. Its own comment
conceded the second was "noise reduction, not a security gate."

There was no shared secret, no signature, no nonce, no rate limit, no IP
restriction. `approved_ids.txt` was on the writable allow-list. Anyone who read
the Worker URL out of the shipped exe could, with a single unauthenticated HTTP
POST, grant themselves a free licence or empty the allowlist and disable every
paying customer at once.

**This directory is retained for reference only. Do not deploy it.**

Delete the Worker in the Cloudflare dashboard, and delete the `GIST_TOKEN`
secret bound to it. Licensing is now server-authoritative — see
`Working/backend/`.
