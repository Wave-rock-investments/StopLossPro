# Sterling_Room — Telegram Production Configuration

**Status as of 2026-08-16: the production Telegram structure is
finalized.** The two production channels exist, `@SterlingroomBot` is
already an administrator in both, and their chat IDs are recorded below.
What has **not** happened yet: `TELEGRAM_FREE_CHAT_ID` /
`TELEGRAM_PREMIUM_CHAT_ID` / `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_WEBHOOK_SECRET` have not been set in any real production secret
manager from this session, the app has not been deployed with them, and
the webhook has not been registered — see §5 "Cutover sequence" for what's
still outstanding before this is actually live.

**Architecture note (2026-08-16 decision): there is no separate Results
channel.** Verified CLOSED/STOPPED trade results publish automatically to
`TELEGRAM_FREE_CHAT_ID` — Sterling_Room's own free channel is the results
destination, by design (see `DEPLOYMENT.md` §4). This document previously
described provisioning a third dedicated Results channel; that section has
been removed to match the finalized architecture.

## 1. Bot

`@SterlingroomBot` — a single production bot, already an administrator in
both production channels (confirmed 2026-08-16). No new bot needed.

**Isolation note carried over from `STAGING.md` §1**: if this same bot
token is also used for dev/staging testing, that means dev/staging/prod
all share one bot token — the weaker of the two isolation options
`STAGING.md` describes. If a dedicated production-only token is preferred,
create a second bot via @BotFather and keep dev/staging pointed at the
original. Whichever token is used in production, it is set directly in
the production secret manager — **never in any file in this repo.**

## 2. FREE CHANNEL

| | |
|---|---|
| Name | Sterling_Room |
| Chat ID | `-1004319935784` |
| Setting | `TELEGRAM_FREE_CHAT_ID` |
| Purpose | Free trading calls, market content, verified trade results (see architecture note above), premium-conversion content |

Bot is already an administrator with posting permission. `TELEGRAM_FREE_CHANNEL_LINK`
(the channel's public `t.me/...` link, shown by the bot's FREE ACCESS
button) still needs to be recorded separately — not provided as part of
this configuration round.

## 3. PREMIUM PRIVATE CHANNEL

| | |
|---|---|
| Name | SterlingRoom_Premium |
| Chat ID | `-1004292117841` |
| Setting | `TELEGRAM_PREMIUM_CHAT_ID` |
| Purpose | Full paid trading calls, premium trade updates |

Bot is already an administrator. Confirm (not assumed, since it wasn't
part of the information supplied for this pass) that its admin rights
include "Add Users" / "Invite Users via Link" — required by
`app/telegram_access.py::grant_premium_access` to create the single-use
invite links subscribers receive — and "Ban Users," required by
`revoke_premium_access`'s ban-then-unban mechanism. If those specific
rights aren't confirmed, premium access grant/revoke will fail at runtime
even though the bot is nominally "an administrator."

**Do not** generate a general-purpose invite link for this channel —
access flows exclusively through `grant_premium_access()`'s single-use
links, issued per subscriber. A standing public/reusable invite link
defeats the access-control model.

## 4. Community group (if applicable)

If a community/discussion group exists or is planned: keep it **entirely
separate** from the two chat IDs above. Sterling_Room's bot does not
manage membership in a community group, and neither
`TELEGRAM_FREE_CHAT_ID` nor `TELEGRAM_PREMIUM_CHAT_ID` should ever point
at one.

## 5. Cutover sequence (what's still outstanding)

1. In the production secret manager only (never staging, never a
   committed file): set `TELEGRAM_BOT_TOKEN` (§1), `TELEGRAM_FREE_CHAT_ID=-1004319935784`,
   `TELEGRAM_PREMIUM_CHAT_ID=-1004292117841`, `TELEGRAM_FREE_CHANNEL_LINK`,
   and a freshly generated `TELEGRAM_WEBHOOK_SECRET`.
2. Deploy production with those values.
3. Register the webhook against the production bot token
   (`DEPLOYMENT.md` §5).
4. Send one clearly-marked internal test call through the full pipeline
   (`POST /calls` → confirm it lands correctly in FREE and PREMIUM), one
   test CLOSE (confirm the result posts to FREE, not a second
   destination), and one `/start` through the bot — confirming responses
   land in the right place — before telling anyone this is live.
5. Confirm the background worker (`app/worker.py`, see `DEPLOYMENT.md`
   §5b) is actually scheduled on the production host — the retry and
   subscription-expiry jobs do nothing if nothing invokes them.
