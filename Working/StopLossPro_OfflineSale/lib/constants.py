# constants.py — shared constants, logger, symbol tables, helper functions.
# No imports from other project files → zero circular-import risk.
# ─────────────────────────────────────────────────────────────────────────────
import os
import logging

from kivy.utils import platform as _plat


log = logging.getLogger("StopLossPro.constants")

platform = _plat  # both names used throughout codebase

# MT5 order type display labels
MT5_ORDER_TYPE_LABELS = {
    'MARKET_BUY': 'Market Buy',   'MARKET_SELL': 'Market Sell',
    'BUY_LIMIT':  'Buy Limit',    'SELL_LIMIT':  'Sell Limit',
    'BUY_STOP':   'Buy Stop',     'SELL_STOP':   'Sell Stop',
    'BUY_STOP_LIMIT': 'Stop Limit', 'SELL_STOP_LIMIT': 'Stop Limit',
}

_MT5_EXE_CANDIDATES = [
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
]


# ── dp cache (populated lazily after Kivy window initialises) ─────────────────
_DP = {}  # populated lazily after Kivy initialises (requires Window)
def _get_dp(val):
    if val not in _DP:
        from kivy.metrics import dp as _dp
        _DP[val] = _dp(val)
    return _DP[val]

CONTRACTS = {
    "XAUUSD": 100,    "XAGUSD": 5000,   "USOIL":  1000, "UKOIL":  1000, "NGAS":   10000,
    "BTCUSD": 1,      "ETHUSD": 1,      "BNBUSD": 1,
    "EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 100000,
    "AUDUSD": 100000, "USDCAD": 100000, "NZDUSD": 100000, "USDCHF": 100000,
}
DECIMALS = {
    "XAUUSD": 2, "XAGUSD": 4, "USOIL": 2, "UKOIL": 2, "NGAS": 3,
    "BTCUSD": 2, "ETHUSD": 2, "BNBUSD": 2,
    "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3,
    "AUDUSD": 5, "USDCAD": 5, "NZDUSD": 5, "USDCHF": 5,
}

# Static fallback aliases for common broker rename patterns (offline / pre-connect).
# Extended automatically at runtime from live broker data — see _update_dynamic_aliases().
_BROKER_ALIASES = {
    # Gold
    "GOLD": "XAUUSD", "XAUUSDM": "XAUUSD",
    # Silver
    "SILVER": "XAGUSD",
    # Oil
    "OIL": "USOIL", "WTI": "USOIL", "CRUDEOIL": "USOIL", "BRENT": "UKOIL",
    # Natural Gas
    "NATGAS": "NGAS", "NG": "NGAS",
    # Crypto short names
    "BTC": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETH": "ETHUSD", "BNB": "BNBUSD",
}

# Populated from live broker data on MT5 connect (broker_name.upper() → CONTRACTS key).
# Takes priority over _BROKER_ALIASES so real broker data always wins.
_dynamic_aliases: dict = {}


def _strip_broker_suffix(s: str) -> str:
    """Remove separators and trailing broker suffixes from an uppercased symbol name."""
    for sep in ('-', '.', '/'):
        if sep in s:
            s = s.split(sep)[0]
    return s.rstrip('M+#_')


def _base_sym(symbol: str) -> str:
    """
    Map any broker symbol name to the canonical CONTRACTS key.
    Priority: live broker map → static aliases → stripped name.
    Examples: GOLD→XAUUSD, XAUUSDm→XAUUSD, XAUUSD-std→XAUUSD, EURUSD→EURUSD.
    """
    s = _strip_broker_suffix(symbol.upper().strip())
    return _dynamic_aliases.get(s) or _BROKER_ALIASES.get(s, s)


DEFAULT_SL  = 1.5
DEFAULT_WR  = 50.0
MAX_LOT     = 100.0
MAX_HIST    = 30
STORE_FILE  = "stoploss.json"

# ── MT5 feature flags ─────────────────────────────────────────────────────────
_MT5_OK            = True   # build supports MT5 (False → hide MT5 UI entirely)
_MT5_PING_INTERVAL = 30     # seconds between MT5 keep-alive pings

# ── Cluster execution helper ─────────────────────────────────────────────
def _cluster_lots(volume: float, vol_max: float, vol_min: float, vol_step: float):
    """Split *volume* into N broker-legal cluster sizes when volume > vol_max.

    Example: 10M capital, 500k at risk → needs 350 lots, broker max=100
             → [87.5, 87.5, 87.5, 87.5]  (4 orders × 87.5 = 350)
    Each chunk is ≤ vol_max, ≥ vol_min, and rounded to vol_step.
    Returns a single-element list when no splitting is needed.
    """
    import math as _m
    vol_max  = float(vol_max  or 100.0)
    vol_min  = float(vol_min  or 0.01)
    vol_step = float(vol_step or 0.01)
    if volume <= vol_max:
        return [round(volume, 8)]
    n      = _m.ceil(volume / vol_max)
    chunk  = _m.floor(volume / n / vol_step) * vol_step
    chunk  = max(round(chunk, 8), vol_min)
    parts  = [chunk] * (n - 1)
    rem    = round(volume - chunk * (n - 1), 8)
    rem    = max(_m.floor(rem / vol_step) * vol_step, vol_min)
    parts.append(round(min(rem, vol_max), 8))
    return parts

# BUG 20 FIX: named layout constants replacing magic numbers
_KV_CARD_H      = 290   # dp — buy/sell card height (was 307, reduced for small phones)
_KV_NUMPAD_H    = 252   # dp — full numpad panel height
_KV_NUMPAD_ROW  = 48    # dp — each numpad button row height
_KV_NUMPAD_BAR  = 38    # dp — collapsed numpad bar height
_KV_MIN_TOUCH   = 48    # dp — Android minimum touch target
_RATE_LIMIT_TG  = 3.0   # s  — minimum seconds between TG posts

# ── Licensing ─────────────────────────────────────────────────────────────────
# PHASE 12: every endpoint that previously lived here has been deleted.
#
#   _NOTIFY_URL / _HB_URL      public ntfy topics — published customer MT5 login,
#                              balance, equity, open positions and GPS to a
#                              world-readable URL, and accepted APPROVED/REVOKE
#                              from anyone who knew the topic name
#   _APPROVE_URL               Gist allowlist — client-side licence state
#   _SESSIONS_URL              Gist session file — shared across P1/P2/StopLossPro
#   _GIST_PROXY_URL            Cloudflare Worker — authenticated writes on a
#                              User-Agent prefix, i.e. not at all
#   _LINK_URL                  unused checkout stub
#   _LIC_CACHE / _GPS_CACHE    plaintext, user-writable local state
#   _CACHED_COORDS             in-memory GPS cache
#
# Licensing now lives in lib/licensing.py and talks to an authenticated API.
# The server is authoritative; this client cannot grant itself anything.
# Session state is sealed with Windows DPAPI, not written as plaintext.

_REG_FILE = os.path.join(os.path.expanduser("~"), ".slcalc_reg")   # legacy marker only

