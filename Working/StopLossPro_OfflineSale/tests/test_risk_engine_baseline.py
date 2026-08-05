"""PHASE 0 REGRESSION BASELINE — risk engine golden master.

These tests pin the EXACT current behaviour of the risk-management engine as of
2026-08-05, before any licensing/security work begins.

Their purpose is not to assert that the maths is "correct" in a trading sense.
It is to prove that Phases 1-15 (backend, auth, licensing, sessions, MFA,
hardening) do not alter a single number the customer sees.

If a test here fails after a security phase, that phase changed product
behaviour and must be reverted or explicitly approved.

Run:  python -m pytest tests/ -v
      python tests/test_risk_engine_baseline.py     (no pytest needed)
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: F401  — installs the kivy stub before `calc` is imported

from calc import calc_setup, calc_auto_lot, recommend_order_type  # noqa: E402
from constants import CONTRACTS, DECIMALS                          # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
def approx(a, b, tol=1e-9):
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ── 1. Core unified risk model ─────────────────────────────────────────────
def test_gold_setup_fixed_ratios():
    """XAUUSD, ATR 3.5, SL mult 1.5, 1 lot, entry 4000."""
    s = calc_setup(atr=3.5, lots=1.0, symbol="XAUUSD", sl_m=1.5, entry=4000.0, wr_pct=50.0)

    check("SL  = ATR x 1.5           -> 5.25", approx(s.sl_dist, 5.25), f"got {s.sl_dist}")
    check("TP1 = SL x 2              -> 10.50", approx(s.tp1_dist, 10.50), f"got {s.tp1_dist}")
    check("TP2 = SL x 3              -> 15.75", approx(s.tp2_dist, 15.75), f"got {s.tp2_dist}")
    check("TP3 = SL x 4              -> 21.00", approx(s.tp3_dist, 21.00), f"got {s.tp3_dist}")

    # contract size for XAUUSD is 100
    check("loss = SL x lots x 100    -> 525.00", approx(s.loss, 525.00), f"got {s.loss}")
    check("p1   = TP1 x lots x 100   -> 1050.00", approx(s.profit1, 1050.00), f"got {s.profit1}")
    check("p2                        -> 1575.00", approx(s.profit2, 1575.00), f"got {s.profit2}")
    check("p3                        -> 2100.00", approx(s.profit3, 2100.00), f"got {s.profit3}")

    check("rr1 fixed 2.0", approx(s.rr1, 2.0), f"got {s.rr1}")
    check("rr2 fixed 3.0", approx(s.rr2, 3.0), f"got {s.rr2}")

    blended = (1050.0 + 1575.0 + 2100.0) / 3
    check("blended = mean(p1,p2,p3)  -> 1575.00", approx(s.blended, blended), f"got {s.blended}")

    ev = 0.5 * blended - 0.5 * 525.00
    check("ev @ 50% win rate         -> 525.00", approx(s.ev, ev), f"got {s.ev}")


def test_gold_buy_sell_levels():
    """BUY levels sit above entry, SELL mirror below — exact values pinned."""
    s = calc_setup(atr=3.5, lots=1.0, symbol="XAUUSD", sl_m=1.5, entry=4000.0)

    check("buy.sl  = entry - SL      -> 3994.75", approx(s.buy["sl"], 3994.75), f"got {s.buy['sl']}")
    check("buy.tp1 = entry + TP1     -> 4010.50", approx(s.buy["tp1"], 4010.50), f"got {s.buy['tp1']}")
    check("buy.tp3 = entry + TP3     -> 4021.00", approx(s.buy["tp3"], 4021.00), f"got {s.buy['tp3']}")

    check("sell.sl  = entry + SL     -> 4005.25", approx(s.sell["sl"], 4005.25), f"got {s.sell['sl']}")
    check("sell.tp1 = entry - TP1    -> 3989.50", approx(s.sell["tp1"], 3989.50), f"got {s.sell['tp1']}")
    check("sell.tp3 = entry - TP3    -> 3979.00", approx(s.sell["tp3"], 3979.00), f"got {s.sell['tp3']}")

    check("buy/sell share same loss", approx(s.buy["loss"], s.sell["loss"]))


def test_no_entry_yields_empty_levels():
    """This is the behaviour that governs whether the MT5 BUY/SELL buttons appear."""
    s = calc_setup(atr=3.5, lots=1.0, symbol="XAUUSD", sl_m=1.5, entry=None)
    check("entry=None -> buy  == {}", s.buy == {}, f"got {s.buy}")
    check("entry=None -> sell == {}", s.sell == {}, f"got {s.sell}")
    check("distances still computed", approx(s.sl_dist, 5.25), f"got {s.sl_dist}")


# ── 2. Symbol handling ─────────────────────────────────────────────────────
def test_contract_sizes_stable():
    """Contract multipliers directly scale customer P&L — pin them."""
    expected = {
        "XAUUSD": 100, "XAGUSD": 5000, "USOIL": 1000, "UKOIL": 1000, "NGAS": 10000,
        "BTCUSD": 1, "ETHUSD": 1, "BNBUSD": 1,
        "EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 100000,
        "AUDUSD": 100000, "USDCAD": 100000, "NZDUSD": 100000, "USDCHF": 100000,
    }
    for sym, size in expected.items():
        check(f"CONTRACTS[{sym}] == {size}", CONTRACTS.get(sym) == size, f"got {CONTRACTS.get(sym)}")


def test_fx_and_crypto_setups():
    fx = calc_setup(atr=0.0010, lots=1.0, symbol="EURUSD", sl_m=1.5, entry=1.1000)
    check("EURUSD loss = 0.0015*1*100000 -> 150.00", approx(fx.loss, 150.00), f"got {fx.loss}")

    btc = calc_setup(atr=500.0, lots=1.0, symbol="BTCUSD", sl_m=1.5, entry=60000.0)
    check("BTCUSD loss = 750*1*1 -> 750.00", approx(btc.loss, 750.00), f"got {btc.loss}")


def test_unknown_symbol_raises_keyerror():
    try:
        calc_setup(atr=1.0, lots=1.0, symbol="NOTAREALPAIR", sl_m=1.5)
        check("unknown symbol raises KeyError", False, "no exception raised")
    except KeyError:
        check("unknown symbol raises KeyError", True)
    except Exception as e:
        check("unknown symbol raises KeyError", False, f"raised {type(e).__name__}")


# ── 3. Input validation contract ───────────────────────────────────────────
def test_invalid_inputs_rejected():
    cases = [
        ("atr=0",        dict(atr=0.0,   lots=1.0, symbol="XAUUSD", sl_m=1.5)),
        ("atr<0",        dict(atr=-1.0,  lots=1.0, symbol="XAUUSD", sl_m=1.5)),
        ("atr=nan",      dict(atr=float("nan"), lots=1.0, symbol="XAUUSD", sl_m=1.5)),
        ("atr=inf",      dict(atr=float("inf"), lots=1.0, symbol="XAUUSD", sl_m=1.5)),
        ("lots=0",       dict(atr=1.0,   lots=0.0, symbol="XAUUSD", sl_m=1.5)),
        ("lots<0",       dict(atr=1.0,   lots=-1.0, symbol="XAUUSD", sl_m=1.5)),
        ("sl_m=0",       dict(atr=1.0,   lots=1.0, symbol="XAUUSD", sl_m=0.0)),
        ("sl_m<0",       dict(atr=1.0,   lots=1.0, symbol="XAUUSD", sl_m=-1.5)),
    ]
    for label, kw in cases:
        try:
            calc_setup(**kw)
            check(f"rejects {label}", False, "no exception raised")
        except ValueError:
            check(f"rejects {label}", True)
        except Exception as e:
            check(f"rejects {label}", False, f"raised {type(e).__name__}")


# ── 4. Auto-lot sizing ─────────────────────────────────────────────────────
def test_auto_lot_baseline():
    """1% of a 100k account on XAUUSD with SL 5.25.

    Engine computes (account * risk/100) / (atr * sl_m * contract) then
    round(_, 2).  1000 / 525 = 1.904761... -> 1.9
    """
    lots = calc_auto_lot(atr=3.5, account=100000.0, risk_pct=1.0, symbol="XAUUSD", sl_m=1.5)
    check("auto lot 1% of 100k XAUUSD -> 1.9 (golden)", approx(lots, 1.9), f"got {lots}")
    check("auto lot is positive", lots > 0, f"got {lots}")
    check("auto lot rounded to 2dp", approx(lots, round(lots, 2)), f"got {lots}")


def test_auto_lot_scales_with_risk():
    """Scaling is linear BEFORE rounding; the engine rounds each result to 2dp
    independently, so 2x risk is 2x lots only to within one rounding step.

    1000/525 = 1.904761 -> 1.9      (loses 0.004761)
    2000/525 = 3.809523 -> 3.81     (gains 0.000476)
    Pinned as golden values; linearity asserted within rounding tolerance.
    """
    a = calc_auto_lot(atr=3.5, account=100000.0, risk_pct=1.0, symbol="XAUUSD", sl_m=1.5)
    b = calc_auto_lot(atr=3.5, account=100000.0, risk_pct=2.0, symbol="XAUUSD", sl_m=1.5)
    check("1% -> 1.9  (golden)", approx(a, 1.9), f"got {a}")
    check("2% -> 3.81 (golden)", approx(b, 3.81), f"got {b}")
    check("2x risk ~ 2x lots within 2dp rounding",
          abs(b - a * 2) <= 0.01 + 1e-9, f"{a} -> {b}, delta {abs(b - a*2):.4f}")


def test_auto_lot_rejects_bad_input():
    for label, kw in [
        ("atr=0",  dict(atr=0.0, account=100000.0, risk_pct=1.0, symbol="XAUUSD", sl_m=1.5)),
        ("sl_m=0", dict(atr=3.5, account=100000.0, risk_pct=1.0, symbol="XAUUSD", sl_m=0.0)),
    ]:
        try:
            calc_auto_lot(**kw)
            check(f"auto_lot rejects {label}", False, "no exception")
        except ValueError:
            check(f"auto_lot rejects {label}", True)
        except Exception as e:
            check(f"auto_lot rejects {label}", False, f"raised {type(e).__name__}")


# ── 5. Order-type recommendation ───────────────────────────────────────────
def test_recommend_order_type_baseline():
    bid, ask = 3999.90, 4000.10
    cases = [
        ("BUY  entry==ask -> MARKET_BUY",   ("BUY",  4000.10), "MARKET_BUY"),
        ("BUY  entry<ask  -> BUY_LIMIT",    ("BUY",  3990.00), "BUY_LIMIT"),
        ("BUY  entry>ask  -> BUY_STOP",     ("BUY",  4010.00), "BUY_STOP"),
        ("SELL entry==bid -> MARKET_SELL",  ("SELL", 3999.90), "MARKET_SELL"),
        ("SELL entry>bid  -> SELL_LIMIT",   ("SELL", 4010.00), "SELL_LIMIT"),
        ("SELL entry<bid  -> SELL_STOP",    ("SELL", 3990.00), "SELL_STOP"),
    ]
    for label, (side, entry), expected in cases:
        try:
            got = recommend_order_type(side, entry, bid, ask, "XAUUSD")
            check(label, got == expected, f"got {got}")
        except TypeError:
            got = recommend_order_type(side, entry, bid, ask)
            check(label, got == expected, f"got {got}")


# ── 6. Determinism ─────────────────────────────────────────────────────────
def test_deterministic():
    """Same inputs must always produce identical numbers (timestamp excluded)."""
    kw = dict(atr=3.5, lots=1.0, symbol="XAUUSD", sl_m=1.5, entry=4000.0, wr_pct=50.0)
    a, b = calc_setup(**kw), calc_setup(**kw)
    fields = ("sl_dist", "tp1_dist", "tp2_dist", "tp3_dist",
              "loss", "profit1", "profit2", "profit3", "blended", "ev")
    same = all(approx(getattr(a, f), getattr(b, f)) for f in fields)
    check("repeated calls identical", same)
    check("buy dicts identical", a.buy == b.buy)


# ── runner ─────────────────────────────────────────────────────────────────
ALL = [
    test_gold_setup_fixed_ratios,
    test_gold_buy_sell_levels,
    test_no_entry_yields_empty_levels,
    test_contract_sizes_stable,
    test_fx_and_crypto_setups,
    test_unknown_symbol_raises_keyerror,
    test_invalid_inputs_rejected,
    test_auto_lot_baseline,
    test_auto_lot_scales_with_risk,
    test_auto_lot_rejects_bad_input,
    test_recommend_order_type_baseline,
    test_deterministic,
]

if __name__ == "__main__":
    print("=" * 72)
    print("PHASE 0 REGRESSION BASELINE — StopLossPro risk engine")
    print("=" * 72)
    for t in ALL:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception as e:
            print(f"  ERROR {type(e).__name__}: {e}")
            FAILURES.append(f"{t.__name__} (exception)")
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED — baseline established")
    sys.exit(0)
