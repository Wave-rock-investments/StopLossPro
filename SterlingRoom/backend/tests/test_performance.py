import datetime as dt
import itertools

from app import performance, services
from app.models import CallEventType, CallStatus

_counter = itertools.count()


def _closed_call(db, result_r, *, status=CallStatus.CLOSED, closed_at=None, source_call_id=None):
    call = services.create_call(db, dict(
        source_call_id=source_call_id or f"perf-{next(_counter)}",
        instrument="EURUSD", direction="BUY", stop_loss=1.08,
    ), actor="test")
    db.commit()
    services.transition_call(db, call, status, actor="test",
                              event_type=CallEventType.CALL_CLOSED if status == CallStatus.CLOSED else CallEventType.CALL_STOPPED)
    call.result_r = result_r
    if closed_at is not None:
        call.closed_at = closed_at
    db.commit()
    return call


def test_empty_ledger(db):
    stats = performance.compute_stats(db)
    assert stats.total_trades == 0
    assert stats.win_rate is None
    assert stats.net_r == 0.0


def test_basic_win_loss_counts(db):
    _closed_call(db, 2.0)
    _closed_call(db, -1.0)
    _closed_call(db, 1.5)
    _closed_call(db, 0.0)  # breakeven

    stats = performance.compute_stats(db)
    assert stats.total_trades == 4
    assert stats.wins == 2
    assert stats.losses == 1
    assert stats.breakeven == 1
    assert stats.win_rate == 50.0
    assert stats.net_r == 2.5


def test_expectancy_and_profit_factor(db):
    _closed_call(db, 2.0)
    _closed_call(db, 2.0)
    _closed_call(db, -1.0)

    stats = performance.compute_stats(db)
    # win_rate = 2/3, avg_winner = 2.0, avg_loser = -1.0
    # expectancy = (2/3)*2.0 - (1/3)*1.0 = 1.333 - 0.333 = 1.0
    assert stats.expectancy_r == 1.0
    assert stats.profit_factor == round(4.0 / 1.0, 3)


def test_max_drawdown(db):
    # equity curve: +2 -> +1 (peak2, dd -1) -> -2 (dd -3 from peak 2) -> +1 (dd -2)
    _closed_call(db, 2.0, closed_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    _closed_call(db, -1.0, closed_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
    _closed_call(db, -2.0, closed_at=dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc))
    _closed_call(db, 3.0, closed_at=dt.datetime(2026, 1, 4, tzinfo=dt.timezone.utc))

    stats = performance.compute_stats(db)
    assert stats.max_drawdown_r == -3.0


def test_consecutive_streaks(db):
    for i, r in enumerate([1, 1, 1, -1, -1, 1, 0, -1, -1, -1]):
        _closed_call(db, float(r), closed_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=i))
    stats = performance.compute_stats(db)
    assert stats.max_consecutive_wins == 3
    assert stats.max_consecutive_losses == 3


def test_cancelled_and_invalidated_excluded(db):
    call = services.create_call(db, dict(source_call_id="excl-1", instrument="BTCUSD", direction="SELL", stop_loss=60000), actor="test")
    db.commit()
    services.transition_call(db, call, CallStatus.CANCELLED, actor="test", event_type=CallEventType.CALL_CANCELLED)
    call.result_r = -5.0  # even if someone sets this, cancelled trades shouldn't count
    db.commit()

    stats = performance.compute_stats(db)
    assert stats.total_trades == 0


def test_daily_results_filters_by_date(db):
    today = dt.datetime.now(dt.timezone.utc)
    yesterday = today - dt.timedelta(days=1)
    _closed_call(db, 1.0, closed_at=today)
    _closed_call(db, -5.0, closed_at=yesterday)

    stats = performance.daily_results(db, day=today)
    assert stats.total_trades == 1
    assert stats.net_r == 1.0


def test_monthly_results(db):
    _closed_call(db, 1.0, closed_at=dt.datetime(2026, 3, 15, tzinfo=dt.timezone.utc))
    _closed_call(db, 2.0, closed_at=dt.datetime(2026, 3, 20, tzinfo=dt.timezone.utc))
    _closed_call(db, 9.0, closed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc))

    stats = performance.monthly_results(db, year=2026, month=3)
    assert stats.total_trades == 2
    assert stats.net_r == 3.0
