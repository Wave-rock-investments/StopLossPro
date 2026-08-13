# mixin_trading.py — Positions fetch, card build, close/partial/BE/modify, trail, auto-refresh.
# ─────────────────────────────────────────────────────────────────────────────
import os, time, math, logging, weakref, datetime, threading, base64, hashlib

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.animation import Animation
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.factory import Factory
from kivy.storage.jsonstore import JsonStore

from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.label import MDLabel
from kivymd.uix.card import MDCard
from kivymd.uix.dialog import MDDialog
from kivymd.uix.button import MDFlatButton, MDRaisedButton
try:
    from kivymd.uix.snackbar import MDSnackbar, MDSnackbarText
except ImportError:
    from kivymd.uix.snackbar import MDSnackbar
    MDSnackbarText = None

from constants import (
    log, platform,
    CONTRACTS, DECIMALS, DEFAULT_SL, DEFAULT_WR,
    MAX_LOT, MAX_HIST, STORE_FILE,
    _get_dp, _base_sym, _strip_broker_suffix,
    _KV_CARD_H, _KV_NUMPAD_H, _KV_NUMPAD_ROW,
    _KV_NUMPAD_BAR, _KV_MIN_TOUCH,
    _MT5_PING_INTERVAL, MT5_ORDER_TYPE_LABELS, _MT5_OK,
    _RATE_LIMIT_TG,
)
from calc import (
    TradeSetup, calc_setup, calc_auto_lot, recommend_order_type,
    _update_dynamic_aliases, _cluster_lots,
)
from mt5_api import (
    mt5_check_status, mt5_place_order, mt5_fetch_candle,
    mt5_get_account, mt5_get_positions, mt5_close_order,
    mt5_close_all_bulk, mt5_modify_sl_tp, mt5_resolve_symbols,
)


