# mt5_api.py — MetaTrader 5 API wrappers.
# Every public function runs in a daemon thread; result fires via callback on
# the Kivy main thread using Clock.schedule_once(lambda dt: cb(...), 0).
# ─────────────────────────────────────────────────────────────────────────────
import os, time, threading, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from kivy.clock import Clock

from constants import _MT5_OK, MT5_ORDER_TYPE_LABELS, _MT5_EXE_CANDIDATES, MAX_LOT, _BROKER_ALIASES, _cluster_lots

import logging
log = logging.getLogger("StopLossPro.mt5_api")

try:
    import MetaTrader5 as mt5
    _MT5_DIRECT = mt5 is not None
except Exception:
    mt5 = None          # type: ignore[assignment]
    _MT5_DIRECT = False


def _deliver(callback, *args):
    """Deliver a callback on the Kivy main thread.
    Falls back to direct call if Kivy clock is unavailable (tests, CLI).
    This is the single enforcement point for the API threading contract.
    """
    if callback is None:
        return
    try:
        from kivy.clock import Clock
        Clock.schedule_once(lambda dt: callback(*args), 0)
    except Exception:
        # Kivy not running (unit tests, offline mode) — call directly
        try:
            callback(*args)
        except Exception as exc:
            log.debug("[_deliver] callback error: %s", exc)

def _find_mt5_exe():
    try:
        import winreg
        for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\MetaQuotes Software\MetaTrader 5",
                        r"SOFTWARE\WOW6432Node\MetaQuotes Software\MetaTrader 5"):
                try:
                    with winreg.OpenKey(root_key, sub) as k:
                        d, _ = winreg.QueryValueEx(k, "Path")
                        p = os.path.join(d, "terminal64.exe")
                        if os.path.exists(p):
                            return p
                except Exception:
                    pass
    except Exception:
        pass
    for p in _MT5_EXE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

def _mt5_ensure_init():
    """Initialize MT5, launching the terminal if not already running."""
    if mt5 is None:
        return False
    if mt5.initialize():
        return True
    exe = _find_mt5_exe()
    if exe:
        try:
            subprocess.Popen([exe])
        except Exception as e:
            log.warning("MT5 launch: %s", e)
            return False
        import time as _t
        for _ in range(6):
            _t.sleep(3)
            if mt5.initialize():
                return True
    return False

def mt5_check_status(on_success, on_error):
    def _run():
        try:
            if not _mt5_ensure_init():
                _deliver(on_error, "Cannot connect to MT5"); return
            info = mt5.account_info()
            if info is None:
                _deliver(on_error, str(mt5.last_error())); return
            _deliver(on_success, {
                'connected': True,
                'account': {
                    'balance': info.balance, 'equity': info.equity,
                    'currency': info.currency, 'name': info.name, 'server': info.server,
                }
            })
        except Exception as e:
            _deliver(on_error, str(e))
    threading.Thread(target=_run, daemon=True).start()

