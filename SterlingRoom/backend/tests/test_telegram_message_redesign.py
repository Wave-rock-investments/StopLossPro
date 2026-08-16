"""2026-08-16 Telegram signal UI redesign — presentation-layer only.

Verifies every item on the redesign's explicit test checklist. This file
does NOT re-test call/entry/SL/TP/R:R business logic, delivery routing, the
15-minute Free delay, or subscription/payment behavior — those are
unchanged by this redesign and already covered by test_worker.py,
test_telegram_bot.py, test_services.py, and test_end_to_end.py. What's
verified here is specifically: the new visual format, that price numbers
land where they should (Premium) and never where they shouldn't (Free),
that no bot/channel URL footer exists on any trading-call message, that
results still come from the one authoritative `calls.result_r` rather than
a second calculation, and that redesigning the renderers didn't disturb
distribute_call's dedup/retry mechanics or the StopLossPro adapter's API
contract.
"""
import uuid

from app import services, telegram_bot
from app.models import Call, CallDirection, CallStatus, DeliveryStatus, MessageType


def _make_call(db, **overrides) -> Call:
    fields = dict(
        trade_id=f"SR-260816-{uuid.uuid4().hex[:3]}", source_call_id=uuid.uuid4().hex,
        instrument="BTCUSD", direction=CallDirection.BUY,
        entry_min=63209.70, stop_loss=63123.75, tp1=63381.60, tp2=63467.55, tp3=63553.50,
        status=CallStatus.ACTIVE, route_premium=True,
    )
    fields.update(overrides)
    call = Call(**fields)
    db.add(call)
    db.flush()
    return call


# ── 1. Premium message uses the new format ──────────────────────────────
def test_premium_message_uses_the_new_boxed_format(db):
    call = _make_call(db)
    text = telegram_bot.render_entry_message(call)
    assert telegram_bot._BOX_TL in text and telegram_bot._BOX_BR in text  # ╔ ... ╝ boxed header
    assert telegram_bot._DIVIDER in text  # ━━━ section dividers
    assert telegram_bot._DIAMOND in text  # ◈ field markers


# ── 2-6. Premium message contains Entry / SL / TP1 / TP2 / TP3 ──────────
def test_premium_message_contains_entry_price(db):
    call = _make_call(db, entry_min=63209.70, entry_max=None)
    text = telegram_bot.render_entry_message(call)
    assert telegram_bot._format_price(63209.70) in text


def test_premium_message_contains_sl_price(db):
    call = _make_call(db, stop_loss=63123.75)
    text = telegram_bot.render_entry_message(call)
    assert telegram_bot._format_price(63123.75) in text


def test_premium_message_contains_tp1_price(db):
    call = _make_call(db, tp1=63381.60)
    text = telegram_bot.render_entry_message(call)
    assert telegram_bot._format_price(63381.60) in text


def test_premium_message_contains_tp2_price(db):
    call = _make_call(db, tp2=63467.55)
    text = telegram_bot.render_entry_message(call)
    assert telegram_bot._format_price(63467.55) in text


def test_premium_message_contains_tp3_price(db):
    call = _make_call(db, tp3=63553.50)
    text = telegram_bot.render_entry_message(call)
    assert telegram_bot._format_price(63553.50) in text


# ── 7. Premium message contains R:R ──────────────────────────────────────
def test_premium_message_contains_dynamically_calculated_rr(db):
    # entry 63209.70, sl 63123.75 -> 1R = 85.95
    # tp1 63381.60 -> reward 171.90 -> exactly 2R ; tp2 -> 3R ; tp3 -> 4R
    call = _make_call(db, entry_min=63209.70, entry_max=None, stop_loss=63123.75,
                       tp1=63381.60, tp2=63467.55, tp3=63553.50)
    text = telegram_bot.render_entry_message(call)
    assert "1:2" in text
    assert "1:3" in text
    assert "1:4" in text


def test_rr_is_derived_from_this_calls_own_values_not_hardcoded(db):
    """Different entry/SL/TP geometry must produce a different ratio —
    proves the R:R shown isn't a fixed/hardcoded string."""
    call = _make_call(db, instrument="EURUSD", entry_min=1.1000, entry_max=None,
                       stop_loss=1.0950, tp1=1.1100, tp2=None, tp3=None)
    text = telegram_bot.render_entry_message(call)
    assert "1:2" in text  # risk .0050, reward .0100 -> exactly 2R
    assert "63,381.60" not in text  # not leaking the other test's fixture values
    assert "BTCUSD" not in text
    assert "EURUSD" in text


# ── 8. Premium message does NOT contain the old bot URL ─────────────────
def test_premium_message_has_no_bot_url_footer(db):
    call = _make_call(db)
    text = telegram_bot.render_entry_message(call)
    assert "https://t.me/SterlingroomBot" not in text
    assert "t.me/" not in text


def test_no_trading_call_message_type_has_a_bot_url_footer(db):
    """Sweeps every renderer that produces a trading-call/update/result
    message (not /start or onboarding, which are explicitly out of scope
    and keep their own required links elsewhere)."""
    call = _make_call(db, status=CallStatus.CLOSED, result_r=2.0)
    texts = [
        telegram_bot.render_entry_message(call),
        telegram_bot.render_free_teaser_message(call),
        telegram_bot.render_tp1_message(call),
        telegram_bot.render_update_message(call, "note", 1),
        telegram_bot.render_exit_message(call),
        telegram_bot.render_invalidated_message(call, "reason"),
        telegram_bot.render_results_message(call),
    ]
    for text in texts:
        assert "https://t.me/SterlingroomBot" not in text
        assert "t.me/" not in text


