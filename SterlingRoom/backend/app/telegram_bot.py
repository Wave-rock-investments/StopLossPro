"""Telegram distribution — outbound call/update/exit messages to the
Free/Premium/Results channels (master-prompt §21-26, §29-30).

Scope of what's built now vs. deferred, stated plainly:

BUILT: message templates (§21-25), channel routing (§26), delivery-status
recording with per-(call, chat, type, content) dedup (§28/§30), synchronous
retry with backoff for the request itself.

NOT built yet (deferred, not silently skipped): the interactive bot side —
/start menu, plan selection, payment flow (master-prompt §9-13) — because
that whole flow terminates in a payment step, and per the 2026-08-16
decision payments are "abstraction first, provider later." Building the
interactive menu now would mean shipping a premium-signup flow that dead-
ends at an unimplemented payment step. A background delivery-retry WORKER
(master-prompt §29 "QUEUE -> RETRY -> SUCCESS") is also not built yet —
today's retry is synchronous, within the request; `mark_for_retry()` below
exists so a future worker has something to poll, but nothing currently
calls it on a schedule.

Uses stdlib `urllib` only, no extra dependency — same reasoning as this
repo's Historical/main_dispatcher/signal_dispatcher.py: one fewer thing to
pin, one fewer supply-chain surface, and the Bot API is a plain POST.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Call, CallDirection, CallMessage, DeliveryStatus, MessageType
from app.services import content_hash

log = logging.getLogger("sterling.telegram_bot")

_API_TIMEOUT_S = 12
_MAX_SEND_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1, 3)  # between attempt 1->2 and 2->3

# ── Background retry (Phase 10) ─────────────────────────────────────────────
# A message that exhausted _MAX_SEND_ATTEMPTS synchronously (above) is not
# abandoned — app/worker.py's scheduled job picks it back up, up to this
# many ADDITIONAL attempts, each spaced further apart than the last so a
# real Telegram outage doesn't turn into a tight request storm.
_MAX_BACKGROUND_RETRY_ATTEMPTS = 5
_MAX_TOTAL_ATTEMPTS = _MAX_SEND_ATTEMPTS + _MAX_BACKGROUND_RETRY_ATTEMPTS  # 8
_BACKGROUND_BACKOFF_S = (30, 60, 120, 300, 600)  # indexed by retry_count, capped at the last entry


# ══════════════════════════════════════════════════════════════════════════
# Visual styling helpers (2026-08-16 premium-desk redesign — presentation
# layer only, see docstrings on the individual render_* functions below for
# what each one deliberately does NOT change).
#
# Every glyph used here (box-drawing borders, Mathematical Bold letters/
# digits, circled numbers, diamonds/bars) is a plain Unicode character, not
# Telegram markup — _send_telegram_message below sends with no `parse_mode`
# set (unchanged by this redesign), so Telegram never attempts to parse
# these messages as Markdown/HTML in the first place. That means there is
# nothing here for Telegram's parser to reject, and no escaping is needed.
#
# _bold() is a programmatic Unicode Mathematical Bold transform (codepoint
# arithmetic against the standard Mathematical Alphanumeric Symbols block),
# used consistently for every bold header/label below, rather than hand-
# transcribing the several visually-different "fancy text" alphabets (bold
# upright vs. bold italic vs. small-caps) seen in ad-hoc mockups — those are
# easy to get a code point wrong on when typed by hand, and are not
# reliably distinguishable from each other in most Telegram clients anyway.
# ══════════════════════════════════════════════════════════════════════════
_BOLD_UPPER_BASE = 0x1D400  # MATHEMATICAL BOLD CAPITAL A
_BOLD_LOWER_BASE = 0x1D41A  # MATHEMATICAL BOLD SMALL A
_BOLD_DIGIT_BASE = 0x1D7CE  # MATHEMATICAL BOLD DIGIT ZERO


def _bold(text: str) -> str:
    """Unicode Mathematical Bold transform for headers/labels only — never
    applied to numeric price/result values (see _format_price), so a test
    (or a support agent) can always find the exact rendered number as
    plain digits regardless of label styling."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(_BOLD_UPPER_BASE + (ord(ch) - ord("A"))))
        elif "a" <= ch <= "z":
            out.append(chr(_BOLD_LOWER_BASE + (ord(ch) - ord("a"))))
        elif "0" <= ch <= "9":
            out.append(chr(_BOLD_DIGIT_BASE + (ord(ch) - ord("0"))))
        else:
            out.append(ch)  # spaces, punctuation, box-drawing, emoji pass through unchanged
    return "".join(out)


