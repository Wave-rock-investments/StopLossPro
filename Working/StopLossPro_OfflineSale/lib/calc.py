# calc.py — trade-setup math: lot sizing, SL/TP distances, profit, EV.
# Pure Python — no Kivy imports; safe to call from any thread.
# ─────────────────────────────────────────────────────────────────────────────
import math, time
from dataclasses import dataclass
from typing import Optional

from constants import (
    log, CONTRACTS, DECIMALS, _BROKER_ALIASES,
    DEFAULT_SL, DEFAULT_WR, MAX_LOT,
    _get_dp, _strip_broker_suffix, _base_sym,
    _dynamic_aliases,      # mutable ref — updated in-place by _update_dynamic_aliases()
    _cluster_lots,         # re-exported here so mixins can do: from calc import _cluster_lots
)


def _update_dynamic_aliases(broker_symbol_map: dict) -> None:
    """
    Rebuild the live broker-symbol alias table from MT5 on-connect data.

    broker_symbol_map: {canonical_sym: broker_sym}
        e.g. {'XAUUSD': 'XAUUSDm', 'EURUSD': 'EURUSDm'}

    Writes the reverse mapping into constants._dynamic_aliases:
        {stripped_broker_name_upper → canonical}

    constants._base_sym() consults _dynamic_aliases first (highest priority),
    then the static _BROKER_ALIASES table, giving live broker data priority over
    pre-baked aliases at all times.  Thread-safe: dict.clear()+update() are GIL-
    protected for CPython; _base_sym() never sees a partially-updated state.
    """
    _dynamic_aliases.clear()
    for canonical, broker_name in broker_symbol_map.items():
        key = _strip_broker_suffix(broker_name.upper())
        _dynamic_aliases[key] = canonical


@dataclass
class TradeSetup:
    symbol:   str
    atr:      float
    lots:     float
    entry:    Optional[float]
    sl_dist:  float
    tp1_dist: float
    tp2_dist: float
    tp3_dist: float
    loss:     float
    profit1:  float
    profit2:  float
    profit3:  float
    rr1:      float
    rr2:      float
    buy:      dict
    sell:     dict
    blended:  float = 0.0
    ev:       float = 0.0
    timestamp: float = 0.0


def calc_setup(atr, lots, symbol, sl_m,
               entry=None, wr_pct=50.0):
    """
    Unified risk model — TP ratios are ALWAYS fixed:
        SL  = ATR × sl_m
        TP1 = SL × 2  (1:2)
        TP2 = SL × 3  (1:3)
        TP3 = SL × 4  (1:4)
    """
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError(f"ATR must be a finite positive number, got {atr}")
    if not math.isfinite(sl_m) or sl_m <= 0:
        raise ValueError(f"SL multiplier must be finite and > 0, got {sl_m}")
    if not math.isfinite(lots) or lots <= 0:
        raise ValueError(f"Lots must be finite and > 0, got {lots}")
    _sym = _base_sym(symbol)
    if _sym not in CONTRACTS:
        raise KeyError(f"Unknown symbol: {symbol}")

    c   = CONTRACTS[_sym]
    d   = DECIMALS.get(_sym, 2)
    sl  = round(atr * sl_m, d)
    tp1 = round(sl  * 2,    d)
    tp2 = round(sl  * 3,    d)
    tp3 = round(sl  * 4,    d)

    pnl  = lots * c
    loss = round(sl  * pnl, 2)
    p1   = round(tp1 * pnl, 2)
    p2   = round(tp2 * pnl, 2)
    p3   = round(tp3 * pnl, 2)

    w        = 1 / 3
    blended  = p1 * w + p2 * w + p3 * w
    wr       = wr_pct / 100
    ev       = wr * blended - (1 - wr) * loss

    buy = sell = {}
    if entry is not None:
        buy  = {"entry":   entry,
                "sl":      round(entry - sl,  d),
                "tp1":     round(entry + tp1, d),
                "tp2":     round(entry + tp2, d),
                "tp3":     round(entry + tp3, d),
                "loss":    loss,
                "profit1": p1,
                "profit2": p2,
                "profit3": p3}
        sell = {"entry":   entry,
                "sl":      round(entry + sl,  d),
                "tp1":     round(entry - tp1, d),
                "tp2":     round(entry - tp2, d),
                "tp3":     round(entry - tp3, d),
                "loss":    loss,
                "profit1": p1,
                "profit2": p2,
                "profit3": p3}

    return TradeSetup(
        symbol=symbol, atr=atr, lots=lots, entry=entry,
        sl_dist=round(sl, d),
        tp1_dist=round(tp1, d),
        tp2_dist=round(tp2, d),
        tp3_dist=round(tp3, d),
        loss=loss, profit1=p1, profit2=p2, profit3=p3,
        rr1=2.0, rr2=3.0,
        buy=buy, sell=sell,
        blended=blended, ev=ev, timestamp=time.time(),
    )


def calc_auto_lot(atr, account, risk_pct, symbol, sl_m):
    if atr <= 0 or sl_m <= 0:
        raise ValueError("ATR and SL multiplier must be > 0")
    if account <= 0:
        raise ValueError("Account balance must be > 0")
    if risk_pct <= 0:
        raise ValueError("Risk % must be > 0")
    contract = CONTRACTS.get(_base_sym(symbol))
    if contract is None:
        raise ValueError(f"Unknown symbol '{symbol}' — cannot auto-calculate lot size")
    denominator = atr * sl_m * contract
    if denominator <= 0:
        raise ValueError("Invalid denominator in auto-lot calculation")
    lot = (account * risk_pct / 100) / denominator
    return round(lot, 2)


def recommend_order_type(side: str, entry: float, bid: float, ask: float,
                          digits: int = 5) -> str:
    """
    Determine the appropriate MT5 order type based on side and
    the relationship between entry price and current market price.

    BUY side logic:
      entry ≈ ask  (within 1 pip)  →  MARKET_BUY   (fill at market)
      entry < ask                  →  BUY_LIMIT    (buy cheaper on pullback)
      entry > ask                  →  BUY_STOP     (buy on breakout above)

    SELL side logic:
      entry ≈ bid  (within 1 pip)  →  MARKET_SELL  (fill at market)
      entry > bid                  →  SELL_LIMIT   (sell at premium on rally)
      entry < bid                  →  SELL_STOP    (sell on breakdown below)
    """
    pip = 10 ** (-digits)
    tol = pip * (10 if digits <= 3 else 2)

    if side.upper() == "BUY":
        if abs(entry - ask) <= tol:  return "MARKET_BUY"
        if entry < ask:              return "BUY_LIMIT"
        return "BUY_STOP"
    else:
        if abs(entry - bid) <= tol:  return "MARKET_SELL"
        if entry > bid:              return "SELL_LIMIT"
        return "SELL_STOP"