def mt5_place_order(symbol, order_type, volume, price, sl, tp, comment,
                    on_success, on_error):
    """Place an order — instant execution regardless of cluster count.

    Single order: direct order_send, no overhead.
    Cluster order (volume > broker max lot):
      - One shared price snapshot taken before dispatch
      - All sub-orders submitted simultaneously via ThreadPoolExecutor
      - pool.shutdown(wait=False): Python returns in <50 ms, clusters
        continue executing in background daemon threads
      - on_success fires on the FIRST cluster confirmation so the UI
        unlocks immediately — remaining clusters complete in background
      - on_error fires only if the first confirmation is a failure and
        no success has been signalled yet
    """
    def _run():
        from concurrent.futures import ThreadPoolExecutor
        import threading as _th
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return

            _OT = {
                'MARKET_BUY':      mt5.ORDER_TYPE_BUY,
                'MARKET_SELL':     mt5.ORDER_TYPE_SELL,
                'BUY_LIMIT':       mt5.ORDER_TYPE_BUY_LIMIT,
                'SELL_LIMIT':      mt5.ORDER_TYPE_SELL_LIMIT,
                'BUY_STOP':        mt5.ORDER_TYPE_BUY_STOP,
                'SELL_STOP':       mt5.ORDER_TYPE_SELL_STOP,
                'BUY_STOP_LIMIT':  mt5.ORDER_TYPE_BUY_STOP_LIMIT,
                'SELL_STOP_LIMIT': mt5.ORDER_TYPE_SELL_STOP_LIMIT,
            }
            ot = _OT.get(order_type)
            if ot is None:
                _deliver(on_error, f"Unknown order type: {order_type}"); return
            is_market = order_type in ('MARKET_BUY', 'MARKET_SELL')

            # ── Broker volume limits & cluster split ─────────────────────
            si       = mt5.symbol_info(symbol)
            vol_max  = float(si.volume_max  if si and si.volume_max  > 0 else MAX_LOT)
            vol_min  = float(si.volume_min  if si and si.volume_min  > 0 else 0.01)
            vol_step = float(si.volume_step if si and si.volume_step > 0 else 0.01)
            clusters = _cluster_lots(float(volume), vol_max, vol_min, vol_step)
            n        = len(clusters)

            # ── Single shared price snapshot ─────────────────────────────
            tick = mt5.symbol_info_tick(symbol)   # always fetch fresh tick
            if tick is None:
                _deliver(on_error, "Cannot read price — MT5 tick unavailable"); return

            if is_market:
                base_price = tick.ask if order_type == 'MARKET_BUY' else tick.bid
            else:
                base_price = float(price)
                # ── Stops-level guard for pending orders ──────────────────
                # MT5 rejects a pending order whose entry is within the
                # broker's minimum-distance zone (trade_stops_level × point).
                # If entry is that close to market it IS essentially a market
                # fill — convert silently to avoid "invalid price" rejection.
                stops_pts = 0.0
                if si and si.point:
                    stops_pts = (si.trade_stops_level or 0) * si.point
                    # floor: at least 2× the current spread
                    spread    = tick.ask - tick.bid
                    stops_pts = max(stops_pts, spread * 2.0)
                bid_now = tick.bid;  ask_now = tick.ask
                _too_close = False
                if order_type in ('SELL_LIMIT', 'SELL_STOP'):
                    _too_close = abs(base_price - bid_now) <= stops_pts
                elif order_type in ('BUY_LIMIT', 'BUY_STOP'):
                    _too_close = abs(base_price - ask_now) <= stops_pts
                if _too_close:
                    # Entry is inside the no-pending zone — treat as market fill
                    log.debug(
                        "PENDING→MARKET: %s entry=%.5f bid=%.5f ask=%.5f stops=%.5f",
                        order_type, base_price, bid_now, ask_now, stops_pts)
                    is_market  = True
                    ot         = (mt5.ORDER_TYPE_SELL if 'SELL' in order_type
                                  else mt5.ORDER_TYPE_BUY)
                    base_price = bid_now if 'SELL' in order_type else ask_now
                # Also re-validate direction: if wrong side (e.g., SELL_LIMIT
                # but entry now < bid), flip to the correct pending type.
                elif not is_market:
                    if order_type == 'SELL_LIMIT' and base_price < bid_now:
                        ot = mt5.ORDER_TYPE_SELL_STOP
                        log.debug("SELL_LIMIT→SELL_STOP auto (entry<bid)")
                    elif order_type == 'SELL_STOP' and base_price > bid_now:
                        ot = mt5.ORDER_TYPE_SELL_LIMIT
                        log.debug("SELL_STOP→SELL_LIMIT auto (entry>bid)")
                    elif order_type == 'BUY_LIMIT' and base_price > ask_now:
                        ot = mt5.ORDER_TYPE_BUY_STOP
                        log.debug("BUY_LIMIT→BUY_STOP auto (entry>ask)")
                    elif order_type == 'BUY_STOP' and base_price < ask_now:
                        ot = mt5.ORDER_TYPE_BUY_LIMIT
                        log.debug("BUY_STOP→BUY_LIMIT auto (entry<ask)")

            action = mt5.TRADE_ACTION_DEAL if is_market else mt5.TRADE_ACTION_PENDING

            # ─────────────────────────────────────────────────────────────
            # FAST PATH: single order — no executor overhead
            # ─────────────────────────────────────────────────────────────
            if n == 1:
                req = {
                    "action":       action,
                    "symbol":       symbol,
                    "volume":       float(clusters[0]),
                    "type":         ot,
                    "price":        base_price,
                    "sl":           float(sl) if sl else 0.0,
                    "tp":           float(tp) if tp else 0.0,
                    "comment":      comment or 'SL Calculator',
                    "type_time":    mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    _deliver(on_success, {'ticket': res.order,
                                               'tickets': [res.order], 'clusters': 1})
                else:
                    msg = res.comment if res else str(mt5.last_error())
                    _deliver(on_error, msg)
                return

            # ─────────────────────────────────────────────────────────────
            # CLUSTER PATH: fire all N simultaneously, return in <50 ms
            # ─────────────────────────────────────────────────────────────
            log.debug("INSTANT CLUSTER: %.2f lots → %d parallel orders "
                      "(vol_max=%.2f, price=%.5f)", volume, n, vol_max, base_price)

            # Thread-safe one-shot notification: first confirmation wins
            _notified = _th.Event()

            def _send(idx, clot):
                suffix = f"[{idx+1}/{n}]"
                req = {
                    "action":       action,
                    "symbol":       symbol,
                    "volume":       float(clot),
                    "type":         ot,
                    "price":        base_price,
                    "sl":           float(sl) if sl else 0.0,
                    "tp":           float(tp) if tp else 0.0,
                    "comment":      f"{comment or 'SL Calculator'}{suffix}",
                    "type_time":    mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log.debug("CLUSTER %d/%d OK ticket=%s", idx+1, n, res.order)
                    # Signal on_success exactly once — whichever cluster
                    # confirms first, UI unlocks immediately
                    if not _notified.is_set():
                        _notified.set()
                        _deliver(on_success, {'ticket': res.order,
                                                   'tickets': [res.order],
                                                   'clusters': n})
                else:
                    msg = res.comment if res else str(mt5.last_error())
                    log.warning("CLUSTER %d/%d failed: %s", idx+1, n, msg)
                    # Only raise on_error if nothing succeeded yet
                    if not _notified.is_set():
                        _notified.set()
                        _deliver(on_error, 
                            f"Cluster {idx+1}/{n} failed: {msg}")

            # Submit all clusters — shutdown(wait=False) returns immediately.
            # Worker threads are daemon threads; they complete in background
            # without blocking the UI or this function.
            pool = ThreadPoolExecutor(max_workers=min(n, 512))
            for idx, clot in enumerate(clusters):
                pool.submit(_send, idx, clot)
            pool.shutdown(wait=False)   # ← instant return, no blocking

        except Exception as e:
            _deliver(on_error, str(e))
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception as _te:
        # OS thread limit hit — call on_error so _mt5_posting gets cleared
        log.error("mt5_place_order: failed to start thread: %s", _te)
        _deliver(on_error, f"Thread start failed: {_te}")

def mt5_fetch_candle(symbol, timeframe, period, on_success, on_error):
    def _run():
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return
            tf_map = {
                'M1': mt5.TIMEFRAME_M1,  'M5':  mt5.TIMEFRAME_M5,
                'M15': mt5.TIMEFRAME_M15,'M30': mt5.TIMEFRAME_M30,
                'H1':  mt5.TIMEFRAME_H1, 'H4':  mt5.TIMEFRAME_H4,
                'D1':  mt5.TIMEFRAME_D1, 'W1':  mt5.TIMEFRAME_W1,
            }
            tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, period + 2)
            if rates is None or len(rates) < 2:
                _deliver(on_error, "No candle data from MT5"); return

            # Index -2 is the previous completed candle (index -1 is current active candle)
            last = rates[-2]
            trs = []
            for i in range(1, len(rates) - 1):
                try:
                    h, l, pc = rates[i]['high'], rates[i]['low'], rates[i-1]['close']
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                except Exception:
                    pass

            atr = (sum(trs) / len(trs)) if trs else float(last['high'] - last['low'])
            if not (atr > 0 and atr == atr):  # check for positive and not NaN
                atr = float(last['high'] - last['low']) if (last['high'] - last['low']) > 0 else 0.0001

            sym_info = mt5.symbol_info(symbol)
            digits = sym_info.digits if sym_info else 5

            def _get_val(rec, key, fallback=0.0):
                try:
                    if hasattr(rec, key):
                        val = float(getattr(rec, key))
                    else:
                        val = float(rec[key])
                    import math
                    return val if math.isfinite(val) else fallback
                except Exception:
                    return fallback

            c_close = _get_val(last, 'close', 0.0)
            c_open  = _get_val(last, 'open',  c_close)
            c_high  = _get_val(last, 'high',  c_close)
            c_low   = _get_val(last, 'low',   c_close)
            c_time  = int(_get_val(last, 'time', 0))

            _deliver(on_success, {
                'close':       c_close,
                'open':        c_open,
                'high':        c_high,
                'low':         c_low,
                'atr':         float(atr),
                'digits':      int(digits),
                'candle_time': c_time,
                'symbol':      symbol,
            })
        except Exception as e:
            log.warning("mt5_fetch_candle exception: %s", e)
            _deliver(on_error, f"Candle fetch error: {e}")
    threading.Thread(target=_run, daemon=True).start()

def mt5_get_account(on_success, on_error):
    def _run():
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return
            info = mt5.account_info()
            if info is None:
                _deliver(on_error, str(mt5.last_error())); return
            _deliver(on_success, {'balance': info.balance, 'equity': info.equity})
        except Exception as e:
            _deliver(on_error, str(e))
    threading.Thread(target=_run, daemon=True).start()


def mt5_get_positions(on_success, on_error):
    """Fetch all open positions (market orders)."""
    def _run():
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return
            positions = mt5.positions_get()
            if positions is None:
                positions = []
            result = []
            tick_cache = {}          # fetch each symbol's tick only once
            for pos in positions:
                if pos.symbol not in tick_cache:
                    tick_cache[pos.symbol] = mt5.symbol_info_tick(pos.symbol)
                tick = tick_cache[pos.symbol]
                cur  = (tick.bid if pos.type == 0 else tick.ask) if tick else 0.0
                result.append({
                    "ticket":        pos.ticket,
                    "symbol":        pos.symbol,
                    "type":          "BUY" if pos.type == 0 else "SELL",
                    "volume":        pos.volume,
                    "open_price":    pos.price_open,
                    "sl":            pos.sl,
                    "tp":            pos.tp,
                    "profit":        pos.profit,
                    "swap":          pos.swap,
                    "current_price": float(cur),
                    "open_time":     pos.time,
                    "comment":       pos.comment,
                    "magic":         pos.magic,
                })
            _deliver(on_success, result)
        except Exception as e:
            _deliver(on_error, str(e))
    threading.Thread(target=_run, daemon=True).start()


def mt5_close_order(ticket, volume, on_success, on_error):
    """Close a position fully (volume=0) or partially."""
    def _run():
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return
            pos_list = mt5.positions_get(ticket=int(ticket))
            if not pos_list:
                _deliver(on_error, f"Position {ticket} not found")
                return
            pos  = pos_list[0]
            vol  = float(volume) if volume and float(volume) > 0 else pos.volume
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                _deliver(on_error, f"No tick for {pos.symbol}")
                return
            price      = tick.bid if pos.type == 0 else tick.ask
            close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
            result = mt5.order_send({
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       vol,
                "type":         close_type,
                "position":     int(ticket),
                "price":        price,
                "comment":      "SL Calc Close",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            })
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                _deliver(on_success, {"ticket": result.order})
                return
            err = result.comment if result else str(mt5.last_error())
            _deliver(on_error, err)
        except Exception as e:
            _deliver(on_error, str(e))
    threading.Thread(target=_run, daemon=True).start()


def mt5_close_all_bulk(positions_snap, on_done):
    """Close ALL positions in positions_snap simultaneously — target <2 seconds.

    Optimised 2-wave execution:
      Wave 1: parallel symbol_info_tick for every unique symbol (no per-position round-trip)
      Wave 2: parallel order_send for every position using pre-fetched tick prices

    positions_snap must be a list of dicts with keys:
        ticket, symbol, type (0=buy/1=sell), volume
    on_done(closed, failed, total) is called exactly once from a background thread.
    """
    def _run():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        snap = list(positions_snap)
        n    = len(snap)
        if n == 0:
            if on_done: on_done(0, 0, 0)
            return
        closed_n = failed_n = 0
        try:
            if mt5 is None or not _mt5_ensure_init():
                if on_done: on_done(0, n, n)
                return

            # Wave 1 — fetch tick for every unique symbol in parallel
            # timeout=0.8s: if any tick hangs we still proceed with the rest
            symbols = list({p['symbol'] for p in snap})
            ticks   = {}
            with ThreadPoolExecutor(max_workers=min(len(symbols), 64)) as pool:
                futs = {pool.submit(mt5.symbol_info_tick, sym): sym for sym in symbols}
                for fut in as_completed(futs, timeout=0.8):
                    sym = futs[fut]
                    try:
                        res = fut.result()
                        if res: ticks[sym] = res
                    except Exception: pass

            # Wave 2 — fire every order_send in parallel (no positions_get needed)
            def _close_one(pos):
                sym   = pos['symbol']
                tick  = ticks.get(sym)
                if not tick:
                    return False, f"No tick for {sym}"
                pos_type   = pos['type']            # 0=buy, 1=sell
                price      = tick.bid if pos_type == 0 else tick.ask
                close_type = mt5.ORDER_TYPE_SELL   if pos_type == 0 else mt5.ORDER_TYPE_BUY
                result = mt5.order_send({
                    "action":       mt5.TRADE_ACTION_DEAL,
                    "symbol":       sym,
                    "volume":       float(pos['volume']),
                    "type":         close_type,
                    "position":     int(pos['ticket']),
                    "price":        price,
                    "comment":      "SL Calc Close",
                    "type_time":    mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                })
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    return True, result.order
                err = result.comment if result else str(mt5.last_error())
                return False, err

            # Wave 2 — timeout=1.5s guarantees on_done fires within 2s total
            # (0.8s Wave 1 + 1.5s Wave 2 + overhead ≤ 2.5s worst case,
            #  but typical MT5 latency is <50ms per order so real-world ~200ms)
            fmap2 = {}
            pool2 = ThreadPoolExecutor(max_workers=min(n, 512))
            for pos in snap:
                fmap2[pool2.submit(_close_one, pos)] = pos
            pool2.shutdown(wait=False)          # fire all, don't block here
            import time as _t; _deadline = _t.time() + 1.5
            try:
                for fut in as_completed(fmap2, timeout=1.5):
                    try:
                        ok, info = fut.result()
                        if ok:
                            closed_n += 1
                            log.debug("bulk_close ticket=%s OK", info)
                        else:
                            failed_n += 1
                            log.warning("bulk_close failed: %s", info)
                    except Exception as _fe:
                        failed_n += 1
                        log.warning("bulk_close future err: %s", _fe)
            except Exception:
                # TimeoutError after 1.5s — count remaining as failed
                failed_n = n - closed_n
                log.warning("bulk_close wave-2 timeout: %d closed, %d timed-out", closed_n, failed_n)

        except Exception as e:
            log.error("mt5_close_all_bulk: %s", e)
            failed_n = n - closed_n

        if on_done: on_done(closed_n, failed_n, n)
    threading.Thread(target=_run, daemon=True).start()


def mt5_modify_sl_tp(ticket, sl, tp, on_success, on_error):
    """Modify SL and/or TP of an open position."""
    def _run():
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return
            pos_list = mt5.positions_get(ticket=int(ticket))
            if not pos_list:
                _deliver(on_error, f"Position {ticket} not found")
                return
            pos    = pos_list[0]
            result = mt5.order_send({
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   pos.symbol,
                "position": int(ticket),
                "sl":       float(sl or 0),
                "tp":       float(tp or 0),
            })
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                _deliver(on_success, {})
                return
            err = result.comment if result else str(mt5.last_error())
            _deliver(on_error, err)
        except Exception as e:
            _deliver(on_error, str(e))
    threading.Thread(target=_run, daemon=True).start()


def mt5_resolve_symbols(symbols, on_success, on_error):
    def _run():
        try:
            if mt5 is None or not _mt5_ensure_init():
                _deliver(on_error, "MT5 not connected"); return
            result = {}
            for base in symbols:
                info = mt5.symbol_info(base)
                if info:
                    result[base] = info.name
                else:
                    for alias, mapped in _BROKER_ALIASES.items():
                        if mapped == base:
                            ai = mt5.symbol_info(alias)
                            if ai:
                                result[base] = ai.name
                                break
            _deliver(on_success, {'symbols': result})
        except Exception as e:
            _deliver(on_error, str(e))
    threading.Thread(target=_run, daemon=True).start()

# ── CONTRACTS ──────────────────────────────────────────────────────────────
# ── CONTRACTS ──────────────────────────────────────────────────────────────
# ── Pre-computed dp values for history card layout ─────────────────────────
# dp() is a float multiply — cheap, but called 450+ times during history build.
# Pre-compute at module load time and reuse.
_DP = {}  # populated lazily after Kivy initialises (requires Window)