# Box-drawing / marker glyphs, named by codepoint rather than pasted so the
# exact character is auditable at a glance instead of relying on a
# copy-pasted glyph rendering correctly in every editor/terminal.
_BOX_TL, _BOX_TR = chr(0x2554), chr(0x2557)   # ╔ ╗  (double line)
_BOX_BL, _BOX_BR = chr(0x255A), chr(0x255D)   # ╚ ╝
_BOX_H, _BOX_V = chr(0x2550), chr(0x2551)     # ═ ║
_DIVIDER = chr(0x2501) * 20                    # ━━━━━━━━━━━━━━━━━━━━ (heavy horizontal)
_DIAMOND = chr(0x25C8)                         # ◈
_VBAR = chr(0x2502)                            # │
_VBAR_HEAVY = chr(0x2503)                      # ┃
_TP_MARKERS = [chr(0x2460 + i) for i in range(3)]  # ① ② ③


def _box_header(label: str) -> str:
    """╔══...══╗ / ║  label  ║ / ╚══...══╝ sized to the (already-bold)
    label passed in, with 3 spaces of padding each side."""
    pad = 3
    width = len(label) + pad * 2
    return "\n".join([
        _BOX_TL + _BOX_H * width + _BOX_TR,
        _BOX_V + " " * pad + label + " " * pad + _BOX_V,
        _BOX_BL + _BOX_H * width + _BOX_BR,
    ])


def _section_label(text: str) -> str:
    return _bold(text).center(len(_DIVIDER))


def _format_price(value) -> str:
    """Comma-grouped price display that PRESERVES the value's real
    precision instead of forcing a fixed decimal count — e.g. 63209.70 ->
    '63,209.70' (a whole/near-whole price gets padded to 2 decimals for
    readability), but a 4-decimal forex quote like 1.4321 stays exactly
    '1.4321', never truncated to '1.43'. Truncating a real stop-loss/entry/
    TP price would silently change the actual level shown to Premium
    subscribers, which is a correctness bug this redesign must not
    introduce (it is presentation-only). Uses Decimal throughout (never
    float) to avoid binary-float rounding artifacts. Deliberately plain
    (non-bold) digits — see _bold()'s docstring."""
    if value is None:
        return "—"
    try:
        d = Decimal(str(value)).normalize()
    except (InvalidOperation, TypeError):
        return str(value)
    # normalize() can flip a whole number into exponent form (e.g.
    # Decimal('1900') -> Decimal('1.9E+3')) — undo that so formatting
    # below never emits scientific notation.
    if d.as_tuple().exponent > 0:
        d = d.quantize(Decimal(1))
    text = f"{d:,f}"
    if "." in text:
        integer_part, frac = text.split(".")
        text = f"{integer_part}.{frac.ljust(2, '0')}"
    else:
        text = f"{text}.00"
    return text


def _compute_rr(call: Call, tp_value) -> str | None:
    """Risk:reward ratio for one target, derived at render time from the
    call's own authoritative entry/stop_loss/tp fields — there is no
    separate stored R:R value anywhere else in this codebase to preserve
    or contradict (checked: no such field/function exists outside this
    renderer), so this IS the one and only R:R calculation, not a second
    one competing with an existing "authoritative" source. Returns None
    (never a fabricated ratio) whenever entry price, the target, or a
    sane positive risk aren't all available — e.g. a MARKET-entry call
    with no entry_min/entry_max simply shows no ratio for that target."""
    if tp_value is None:
        return None
    entry = None
    if call.entry_min is not None and call.entry_max is not None:
        entry = (float(call.entry_min) + float(call.entry_max)) / 2
    elif call.entry_min is not None:
        entry = float(call.entry_min)
    elif call.entry_max is not None:
        entry = float(call.entry_max)
    if entry is None:
        return None

    sl, tp = float(call.stop_loss), float(tp_value)
    if call.direction == CallDirection.SELL:
        risk, reward = sl - entry, entry - tp
    else:
        risk, reward = entry - sl, tp - entry
    if risk <= 0 or reward <= 0:
        return None

    ratio = reward / risk
    if abs(ratio - round(ratio)) < 0.05:
        return f"1:{round(ratio)}"
    return f"1:{ratio:.1f}"


