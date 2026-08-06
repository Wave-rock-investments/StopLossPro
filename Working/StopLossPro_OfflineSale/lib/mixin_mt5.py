# mixin_mt5.py — MT5 connect/disconnect, ping, keystore, broker symbol, on_mt5_order.
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
    platform,
    CONTRACTS, DECIMALS, DEFAULT_SL, DEFAULT_WR,
    MAX_LOT, MAX_HIST, STORE_FILE,
    _get_dp, _base_sym, _strip_broker_suffix,
    _KV_CARD_H, _KV_NUMPAD_H, _KV_NUMPAD_ROW,
    _KV_NUMPAD_BAR, _KV_MIN_TOUCH,
    _MT5_PING_INTERVAL, MT5_ORDER_TYPE_LABELS, _MT5_OK,
    _RATE_LIMIT_TG, _dynamic_aliases,
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

import logging
log = logging.getLogger("StopLossPro.mixin_mt5")


class MT5Mixin:
    def on_mt5_toggle(self, active: bool):
        self._mt5_enabled = active
        log.debug("[EVENT] MT5_TOGGLE active=%r", active)
        self._save_mt5_settings()
        self._update_mt5_status()
        if active:
            self._start_mt5_ping()
        else:
            self._stop_mt5_ping()
            self._mt5_connected = False
        self._apply_mt5_visuals()

    def on_mt5_test(self):
        if getattr(self, "_mt5_testing", False):
            self._show_snackbar("Test already in progress…")
            return
        self._mt5_testing = True
        # Persistent status label, not a snackbar — the connection check
        # can legitimately take up to ~45s when MT5 isn't running yet
        # (we launch it and wait for it to come up and log in to the
        # broker). A snackbar auto-dismisses after 2s, which made this
        # look stuck/broken on any machine where the real check took
        # longer than that — the check was still working, it just wasn't
        # showing anything anymore. _ping_mt5's _ok/_err below update
        # this same label once the check actually finishes.
        self._set_status("Connecting to MT5… (can take up to 30-45s on first launch)")
        _r = weakref.ref(self)
        def _done_testing():
            def _ui(dt):
                r = _r()
                if r: r._mt5_testing = False
            Clock.schedule_once(_ui, 0)
        self._ping_mt5(on_done=_done_testing)

    def on_tf_change(self, tf: str):
        if tf and tf != self._atr_timeframe:
            self._atr_timeframe = tf
            # Clear dedup cache — switching TF means user wants fresh data,
            # not a block because the same candle was already fetched on this TF.
            self._last_candle_time.clear()
            log.debug("[EVENT] TF_CHANGE %s", tf)
            # Restart auto-fetch with the new interval and fire immediately
            # so ATR/Entry update without the user having to tap FETCH.
            self._start_auto_fetch(immediate=True)

    def _save_mt5_settings(self):
        try:
            self._store.put('mt5_settings',
                enabled=self._mt5_enabled,
                share_on=self._mt5_share_on,
                atr_tf=self._atr_timeframe)
        except Exception as e:
            log.warning("_save_mt5_settings: %s", e)

    def _load_mt5_settings(self):
        _VALID_TF = {"M1","M5","M15","M30","H1","H4","D1","W1"}
        try:
            if self._store.exists('mt5_settings'):
                s = self._store.get('mt5_settings')
                self._mt5_enabled  = s.get('enabled',  False)
                self._mt5_share_on = s.get('share_on', False)
                saved_tf = s.get('atr_tf', 'H1')
                if saved_tf in _VALID_TF:
                    self._atr_timeframe = saved_tf
                    try:
                        self.ids.tf_spinner.text = saved_tf
                    except Exception:
                        pass
        except Exception as e:
            log.debug("_load_mt5_settings: %s", e)

    def _ping_mt5(self, on_done=None):
        """MT5 connectivity ping — skips if a previous ping is still in progress."""
        if getattr(self, '_mt5_pinging', False):
            log.debug("[MT5_PING] skipped — previous ping still running")
            return
        self._mt5_pinging = True
        log.debug("[EVENT] MT5_PING direct")
        _r = weakref.ref(self)
        def _ok(data):
            def _ui(dt):
                r = _r()
                if r is None: return
                r._mt5_pinging = False
                was_connected = r._mt5_connected
                r._mt5_connected = data.get('connected', False)
                log.debug("[EVENT] MT5_PING_OK connected=%s", r._mt5_connected)
                r._update_mt5_status(); r._apply_mt5_visuals()
                # Only touch the persistent status label for a manual Test —
                # the silent 30s background ping should stay silent when
                # it succeeds instantly, which is the normal case.
                if getattr(r, '_mt5_testing', False):
                    r._set_status(
                        "✓ Connected to MT5" if r._mt5_connected else "MT5 not connected",
                        "success" if r._mt5_connected else "error")
                if r._mt5_connected and not was_connected:
                    r._resolve_broker_symbols()
                    Clock.schedule_once(lambda dt: r._start_auto_refresh(), 1.0)
                if on_done: on_done()
            Clock.schedule_once(_ui, 0)
        def _err(msg):
            def _ui(dt):
                r = _r()
                if r is None: return
                r._mt5_pinging = False
                r._mt5_connected = False
                r._broker_symbol_map = {}
                r._update_broker_sym_label()
                log.debug("[EVENT] MT5_PING_ERR %s", msg)
                r._update_mt5_status(); r._apply_mt5_visuals()
                if getattr(r, '_mt5_testing', False):
                    r._set_status(msg, "error")
                if on_done: on_done()
            Clock.schedule_once(_ui, 0)
        mt5_check_status(on_success=_ok, on_error=_err)

    def _start_mt5_ping(self):
        self._stop_mt5_ping()
        self._ping_mt5()
        self._mt5_ping_event = Clock.schedule_interval(
            lambda dt: self._ping_mt5(), 30)

    def _stop_mt5_ping(self):
        if self._mt5_ping_event:
            try: self._mt5_ping_event.cancel()
            except Exception: pass
            self._mt5_ping_event = None
        self._mt5_connected     = False
        self._broker_symbol_map = {}
        _dynamic_aliases.clear()          # live reverse map no longer valid
        self._stop_auto_refresh()
        app = MDApp.get_running_app()
        if app:
            app.rebuild_instrument_menu(list(CONTRACTS.keys()))

    def _resolve_broker_symbols(self):
        """On MT5 connect, resolve broker-specific symbol names for all instruments
        and rebuild the dropdown so users see their broker's actual symbol names."""
        _r = weakref.ref(self)
        def _ok(data):
            def _ui(dt):
                r = _r()
                if r is None: return
                r._broker_symbol_map = data.get('symbols', {})
                _update_dynamic_aliases(r._broker_symbol_map)   # live reverse map for _base_sym
                log.debug("broker_sym_map: %s", r._broker_symbol_map)
                # Build the menu in CONTRACTS order using broker-resolved names
                broker_names = []
                for base in CONTRACTS:
                    broker = r._broker_symbol_map.get(base, base)
                    if broker not in broker_names:
                        broker_names.append(broker)
                app = MDApp.get_running_app()
                if app:
                    app.rebuild_instrument_menu(broker_names)
                r._update_broker_sym_label()
            Clock.schedule_once(_ui, 0)
        def _err(msg):
            log.debug("resolve_broker_symbols: %s", msg)
        mt5_resolve_symbols(symbols=list(CONTRACTS.keys()),
                            on_success=_ok, on_error=_err)

    def _broker_sym(self, base: str) -> str:
        """Return broker-specific symbol when MT5 connected, else base name."""
        if self._mt5_connected:
            return self._broker_symbol_map.get(base.upper(), base)
        return base

    def _update_broker_sym_label(self):
        """Show resolved broker symbol under the instrument field.
        Hidden when the field already contains the broker name (selected from menu),
        shown when user typed a base name that maps to a different broker name.
        """
        try:
            text   = self.ids.instrument.text.strip()
            lbl    = self.ids.broker_sym_lbl
            if not self._mt5_connected or not text:
                lbl.text = ""; lbl.opacity = 0; return
            base   = _base_sym(text)
            broker = self._broker_symbol_map.get(base, base)
            # Show only when the field does NOT already hold the broker name
            if broker and broker != text:
                lbl.text    = f"↪ {broker}"
                lbl.opacity = 1
            else:
                lbl.text    = ""
                lbl.opacity = 0
        except Exception as e:
            log.debug("_update_broker_sym_label: %s", e)

    def _update_mt5_status(self):
        try:
            lbl = self.ids.mt5_status_lbl
            if not self._mt5_enabled:
                lbl.text = "Disabled"
                lbl.theme_text_color = "Secondary"
            elif self._mt5_connected:
                lbl.text = "✓ Connected"
                lbl.theme_text_color = "Custom"
                lbl.text_color = (0.15, 0.79, 0.36, 1) if MDApp.get_running_app().theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)
            else:
                lbl.text = "✗ Disconnected"
                lbl.theme_text_color = "Custom"
                lbl.text_color = (1.0, 0.26, 0.26, 1) if MDApp.get_running_app().theme_cls.theme_style == "Dark" else (0.82, 0.10, 0.10, 1)
        except Exception as e:
            log.debug("_update_mt5_status widget: %s", e)

    def _apply_mt5_visuals(self):
        """MT5 BUY/SELL visible only when connected + levels ready."""
        try:
            levels_ready = bool(self.buy) and bool(self.sell)
            mt5_show     = self._mt5_enabled and self._mt5_connected

            for bid in ('mt5_buy_btn', 'mt5_sell_btn'):
                try:
                    b = self.ids[bid]
                    b.opacity  = 1.0 if mt5_show else 0.0
                    b.disabled = not (mt5_show and levels_ready)
                except Exception as e:
                    log.debug("_apply_mt5_visuals %s: %s", bid, e)

            # FETCH & CALCULATE visible only when MT5 enabled
            try:
                fr = self.ids.fetch_row
                if self._mt5_enabled:
                    fr.height  = dp(52)
                    fr.opacity = 1
                else:
                    fr.height  = 0
                    fr.opacity = 0
            except Exception as e:
                log.debug("_apply_mt5_visuals fetch_row: %s", e)
        except Exception as e:
            log.debug("_apply_mt5_visuals outer: %s", e)

    def on_mt5_order(self, direction: str):
        import time as _time
        log.debug("[EVENT] MT5_ORDER direction=%r connected=%r posting=%r", direction, self._mt5_connected, self._mt5_posting)
        # Auto-reset posting flag if stuck > 20s (network timeout is 8s)
        if self._mt5_posting:
            stuck = _time.monotonic() - getattr(self, '_mt5_posting_ts', 0)
            if stuck > 20:
                log.debug("[EVENT] MT5_POSTING stuck %.0fs — auto-reset", stuck)
                self._mt5_posting = False
            else:
                self._show_snackbar("Order in progress — please wait"); return
        if not self._mt5_enabled:
            self._show_snackbar("MT5 disabled — enable in Settings"); return
        if not self._mt5_connected:
            self._show_snackbar("MT5 not connected — open MT5 and tap Connect in Settings"); return
        data = self.buy if direction == "BUY" else self.sell
        if not data:
            self._show_snackbar("No levels — tap Calculate first"); return
        try:
            self._show_trade_popup(direction, data)
        except Exception as exc:
            log.debug("[EVENT] MT5_ORDER popup error: %s", exc)
            self._show_snackbar(f"Popup error: {exc}")

    # ── Fetch & Calculate ────────────────────────────────────────────────


