"""Telegram premium-channel access management (Phase 5) — grant/revoke via
Telegram's own supported mechanisms, not a side-channel.

Grant: a single-use invite link (`createChatInviteLink`, member_limit=1) —
the standard Bot API pattern for "let exactly one specific person in" to a
channel the bot administers. Telegram has no "add this user ID directly to
a channel" API for privacy reasons; an invite link the user must tap is the
supported mechanism.

Revoke: `banChatMember` immediately followed by `unbanChatMember` — the
standard idiom for "remove this member now" without a permanent ban, so a
renewed subscriber can rejoin with a fresh invite link later. A bare ban
with no unban would permanently block rejoining even after renewal, which
is wrong for an expired-not-permanently-terminated subscription.

REQUIRES the bot to actually be an administrator of the premium channel
with "Invite Users via Link" and "Ban Users" rights — this code cannot grant
itself those rights; that's a one-time manual step in Telegram's channel
admin settings (documented in DEPLOYMENT.md). Every function here degrades
to a returned error string rather than raising, consistent with the rest of
this codebase's "Sterling_Room being down/misconfigured must not crash the
caller" discipline.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("sterling.telegram_access")

_API_TIMEOUT_S = 10


@dataclass
class AccessResult:
    ok: bool
    invite_link: str | None = None
    error: str | None = None


def _call(bot_token: str, method: str, payload: dict) -> tuple[bool, dict]:
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
        return False, {"ok": False, "description": f"Network error: {e.reason}"}
    except Exception as e:
        return False, {"ok": False, "description": str(e)}
    return bool(data.get("ok")), data


def grant_premium_access(bot_token: str, premium_chat_id: str, *, name_label: str = "") -> AccessResult:
    """Creates a single-use invite link for the premium channel. Caller
    (the bot's PREMIUM flow / admin console) is responsible for sending the
    resulting link to the specific subscriber — Telegram invite links carry
    no user binding themselves beyond the member_limit=1 cap.
    """
    if not bot_token or not premium_chat_id:
        return AccessResult(ok=False, error="Telegram not configured")

    payload = {"chat_id": premium_chat_id, "member_limit": 1, "creates_join_request": False}
    if name_label:
        payload["name"] = name_label[:32]  # Telegram caps invite link names at 32 chars
    ok, data = _call(bot_token, "createChatInviteLink", payload)
    if not ok:
        return AccessResult(ok=False, error=data.get("description", "createChatInviteLink failed"))
    return AccessResult(ok=True, invite_link=data.get("result", {}).get("invite_link"))


def revoke_premium_access(bot_token: str, premium_chat_id: str, telegram_user_id: str) -> AccessResult:
    """ban then immediately unban — removes the member now, without
    permanently blocking a future rejoin after renewal."""
    if not bot_token or not premium_chat_id:
        return AccessResult(ok=False, error="Telegram not configured")

    ok, data = _call(bot_token, "banChatMember", {"chat_id": premium_chat_id, "user_id": int(telegram_user_id)})
    if not ok:
        # "not enough rights" / "user not found" / already-not-a-member are
        # all plausible and not fatal to the caller's revoke workflow —
        # surface the reason, let the caller decide whether to treat it as
        # an error or a no-op (e.g. the user never actually joined).
        return AccessResult(ok=False, error=data.get("description", "banChatMember failed"))

    ok2, data2 = _call(bot_token, "unbanChatMember",
                        {"chat_id": premium_chat_id, "user_id": int(telegram_user_id), "only_if_banned": True})
    if not ok2:
        log.warning("revoke_premium_access: ban succeeded but unban failed for %s: %s",
                    telegram_user_id, data2.get("description"))
        # Still report success for the revoke itself — the member IS removed,
        # which was the actual goal; the unban failing just means a manual
        # follow-up may be needed before they can rejoin via a fresh link.
    return AccessResult(ok=True)