# ══════════════════════════════════════════════════════════════════════════
# Templates (master-prompt §21-25) — configurable via the settings dict
# passed in, never hardcoded business copy scattered through the codebase.
#
# 2026-08-16 premium-desk redesign: presentation only. Every dynamic value
# below (direction, instrument, entry/SL/TP prices, R:R, result_r, status,
# trade_id) is read straight off the same `Call`/`CallMessage` fields the
# previous templates used — no call/entry/SL/TP/R:R/result logic changed,
# only how it's laid out as text. See docstrings below for the two
# deliberate scope boundaries: the Free teaser still never touches
# execution-number fields (unchanged principle from before this redesign),
# and no template appends a bot/channel URL (there never was one in these
# renderers — see TELEGRAM_PRODUCTION_SETUP.md's /start flow for the
# separate, untouched onboarding link).
# ══════════════════════════════════════════════════════════════════════════
def render_entry_message(call: Call) -> str:
    """Premium call — full execution detail, immediately. See module
    docstring above for the redesign's scope boundaries."""
    arrow = "🔴" if call.direction == CallDirection.SELL else "🟢"
    if call.entry_min is not None and call.entry_max is not None:
        entry_display = f"{_format_price(call.entry_min)} – {_format_price(call.entry_max)}"
    elif call.entry_min is not None or call.entry_max is not None:
        entry_display = _format_price(call.entry_min if call.entry_min is not None else call.entry_max)
    else:
        entry_display = "MARKET"

    lines = [
        _box_header(_bold("STERLING ROOM") + "  •  " + _bold("PREMIUM")),
        "",
        f"{arrow}  {call.direction.value} {call.instrument}",
        _DIVIDER,
        _section_label("ENTRY  •  SL"),
        _DIVIDER,
        f"{_DIAMOND} {_bold('ENTRY')}   {_VBAR} {entry_display}",
        f"{_DIAMOND} {_bold('SL')}       {_VBAR} {_format_price(call.stop_loss)}",
    ]

    tp_lines = []
    for marker, tp_value in zip(_TP_MARKERS, (call.tp1, call.tp2, call.tp3)):
        if tp_value is None:
            continue
        rr = _compute_rr(call, tp_value)
        suffix = f"   {_VBAR_HEAVY} {rr}" if rr else ""
        tp_lines.append(f"{marker}  {_format_price(tp_value)}{suffix}")
    if tp_lines:
        lines += [_DIVIDER, _section_label("TAKE PROFIT"), _DIVIDER] + tp_lines

    lines += [_DIVIDER, f"⚡ {_bold('RISK MANAGEMENT FIRST')}"]

    extras = []
    if call.risk_percent:
        extras.append(f"{_DIAMOND} {_bold('RISK')}     {_VBAR} {call.risk_percent}%")
    if call.setup_type:
        extras.append(f"{_DIAMOND} {_bold('SETUP')}    {_VBAR} {call.setup_type}")
    if call.invalidation:
        extras.append(f"{_DIAMOND} {_bold('INVALIDATION')} {_VBAR} {call.invalidation}")
    if extras:
        lines += [_DIVIDER] + extras

    lines += [_DIVIDER, f"{_bold('TRADE ID')}   {call.trade_id}", _DIVIDER]
    return "\n".join(lines)


