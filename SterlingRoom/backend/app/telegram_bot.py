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
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Call, CallDirection, CallMessage, DeliveryStatus, MessageType
from app.services import content_hash

log = logging.getLogger("sterling.telegram_bot")

_API_TIMEOUT_S = 12
_MAX_SEND_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1, 3)  # between attempt 1->2 and 2->3


# ══════════════════════════════════════════════════════════════════════════
# Templates (master-prompt §21-25) — configurable via the settings dict
# passed in, never hardcoded business copy scattered through the codebase.
# ══════════════════════════════════════════════════════════════════════════
def render_entry_message(call: Call) -> str:
    arrow = "🔴" if call.direction == CallDirection.SELL else "🟢"
    entry = (f"{call.entry_min} – {call.entry_max}" if call.entry_min and call.entry_max
              else (str(call.entry_min or call.entry_max) if (call.entry_min or call.entry_max) else "MARKET"))
    lines = [
        "━━━━━━━━━━━━━━━━",
        "STERLING_ROOM",
        "TRADE CALL",
        "━━━━━━━━━━━━━━━━",
        "",
        f"{arrow} {call.instrument} — {call.direction.value}",
        "",
        "ENTRY",
        entry,
        "",
        "STOP LOSS",
        str(call.stop_loss),
    ]
    if call.tp1:
        lines += ["", "TARGET 1", str(call.tp1)]
    if call.tp2:
        lines += ["", "TARGET 2", str(call.tp2)]
    if call.risk_percent:
        lines += ["", "RISK", f"{call.risk_percent}%"]
    if call.setup_type:
        lines += ["", "SETUP", call.setup_type]
    if call.invalidation:
        lines += ["", "INVALIDATION", call.invalidation]
    lines += ["", "TRADE ID", call.trade_id, "", "━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


def render_update_message(call: Call, update_text: str, update_number: int) -> str:
    return "\n".join([
        "STERLING_ROOM", "TRADE UPDATE", "",
        "TRADE ID", call.trade_id, "",
        f"UPDATE #{update_number}", "", update_text, "",
        "STATUS", call.status.value,
    ])


def render_tp1_message(call: Call, management_instruction: str = "") -> str:
    lines = ["STERLING_ROOM", "TP1 HIT", "", "TRADE ID", call.trade_id, "", "TP1 reached."]
    if management_instruction:
        lines += ["", "MANAGEMENT", management_instruction]
    lines += ["", "REMAINING POSITION", "TP2 active."]
    return "\n".join(lines)


def render_exit_message(call: Call) -> str:
    result = f"{'+' if (call.result_r or 0) >= 0 else ''}{call.result_r}R" if call.result_r is not None else "—"
    return "\n".join([
        "STERLING_ROOM", "TRADE CLOSED", "",
        "TRADE ID", call.trade_id, "",
        "RESULT", result, "",
        "STATUS", "COMPLETED",
    ])


def render_invalidated_message(call: Call, reason: str = "") -> str:
    lines = ["STERLING_ROOM", "SETUP INVALIDATED", "", "TRADE ID", call.trade_id, "", "No trade."]
    if reason:
        lines += ["", "REASON", reason]
    return "\n".join(lines)


_RENDERERS = {
    MessageType.ENTRY: lambda call, **kw: render_entry_message(call),
    MessageType.TP1: lambda call, **kw: render_tp1_message(call, kw.get("management_instruction", "")),
    MessageType.EXIT: lambda call, **kw: render_exit_message(call),
    MessageType.INVALIDATED: lambda call, **kw: render_invalidated_message(call, kw.get("reason", "")),
    MessageType.UPDATE: lambda call, **kw: render_update_message(call, kw.get("update_text", ""), kw.get("update_number", 1)),
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
        if existing is None:
            db.add(msg)
            db.flush()

        last_error = None
        for attempt in range(_MAX_SEND_ATTEMPTS):
            outcome = _send_telegram_message(settings.TELEGRAM_BOT_TOKEN, chat_id, text)
            if outcome.ok:
                msg.delivery_status = DeliveryStatus.SENT
                msg.telegram_message_id = outcome.telegram_message_id
                msg.sent_at = datetime.now(timezone.utc)
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


def mark_for_retry(db: Session) -> list[CallMessage]:
    """Not called by anything yet — the hook a future background worker
    (master-prompt §48 "Telegram delivery") would poll. Documented here
    rather than silently omitted."""
    return db.query(CallMessage).filter_by(delivery_status=DeliveryStatus.FAILED).all()
