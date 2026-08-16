"""Tests for app/bot.py (Phase 4 — interactive Telegram bot).

Transport (telegram_bot.send_message / answer_callback_query) is mocked the
same way test_telegram_access.py mocks the Bot API transport — these tests
verify what the bot DOES (db state, routing, idempotency), not that HTTP
calls to api.telegram.org succeed.
"""
import datetime as dt

import pytest

from app import bot
from app.config import Settings
from app.models import (
    Call, CallStatus, Plan, SupportTicket, TelegramUpdateLog, TicketCategory,
)
from app import services


def _settings(**overrides):
    kw = dict(
        DATABASE_URL="sqlite:///:memory:",
        ADAPTER_API_KEYS="test-key-123",
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_FREE_CHANNEL_LINK="https://t.me/sterling_room_free",
    )
    kw.update(overrides)
    return Settings(**kw)


@pytest.fixture()
def sent(monkeypatch):
    """Captures every outbound send_message/answer_callback_query call
    instead of hitting the network, and returns the list for assertions."""
    calls = []

    def fake_send(bot_token, chat_id, text, *, reply_markup=None):
        calls.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        from app.telegram_bot import SendResult
        return SendResult(ok=True, telegram_message_id="1")

    def fake_answer(bot_token, callback_query_id, *, text=""):
        from app.telegram_bot import SendResult
        return SendResult(ok=True)

    monkeypatch.setattr(bot.telegram_bot, "send_message", fake_send)
    monkeypatch.setattr(bot.telegram_bot, "answer_callback_query", fake_answer)
    return calls


def _message_update(update_id, text, *, chat_id="1001", user_id="1001", username="alice", first_name="Alice"):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "from": {"id": int(user_id), "username": username, "first_name": first_name},
            "text": text,
        },
    }


def _callback_update(update_id, data, *, chat_id="1001", user_id="1001"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cbq1",
            "data": data,
            "message": {"chat": {"id": int(chat_id)}},
            "from": {"id": int(user_id)},
        },
    }


def _make_plan(db, **overrides):
    p = dict(plan_id="MONTHLY", name="Monthly", duration_days=30, price=49.0, currency="USD")
    p.update(overrides)
    plan = Plan(**p)
    db.add(plan)
    db.flush()
    return plan


# ══════════════════════════════════════════════════════════════════════════
# /start and main menu
# ══════════════════════════════════════════════════════════════════════════
def test_start_creates_telegram_user_and_sends_menu(db, sent):
    settings = _settings()
    bot.handle_update(db, _message_update(1, "/start"), settings)

    from app.models import TelegramUser
    tu = db.query(TelegramUser).filter_by(telegram_user_id="1001").one()
    assert tu.telegram_username == "alice"
    assert tu.display_name == "Alice"

    assert len(sent) == 1
    assert "STERLING_ROOM" in sent[0]["text"]
    labels = [btn["text"] for row in sent[0]["reply_markup"]["inline_keyboard"] for btn in row]
    assert labels == ["FREE ACCESS", "PREMIUM", "PERFORMANCE", "HOW IT WORKS", "MY SUBSCRIPTION", "SUPPORT"]


def test_start_does_not_duplicate_existing_user(db, sent):
    settings = _settings()
    bot.handle_update(db, _message_update(1, "/start"), settings)
    bot.handle_update(db, _message_update(2, "/start"), settings)

    from app.models import TelegramUser
    count = db.query(TelegramUser).filter_by(telegram_user_id="1001").count()
    assert count == 1


def test_unrecognized_command_is_silently_ignored(db, sent):
    settings = _settings()
    bot.handle_update(db, _message_update(1, "/nonsense"), settings)
    assert sent == []