def render_free_teaser_message(call: Call) -> str:
    """Sanitized, delayed Free-channel teaser (2026-08-16 production
    architecture; 2026-08-16 restyled for the premium-desk redesign —
    presentation only, the underlying safety principle is UNCHANGED).
    Deliberately built from a FIXED, hardcoded template plus only two call
    fields — instrument and direction — never from
    call.analysis/call.setup_type/call.invalidation or any of the
    price/risk fields (entry_min/max, stop_loss, tp1-3, risk_percent).

    That's a stricter reading than the letter of the spec (which allows
    "general setup"/"market structure" as categories) — those fields are
    free-text supplied by the adapter/analyst and this renderer cannot
    algorithmically guarantee they never contain a leaked price or level.
    Given the explicit STOP CONDITION ("if Free can expose actionable
    numerical information, do not deploy"), the only version of this
    renderer that can be verified never to leak is one that structurally
    cannot: fixed copy, no passthrough of free-form fields, and — as of
    this redesign — still no price-formatting helper (_format_price) is
    ever called anywhere in this function. If a richer, still-safe
    "general context" is wanted later, it needs a real separately-authored
    sanitized-copy field, not a filter over analyst-supplied text.
    """
    arrow = "🔴" if call.direction == CallDirection.SELL else "🟢"
    lines = [
        _box_header(_bold("STERLING ROOM")),
        "",
        f"{arrow}  {call.direction.value} {call.instrument}",
        _DIVIDER,
        _section_label("PREMIUM CALL"),
        _DIVIDER,
        f"⚡ {_bold('Full execution levels')}",
        f"🔒 {_bold('Entry')} • {_bold('SL')} • {_bold('TP')}",
        "",
        "Available inside SterlingRoom Premium.",
        _DIVIDER,
        f"{_bold('TRADE ID')}   {call.trade_id}",
        _DIVIDER,
    ]
    return "\n".join(lines)


def render_update_message(call: Call, update_text: str, update_number: int) -> str:
    lines = [
        _box_header(_bold("STERLING ROOM") + "  •  " + _bold("UPDATE")),
        "",
        f"{_bold('TRADE ID')}   {call.trade_id}",
        f"{_bold('UPDATE')} #{update_number}",
        _DIVIDER,
        update_text,
        _DIVIDER,
        f"{_DIAMOND} {_bold('STATUS')}   {_VBAR} {call.status.value}",
        _DIVIDER,
    ]
    return "\n".join(lines)


def render_tp1_message(call: Call, management_instruction: str = "") -> str:
    lines = [
        _box_header(_bold("STERLING ROOM") + "  •  " + _bold("UPDATE")),
        "",
        f"🎯  {call.instrument} {call.direction.value}  —  {_bold('TP1 HIT')}",
        _DIVIDER,
        f"{_bold('TRADE ID')}   {call.trade_id}",
    ]
    if management_instruction:
        lines += [_DIVIDER, f"{_DIAMOND} {_bold('MANAGEMENT')} {_VBAR} {management_instruction}"]
    lines += [_DIVIDER, f"{_DIAMOND} {_bold('REMAINING')}   {_VBAR} {_bold('TP2 ACTIVE')}", _DIVIDER]
    return "\n".join(lines)


def _format_result_r(result_r) -> str:
    """Normalizes to float before formatting. `result_r` is a
    Numeric(8,3) column — SQLAlchemy hands back a plain float for a value
    just assigned in this same request/session, but a `decimal.Decimal`
    for one round-tripped through an actual read from the database (e.g.
    a retried request that re-fetched the Call fresh). Rendering those two
    representations directly would produce different TEXT for the exact
    same value ("+3.0R" vs "+3.000R"), which breaks distribute_call's
    content-hash dedup and would let a retried close event double-post —
    see app/api.py::transition_call's idempotent-retry handling."""
    if result_r is None:
        return "—"
    r = float(result_r)
    return f"{'+' if r >= 0 else ''}{r}R"


def render_exit_message(call: Call) -> str:
    result = _format_result_r(call.result_r)
    lines = [
        _box_header(_bold("STERLING ROOM") + "  •  " + _bold("CLOSED")),
        "",
        f"{_bold('TRADE ID')}   {call.trade_id}",
        _DIVIDER,
        f"{_DIAMOND} {_bold('RESULT')}   {_VBAR} {result}",
        f"{_DIAMOND} {_bold('STATUS')}   {_VBAR} {_bold('COMPLETED')}",
        _DIVIDER,
    ]
    return "\n".join(lines)


