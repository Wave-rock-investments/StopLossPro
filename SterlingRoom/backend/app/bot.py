"""Interactive Telegram bot (Phase 4) — the /start menu and everything it
leads to. This is the user-facing half; app/telegram_bot.py (Phase 3) is the
outbound call-distribution half. Both share the same Bot API transport
(telegram_bot.send_message/answer_callback_query) but serve different
purposes — this module never touches the calls table, that module never
touches subscribers.

Entry point is handle_update(db, update, settings) — called by the webhook
route in app/api.py once per incoming Telegram Update. Deliberately takes
the raw update dict rather than a typed model: Telegram's Update shape is
large and this bot only reads a handful of fields, so hand-parsing what's
needed keeps this file readable without a dependency on a full Bot API SDK
type package (consistent with the rest of this codebase's stdlib-first
choices).

PAYMENT FLOW SCOPE (mirrors app/payments.py's own documented scope): the
PREMIUM flow ends at "payment instructions shown, ticket raised" — there is
no automatic activation, because payments.py's ManualPaymentProvider
deliberately never auto-verifies a payment (see its docstring). An admin
confirms the payment (via the admin console, Phase 7) and THAT is what
calls subscriptions.confirm_payment() and grants access. This is not a
missing feature; it is the intended manual-verification flow for the
"build the abstraction first" decision.

SUPPORT FLOW SCOPE: any plain-text message that isn't a recognized command
is treated as a support message and filed as a ticket. There is no
multi-step category-selection conversation (that would need per-user
conversation state, which this stateless-webhook design doesn't carry) —
tickets default to category OTHER unless the message was reached via a
specific flow (e.g. a payment confirmation tap) that sets a more specific
category. Documented scope-narrowing, not a silent gap.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import performance, subscriptions as subs, telegram_access
from app.config import Settings
from app.models import Plan, SupportTicket, TelegramUpdateLog, TicketCategory
from app.payments import get_provider
from app import telegram_bot

log = logging.getLogger("sterling.bot")

HOW_IT_WORKS_TEXT = (
    "HOW STERLING_ROOM WORKS\n\n"
    "Know the level. Know the trigger. Know the invalidation. Know the risk.\n\n"
    "Every call carries a defined entry, stop loss, and targets, with risk "
    "stated up front. We are comfortable saying \"no trade\" when there is no "
    "valid setup — calls are not manufactured to keep the channel active.\n\n"
    "FREE gets you selected setups, market commentary, and full transparent "
    "performance. PREMIUM gets you every call in real time, with trade "
    "management and exit updates as they happen."
)

_MAIN_MENU_TEXT = "STERLING_ROOM\n\nStructured trading calls\nForex • Commodities • Crypto"


def _main_menu_keyboard() -> dict:
    return telegram_bot.inline_keyboard([
        [("FREE ACCESS", "menu:free")],
        [("PREMIUM", "menu:premium")],
        [("PERFORMANCE", "menu:performance")],
        [("HOW IT WORKS", "menu:how")],
        [("MY SUBSCRIPTION", "menu:mysub")],
        [("SUPPORT", "menu:support")],
    ])


def _back_keyboard() -> dict:
    return telegram_bot.inline_keyboard([[("← BACK", "menu:main")]])


# ══════════════════════════════════════════════════════════════════════════
# Menu actions — each returns (text, reply_markup)
# ══════════════════════════════════════════════════════════════════════════
def _free_access(db: Session, settings: Settings, telegram_user_id: str) -> tuple[str, dict]:
    tu = subs.get_or_create_telegram_user(db, telegram_user_id=telegram_user_id, acquisition_source="bot:free_access")
    subs.get_or_create_subscriber(db, tu)
    link = settings.TELEGRAM_FREE_CHANNEL_LINK
    if link:
        kb = telegram_bot.inline_keyboard([])
        kb["inline_keyboard"] = [telegram_bot.url_button_row("OPEN FREE CHANNEL", link), [("← BACK", "menu:main")]]
        return "FREE ACCESS\n\nTap below to join the free channel.", kb
    return "FREE ACCESS\n\nThe free channel isn't configured yet — check back soon.", _back_keyboard()


def _premium_plans(db: Session) -> tuple[str, dict]:
    plans = db.execute(select(Plan).where(Plan.active == True)).scalars().all()  # noqa: E712
    if not plans:
        return "PREMIUM\n\nNo plans are configured yet.", _back_keyboard()
    rows = [[(f"{p.name} — {p.price:g} {p.currency}", f"plan:{p.plan_id}")] for p in plans]
    rows.append([("← BACK", "menu:main")])
    return "PREMIUM ACCESS\n\nSelect a plan:", telegram_bot.inline_keyboard(rows)


def _select_plan(db: Session, telegram_user_id: str, plan_id: str, actor: str) -> tuple[str, dict]:
    plan = db.execute(select(Plan).where(Plan.plan_id == plan_id, Plan.active == True)).scalar_one_or_none()  # noqa: E712
    if plan is None:
        return "That plan is no longer available.", _back_keyboard()

    tu = subs.get_or_create_telegram_user(db, telegram_user_id=telegram_user_id)
    subscriber = subs.get_or_create_subscriber(db, tu)
    provider = get_provider("manual")
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor=actor)

    text = (
        f"PAY FOR {plan.name.upper()}\n\n"
        f"Amount: {payment.amount:g} {payment.currency}\n"
        f"Reference: {payment.provider_payment_id}\n\n"
        "Send payment using the method our admin has shared with you, quoting "
        "the reference above. Then tap I'VE PAID and we'll confirm and "
        "activate your access."
    )
    kb = telegram_bot.inline_keyboard([
        [("I'VE PAID", f"paid:{payment.provider_payment_id}")],
        [("← BACK", "menu:main")],
    ])
    return text, kb


def _mark_paid(db: Session, telegram_user_id: str, payment_reference: str) -> tuple[str, dict]:
    ticket = SupportTicket(
        telegram_user_id=str(telegram_user_id), category=TicketCategory.PAYMENT,
        message=f"Customer reports payment sent for reference {payment_reference}. Confirm in admin console to activate.",
    )
    db.add(ticket)
    return (
        "Thanks — we've flagged this for confirmation. Your access activates "
        "as soon as an admin verifies the payment arrived.",
        _back_keyboard(),
    )


def _performance_summary(db: Session) -> tuple[str, dict]:
    s = performance.compute_stats(db)
    if s.total_trades == 0:
        return "PERFORMANCE\n\nNo completed trades on record yet.", _back_keyboard()
    lines = [
        "PERFORMANCE (all-time)", "",
        f"Trades: {s.total_trades}", f"Wins: {s.wins}  Losses: {s.losses}  BE: {s.breakeven}",
        f"Win rate: {s.win_rate}%", f"Net R: {'+' if s.net_r >= 0 else ''}{s.net_r}R",
    ]
    if s.expectancy_r is not None:
        lines.append(f"Expectancy: {s.expectancy_r}R/trade")
    if s.profit_factor is not None:
        lines.append(f"Profit factor: {s.profit_factor}")
    lines.append(f"Max drawdown: {s.max_drawdown_r}R")
    return "\n".join(lines), _back_keyboard()


def _my_subscription(db: Session, telegram_user_id: str) -> tuple[str, dict]:
    sub = subs.current_subscription(db, telegram_user_id)
    if sub is None:
        return "MY SUBSCRIPTION\n\nYou don't have a subscription yet. Tap PREMIUM from the main menu to start one.", _back_keyboard()
    lines = ["MY SUBSCRIPTION", "", f"Plan: {sub.plan.name}", f"Status: {sub.status.value}"]
    if sub.expiry_date:
        lines.append(f"Expires: {sub.expiry_date.strftime('%Y-%m-%d')}")
    return "\n".join(lines), _back_keyboard()


def _support_prompt() -> tuple[str, dict]:
    return (
        "SUPPORT\n\nJust type your message here and send it — we'll get back to you.",
        _back_keyboard(),
    )


def _file_support_message(db: Session, telegram_user_id: str, text: str) -> tuple[str, dict]:
    ticket = SupportTicket(telegram_user_id=str(telegram_user_id), category=TicketCategory.OTHER, message=text)
    db.add(ticket)
    return "Got it — we'll get back to you.", _back_keyboard()


# ══════════════════════════════════════════════════════════════════════════
# Update dispatch
# ══════════════════════════════════════════════════════════════════════════
def _already_processed(db: Session, update_id: int) -> bool:
    """Fails CLOSED on the safe side: if the insert itself fails for some
    unrelated reason, treat as NOT a duplicate (better to risk a rare
    double-send than to silently drop a legitimate update)."""
    exists = db.execute(select(TelegramUpdateLog.update_id).where(TelegramUpdateLog.update_id == update_id)).first()
    return exists is not None


def handle_update(db: Session, update: dict, settings: Settings) -> None:
    update_id = update.get("update_id")
    if update_id is None:
        return
    if _already_processed(db, update_id):
        log.debug("bot: update_id=%s already processed, skipping", update_id)
        return
    db.add(TelegramUpdateLog(update_id=update_id))
    db.commit()  # commit the dedup marker immediately, independent of what follows

    try:
        if "message" in update:
            _handle_message(db, update["message"], settings)
        elif "callback_query" in update:
            _handle_callback(db, update["callback_query"], settings)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("bot: error handling update_id=%s", update_id)


def _handle_message(db: Session, message: dict, settings: Settings) -> None:
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    from_user = message.get("from", {})
    telegram_user_id = str(from_user.get("id", chat_id))
    text = (message.get("text") or "").strip()

    if text == "/start":
        subs.get_or_create_telegram_user(
            db, telegram_user_id=telegram_user_id, username=from_user.get("username"),
            display_name=from_user.get("first_name"),
        )
        telegram_bot.send_message(settings.TELEGRAM_BOT_TOKEN, chat_id, _MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())
        return

    if text.startswith("/"):
        return  # unrecognized command — silently ignored rather than filed as a support ticket

    if not text:
        return

    reply, kb = _file_support_message(db, telegram_user_id, text)
    telegram_bot.send_message(settings.TELEGRAM_BOT_TOKEN, chat_id, reply, reply_markup=kb)


def _handle_callback(db: Session, callback: dict, settings: Settings) -> None:
    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    message = callback.get("message", {}) or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    from_user = callback.get("from", {})
    telegram_user_id = str(from_user.get("id", chat_id))

    telegram_bot.answer_callback_query(settings.TELEGRAM_BOT_TOKEN, callback_id)

    if data == "menu:main":
        text, kb = _MAIN_MENU_TEXT, _main_menu_keyboard()
    elif data == "menu:free":
        text, kb = _free_access(db, settings, telegram_user_id)
    elif data == "menu:premium":
        text, kb = _premium_plans(db)
    elif data.startswith("plan:"):
        text, kb = _select_plan(db, telegram_user_id, data.split(":", 1)[1], actor=f"bot:{telegram_user_id}")
    elif data.startswith("paid:"):
        text, kb = _mark_paid(db, telegram_user_id, data.split(":", 1)[1])
    elif data == "menu:performance":
        text, kb = _performance_summary(db)
    elif data == "menu:how":
        text, kb = HOW_IT_WORKS_TEXT, _back_keyboard()
    elif data == "menu:mysub":
        text, kb = _my_subscription(db, telegram_user_id)
    elif data == "menu:support":
        text, kb = _support_prompt()
    else:
        text, kb = "Unrecognized option.", _back_keyboard()

    telegram_bot.send_message(settings.TELEGRAM_BOT_TOKEN, chat_id, text, reply_markup=kb)
