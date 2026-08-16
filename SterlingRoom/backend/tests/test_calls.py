import pytest

from app import services
from app.models import Call, CallEventType, CallStatus
from app.trade_id import allocate_unique_trade_id, next_trade_id


def _payload(**overrides):
    p = dict(
        source_call_id="src-1", instrument="XAUUSD", direction="SELL",
        stop_loss=1950.0, tp1=1900.0, tp2=1870.0, tp3=1840.0, risk_percent=0.5,
    )
    p.update(overrides)
    return p


def test_trade_id_format(db):
    tid = allocate_unique_trade_id(db)
    assert tid.startswith("SR-")
    assert len(tid) == len("SR-260816-001")
    assert tid.endswith("-001")


def test_trade_id_increments(db):
    c1 = services.create_call(db, _payload(source_call_id="a"), actor="test")
    db.commit()
    c2 = services.create_call(db, _payload(source_call_id="b"), actor="test")
    db.commit()
    assert c1.trade_id != c2.trade_id
    n1 = int(c1.trade_id.split("-")[-1])
    n2 = int(c2.trade_id.split("-")[-1])
    assert n2 == n1 + 1


def test_create_call_is_idempotent_on_source_call_id(db):
    c1 = services.create_call(db, _payload(source_call_id="dup-1"), actor="test")
    db.commit()
    first_trade_id = c1.trade_id

    with pytest.raises(services.DuplicateCall) as exc_info:
        services.create_call(db, _payload(source_call_id="dup-1"), actor="test")
    assert exc_info.value.call.trade_id == first_trade_id

    # confirm only one row actually exists
    count = db.query(Call).filter_by(source_call_id="dup-1").count()
    assert count == 1


def test_validation_rejects_missing_stop_loss(db):
    with pytest.raises(services.ValidationError):
        services.create_call(db, _payload(stop_loss=None), actor="test")


def test_validation_rejects_bad_direction(db):
    with pytest.raises(services.ValidationError):
        services.create_call(db, _payload(direction="SIDEWAYS"), actor="test")


def test_validation_rejects_non_finite(db):
    with pytest.raises(services.ValidationError):
        services.create_call(db, _payload(stop_loss=float("nan")), actor="test")
    with pytest.raises(services.ValidationError):
        services.create_call(db, _payload(stop_loss=-5), actor="test")


def test_call_created_event_recorded(db):
    call = services.create_call(db, _payload(), actor="test")
    db.commit()
    assert len(call.events) == 1
    assert call.events[0].event_type == CallEventType.CALL_CREATED
    assert call.events[0].new_status == CallStatus.ACTIVE


def test_legal_transition_tp1(db):
    call = services.create_call(db, _payload(), actor="test")
    db.commit()
    services.transition_call(db, call, CallStatus.TP1_HIT, actor="test", event_type=CallEventType.TP1_REACHED)
    db.commit()
    assert call.status == CallStatus.TP1_HIT


def test_illegal_transition_closed_to_active_rejected(db):
    call = services.create_call(db, _payload(), actor="test")
    db.commit()
    services.transition_call(db, call, CallStatus.CLOSED, actor="test", event_type=CallEventType.CALL_CLOSED)
    db.commit()
    assert call.status == CallStatus.CLOSED
    assert call.closed_at is not None

    with pytest.raises(services.InvalidTransition):
        services.transition_call(db, call, CallStatus.ACTIVE, actor="test", event_type=CallEventType.CALL_UPDATED)


def test_terminal_status_has_no_further_transitions(db):
    call = services.create_call(db, _payload(), actor="test")
    db.commit()
    services.transition_call(db, call, CallStatus.STOPPED, actor="test", event_type=CallEventType.CALL_STOPPED)
    db.commit()
    with pytest.raises(services.InvalidTransition):
        services.transition_call(db, call, CallStatus.TP1_HIT, actor="test", event_type=CallEventType.TP1_REACHED)