def render_invalidated_message(call: Call, reason: str = "") -> str:
    lines = [
        _box_header(_bold("STERLING ROOM") + "  •  " + _bold("INVALIDATED")),
        "",
        f"⚠ {call.instrument} {call.direction.value}  —  {_bold('NO TRADE')}",
        _DIVIDER,
        f"{_bold('TRADE ID')}   {call.trade_id}",
    ]
    if reason:
        lines += [_DIVIDER, f"{_DIAMOND} {_bold('REASON')}   {_VBAR} {reason}"]
    lines += [_DIVIDER]
    return "\n".join(lines)


def render_results_message(call: Call) -> str:
    """Results-channel post (Phase 10) — CALL CLOSED -> Performance Ledger
    -> Verified R Result -> RESULTS CHANNEL. Every field here is read
    directly off the authoritative `Call` row (the same row
    app/performance.py sums over for the ledger) — nothing is recomputed
    independently for Telegram, so this can never drift from what the
    performance ledger reports for the same trade (master-prompt Phase 10:
    "do not manually calculate results separately for Telegram"). 2026-08-16
    premium-desk redesign: presentation only — `call.result_r` is read and
    formatted (_format_result_r, unchanged) exactly as before, never
    recomputed.
    """
    result = _format_result_r(call.result_r)
    r_value = float(call.result_r) if call.result_r is not None else 0.0
    if r_value > 0:
        outcome, icon = "WIN", "✅"
    elif r_value < 0:
        outcome, icon = "LOSS", "❌"
    else:
        outcome, icon = "BREAKEVEN", "➖"

    lines = [
        _box_header(_bold("STERLING ROOM") + "  •  " + _bold("RESULT")),
        "",
        f"{icon}  {call.instrument} {call.direction.value}  —  {_bold(outcome)}",
        _DIVIDER,
        f"{_DIAMOND} {_bold('RESULT')}   {_VBAR} {result}",
        f"{_DIAMOND} {_bold('STATUS')}   {_VBAR} {call.status.value}",
        _DIVIDER,
        f"{_bold('TRADE ID')}   {call.trade_id}",
        _DIVIDER,
    ]
    return "\n".join(lines)


_RENDERERS = {
    MessageType.ENTRY: lambda call, **kw: render_entry_message(call),
    MessageType.FREE_ENTRY: lambda call, **kw: render_free_teaser_message(call),
    MessageType.TP1: lambda call, **kw: render_tp1_message(call, kw.get("management_instruction", "")),
    MessageType.EXIT: lambda call, **kw: render_exit_message(call),
    MessageType.INVALIDATED: lambda call, **kw: render_invalidated_message(call, kw.get("reason", "")),
    MessageType.UPDATE: lambda call, **kw: render_update_message(call, kw.get("update_text", ""), kw.get("update_number", 1)),
    MessageType.RESULTS: lambda call, **kw: render_results_message(call),
}


# ══════════════════════════════════════════════════════════════════════════
# Bot API transport
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class SendResult:
    ok: bool
    telegram_message_id: str | None = None
    error: str | None = None


