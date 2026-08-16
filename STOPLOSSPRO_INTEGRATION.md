# StopLossPro Integration — how calls actually get generated and sent today

*Audited 2026-08-16 by direct source inspection, not assumption. Covers `Historical/main_dispatcher/main.py` — the "StopLossPro Personal" build, confirmed as the upstream call source (see `STERLING_ROOM_AUDIT.md` §2).*

## Current call-generation flow

1. User enters ATR / lots / account / risk% via the numpad, or taps FETCH to pull the last closed candle.
2. `on_calculate()` → `calc_setup()` (pure risk-engine function, `calc.py`-equivalent logic inlined in `main.py`) computes the full BUY and SELL ladders: entry, SL, TP1 (1:2), TP2 (1:3), TP3 (1:4), cash P&L, blended EV. Results are stored on `self.buy` / `self.sell` dicts.
3. User taps **SHARE BUY** / **SHARE SELL** → `share_levels(side)` (line 4935).

## Current call-send flow

`share_levels(side)`:
- Pulls `data = self.buy` or `self.sell` (the already-computed ladder for that side).
- If Telegram is configured and ready (`self._tg_ready()`) → calls `_post_telegram_signal(side, data)`.
- Else if Telegram is enabled but not fully configured → nudges the user to Settings.
- Else → falls back to clipboard / OS share sheet (unchanged legacy behavior).

`_post_telegram_signal(side, data)` (line 4853) is the actual dispatch point — also the same function the Settings-tab "auto-post when I place an MT5 order" toggle calls (line 6310, in `_execute_mt5_order`'s success callback) when a real MT5 order fills. So there are exactly **two triggers**, both converging on this one function:
- Manual SHARE BUY/SHARE SELL tap.
- Automatic, right after a real MT5 order is placed and confirmed filled (only if the user has switched the auto-post toggle on).

Inside `_post_telegram_signal`, it:
- Rate-limits to 1 signal per 3 seconds (`_last_signal_ts`).
- Formats `sig_data` — a dict of `entry`, `sl`, `tp1`, `tp2`, `tp3`, each pre-formatted as a decimal string at the instrument's correct precision (`DECIMALS.get(_base_sym(symbol))`).
- Calls `dispatch_signal(bot_token, channel_id, symbol, direction, entry, sl, tp1, tp2, tp3, channel_link, on_success, on_error)` — this is `signal_dispatcher.dispatch_signal()`, which POSTs a plain-text message to Telegram's `sendMessage` Bot API endpoint for the **one** `channel_id` configured in Settings.
- On success: shows a snackbar, opens the CLOSE TRADE panel for later win/loss tap-to-report.
- On error: shows `f"Telegram error: {msg}"` — this is the exact mechanism behind the error the user was troubleshooting earlier in this session.

## Call data structure available at the send point

```python
{
    "symbol":    str,   # canonical or broker-resolved instrument name
    "direction": str,   # "BUY" or "SELL"
    "entry":     str,   # pre-formatted decimal string
    "sl":        str,
    "tp1":       str,   # 1:2 R:R, fixed by the risk engine
    "tp2":       str,   # 1:3 R:R
    "tp3":       str,   # 1:4 R:R
}
```

Notably **absent** from what's available at this point: no risk_percent (calculated upstream but not threaded into `sig_data`), no setup_type/analysis/invalidation text (this app has no concept of a "setup narrative" — it's a pure calculator, not a discretionary-call annotator), no event/news metadata. Sterling_Room's call schema (master-prompt §17) will need to either accept these as optional/empty from this source, or add a lightweight annotation step in `main.py` (e.g. a text field for "setup notes" before SHARE) if the business wants that copy in every published call. **Not assumed here — flag for the user.**

## Integration point (recommended)

Do **not** touch `calc_setup()`, the risk engine, or MT5 execution. The single, minimal, additive integration point is `_post_telegram_signal()` (line 4853):

- **Option A (adapter alongside, not instead of)**: after building `sig_data`, also `POST` the same payload (plus `risk_percent`, pulled from `self._calc_risk_pct` or equivalent, which *is* available in scope even though it isn't in `sig_data` today) to a new Sterling_Room endpoint, e.g. `POST {STERLING_API}/calls`. Sterling_Room's backend does its own validation, Trade ID assignment, storage, and Telegram fan-out to Free/Premium/Results channels — completely independent of this app's existing single-channel `dispatch_signal()` call, which can keep running unchanged for the user's personal use if desired.
- **Option B (replace)**: swap the direct `dispatch_signal()` call for the new `POST /calls` call, and let Sterling_Room's backend be the one to actually talk to Telegram (all channels, not just one). Simpler long-term (single source of truth for delivery, retries, dedup), but changes existing personal-app behavior — the personal build would then depend on Sterling_Room's API being reachable to post to Telegram at all, which isn't true today.

**Recommend Option A initially** (adapter runs alongside the existing direct-send path, both can be independently enabled/disabled via Settings), specifically because §3 of the master prompt is explicit that StopLossPro's existing call-generation/send behavior should not be disrupted, and because it keeps the personal app fully functional even if Sterling_Room's backend is temporarily down. Migrating to Option B can happen later once Sterling_Room's delivery reliability (retry queue, dedup) is proven out.

## Required modifications

1. `Historical/main_dispatcher/main.py`: extend `_post_telegram_signal()` with a second outbound call (new `sterling_adapter.py` module, mirroring the existing `mt5_dispatcher.py`/`signal_dispatcher.py` background-thread + `Clock.schedule_once` pattern already used twice in this file — third instance of the same pattern, not a new one).
2. New Settings fields: Sterling_Room API base URL + an auth token/key for this device to authenticate to the adapter endpoint (needs a real value from whoever stands up the Sterling_Room backend — cannot be fabricated).
3. Sterling_Room backend: new `POST /calls` (or `/sterling/calls`) endpoint, idempotent on a `source_call_id` the client generates client-side (e.g. UUID4 per SHARE tap) so retries from a flaky connection never create two Trade IDs for one call — this is exactly the duplicate-prevention rule in master-prompt §28.

## Risks

- The personal app has no authentication of its own (deliberately — it's single-user, single-laptop). The new adapter call from it to Sterling_Room's API will need *some* credential (a long-lived API key is simplest) so the endpoint isn't open to the public internet — this is a real design decision, not a detail to skip.
- `_post_telegram_signal`'s 3-second rate limit and `_signal_posting` in-flight guard exist to protect the *existing* single Telegram send; they say nothing about the new adapter call's own failure modes (timeout, Sterling_Room API down). The adapter call should fail *silently relative to the existing flow* — i.e. never block or break the existing direct-to-Telegram SHARE behavior if the new endpoint is unreachable — matching master-prompt §59's "StopLossPro unavailable → Sterling_Room must not crash" in reverse (Sterling_Room unavailable must not break StopLossPro).

## Rollback method

The integration is additive (Option A) — reverting is deleting the new outbound call and the new Settings fields, with zero effect on existing calculator/MT5/Telegram-direct behavior. If Option B is chosen later, keep the old `dispatch_signal()` code path intact but unreachable (not deleted) for at least one release cycle, so reverting is a one-line change back to the direct call rather than restoring deleted code.
