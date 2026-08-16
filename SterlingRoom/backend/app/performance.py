"""Performance ledger (Phase 6) — R-multiple statistics computed directly
from the calls table. No number here is ever hand-entered: every stat is a
pure function of `Call.result_r` + `Call.status` + `Call.closed_at`, so the
same trade ledger always reproduces the same numbers (master-prompt "every
displayed number must have a traceable database source" / "performance
calculations must be deterministic").

A call counts toward the ledger once it reaches a terminal, closed-with-a-
result state (CLOSED or STOPPED) AND has a `result_r` recorded — CANCELLED
and INVALIDATED calls are excluded (they were never trades), matching
master-prompt §33/§60's "never delete losing trades" (STOPPED calls with a
negative result_r are included) combined with "no trade" calls not counting
as trades at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Call, CallStatus

_COUNTED_STATUSES = (CallStatus.CLOSED, CallStatus.STOPPED)


@dataclass
class PerformanceStats:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float | None = None
    net_r: float = 0.0
    avg_winner_r: float | None = None
    avg_loser_r: float | None = None
    expectancy_r: float | None = None
    profit_factor: float | None = None
    max_drawdown_r: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    trade_ids: list[str] = field(default_factory=list)


def _closed_calls_query(*, since: datetime | None, until: datetime | None):
    q = select(Call).where(Call.status.in_(_COUNTED_STATUSES), Call.result_r.is_not(None))
    if since is not None:
        q = q.where(Call.closed_at >= since)
    if until is not None:
        q = q.where(Call.closed_at < until)
    return q.order_by(Call.closed_at.asc())


def compute_stats(db: Session, *, since: datetime | None = None, until: datetime | None = None) -> PerformanceStats:
    calls = db.execute(_closed_calls_query(since=since, until=until)).scalars().all()
    stats = PerformanceStats()
    if not calls:
        return stats

    r_values = [float(c.result_r) for c in calls]
    stats.total_trades = len(r_values)
    stats.trade_ids = [c.trade_id for c in calls]

    winners = [r for r in r_values if r > 0]
    losers = [r for r in r_values if r < 0]
    stats.wins = len(winners)
    stats.losses = len(losers)
    stats.breakeven = stats.total_trades - stats.wins - stats.losses

    stats.win_rate = round(100.0 * stats.wins / stats.total_trades, 2)
    stats.net_r = round(sum(r_values), 3)
    stats.avg_winner_r = round(sum(winners) / len(winners), 3) if winners else None
    stats.avg_loser_r = round(sum(losers) / len(losers), 3) if losers else None

    win_rate_frac = stats.wins / stats.total_trades
    loss_rate_frac = stats.losses / stats.total_trades
    if stats.avg_winner_r is not None or stats.avg_loser_r is not None:
        stats.expectancy_r = round(
            win_rate_frac * (stats.avg_winner_r or 0) - loss_rate_frac * abs(stats.avg_loser_r or 0), 3
        )

    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    if gross_loss > 0:
        stats.profit_factor = round(gross_profit / gross_loss, 3)
    elif gross_profit > 0:
        stats.profit_factor = None  # undefined (no losses to divide by) — not "infinite", just not meaningful yet

    # Max drawdown (in R) — peak-to-trough on the cumulative-R equity curve,
    # walked in closed_at order so it reflects the actual sequence of trades.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    stats.max_drawdown_r = round(max_dd, 3)  # <= 0, e.g. -3.5R

    # Consecutive win/loss streaks, breakeven trades break both streaks.
    cur_win_streak = cur_loss_streak = 0
    max_win_streak = max_loss_streak = 0
    for r in r_values:
        if r > 0:
            cur_win_streak += 1
            cur_loss_streak = 0
        elif r < 0:
            cur_loss_streak += 1
            cur_win_streak = 0
        else:
            cur_win_streak = cur_loss_streak = 0
        max_win_streak = max(max_win_streak, cur_win_streak)
        max_loss_streak = max(max_loss_streak, cur_loss_streak)
    stats.max_consecutive_wins = max_win_streak
    stats.max_consecutive_losses = max_loss_streak

    return stats


def daily_results(db: Session, *, day: datetime | None = None) -> PerformanceStats:
    day = day or datetime.now(timezone.utc)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return compute_stats(db, since=start, until=end)


def weekly_results(db: Session, *, week_start: datetime | None = None) -> PerformanceStats:
    now = datetime.now(timezone.utc)
    if week_start is None:
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    return compute_stats(db, since=week_start, until=week_end)


def monthly_results(db: Session, *, year: int | None = None, month: int | None = None) -> PerformanceStats:
    now = datetime.now(timezone.utc)
    year = year or now.year
    month = month or now.month
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return compute_stats(db, since=start, until=end)
