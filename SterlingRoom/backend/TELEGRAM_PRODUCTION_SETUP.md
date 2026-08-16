# Sterling_Room — Telegram Production Configuration

Preparation only, per the explicit instruction: **do not migrate off the
current dev/test group until staging validation passes** (see
`STAGING.md` §5 for what that means). Nothing in this document has been
executed — no channels have been created, no `.env` values changed. It is
the exact sequence to follow when you're ready.

## 1. Bot

If the existing Sterling_Room bot (used throughout dev) will also serve
production: no new bot needed, skip to §2 — but note it means dev,
staging, and production all share one bot token, which is the weaker of
the two isolation options in `STAGING.md` §1. If a dedicated production
bot is preferred (recommended): create one via @BotFather (`/newbot`),
save the token immediately into the production secret manager — never
into any file in this repo.

Set the bot's commands (`/setcommands` via @BotFather) so `/start` shows
a description in Telegram's UI — cosmetic, optional:
```
start - Open the Sterling_Room menu
```

## 2. FREE CHANNEL

1. Create a public Telegram channel.
2. Add the bot as an administrator with "Post Messages" permission
   (minimum required — do not grant more than needed).
3. Get the channel's numeric chat ID: forward any message from the
   channel to `@userinfobot`, or call `getUpdates`/`getChat` on the Bot
   API after posting once. Public channel chat IDs are negative numbers
   (e.g. `-1001234567890`).
4. Get the channel's public invite link (`t.me/<channel_username>`) for
   `TELEGRAM_FREE_CHANNEL_LINK`.
5. Record the chat ID for `TELEGRAM_FREE_CHAT_ID` — **do not set this
   environment variable yet**; record it for the migration step in §5.

## 3. PREMIUM PRIVATE CHANNEL

1. Create a **private** Telegram channel (invite-only, no public link).
2. Add the bot as an administrator with "Add Users" / "Invite Users via
   Link" permission — this is what `app/telegram_access.py::grant_premium_access`
   needs to create single-use invite links, and "Ban Users" — needed by
   `revoke_premium_access`'s ban-then-unban mechanism.
3. Get the chat ID the same way as §2 step 3 (will also be negative).
4. Record it for `TELEGRAM_PREMIUM_CHAT_ID` (§5).
5. **Do not** generate a general-purpose invite link for this channel —
   access is meant to flow exclusively through
   `grant_premium_access()`'s single-use links, issued per subscriber.
   A standing public/reusable invite link defeats the whole
   access-control model (anyone with the link could join without ever
   subscribing).

## 4. RESULTS CHANNEL

1. Create a channel (public or private, business decision — not a
   technical one).
2. Add the bot as administrator with "Post Messages" permission.
3. Get the chat ID, record for `TELEGRAM_RESULTS_CHAT_ID` (§5).
4. **Known gap, not fixed in this pass:** nothing in the current codebase
   posts to this channel automatically on call close — see the release
   audit's blockers list. Until that automation exists, treat this as a
   manually-posted channel, or deprioritize connecting it in the first
   production cutover.

## 5. Community group (if applicable)

If a community/discussion group exists or is planned: keep it **entirely
separate** from the three chat IDs above. Sterling_Room's bot does not
manage membership in a community group, and none of
`TELEGRAM_FREE_CHAT_ID`/`TELEGRAM_PREMIUM_CHAT_ID`/`TELEGRAM_RESULTS_CHAT_ID`
should ever point at one.

## 6. Cutover sequence (only after staging validation passes)

1. Confirm `STAGING.md` §5's three conditions are all met.
2. In the production secret manager only (never staging, never a
   committed file): set `TELEGRAM_BOT_TOKEN` (§1),
   `TELEGRAM_FREE_CHAT_ID`/`TELEGRAM_PREMIUM_CHAT_ID`/`TELEGRAM_RESULTS_CHAT_ID`
   (§2-4), `TELEGRAM_FREE_CHANNEL_LINK`, and a freshly generated
   `TELEGRAM_WEBHOOK_SECRET`.
3. Deploy production with those values.
4. Register the webhook against the production bot token
   (`DEPLOYMENT.md` §5).
5. Send one clearly-marked internal test call through the full pipeline
   (`POST /calls` → confirm it lands correctly in FREE and PREMIUM) and
   one `/start` through the bot, confirming responses land in the right
   place, before telling anyone this is live.
6. Only then consider the dev/test group's job here done — it can keep
   existing as a place to test future changes without touching
   production again.