class TradingMixin:
    def on_fetch(self):
        """User tapped FETCH.
        Fetches last closed candle close+ATR in one call.
        Blocks update if the same candle has already been fetched.
        """
        if self._ui_locked or self._fetch_pending:
            return
        if not _MT5_OK:
            self._show_snackbar("MT5 module not available")
            return
        if not self._mt5_enabled:
            self._show_snackbar("MT5 not connected — enable in Settings")
            return
        sym = self.ids.instrument.text.strip()   # broker name as-is (case-sensitive)
        if not sym:
            self._show_snackbar('Select an instrument first')
            return
        base_sym = _base_sym(sym)   # strip broker suffix → CONTRACTS key
        tf = getattr(self, '_atr_timeframe', 'H1')

        self._fetch_pending = True
        self._ui_locked     = True
        try:
            self.ids.fetch_btn.disabled = True
            self.ids.fetch_btn.text     = 'Fetching…'
        except Exception as e:
            log.debug('fetch_btn start: %s', e)
        self._set_status('Fetching last candle…')

        def _fetch_timeout(dt):
            if self._fetch_pending:
                log.warning("Fetch timed out — unlocking UI")
                self._fetch_done()
                self._show_snackbar("Fetch timed out — check MT5 is running and connected")
        # 55s — must stay above mt5_fetch_candle's own 50s watchdog, or this
        # UI-level timeout fires first and reports "timed out" on a cold
        # start (MT5 not running yet) that was actually still succeeding.
        self._fetch_timeout_ev = Clock.schedule_once(_fetch_timeout, 55)

        def _ok(data):
            def _ui(_dt):
                self._do_fetch_complete(base_sym, sym, tf, data)
            Clock.schedule_once(_ui, 0)

        def _err(msg):
            def _ui(_dt):
                if not self._fetch_pending:
                    return
                self._fetch_done()
                self._show_snackbar('Fetch error: ' + msg)
            Clock.schedule_once(_ui, 0)

        mt5_fetch_candle(symbol=sym, timeframe=tf, period=14,
                         on_success=_ok, on_error=_err)

    def _do_fetch_complete(self, base_sym, sym, tf, data, silent=False):
        """Apply last closed candle data to UI fields then calculate.
        Blocks update when the same candle has already been fetched.
        silent=True suppresses the 'no new candle' toast (used by auto-fetch).
        Executes entry at the previous candle closing price (OHLC Close).
        """
        try:
            if data is None or not isinstance(data, dict):
                self._show_snackbar("Fetch failed — no data received")
                self._fetch_done()
                return

            import math
            close       = float(data.get('close',       0))
            low         = float(data.get('low',          0))
            atr         = float(data.get('atr',         0))
            digits      = int(  data.get('digits',       5))
            candle_time = int(  data.get('candle_time',  0))

            if not math.isfinite(atr) or atr <= 0 or not math.isfinite(close) or close <= 0:
                self._show_snackbar("Invalid candle data received — try again")
                self._fetch_done()
                return

            # Previous candle CLOSE is used for trade entry — changed
            # 2026-08-06 at the user's request ("execute orders in closing
            # only"), replacing the earlier LOW-based entry (that fix
            # shipped to P1/P2 on 2026-07-21). `low` is still parsed above
            # for any other OHLC use, but no longer feeds entry price.
            entry_price = close

            # Block duplicate fetch: only update when a new candle has closed
            key       = f"{base_sym}_{tf}"
            prev_time = self._last_candle_time.get(key, 0)
            if candle_time > 0 and candle_time <= prev_time:
                if not silent:
                    try:
                        import datetime as _dt
                        candle_str = _dt.datetime.fromtimestamp(candle_time).strftime("%H:%M")
                    except Exception:
                        candle_str = ""
                    self._show_snackbar(
                        f"No new {tf} candle — last close {candle_str}, values unchanged")
                self._fetch_done()
                return

            if candle_time > 0:
                self._last_candle_time[key] = candle_time

            # Update broker symbol map if resolved name differs
            resolved = data.get("symbol", sym)
            if resolved and resolved != base_sym:
                try:
                    self._broker_symbol_map[base_sym] = resolved
                    self._update_broker_sym_label()
                except Exception as e:
                    log.debug("_do_fetch_complete broker symbol map: %s", e)

            fmt = "{:." + str(digits) + "f}"
            ids = self.ids
            ids.atr.text   = fmt.format(round(atr,         digits))
            ids.entry.text = fmt.format(round(entry_price, digits))

            try:
                acct     = float(ids.account.text.strip() or "0")
                risk_pct = float(ids.risk.text.strip()    or "0")
                sl_mult  = self.sl_mult
                if acct > 0 and risk_pct > 0:
                    lots = calc_auto_lot(atr=atr, account=acct,
                                        risk_pct=risk_pct, symbol=base_sym, sl_m=sl_mult)
                    ids.lots.text = "{:.2f}".format(lots)
            except Exception as e:
                log.debug("fetch auto-lot: %s", e)

            candle_str = ""
            if candle_time > 0:
                try:
                    import datetime as _dt
                    candle_str = _dt.datetime.fromtimestamp(candle_time).strftime("%H:%M")
                except Exception:
                    candle_str = ""

            self._show_snackbar(
                f"{tf} close {candle_str}  Close={fmt.format(entry_price)}  ATR={fmt.format(atr)}")
            self._fetch_done()
            self.on_calculate()
        except Exception as e:
            log.warning("_do_fetch_complete: %s", e)
            self._show_snackbar("Fetch error: " + str(e))
            self._fetch_done()

    def _fetch_done(self):
        """Reset fetch state \u2014 always called after fetch completes or errors."""
        try:
            ev = getattr(self, '_fetch_timeout_ev', None)
            if ev:
                Clock.unschedule(ev)
                self._fetch_timeout_ev = None
        except Exception:
            pass
        self._fetch_pending = False
        self._ui_locked     = False
        try:
            self.ids.fetch_btn.disabled = False
            self.ids.fetch_btn.text     = "FETCH"
        except Exception as e:
            log.debug("fetch_btn reset: %s", e)
        self._set_status("Ready")

    # ══════════════════════════════════════════════════════════════════════
    # POSITION MANAGER — fetch, display, close, modify, ATR trail
    # ══════════════════════════════════════════════════════════════════════

    def refresh_positions(self):
        """Manual refresh — triggered by the REFRESH button."""
        if self._pos_fetching:
            return
        if not self._mt5_connected:
            self._show_snackbar("MT5 not connected")
            return
        self._pos_fetching = True
        try:
            self.ids.pos_last_update_lbl.text = "Loading…"
        except Exception:
            pass
        _r = weakref.ref(self)
        def _ok(positions):
            def _ui(dt):
                r = _r()
                if r is None: return
                r._positions_data = positions
                r._build_positions_ui(positions)
                r._pos_fetching = False
            Clock.schedule_once(_ui, 0)
        def _err(msg):
            def _ui(dt):
                r = _r()
                if r is None: return
                r._pos_fetching = False
                try:
                    r.ids.pos_last_update_lbl.text = "Error"
                except Exception:
                    pass
                r._show_snackbar(f"Positions: {msg}")
            Clock.schedule_once(_ui, 0)
        mt5_get_positions(on_success=_ok, on_error=_err)

        # Also refresh account balance
        def _acct_ok(data):
            def _ui(dt):
                r = _r()
                if r is None: return
                try:
                    bal = data.get('balance', 0)
                    eq  = data.get('equity',  0)
                    r.ids.pos_balance_lbl.text = f"Balance: {bal:,.2f}"
                    r.ids.pos_equity_lbl.text  = f"Equity:  {eq:,.2f}"
                except Exception as e:
                    log.debug("pos acct labels: %s", e)
            Clock.schedule_once(_ui, 0)
        mt5_get_account(on_success=_acct_ok, on_error=lambda m: None)

    def _build_positions_ui(self, positions):
        """Populate RecycleView data list — zero widget creation on updates."""
        import datetime as _dt
        try:
            rv = self.ids.positions_list
        except Exception as e:
            log.debug("positions_list not found: %s", e); return

        app = MDApp.get_running_app()
        is_dark = app.theme_cls.theme_style == "Dark" if app else True

        # ── Empty state ──────────────────────────────────────────────────────
        if not positions:
            rv.data = []
            try:
                self.ids.pos_empty_lbl.opacity    = 1
                self.ids.pos_count_lbl.text       = "0 positions"
                self.ids.pos_pnl_lbl.text         = "Float: 0.00"
                self.ids.pos_last_update_lbl.text = _dt.datetime.now().strftime("%H:%M:%S")
            except Exception:
                pass
            return

        # ── Summary bar ──────────────────────────────────────────────────────
        total_pnl = sum(p.get("profit", 0) + p.get("swap", 0) for p in positions)
        try:
            self.ids.pos_empty_lbl.opacity    = 0
            n = len(positions)
            self.ids.pos_count_lbl.text       = f"{n} position{'s' if n != 1 else ''}"
            pnl_sign = "▲" if total_pnl >= 0 else "▼"
            self.ids.pos_pnl_lbl.text         = f"Float: {pnl_sign}{total_pnl:+.2f}"
            self.ids.pos_last_update_lbl.text = _dt.datetime.now().strftime("%H:%M:%S")
        except Exception:
            pass

        # ── Feed RecycleView — diff-based update (MT5 dirty-cell principle) ────
        # Only replace rv.data when position count or key fields actually changed.
        # This prevents RecycleView from re-rendering all visible cards every 15s
        # when only current_price ticked (already handled by _fast_price_tick).
        _ref = weakref.ref(self)
        new_data = [
            {**p,
             "is_dark":      is_dark,
             "trail_active": p["ticket"] in self._active_trails,
             "app_ref":      _ref}
            for p in positions
        ]
        _DIFF_KEYS = ('ticket', 'profit', 'swap', 'sl', 'tp', 'volume',
                      'trail_active', 'is_dark', 'open_price')
        old_data = rv.data
        needs_rebuild = (
            len(new_data) != len(old_data) or
            any(new_data[i].get(k) != old_data[i].get(k)
                for i in range(len(new_data))
                for k in _DIFF_KEYS)
        )
        if needs_rebuild:
            rv.data = new_data
        # If only current_price changed, _fast_price_tick already updated the labels.
        # No rv.data assignment → RecycleView stays idle → zero layout cost.

    def close_position(self, ticket, symbol, volume=0):
        """Close position fully — shows confirmation dialog."""
        if not self._mt5_connected:
            self._show_snackbar("MT5 not connected"); return
        _r = weakref.ref(self)
        def _do():
            def _ok(data):
                def _ui(dt):
                    r = _r()
                    if r: r._show_snackbar(f"#{ticket} {symbol} closed ✓")
                    Clock.schedule_once(lambda _: r.refresh_positions() if r else None, 0.5)
                Clock.schedule_once(_ui, 0)
            def _err(msg):
                Clock.schedule_once(lambda dt: _r() and _r()._show_snackbar(f"Close failed: {msg}"), 0)
            mt5_close_order(ticket=ticket, volume=0, on_success=_ok, on_error=_err)
        self._confirm_dialog(f"Close Position",
                             f"Close #{ticket} {symbol} ({volume:.2f} lots)?", _do)

    def partial_close_dialog(self, ticket, symbol, full_volume):
        """Dialog to enter partial close volume."""
        from kivymd.uix.textfield import MDTextField as _MTF
        half = round(full_volume / 2, 2)
        content = MDBoxLayout(orientation="vertical", size_hint_y=None,
                              height=dp(90), spacing=dp(8),
                              padding=[dp(12), dp(8), dp(12), dp(8)])
        content.add_widget(MDLabel(
            text=f"Volume to close (max {full_volume:.2f}):",
            font_style="Caption", theme_text_color="Secondary",
            size_hint_y=None, height=dp(24)))
        vol_field = _MTF(text=str(half), hint_text="Volume",
                         mode="rectangle", size_hint_y=None,
                         height=dp(44), input_filter="float")
        content.add_widget(vol_field)

        _r = weakref.ref(self)
        def _do(_inst):
            try: v = float(vol_field.text.strip() or "0")
            except ValueError: v = 0
            r = _r()
            if not r: return
            if v <= 0 or v > full_volume:
                r._show_snackbar("Invalid volume"); return
            if r._popup_dialog:
                try: r._popup_dialog.dismiss()
                except Exception: pass
            def _ok(data):
                Clock.schedule_once(
                    lambda dt: _r() and _r()._show_snackbar(f"#{ticket} partial {v:.2f} lots ✓"), 0)
                Clock.schedule_once(
                    lambda dt: _r() and _r().refresh_positions(), 0.6)
            def _err(msg):
                Clock.schedule_once(
                    lambda dt: _r() and _r()._show_snackbar(f"Partial close failed: {msg}"), 0)
            mt5_close_order(ticket=ticket, volume=v, on_success=_ok, on_error=_err)

        dlg = MDDialog(
            title=f"Partial Close — #{ticket} {symbol}",
            type="custom", content_cls=content,
            buttons=[
                MDFlatButton(text="CANCEL", on_release=lambda x: dlg.dismiss()),
                MDRaisedButton(text="CLOSE",
                               md_bg_color=(0.60, 0.05, 0.07, 1),
                               theme_text_color="Custom", text_color=[1,1,1,1],
                               on_release=_do),
            ],
        )
        if self._popup_dialog:
            try: self._popup_dialog.dismiss()
            except Exception: pass
        self._popup_dialog = dlg
        dlg.open()

    def set_break_even(self, ticket, symbol, entry_price, direction):
        """Set SL to entry price (break-even)."""
        if not self._mt5_connected:
            self._show_snackbar("MT5 not connected"); return
        _r = weakref.ref(self)
        def _ok(data):
            Clock.schedule_once(
                lambda dt: _r() and _r()._show_snackbar(f"#{ticket} BE → {entry_price} ✓"), 0)
            Clock.schedule_once(lambda dt: _r() and _r().refresh_positions(), 0.5)
        def _err(msg):
            Clock.schedule_once(
                lambda dt: _r() and _r()._show_snackbar(f"Break-even failed: {msg}"), 0)
        mt5_modify_sl_tp(ticket=ticket, sl=entry_price, tp=0,
                         on_success=_ok, on_error=_err)

    def modify_sl_tp_dialog(self, ticket, symbol, current_sl, current_tp):
        """Dialog to modify SL and TP."""
        from kivymd.uix.textfield import MDTextField as _MTF
        content = MDBoxLayout(orientation="vertical", size_hint_y=None,
                              height=dp(108), spacing=dp(8),
                              padding=[dp(12), dp(4), dp(12), dp(4)])
        sl_field = _MTF(text=str(current_sl) if current_sl else "",
                        hint_text="New SL (0 = remove)", mode="rectangle",
                        size_hint_y=None, height=dp(44), input_filter="float")
        tp_field = _MTF(text=str(current_tp) if current_tp else "",
                        hint_text="New TP (0 = remove)", mode="rectangle",
                        size_hint_y=None, height=dp(44), input_filter="float")
        content.add_widget(sl_field)
        content.add_widget(tp_field)

        _r = weakref.ref(self)
        def _do(_inst):
            try: new_sl = float(sl_field.text.strip() or "0")
            except ValueError: new_sl = 0.0
            try: new_tp = float(tp_field.text.strip() or "0")
            except ValueError: new_tp = 0.0
            r = _r()
            if r and r._popup_dialog:
                try: r._popup_dialog.dismiss()
                except Exception: pass
            def _ok(data):
                Clock.schedule_once(
                    lambda dt: _r() and _r()._show_snackbar(f"#{ticket} SL/TP modified ✓"), 0)
                Clock.schedule_once(lambda dt: _r() and _r().refresh_positions(), 0.5)
            def _err(msg):
                Clock.schedule_once(
                    lambda dt: _r() and _r()._show_snackbar(f"Modify failed: {msg}"), 0)
            mt5_modify_sl_tp(ticket=ticket, sl=new_sl, tp=new_tp,
                             on_success=_ok, on_error=_err)

        dlg = MDDialog(
            title=f"Modify SL/TP — #{ticket} {symbol}",
            type="custom", content_cls=content,
            buttons=[
                MDFlatButton(text="CANCEL", on_release=lambda x: dlg.dismiss()),
                MDRaisedButton(text="APPLY",
                               md_bg_color=(0.20, 0.52, 0.90, 1),
                               theme_text_color="Custom", text_color=[1,1,1,1],
                               on_release=_do),
            ],
        )
        if self._popup_dialog:
            try: self._popup_dialog.dismiss()
            except Exception: pass
        self._popup_dialog = dlg
        dlg.open()

    def close_all_positions(self):
        """Close all open positions after confirmation — instant parallel execution."""
        if not self._mt5_connected:
            self._show_snackbar("MT5 not connected"); return
        if not self._positions_data:
            self._show_snackbar("No open positions"); return
        count = len(self._positions_data)
        snap  = list(self._positions_data)
        _r = weakref.ref(self)
        def _do():
            def _done(closed, failed, total):
                Clock.schedule_once(
                    lambda dt: _r() and _r()._show_snackbar(
                        f"Closed {closed}/{total}  Errors: {failed}"), 0)
                Clock.schedule_once(lambda dt: _r() and _r().refresh_positions(), 0.8)
            mt5_close_all_bulk(snap, _done)
        self._confirm_dialog("Close All Positions",
                             f"Close ALL {count} open position(s)?", _do)

    def toggle_atr_trail(self, ticket, symbol, direction):
        """Enable or disable ATR trailing stop for a position."""
        if ticket in self._active_trails:
            self._active_trails.pop(ticket, None)
            self._last_trail_candles.pop(ticket, None)
            self._show_snackbar(f"#{ticket} ATR trail OFF")
        else:
            try: period = int(self.ids.trail_period_field.text.strip() or "14")
            except Exception: period = 14
            try: mult = float(self.ids.trail_mult_field.text.strip() or "1.5")
            except Exception: mult = 1.5
            period = max(1, min(period, 200))
            mult   = max(0.1, min(mult, 10.0))
            self._active_trails[ticket] = {
                "period": period, "mult": mult,
                "direction": direction, "symbol": symbol,
            }
            self._show_snackbar(f"#{ticket} ATR trail ON  {period}p × {mult}")
        Clock.schedule_once(lambda dt: self.refresh_positions(), 0.15)

    def _trail_check_tick(self, dt):
        """Called every 30s. Update SL for all active ATR trailing stops."""
        if not self._mt5_connected or not self._active_trails:
            return
        for ticket, cfg in list(self._active_trails.items()):
            sym    = cfg["symbol"]
            period = cfg["period"]
            mult   = cfg["mult"]
            dirn   = cfg["direction"]
            _r = weakref.ref(self)
            def _atr_ok(data, _t=ticket, _d=dirn, _m=mult, _s=sym):
                atr         = data.get("atr",         0)
                cur_price   = data.get("close",        0)
                candle_time = data.get("candle_time",  0)
                r = _r()
                if not r or atr <= 0 or cur_price <= 0: return
                prev = r._last_trail_candles.get(_t, 0)
                if candle_time <= prev: return
                r._last_trail_candles[_t] = candle_time
                digs = DECIMALS.get(_base_sym(_s), 5)
                trail_dist = atr * _m
                new_sl = round((cur_price - trail_dist) if _d == "BUY"
                               else (cur_price + trail_dist), digs)
                pos_list = [p for p in r._positions_data if p["ticket"] == _t]
                if pos_list:
                    cur_sl = pos_list[0].get("sl", 0)
                    if _d == "BUY"  and cur_sl >= new_sl: return
                    if _d == "SELL" and cur_sl > 0 and cur_sl <= new_sl: return
                def _ok(data, __t=_t, __sl=new_sl):
                    Clock.schedule_once(
                        lambda dt: _r() and _r()._show_snackbar(
                            f"ATR Trail #{__t}: SL → {__sl}"), 0)
                mt5_modify_sl_tp(ticket=_t, sl=new_sl, tp=0,
                                 on_success=_ok, on_error=lambda m: None)
            mt5_fetch_candle(symbol=sym, timeframe=self._atr_timeframe,
                             period=period,
                             on_success=_atr_ok, on_error=lambda m: None)

    # ══════════════════════════════════════════════════════════════════════
    # AUTO-FETCH — polls for new closed candle on every timeframe tick
    # ══════════════════════════════════════════════════════════════════════

    def _start_auto_fetch(self, immediate=False):
        """Start precise candle-close auto-fetch for the current timeframe.

        Uses one-shot Clock.schedule_once timed to fire at the exact moment
        the next candle closes — zero polling delay, sub-second accuracy.
        Cancels any existing timer first so TF/instrument changes are safe.
        immediate=True also fires one fetch right away (TF or instrument change).
        """
        self._stop_auto_fetch()
        if not self._mt5_connected or not self._mt5_enabled:
            return
        if immediate:
            Clock.schedule_once(self._auto_fetch_tick, 0.5)
        self._schedule_next_candle_fetch()

    def _schedule_next_candle_fetch(self):
        """Calculate exact seconds until the next candle close and arm the timer.

        Candle boundaries are aligned to Unix time (MT5 uses UTC timestamps).
        A 0.5-second buffer is added so the candle is fully closed when we fetch.
        """
        import time as _t
        tf  = self._atr_timeframe
        dur = self._CANDLE_SECONDS.get(tf, 3600)
        now = _t.time()
        # Next candle close = ceiling of now to the next dur-second boundary
        next_close = (int(now / dur) + 1) * dur
        delay = max(0.2, next_close - now + 0.5)   # +0.5 s buffer
        log.debug("auto-fetch: %s next candle in %.1f s (at +%ds boundary)",
                  tf, delay, dur)
        self._auto_fetch_ev = Clock.schedule_once(
            self._auto_fetch_and_reschedule, delay)

    def _auto_fetch_and_reschedule(self, dt):
        """Fired at candle close: run the fetch, then arm the timer for the NEXT candle."""
        self._auto_fetch_tick(dt)
        if self._mt5_connected and self._mt5_enabled:
            self._schedule_next_candle_fetch()

    def _stop_auto_fetch(self):
        """Cancel the candle-close timer — safe to call repeatedly."""
        ev = getattr(self, '_auto_fetch_ev', None)
        if ev:
            try: Clock.unschedule(ev)
            except Exception: pass
        self._auto_fetch_ev = None

    def _auto_fetch_tick(self, dt):
        """Fetch last closed candle and update ATR/Entry/Lots silently.

        Called at exact candle-close time (via _auto_fetch_and_reschedule) or
        immediately on connect/TF/instrument change.
        Uses the same _do_fetch_complete() pipeline as the manual FETCH button
        but with silent=True so the 'no new candle' toast is suppressed.
        A snackbar IS shown when a genuinely new candle is found.

        Guards:
          - Skips if MT5 not connected / disabled
          - Skips if a manual fetch is already in-flight (_fetch_pending)
          - Skips if no instrument selected
        """
        if not self._mt5_connected or not self._mt5_enabled:
            return
        if self._fetch_pending:
            return   # manual fetch in-flight — let it finish
        sym = self.ids.instrument.text.strip()
        if not sym:
            return
        base_sym = _base_sym(sym)
        tf       = self._atr_timeframe
        _r = weakref.ref(self)

        def _ok(data):
            def _ui(_dt):
                r = _r()
                if r: r._do_fetch_complete(base_sym, sym, tf, data, silent=True)
            Clock.schedule_once(_ui, 0)

        mt5_fetch_candle(symbol=sym, timeframe=tf, period=14,
                         on_success=_ok, on_error=lambda m: None)

    def _start_auto_refresh(self):
        """Start all auto-refresh Clock events when MT5 connects."""
        self._stop_auto_refresh()
        self._pos_refresh_ev      = Clock.schedule_interval(self._auto_positions_tick, 15)
        self._acct_refresh_ev     = Clock.schedule_interval(self._auto_account_tick,  30)
        self._trail_ev            = Clock.schedule_interval(self._trail_check_tick,   30)
        self._fast_price_tick_ev  = Clock.schedule_interval(self._fast_price_tick,    2)
        self._start_auto_fetch(immediate=True)   # begin candle-close polling

    def _stop_auto_refresh(self):
        """Cancel all auto-refresh Clock events."""
        for attr in ('_pos_refresh_ev', '_acct_refresh_ev', '_trail_ev', '_fast_price_tick_ev', '_auto_fetch_ev'):
            ev = getattr(self, attr, None)
            if ev:
                try: ev.cancel()
                except Exception: pass
            setattr(self, attr, None)

    def _auto_positions_tick(self, dt):
        """Auto-refresh positions only when Positions tab is visible."""
        if getattr(self, '_last_tab', None) != "positions": return
        if self._mt5_connected and not self._pos_fetching:
            self.refresh_positions()

    def _auto_account_tick(self, dt):
        """Refresh account balance/equity in background every 30s."""
        if not self._mt5_connected: return
        _r = weakref.ref(self)
        def _ok(data):
            def _ui(dt):
                r = _r()
                if r is None: return
                try:
                    bal = data.get('balance', 0)
                    eq  = data.get('equity',  0)
                    r.ids.pos_balance_lbl.text = f"Balance: {bal:,.2f}"
                    r.ids.pos_equity_lbl.text  = f"Equity:  {eq:,.2f}"
                except Exception as e:
                    log.debug("_auto_account_tick: %s", e)
            Clock.schedule_once(_ui, 0)
        mt5_get_account(on_success=_ok, on_error=lambda m: None)

    # ── MT5-style fast price tick ─────────────────────────────────────────────
    def _fast_price_tick(self, dt):
        """Every 2 seconds: fetch only symbol ticks (no full positions_get).
        Directly mutates visible card labels — zero layout recalculation.
        This is how MT5 terminal keeps numbers live without lag:
          - Slow tier (15s): full position metadata + accurate P&L from broker
          - Fast tier  (2s): bid/ask tick → NOW price label only, no widget rebuild
        """
        if getattr(self, '_last_tab', None) != "positions": return
        if not self._mt5_connected: return
        positions = getattr(self, '_positions_data', None)
        if not positions: return
        if self._pos_fetching: return   # full refresh in-flight — skip

        # Collect unique symbols only (avoid redundant tick calls)
        symbols = list({p['symbol'] for p in positions})
        _r = weakref.ref(self)

        def _run():
            from concurrent.futures import ThreadPoolExecutor, as_completed as _ac
            try:
                if mt5 is None: return
                ticks = {}
                # Fetch all unique-symbol ticks in parallel — no sequential loop
                with ThreadPoolExecutor(max_workers=min(len(symbols), 64)) as pool:
                    fmap = {pool.submit(mt5.symbol_info_tick, sym): sym for sym in symbols}
                    for fut in _ac(fmap):
                        sym = fmap[fut]
                        res = fut.result()
                        if res: ticks[sym] = res
                if ticks:
                    Clock.schedule_once(lambda dt: _r() and _r()._apply_fast_ticks(ticks), 0)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _apply_fast_ticks(self, ticks):
        """Main-thread: mutate ONLY the NOW-price label on each visible card.
        No rv.data reassignment → no RecycleView layout pass → zero lag.
        Equivalent to MT5's dirty-cell update: only the changed text bytes redraw."""
        positions = getattr(self, '_positions_data', None)
        if not positions: return

        total_pnl = 0.0
        for p in positions:
            sym  = p['symbol']
            t    = ticks.get(sym)
            # Accumulate P&L from last full refresh (profit already in data)
            total_pnl += p.get('profit', 0.0) + p.get('swap', 0.0)
            if not t:
                continue

            # Pick bid (close BUY at bid) or ask (close SELL at ask) — matches MT5
            cur = float(t.bid if p['type'] == 'BUY' else t.ask)
            old_cur = p.get('current_price', 0.0)

            # Write back so next full refresh diff detects no delta
            p['current_price'] = cur

            # Skip label write if price hasn't moved (saves GPU text re-render)
            if abs(cur - old_cur) < 1e-9:
                continue

            # Direct card mutation — bypasses RecycleView entirely
            card_ref = self._card_refs.get(p['ticket'])
            card     = card_ref() if card_ref else None
            if card is None:
                continue   # card scrolled off-screen — no widget in memory

            digs    = DECIMALS.get(_base_sym(sym), 5)
            new_txt = f"{cur:.{digs}f}"
            # Guard: skip if text unchanged (Kivy still triggers canvas redraw on .text=)
            if card._price_lbl['NOW'].text != new_txt:
                card._price_lbl['NOW'].text = new_txt

        # Update float header with latest P&L sum
        pnl_sign = "▲" if total_pnl >= 0 else "▼"
        try:
            self.ids.pos_pnl_lbl.text = f"Float: {pnl_sign}{total_pnl:+.2f}"
        except Exception:
            pass