def _api_call(bot_token: str, method: str, payload: dict) -> tuple[bool, dict]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            data = {"ok": False, "description": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return False, {"ok": False, "description": f"Network error reaching Telegram: {e.reason}"}
    except Exception as e:
        return False, {"ok": False, "description": f"Telegram send failed: {e}"}
    return bool(data.get("ok")), data


def _send_telegram_message(bot_token: str, chat_id: str, text: str) -> SendResult:
    if not bot_token or not chat_id:
        return SendResult(ok=False, error="Bot token and chat id are required")
    ok, data = _api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    if not ok:
        return SendResult(ok=False, error=data.get("description", "Telegram API returned an error"))
    return SendResult(ok=True, telegram_message_id=str(data.get("result", {}).get("message_id", "")))


def send_message(bot_token: str, chat_id: str, text: str, *, reply_markup: dict | None = None) -> SendResult:
    """General-purpose sender used by the interactive bot (app/bot.py) —
    unlike _send_telegram_message (used by distribute_call for the fixed
    call templates), this supports an inline keyboard."""
    if not bot_token or not chat_id:
        return SendResult(ok=False, error="Bot token and chat id are required")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    ok, data = _api_call(bot_token, "sendMessage", payload)
    if not ok:
        return SendResult(ok=False, error=data.get("description", "Telegram API returned an error"))
    return SendResult(ok=True, telegram_message_id=str(data.get("result", {}).get("message_id", "")))


def answer_callback_query(bot_token: str, callback_query_id: str, *, text: str = "") -> SendResult:
    """Stops the button's loading spinner on the user's client. Best-effort
    — a failure here doesn't matter enough to affect anything else."""
    ok, data = _api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
    return SendResult(ok=ok, error=None if ok else data.get("description"))


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """rows is a list of rows, each a list of (label, callback_data) pairs.
    Small helper so app/bot.py's menu-building code stays readable."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def url_button_row(label: str, url: str) -> list[dict]:
    return [{"text": label, "url": url}]


# ══════════════════════════════════════════════════════════════════════════
# Distribution — routes a call to the right chats, dedups, records history
# ══════════════════════════════════════════════════════════════════════════
def distribute_call(db: Session, call: Call, message_type: MessageType, *, chat_ids: list[str], **template_kwargs) -> list[CallMessage]:
    """Render the appropriate template once, send it to every chat_id given,
    record a CallMessage per (call, chat, type, content) — the unique
    constraint on that tuple means calling this twice with identical inputs
    (e.g. a retried request) never double-posts (master-prompt §28).
    """
    settings = get_settings()
    renderer = _RENDERERS.get(message_type)
    if renderer is None:
        raise ValueError(f"No template for message_type={message_type}")

    text = renderer(call, **template_kwargs)
    h = content_hash(text)
    results: list[CallMessage] = []

    for chat_id in chat_ids:
        existing = db.query(CallMessage).filter_by(
            call_id=call.id, telegram_chat_id=chat_id, message_type=message_type, message_content_hash=h,
        ).one_or_none()
        if existing is not None and existing.delivery_status == DeliveryStatus.SENT:
            results.append(existing)  # already sent — idempotent no-op
            continue

        msg = existing or CallMessage(
            call_id=call.id, telegram_chat_id=chat_id, message_type=message_type, message_content_hash=h,
        )
        msg.message_text = text  # persisted so a later background retry can resend exactly this, see models.py
        if existing is None:
            db.add(msg)
            db.flush()

        last_error = None
        now = datetime.now(timezone.utc)
        for attempt in range(_MAX_SEND_ATTEMPTS):
            outcome = _send_telegram_message(settings.TELEGRAM_BOT_TOKEN, chat_id, text)
            now = datetime.now(timezone.utc)
            msg.last_attempt_at = now
            if outcome.ok:
                msg.delivery_status = DeliveryStatus.SENT
                msg.telegram_message_id = outcome.telegram_message_id
                msg.sent_at = now
                msg.error = None
                break
            last_error = outcome.error
            msg.retry_count = attempt + 1
            if attempt < _MAX_SEND_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
        else:
            msg.delivery_status = DeliveryStatus.FAILED
            msg.error = last_error
            log.warning("telegram send failed for %s -> %s after %d attempts: %s",
                        call.trade_id, chat_id, _MAX_SEND_ATTEMPTS, last_error)

        results.append(msg)

    return results


def resolve_chat_ids(call: Call, *, free_chat_id: str, premium_chat_id: str) -> list[str]:
    """Master-prompt §26: routing is configurable per call, not
    "every call -> every channel"."""
    chats = []
    if call.route_premium and premium_chat_id:
        chats.append(premium_chat_id)
    if call.route_free and free_chat_id:
        chats.append(free_chat_id)
    return chats


@dataclass
class FreeCallDeliveryStats:
    """One tick's worth of delayed-Free-delivery results (2026-08-16
    production architecture, Gate 4). Idempotent and restart-safe the same
    way as RetryRunStats above: "has this call already gotten a FREE_ENTRY
    CallMessage row" is derived from the database on every run, never from
    in-process state, so a crash mid-batch just means the next tick picks
    up the calls it hadn't reached yet — and a call that already has a row
    (whether SENT or FAILED) is never re-queued here; a FAILED one is
    handed off to process_telegram_retries' normal backoff/retry path
    instead of being resent from scratch by this job."""
    candidates: int = 0            # calls past their free_call_due_at
    sent: int = 0                   # distribute_call invoked for the first time this run
    already_delivered: int = 0      # already had a FREE_ENTRY row — skipped
    skipped_not_configured: int = 0  # due, but no Free chat/bot configured
    trade_ids: list[str] = field(default_factory=list)


def process_delayed_free_calls(db: Session, *, now: datetime | None = None) -> FreeCallDeliveryStats:
    """The delayed/sanitized Free-channel delivery job (2026-08-16
    production architecture, Gate 4). Premium gets every call immediately
    with full detail (app/api.py::create_call, unchanged). Free gets a
    separately-rendered, sanitized teaser (render_free_teaser_message) only
    once this call's free_call_due_at has passed — this job is what
    actually sends it, polled from app/worker.py.

    Idempotency: a call is only ever handed to distribute_call here if it
    has NO existing FREE_ENTRY CallMessage row yet (regardless of that
    row's status) — so a call whose first attempt failed is retried by the
    existing process_telegram_retries job's backoff schedule, not
    re-sent-from-scratch by this job on every tick. Restart-safety follows
    the same argument as RetryRunStats: due-ness is derived from
    Call.free_call_due_at (set once, at creation, in app/services.py) and
    CallMessage row existence — both persisted columns, so a crash between
    ticks loses nothing.
    """
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    stats = FreeCallDeliveryStats()

    due_calls = (
        db.query(Call)
        .filter(Call.route_free.is_(True), Call.free_call_due_at.isnot(None), Call.free_call_due_at <= now)
        .order_by(Call.free_call_due_at)
        .all()
    )

    if not due_calls:
        return stats

    if not settings.telegram_configured or not settings.TELEGRAM_FREE_CHAT_ID:
        # Logged once for the whole batch, not once per candidate, so a dev
        # environment with Telegram simply turned off doesn't spam a
        # WARNING every tick.
        stats.skipped_not_configured = len(due_calls)
        log.warning("free_call_delivery_skipped_not_configured", extra={"pending_count": len(due_calls)})
        return stats

    for call in due_calls:
        stats.candidates += 1
        already_queued = db.query(CallMessage).filter_by(
            call_id=call.id, telegram_chat_id=settings.TELEGRAM_FREE_CHAT_ID, message_type=MessageType.FREE_ENTRY,
        ).first()
        if already_queued is not None:
            stats.already_delivered += 1
            continue

        distribute_call(db, call, MessageType.FREE_ENTRY, chat_ids=[settings.TELEGRAM_FREE_CHAT_ID])
        db.commit()  # per-call, not per-batch — matches process_telegram_retries' crash-safety argument
        stats.sent += 1
        stats.trade_ids.append(call.trade_id)
        log.info("free_call_delivered", extra={"trade_id": call.trade_id, "call_id": str(call.id)})

    return stats


def mark_for_retry(db: Session) -> list[CallMessage]:
    """The current FAILED-and-not-yet-exhausted queue — a read-only view
    used for reporting/tests. app/worker.py's scheduled job calls
    process_telegram_retries() below to actually act on it, not this."""
    return (
        db.query(CallMessage)
        .filter_by(delivery_status=DeliveryStatus.FAILED)
        .filter(CallMessage.retry_count < _MAX_TOTAL_ATTEMPTS)
        .all()
    )


@dataclass
class RetryRunStats:
    """One tick's worth of background-retry results — returned so
    app/worker.py can log a single structured summary line per run rather
    than one line per message, and so tests can assert on outcomes without
    re-querying the database."""
    candidates: int = 0       # FAILED rows considered
    sent: int = 0              # succeeded this run
    still_failing: int = 0     # attempted, still failing, will retry again later
    exhausted: int = 0         # hit _MAX_TOTAL_ATTEMPTS this run — no further retries
    not_due: int = 0           # FAILED but backoff window hasn't elapsed yet
    skipped_no_text: int = 0   # FAILED row from before message_text existed — can't safely resend
    trade_ids: list[str] = field(default_factory=list)  # trade_ids touched (sent or newly exhausted), for logging


def _due_for_retry(msg: CallMessage, now: datetime) -> bool:
    # retry_count already includes the _MAX_SEND_ATTEMPTS synchronous
    # attempts distribute_call made before ever marking this FAILED — index
    # the backoff table by how many BACKGROUND retries have happened so
    # far, not by the raw total, so the first background attempt waits
    # _BACKGROUND_BACKOFF_S[0] after the synchronous phase ended, not
    # whatever entry the synchronous attempts already used up.
    background_attempts = max(0, msg.retry_count - _MAX_SEND_ATTEMPTS)
    idx = min(background_attempts, len(_BACKGROUND_BACKOFF_S) - 1)
    last_attempt = msg.last_attempt_at or msg.created_at
    if last_attempt.tzinfo is None:  # SQLite doesn't round-trip tzinfo — see app/admin.py::_aware for the same pattern
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    return (now - last_attempt).total_seconds() >= _BACKGROUND_BACKOFF_S[idx]


def process_telegram_retries(db: Session, *, now: datetime | None = None) -> RetryRunStats:
    """The Telegram-retry background job (master-prompt Phase 10 §1).
    Idempotent and safe to call repeatedly/after a restart: every decision
    is driven off columns persisted on the CallMessage row itself
    (retry_count, last_attempt_at, delivery_status), never in-process
    state, and a message is only ever updated in place — never re-created —
    so a crash mid-run just means the next call picks up exactly where the
    last one left off, honoring the same backoff schedule. Commits after
    each message so partial progress survives a crash later in the batch.
    """
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    stats = RetryRunStats()

    candidates = (
        db.query(CallMessage)
        .filter_by(delivery_status=DeliveryStatus.FAILED)
        .order_by(CallMessage.created_at)
        .all()
    )
    for msg in candidates:
        stats.candidates += 1

        if msg.retry_count >= _MAX_TOTAL_ATTEMPTS:
            stats.exhausted += 1
            continue
        if msg.message_text is None:
            stats.skipped_no_text += 1
            log.warning(
                "telegram_retry_skipped_no_text", extra={
                    "call_message_id": str(msg.id), "call_id": str(msg.call_id),
                    "chat_id": msg.telegram_chat_id, "message_type": msg.message_type.value,
                },
            )
            continue
        if not _due_for_retry(msg, now):
            stats.not_due += 1
            continue

        msg.retry_count += 1
        msg.last_attempt_at = now
        outcome = _send_telegram_message(settings.TELEGRAM_BOT_TOKEN, msg.telegram_chat_id, msg.message_text)
        trade_id = msg.call.trade_id if msg.call is not None else None

        if outcome.ok:
            msg.delivery_status = DeliveryStatus.SENT
            msg.telegram_message_id = outcome.telegram_message_id
            msg.sent_at = now
            msg.error = None
            stats.sent += 1
            if trade_id:
                stats.trade_ids.append(trade_id)
            log.info(
                "telegram_retry_succeeded", extra={
                    "call_message_id": str(msg.id), "trade_id": trade_id,
                    "chat_id": msg.telegram_chat_id, "attempt": msg.retry_count,
                },
            )
        else:
            msg.error = outcome.error
            if msg.retry_count >= _MAX_TOTAL_ATTEMPTS:
                stats.exhausted += 1
                if trade_id:
                    stats.trade_ids.append(trade_id)
                log.error(
                    "telegram_retry_exhausted", extra={
                        "call_message_id": str(msg.id), "trade_id": trade_id,
                        "chat_id": msg.telegram_chat_id, "attempts": msg.retry_count, "error": outcome.error,
                    },
                )
            else:
                stats.still_failing += 1
                log.warning(
                    "telegram_retry_failed", extra={
                        "call_message_id": str(msg.id), "trade_id": trade_id,
                        "chat_id": msg.telegram_chat_id, "attempt": msg.retry_count, "error": outcome.error,
                    },
                )
        # Commit per message, not per batch — a later message's failure (or
        # the process dying) must not roll back an earlier message's
        # already-successful send.
        db.commit()

    return stats
