"""2026-08-16 Telegram signal presentation redesign — presentation-layer
only, now on its SECOND visual pass:

  1st pass: box-drawing header + Mathematical Bold labels + R:R per target.
  2nd pass (this file, current): a direct correction — plain text + standard
  Unicode emoji, no box, no bold, a single headline R:R, and a distinct
  STOPPED-vs-SL-HIT result state. The 1st pass's tests (box header, bold
  transform) no longer apply and have been replaced here rather than left
  stale.

Does NOT re-test call/entry/SL/TP/R:R-math/result business logic, delivery
routing, the 15-minute Free delay, or subscription/payment behavior — those
are unchanged by this redesign and already covered elsewhere (test_worker.py,
test_telegram_bot.py, test_services.py, test_end_to_end.py).
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


# ── 1-2. Premium BUY / Premium SELL ──────────────────────────────────────
def test_premium_buy_renders_correct_arrow_and_direction(db):
    call = _make_call(db, direction=CallDirection.BUY)
    text = telegram_bot.render_entry_message(call)
    assert "👑 PREMIUM" in text
    assert "🟢 BUY BTCUSD" in text
    assert "🔴" not in text


def test_premium_sell_renders_correct_arrow_and_direction(db):
    # Valid SELL geometry: SL above entry, TPs below entry.
    call = _make_call(db, direction=CallDirection.SELL, entry_min=1950.0, entry_max=None,
                       stop_loss=1960.0, tp1=1930.0, tp2=1910.0, tp3=1890.0, instrument="XAUUSD")
    text = telegram_bot.render_entry_message(call)
    assert "🔴 SELL XAUUSD" in text
    assert "🟢" not in text


# ── 3. Render Entry ───────────────────────────────────────────────────────
def test_entry_section_shows_the_entry_price(db):
    call = _make_call(db, entry_min=63209.70, entry_max=None)
    text = telegram_bot.render_entry_message(call)
    assert "🎯 ENTRY" in text
    assert telegram_bot._format_price(63209.70) in text


def test_entry_shows_market_when_no_entry_price_given(db):
    call = _make_call(db, entry_min=None, entry_max=None)
    text = telegram_bot.render_entry_message(call)
    assert "MARKET" in text


# ── 4-6. Render TP1 / TP2 / TP3 ───────────────────────────────────────────
def test_tp1_row_present_with_marker_and_price(db):
    call = _make_call(db, tp1=63381.60)
    text = telegram_bot.render_entry_message(call)
    assert f"{telegram_bot._TP1_MARKER}  TP1 — {telegram_bot._format_price(63381.60)}" in text


def test_tp2_row_present_with_marker_and_price(db):
    call = _make_call(db, tp2=63467.55)
    text = telegram_bot.render_entry_message(call)
    assert f"{telegram_bot._TP2_MARKER}  TP2 — {telegram_bot._format_price(63467.55)}" in text


def test_tp3_row_uses_trophy_marker_not_a_circled_number(db):
    call = _make_call(db, tp3=63553.50)
    text = telegram_bot.render_entry_message(call)
    assert f"🏆  TP3 — {telegram_bot._format_price(63553.50)}" in text
    assert chr(0x2462) not in text  # ③ — TP3 deliberately does NOT use the old circled-3


def test_missing_targets_are_simply_omitted_not_shown_as_blank(db):
    call = _make_call(db, tp1=63381.60, tp2=None, tp3=None)
    text = telegram_bot.render_entry_message(call)
    assert "TP1" in text
    assert "TP2" not in text
    assert "TP3" not in text


def test_headline_rr_uses_the_furthest_configured_target(db):
    # 1R = 85.95 (63209.70 - 63123.75). TP3 reward = 343.80 -> exactly 4R.
    call = _make_call(db, entry_min=63209.70, entry_max=None, stop_loss=63123.75,
                       tp1=63381.60, tp2=63467.55, tp3=63553.50)
    text = telegram_bot.render_entry_message(call)
    assert "⚡ R:R 1:4" in text


# ── 7. Render winning result ──────────────────────────────────────────────
def test_winning_result_shows_profit_header(db):
    call = _make_call(db, status=CallStatus.CLOSED, result_r=2.0)
    text = telegram_bot.render_results_message(call)
    assert "✅ PROFIT" in text
    assert "🛑" not in text and "💥" not in text
    assert telegram_bot._format_result_r(2.0) in text


# ── 8. Render stopped result — and the STOPPED vs. SL HIT distinction ────
def test_stopped_result_at_exactly_minus_1r_is_labeled_sl_hit(db):
    """A clean stop-out at the original stop-loss level (~-1R) — per spec,
    this is SL HIT, not generic STOPPED, and per the corrected mockup both
    lines appear together: STOPPED as the outer status, SL HIT as the
    confirmed detail."""
    call = _make_call(db, status=CallStatus.STOPPED, result_r=-1.0)
    text = telegram_bot.render_results_message(call)
    assert "🛑 STOPPED" in text
    assert "💥 SL HIT" in text


def test_stopped_result_not_at_minus_1r_is_generic_stopped_only(db):
    """A STOPPED trade that closed at a different loss (manual/system
    stop, not a clean SL execution) must show STOPPED WITHOUT the SL HIT
    detail — the spec is explicit that 💥 must never be used for a
    generic stop."""
    call = _make_call(db, status=CallStatus.STOPPED, result_r=-0.35)
    text = telegram_bot.render_results_message(call)
    assert "🛑 STOPPED" in text
    assert "💥" not in text
    assert "SL HIT" not in text


def test_breakeven_result_uses_its_own_marker(db):
    call = _make_call(db, status=CallStatus.CLOSED, result_r=0.0)
    text = telegram_bot.render_results_message(call)
    assert "➖ BREAKEVEN" in text


# ── 9. Render Free teaser ──────────────────────────────────────────────────
def test_free_teaser_renders_and_is_conversion_oriented(db):
    call = _make_call(db)
    text = telegram_bot.render_free_teaser_message(call)
    assert "👑 STERLING ROOM" in text
    assert "Premium" in text
    assert call.instrument in text
    assert call.direction.value in text
    assert call.trade_id in text


# ── 10. No execution numbers leak into Free ───────────────────────────────
def test_free_message_does_not_contain_entry_sl_tp1_tp2_tp3_prices(db):
    call = _make_call(
        db, entry_min=63209.70, entry_max=None, stop_loss=63123.75,
        tp1=63381.60, tp2=63467.55, tp3=63553.50,
    )
    text = telegram_bot.render_free_teaser_message(call)
    for raw in (call.entry_min, call.stop_loss, call.tp1, call.tp2, call.tp3):
        assert str(raw) not in text
        assert telegram_bot._format_price(raw) not in text


def test_free_teaser_does_not_echo_analysis_or_setup_type_free_text(db):
    call = _make_call(
        db, stop_loss=1900, analysis="Enter near 1950.5 with SL at 1900.25 for a clean R:R",
        setup_type="breakout above 1955.0", invalidation="close below 1890.0",
    )
    text = telegram_bot.render_free_teaser_message(call)
    for leaked in ("1950.5", "1900.25", "1955.0", "1890.0"):
        assert leaked not in text


# ── 11. No trading-call t.me URL ──────────────────────────────────────────
def test_no_trading_call_message_type_has_a_bot_url_footer(db):
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


# ── 12. Retry/dedup unchanged ─────────────────────────────────────────────
def test_dedup_protection_still_works_after_redesign(db):
    from unittest.mock import patch

    call = services.create_call(
        db, dict(source_call_id="redesign2-dedup-1", instrument="BTCUSD",
                 direction="BUY", stop_loss=63123.75), actor="test",
    )
    db.commit()
    with patch.object(telegram_bot, "_send_telegram_message") as mock_send:
        mock_send.return_value = telegram_bot.SendResult(ok=True, telegram_message_id="1")
        telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()
        telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()
    assert mock_send.call_count == 1


def test_retry_behavior_still_works_after_redesign(db):
    from unittest.mock import patch

    call = services.create_call(
        db, dict(source_call_id="redesign2-retry-1", instrument="BTCUSD",
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


# ── 13. StopLossPro adapter unchanged ─────────────────────────────────────
def test_stoplosspro_adapter_payload_shape_still_creates_a_call_and_renders(db):
    call = services.create_call(
        db,
        dict(
            source_call_id="stoplosspro-adapter-redesign2-1", source="stoplosspro",
            instrument="XAUUSD", direction="SELL", stop_loss=1950.0,
            entry_min=1945.0, entry_max=None, tp1=1900.0, tp2=1870.0, tp3=1840.0,
            risk_percent=0.5, route_premium=True, route_free=True,
        ),
        actor="stoplosspro-adapter",
    )
    db.commit()
    assert call.trade_id.startswith("SR-")
    text = telegram_bot.render_entry_message(call)
    assert "XAUUSD" in text and "SELL" in text
    assert telegram_bot._format_price(1950.0) in text
