# mixin_settings.py — Load, save and apply user settings (SL mult, win rate, last values).
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


class SettingsMixin:
    def _load_settings_values(self):
        """Load multiplier values into Python attrs. Zero widget access."""
        try:
            if self._store.exists('settings'):
                s = self._store.get('settings')
                self.sl_mult  = s.get('sl_mult',  DEFAULT_SL)
                self.win_rate = s.get('win_rate', DEFAULT_WR)
        except Exception as e:
            log.warning("_load_settings_values: %s", e)
        # Load ui prefs (theme, numpad visibility)
        try:
            if self._store.exists('ui_prefs'):
                prefs = self._store.get('ui_prefs')
                self._theme_style    = prefs.get('theme', 'Dark')
                self._numpad_visible = prefs.get('numpad_visible', True)
            else:
                self._theme_style    = 'Dark'
                self._numpad_visible = True
        except Exception:
            self._theme_style    = 'Dark'
            self._numpad_visible = True
        # ── Monthly stats — load into memory on startup ────────────────────
        now = datetime.date.today()
        current_key = f"{now.year}_{now.month:02d}"
        try:
            if self._store.exists('monthly_stats'):
                stored = self._store.get('monthly_stats')
                if stored.get('key') == current_key:
                    self._stats_wins   = stored.get('wins', 0)
                    self._stats_losses = stored.get('losses', 0)
                    self._stats_key    = current_key
                else:
                    # New month — reset
                    self._stats_wins   = 0
                    self._stats_losses = 0
                    self._stats_key    = current_key
            else:
                self._stats_wins   = 0
                self._stats_losses = 0
                self._stats_key    = current_key
        except Exception:
            self._stats_wins   = 0
            self._stats_losses = 0
            self._stats_key    = current_key

    def _apply_settings_to_widgets(self):
        """Called once when the Settings tab is first visited."""
        try:
            wr    = self.ids.win_rate_slider
            wr_cb = wr.fbind('value', self.on_win_rate)
            try:
                wr.value = self.win_rate
            finally:
                wr.unbind_uid('value', wr_cb)
            # Restore MT5 settings widgets
            try:
                self.ids.mt5_toggle.active = self._mt5_enabled
                self._update_mt5_status()
            except Exception as _e:
                log.debug("_apply_settings_to_widgets mt5: %s", _e)
        except Exception as e:
            log.warning("_apply_settings_to_widgets: %s", e)
        self._update_settings_labels()
        Clock.schedule_once(lambda dt: self._apply_mt5_visuals(), 0.1)

    def _save_settings(self):
        try:
            self._store.put('settings',
                sl_mult=self.sl_mult,
                win_rate=self.win_rate)
        except Exception as e:
            log.warning("_save_settings: %s", e)

    def _update_settings_labels(self):
        """FIX #8 — guard every ID access so early calls are silently skipped."""
        ids = self.ids
        if 'sl_mult_lbl' not in ids:
            return
        ids.sl_mult_lbl.text  = f"{self.sl_mult:.2f}×"
        ids.win_rate_lbl.text = f"{self.win_rate:.0f}%"
        # NOTE: sl_mult_hint intentionally left blank (see layout.kv) — do not
        # reconstruct the "SL = ATR × N" formula string here. Showing the
        # exact multiplier formula in the UI hands the risk model to anyone
        # who screenshots Settings. The numeric badge above (sl_mult_lbl) is
        # enough for the user to see/edit their own chosen value.
        try:
            ids.win_rate_hint.text = f"Win rate {self.win_rate:.0f}% used in Expected Value"
        except Exception as e:
            log.debug("win_rate_hint: %s", e)
        # tp1/tp2 labels are static (fixed unified model)

    def on_win_rate(self, val):
        self.win_rate = round(round(val / 5) * 5)
        self._update_settings_labels()
        self._save_settings()
        self._mark_stale()
        self._live_risk_update()   # EV changes with win rate — update instantly

    def reset_settings(self):
        self.sl_mult  = DEFAULT_SL
        self.win_rate = DEFAULT_WR
        try:
            self.ids.win_rate_slider.value = DEFAULT_WR
        except Exception as e:
            log.debug("reset_settings widget: %s", e)
        self._update_settings_labels()
        self._save_settings()
        self._mark_stale()
        self._show_snackbar("Settings reset to defaults")

    # FIX #5 — stale results banner
    def _mark_stale(self):
        if not self._app_ready:
            return  # suppress stale during startup initialization
        if not self._results_are_stale:
            self._results_are_stale = True
            try:
                w = self.ids.stale_warning
                w.text   = "⚠ Settings changed — tap Calculate to update"
                w.height = dp(22)
                w.opacity = 1
            except Exception as e:
                log.debug("_mark_stale widget: %s", e)

    def _clear_stale(self):
        self._results_are_stale = False
        try:
            w = self.ids.stale_warning
            w.text    = ""
            w.height  = dp(0)
            w.opacity = 0
        except Exception as e:
            log.debug("_clear_stale widget: %s", e)

    # ── last-used field values ─────────────────────────────────────────────
    def _load_last_values(self):
        try:
            sym = 'XAUUSD'  # default for first run
            if self._store.exists('last'):
                v = self._store.get('last') or {}
                self.ids.atr.text  = v.get('atr', '')
                # Only restore lots if it was not the signal-mode auto-set value
                saved_lots = v.get('lots', '')
                if saved_lots != '1.0' or not v.get('lots_auto_set', False):
                    self.ids.lots.text = saved_lots
                self.ids.account.text   = v.get('account',   '')
                self.ids.risk.text      = v.get('risk',      '')
                self.ids.entry.text     = v.get('entry',     '')  # restored — traders reuse levels
                sym = v.get('instrument', '') or 'XAUUSD'
            self.ids.instrument.text = sym
            self._calc_symbol = sym
        except Exception as e:
            log.debug("_load_last_values: %s", e)

    def _save_last_values(self):
        """Capture UI values immediately, defer disk write to next frame."""
        try:
            snapshot = dict(
                atr=self.ids.atr.text,
                lots=self.ids.lots.text,
                entry=self.ids.entry.text,          # persist entry — traders reuse same price
                account=self.ids.account.text,
                risk=self.ids.risk.text,
                instrument=self.ids.instrument.text,
            )
            self._store.put('last', **snapshot)
        except Exception as e:
            log.debug("_save_last_values: %s", e)







