from unittest.mock import patch

from app import services, telegram_bot
from app.models import DeliveryStatus, MessageType


def _payload(**overrides):
    p = dict(
        source_call_id="tg-1", instrument="EURUSD", direction="BUY",
        stop_loss=1.0800, tp1=1.0900, tp2=1.0950, tp3=1.1000,
        setup_type="Breakout retest", invalidation="Close below 1.0790",
    )
    p.update(overrides)
    return p


def test_render_entry_message_contains_key_fields(db):
    """2026-08-16 note: this only checks that the key DATA still appears
    somewhere in the Premium message — the exact 2026-08-16 premium-desk
    visual redesign (box header, bold Unicode labels, R:R) is covered in
    depth by tests/test_telegram_message_redesign.py."""
    call = services.create_call(db, _payload(), actor="test")
    text = telegram_bot.render_entry_message(call)
    assert call.trade_id in text
    assert "EURUSD" in text
    assert "BUY" in text
    assert telegram_bot._format_price(call.stop_loss) in text
    assert "Breakout retest" in text


def test_distribute_call_success_records_sent(db):
    call = services.create_call(db, _payload(), actor="test")
    db.commit()

    with patch.object(telegram_bot, "_send_telegram_message") as mock_send:
        mock_send.return_value = telegram_bot.SendResult(ok=True, telegram_message_id="12345")
        msgs = telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()

    assert len(msgs) == 1
    assert msgs[0].delivery_status == DeliveryStatus.SENT
    assert msgs[0].telegram_message_id == "12345"
    assert mock_send.call_count == 1


def test_distribute_call_retries_then_fails(db):
    call = services.create_call(db, _payload(), actor="test")
    db.commit()

    with patch.object(telegram_bot, "_send_telegram_message") as mock_send, \
         patch.object(telegram_bot.time, "sleep"):  # don't actually wait in tests
        mock_send.return_value = telegram_bot.SendResult(ok=False, error="Telegram: bot was blocked")
        msgs = telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()

    assert msgs[0].delivery_status == DeliveryStatus.FAILED
    assert msgs[0].retry_count == telegram_bot._MAX_SEND_ATTEMPTS
    assert mock_send.call_count == telegram_bot._MAX_SEND_ATTEMPTS


def test_distribute_call_is_deduped_on_retry(db):
    """Same call, same chat, same content, sent twice -> only one real send
    (the second call sees the already-SENT row and skips it) — master-prompt
    §28 duplicate prevention."""
    call = services.create_call(db, _payload(), actor="test")
    db.commit()

    with patch.object(telegram_bot, "_send_telegram_message") as mock_send:
        mock_send.return_value = telegram_bot.SendResult(ok=True, telegram_message_id="1")
        telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()
        telegram_bot.distribute_call(db, call, MessageType.ENTRY, chat_ids=["-1001111"])
        db.commit()

    assert mock_send.call_count == 1  # second call was a no-op, not a re-send


def test_resolve_chat_ids_respects_routing_flags(db):
    call = services.create_call(db, _payload(route_free=False, route_premium=True), actor="test")
    chats = telegram_bot.resolve_chat_ids(call, free_chat_id="-1FREE", premium_chat_id="-1PREM")
    assert chats == ["-1PREM"]

    call2 = services.create_call(db, _payload(source_call_id="tg-2", route_free=True, route_premium=True), actor="test")
    chats2 = telegram_bot.resolve_chat_ids(call2, free_chat_id="-1FREE", premium_chat_id="-1PREM")
    assert set(chats2) == {"-1FREE", "-1PREM"}