# ── 9-13. Free message does NOT contain any execution price ─────────────
def test_free_message_does_not_contain_entry_sl_tp1_tp2_tp3_prices(db):
    call = _make_call(
        db, entry_min=63209.70, entry_max=None, stop_loss=63123.75,
        tp1=63381.60, tp2=63467.55, tp3=63553.50,
    )
    text = telegram_bot.render_free_teaser_message(call)
    for raw in (call.entry_min, call.stop_loss, call.tp1, call.tp2, call.tp3):
        assert str(raw) not in text
        assert telegram_bot._format_price(raw) not in text


def test_free_message_stays_attractive_and_conversion_oriented(db):
    call = _make_call(db)
    text = telegram_bot.render_free_teaser_message(call)
    assert "Premium" in text
    assert call.instrument in text
    assert call.direction.value in text
    assert call.trade_id in text


# ── 14. Result message uses the new visual language ─────────────────────
# Outcome words (WIN/LOSS/BREAKEVEN) are fixed vocabulary, like the section
# labels — rendered bold, same as the mockup's "STATUS  WIN" line — unlike
# the instrument ticker/direction, which are left as plain, greppable text
# (see render_entry_message/render_results_message: call.instrument and
# call.direction.value are deliberately never passed through _bold()).
def test_result_message_uses_the_new_boxed_format(db):
    call = _make_call(db, status=CallStatus.CLOSED, result_r=2.0)
    text = telegram_bot.render_results_message(call)
    assert telegram_bot._BOX_TL in text and telegram_bot._BOX_BR in text
    assert telegram_bot._DIVIDER in text
    assert telegram_bot._bold("WIN") in text
    assert "BTCUSD" in text  # instrument stays plain/greppable
    assert "BUY" in text     # direction stays plain/greppable


def test_losing_and_breakeven_results_use_the_same_visual_hierarchy(db):
    losing = _make_call(db, status=CallStatus.STOPPED, result_r=-1.0)
    breakeven = _make_call(db, status=CallStatus.CLOSED, result_r=0.0)
    losing_text = telegram_bot.render_results_message(losing)
    breakeven_text = telegram_bot.render_results_message(breakeven)
    assert telegram_bot._bold("LOSS") in losing_text and telegram_bot._BOX_TL in losing_text
    assert telegram_bot._bold("BREAKEVEN") in breakeven_text and telegram_bot._BOX_TL in breakeven_text


# ── 15. Result uses the existing authoritative calls.result_r ───────────
def test_result_message_uses_authoritative_result_r_not_a_recalculation(db):
    call = _make_call(db, status=CallStatus.CLOSED, result_r=2.0)
    text = telegram_bot.render_results_message(call)
    assert telegram_bot._format_result_r(call.result_r) in text
    # Changing the stored value changes the rendered value 1:1 — proof
    # there's no independent computation happening in the renderer.
    call.result_r = -1.5
    text2 = telegram_bot.render_results_message(call)
    assert telegram_bot._format_result_r(-1.5) in text2
    assert "-1.5R" in text2


# ── 16. Existing duplicate protection remains intact ────────────────────
def test_dedup_protection_still_works_after_redesign(db, monkeypatch):
    from unittest.mock import patch

    call = services.create_call(
        db, dict(source_call_id="redesign-dedup-1", instrument="BTCUSD",
                 direction="BUY", stop_loss=63123.75), actor="test",
    )
    db.commit()
    with patch.object(telegram_bot, "_send_telegram_message") as mock_send:
        mock_send.return_value = telegram_bot.SendResult(ok=True, telegram_message_id="1")
        telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()
        telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()
    assert mock_send.call_count == 1  # second call was a no-op, same as before the redesign


# ── 17. Existing retry behavior remains intact ───────────────────────────
def test_retry_behavior_still_works_after_redesign(db):
    from unittest.mock import patch

    call = services.create_call(
        db, dict(source_call_id="redesign-retry-1", instrument="BTCUSD",
                 direction="BUY", stop_loss=63123.75), actor="test",
    )
    db.commit()
    with patch.object(telegram_bot, "_send_telegram_message") as mock_send, \
         patch.object(telegram_bot.time, "sleep"):
        mock_send.return_value = telegram_bot.SendResult(ok=False, error="Telegram: bot was blocked")
        msgs = telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()
    assert msgs[0].delivery_status == DeliveryStatus.FAILED
    assert msgs[0].retry_count == telegram_bot._MAX_SEND_ATTEMPTS
    assert mock_send.call_count == telegram_bot._MAX_SEND_ATTEMPTS


# ── 18. Existing StopLossPro adapter compatibility remains intact ───────
def test_stoplosspro_adapter_payload_shape_still_creates_a_call_and_renders(db):
    """The exact minimal payload shape the StopLossPro adapter posts
    (source_call_id/instrument/direction/stop_loss + optional tp/entry/
    risk fields) must still validate, create a Call, and render a Premium
    message without error — proves the redesign didn't change the
    CallIn contract or break on any real adapter payload shape."""
    call = services.create_call(
        db,
        dict(
            source_call_id="stoplosspro-adapter-e2e-1", source="stoplosspro",
            instrument="XAUUSD", direction="SELL", stop_loss=1950.0,
            entry_min=1955.0, entry_max=1956.0, tp1=1900.0, tp2=1870.0, tp3=1840.0,
            risk_percent=0.5, route_premium=True, route_free=True,
        ),
        actor="stoplosspro-adapter",
    )
    db.commit()
    assert call.trade_id.startswith("SR-")
    # Renders cleanly (no exception) with a SELL/entry-range/full-TP shape,
    # which is different geometry from this file's default BUY fixture.
    text = telegram_bot.render_entry_message(call)
    assert "XAUUSD" in text and "SELL" in text
    assert telegram_bot._format_price(1950.0) in text