# ══════════════════════════════════════════════════════════════════════════
# FREE ACCESS
# ══════════════════════════════════════════════════════════════════════════
def test_free_access_shows_channel_link_and_creates_subscriber(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:free"), settings)

    from app.models import TelegramUser, Subscriber
    tu = db.query(TelegramUser).filter_by(telegram_user_id="1001").one()
    assert tu.acquisition_source == "bot:free_access"
    assert db.query(Subscriber).filter_by(telegram_user_id=tu.id).count() == 1

    assert "https://t.me/sterling_room_free" in str(sent[0]["reply_markup"])


def test_free_access_without_configured_link(db, sent):
    settings = _settings(TELEGRAM_FREE_CHANNEL_LINK="")
    bot.handle_update(db, _callback_update(1, "menu:free"), settings)
    assert "isn't configured yet" in sent[0]["text"]


def test_free_access_does_not_duplicate_subscriber_on_repeat_taps(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:free"), settings)
    bot.handle_update(db, _callback_update(2, "menu:free"), settings)

    from app.models import Subscriber
    assert db.query(Subscriber).count() == 1


# ══════════════════════════════════════════════════════════════════════════
# PREMIUM / plan selection / payment instructions
# ══════════════════════════════════════════════════════════════════════════
def test_premium_lists_active_plans_only(db, sent):
    _make_plan(db, plan_id="MONTHLY", name="Monthly", price=49.0)
    _make_plan(db, plan_id="INACTIVE", name="Retired Plan", price=1.0, active=False)
    db.commit()

    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:premium"), settings)

    text = sent[0]["text"]
    labels = [btn["text"] for row in sent[0]["reply_markup"]["inline_keyboard"] for btn in row]
    assert any("Monthly" in l for l in labels)
    assert not any("Retired Plan" in l for l in labels)


def test_premium_with_no_plans_configured(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:premium"), settings)
    assert "No plans are configured yet" in sent[0]["text"]


def test_select_plan_creates_pending_subscription_and_shows_payment_instructions(db, sent):
    plan = _make_plan(db)
    db.commit()

    settings = _settings()
    bot.handle_update(db, _callback_update(1, f"plan:{plan.plan_id}"), settings)

    from app.models import Subscription, SubscriptionStatus
    subscription = db.query(Subscription).one()
    assert subscription.status == SubscriptionStatus.PENDING_PAYMENT

    text = sent[0]["text"]
    assert "PAY FOR MONTHLY" in text
    assert "Reference" in text
    labels = [btn["text"] for row in sent[0]["reply_markup"]["inline_keyboard"] for btn in row]
    assert "I'VE PAID" in labels


def test_select_plan_unknown_plan_id(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "plan:DOES-NOT-EXIST"), settings)
    assert "no longer available" in sent[0]["text"]


def test_mark_paid_files_payment_ticket_and_does_not_auto_activate(db, sent):
    plan = _make_plan(db)
    db.commit()

    settings = _settings()
    bot.handle_update(db, _callback_update(1, f"plan:{plan.plan_id}"), settings)

    from app.models import Payment, Subscription, SubscriptionStatus
    payment = db.query(Payment).one()
    bot.handle_update(db, _callback_update(2, f"paid:{payment.provider_payment_id}"), settings)

    ticket = db.query(SupportTicket).one()
    assert ticket.category == TicketCategory.PAYMENT
    assert payment.provider_payment_id in ticket.message

    # Critically: no auto-activation. Payment/subscription remain pending —
    # only an admin confirming via subscriptions.confirm_payment() activates.
    subscription = db.query(Subscription).one()
    assert subscription.status == SubscriptionStatus.PENDING_PAYMENT
    assert payment.status.value == "PENDING"

    assert "flagged this for confirmation" in sent[-1]["text"]


# ══════════════════════════════════════════════════════════════════════════
# PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════
def test_performance_with_no_trades(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:performance"), settings)
    assert "No completed trades" in sent[0]["text"]


def test_performance_shows_stats_from_ledger(db, sent):
    call = services.create_call(db, dict(
        source_call_id="bot-perf-1", instrument="EURUSD", direction="BUY", stop_loss=1.08,
    ), actor="test")
    db.commit()
    from app.models import CallEventType
    services.transition_call(db, call, CallStatus.CLOSED, actor="test", event_type=CallEventType.CALL_CLOSED)
    call.result_r = 2.0
    db.commit()

    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:performance"), settings)

    text = sent[0]["text"]
    assert "Trades: 1" in text
    assert "Net R: +2.0" in text


# ══════════════════════════════════════════════════════════════════════════
# MY SUBSCRIPTION
# ══════════════════════════════════════════════════════════════════════════
def test_my_subscription_with_none(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:mysub"), settings)
    assert "don't have a subscription yet" in sent[0]["text"]


def test_my_subscription_shows_active_plan(db, sent):
    from app import subscriptions as subs
    from app.payments import ManualPaymentProvider

    plan = _make_plan(db)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="1001")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=ManualPaymentProvider(), actor="bot")
    subs.confirm_payment(db, payment, actor="admin:test")
    db.commit()

    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:mysub"), settings)

    text = sent[0]["text"]
    assert "Monthly" in text
    assert "ACTIVE" in text


# ══════════════════════════════════════════════════════════════════════════
# HOW IT WORKS / SUPPORT
# ══════════════════════════════════════════════════════════════════════════
def test_how_it_works_static_text(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:how"), settings)
    assert sent[0]["text"] == bot.HOW_IT_WORKS_TEXT


def test_support_prompt_then_free_text_files_ticket(db, sent):
    settings = _settings()
    bot.handle_update(db, _callback_update(1, "menu:support"), settings)
    assert "type your message" in sent[0]["text"]

    bot.handle_update(db, _message_update(2, "My payment isn't showing up"), settings)
    ticket = db.query(SupportTicket).one()
    assert ticket.category == TicketCategory.OTHER
    assert ticket.message == "My payment isn't showing up"
    assert "we'll get back to you" in sent[-1]["text"]


def test_empty_text_message_is_ignored(db, sent):
    settings = _settings()
    bot.handle_update(db, _message_update(1, ""), settings)
    assert sent == []
    assert db.query(SupportTicket).count() == 0


# ══════════════════════════════════════════════════════════════════════════
# Duplicate-update-id idempotency (explicit user requirement: "8. Duplicate-
# event tests" — a retried webhook delivery must not double-process).
# ══════════════════════════════════════════════════════════════════════════
def test_duplicate_update_id_is_a_noop(db, sent):
    settings = _settings()
    update = _message_update(42, "/start")

    bot.handle_update(db, update, settings)
    assert len(sent) == 1
    assert db.query(TelegramUpdateLog).filter_by(update_id=42).count() == 1

    # Redeliver the exact same update (Telegram's documented at-least-once
    # webhook behavior) — must not send a second menu or create a 2nd user row.
    bot.handle_update(db, update, settings)
    assert len(sent) == 1
    assert db.query(TelegramUpdateLog).filter_by(update_id=42).count() == 1

    from app.models import TelegramUser
    assert db.query(TelegramUser).filter_by(telegram_user_id="1001").count() == 1


def test_duplicate_callback_update_id_is_a_noop(db, sent):
    plan = _make_plan(db)
    db.commit()
    settings = _settings()
    update = _callback_update(7, f"plan:{plan.plan_id}")

    bot.handle_update(db, update, settings)
    bot.handle_update(db, update, settings)

    from app.models import Subscription
    assert db.query(Subscription).count() == 1
    assert len(sent) == 1


def test_missing_update_id_is_ignored_without_error(db, sent):
    bot.handle_update(db, {"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/start"}}, _settings())
    assert sent == []


def test_error_during_processing_rolls_back_but_still_marks_update_processed(db, sent, monkeypatch):
    """If something downstream throws, the dedup marker must survive (it was
    committed independently before processing) so a redelivered copy of a
    poison update doesn't retry forever, but the partial DB writes from the
    failed attempt must not be left dangling either."""
    def boom(*a, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(bot, "_handle_message", boom)
    settings = _settings()
    update = _message_update(99, "/start")

    bot.handle_update(db, update, settings)  # must not raise
    assert db.query(TelegramUpdateLog).filter_by(update_id=99).count() == 1
