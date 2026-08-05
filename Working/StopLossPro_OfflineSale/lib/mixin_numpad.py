# mixin_numpad.py — Numpad state machine, keyboard/IME, field commit, DONE, live risk update.
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


class NumpadMixin:
    @staticmethod
    def _dismiss_keyboard():
        Window.release_all_keyboards()

    # ── custom numpad — full state machine ────────────────────────────────
    # States: IDLE → EDITING → COMMITTING → IDLE
    # _input_state  : current state string
    # _numpad_owner : id() of the field that opened numpad (prevents wrong-field commits)
    # _numpad_target: the actual MDTextField widget
    # _numpad_value : input buffer — the sole source of truth for the active field
    # _done_pending : blocks DONE spam (rapid OK taps)


    def _end_input_session(self, commit: bool = True, keep_done_pending: bool = False):
        """Centralized input session teardown — called from ALL exit paths.
        commit=True  → write buffer to field (DONE, tab-switch, mode-switch)
        commit=False → discard buffer (Back button cancel)
        Always: resets state to IDLE, clears display bar, releases focus.
        """
        if self._input_state == "IDLE":
            return   # already clean — fast path

        # Commit or discard buffer
        if (self._numpad_target is not None
                and id(self._numpad_target) == self._numpad_owner):
            # Capture before we clear _numpad_target — used by _finish_done
            if commit:
                try:    self._last_committed_hint = self._numpad_target.hint_text
                except: self._last_committed_hint = None
            val = self._numpad_value if commit else self._numpad_prev_value
            if commit and val:
                val = val.rstrip('.')   # "1." → "1", "." → ""
                if val == '':
                    val = ''            # pure dot → empty (treated as missing)
            self._numpad_target.text = val
            # Ensure Android keyboard cannot re-assert on this field
            try:
                _f = self._numpad_target
                def _safe_defocus(dt, w=_f):
                    try:
                        if w is not None and getattr(w, 'focus', False):
                            w.focus = False
                    except Exception as e:
                        log.debug("defocus error: %s", e)
                Clock.schedule_once(_safe_defocus, 0)
            except Exception as e:
                log.debug("_end_input_session widget: %s", e)

        # Reset all state atomically
        self._input_state      = "IDLE"
        self._numpad_target    = None
        self._numpad_owner     = None
        self._numpad_value     = ""
        self._numpad_prev_value= ""
        if not keep_done_pending:
            self._done_pending = False

        # Reset display bar
        try:
            self.ids.numpad_field_label.text = "Tap a field to enter value"
            self.ids.numpad_value_label.text  = ""
        except Exception as e:
            log.debug("_end_input_session widget: %s", e)

        # Release keyboard (belt-and-suspenders)
        try:
            Window.release_all_keyboards()
        except Exception:
            pass

    def _schedule_numpad(self, field):
        """Deduplicated numpad open — guards against Kivy double on_touch_up dispatch."""
        from time import monotonic
        now = monotonic()
        if self._last_numpad_times is None:
            self._last_numpad_times = {}
        fid = id(field)
        if now - self._last_numpad_times.get(fid, 0) < 0.35:
            return
        self._last_numpad_times[fid] = now
        def _safe_open(dt, r=self, f=field):
            try:
                if r and f: r.open_numpad(f)
            except Exception as e:
                log.debug("open_numpad error: %s", e)
        Clock.schedule_once(_safe_open, 0)

    def open_numpad(self, field):
        """IDLE → EDITING: activate field for numpad input."""
        log.debug("[EVENT] NUMPAD field=%r", getattr(field, 'hint_text', ''))
        try:
            if getattr(field, 'focus', False):
                field.focus = False
        except Exception:
            pass
        try:
            Window.release_all_keyboards()
        except Exception:
            pass
        if platform == 'android':
            self._hide_android_ime()

        if self._ui_locked:
            return

        # Auto-expand numpad if minimized
        if not self._numpad_visible:
            def _safe_expand(dt, r=self):
                try:
                    if r: r._expand_numpad()
                except Exception as e:
                    log.debug("_expand_numpad error: %s", e)
            Clock.schedule_once(_safe_expand, 0)

        # Commit previous field on field switch (sync_numpad no longer does this)
        if (self._input_state == "EDITING"
                and self._numpad_target is not None
                and self._numpad_target is not field):
            try:
                self._numpad_target.text = self._numpad_value.rstrip(".")
            except Exception as e:
                log.debug("switch target text: %s", e)

        # Set up new input session immediately (don't wait for deferred call)
        self._input_state       = "EDITING"
        self._numpad_target     = field
        self._numpad_owner      = id(field)
        self._numpad_prev_value = field.text or ""
        self._numpad_value      = field.text or ""
        self._done_pending      = False
        self._numpad_label_ref  = None   # reset cache for new session

        try:
            self.ids.numpad_field_label.text = field.hint_text or ""
            self.ids.numpad_value_label.text = self._numpad_value
        except Exception as e:
            log.debug("open_numpad label: %s", e)

    # _suppress_keyboard removed — superseded by synchronous focus=False in KV on_focus

    def _expand_numpad(self):
        """Expand numpad if minimized. Guard: only expand if actually minimized
        AND not currently mid-animation (prevents double-toggle on rapid taps).
        """
        if not self._numpad_visible and not self._numpad_animating:
            self.toggle_numpad()
    # Module-level vibrator cache — resolved ONCE at first tap, reused forever
    # jnius autoclass() is ~200ms per class — must NOT be called on every keypress
    _vibrator_cache = None    # None = unresolved, False = no vibrator, else = Vibrator obj
    _vibrator_ready = False   # True once resolved
    _imm_cache      = None
    _imm_ready      = False
    _window_cache   = None

    @classmethod
    def _init_vibrator(cls):
        """Resolve and cache the Android Vibrator service — called once on first tap."""
        if platform != 'android':
            cls._vibrator_ready = True
            cls._vibrator_cache = False
            return
        try:
            from jnius import autoclass  # type: ignore[import]
            activity = autoclass('org.kivy.android.PythonActivity').mActivity
            Context  = autoclass('android.content.Context')
            vib      = activity.getSystemService(Context.VIBRATOR_SERVICE)
            cls._vibrator_cache = vib if (vib and vib.hasVibrator()) else False
        except Exception as e:
            log.debug("_init_vibrator: %s", e)
            cls._vibrator_cache = False
        cls._vibrator_ready = True

    @classmethod
    def _init_ime(cls):
        """Pre-warm InputMethodManager + Window refs at startup (t=2s).
        Eliminates 100-200ms jnius cold-start in _hide_android_ime().
        Without this, first numpad tap on MIUI has a flash window where
        the IME daemon fires before we kill it.
        """
        if platform != 'android':
            cls._imm_ready = True
            return
        try:
            from jnius import autoclass  # type: ignore[import]
            activity          = autoclass('org.kivy.android.PythonActivity').mActivity
            Context           = autoclass('android.content.Context')
            imm = activity.getSystemService(Context.INPUT_METHOD_SERVICE)
            win = activity.getWindow()
            if imm is not None:
                cls._imm_cache = imm
            if win is not None:
                cls._window_cache = win
        except Exception as e:
            log.debug("_init_ime: %s", e)
        cls._imm_ready = True

    @classmethod
    def _haptic_tap(cls):
        """Short haptic feedback on numpad keypress (10ms).
        Vibrator is resolved ONCE at first tap and cached for all subsequent calls.
        Zero overhead after first tap — no jnius import, no service lookup.
        """
        if not cls._vibrator_ready:
            cls._init_vibrator()   # one-time setup — first tap only
        vib = cls._vibrator_cache
        if vib:
            try:
                vib.vibrate(10)
            except Exception as e:
                log.debug("_haptic_tap vibrate: %s", e)

    # Cache numpad display label — looked up once, reused on every keystroke
    _numpad_label_ref = None   # cached ref to ids.numpad_value_label

    def _sync_numpad(self):
        """Write buffer → field.text AND display label on every key press.
        Safe because field.focus is set False synchronously in KV on_focus,
        so the MDTextField is NEVER focused during input.
        Writing to an unfocused MDTextField = plain label update only,
        NO ripple / focus animation fires → performance is fine.
        Perf: label ref cached — zero dict lookup on subsequent calls.
        """
        v = self._numpad_value
        if v.count(".") > 1:
            parts = v.split(".")
            v = parts[0] + "." + "".join(parts[1:])
            self._numpad_value = v
        try:
            # Update the MDTextField so the user sees the value while typing
            if self._numpad_target is not None:
                self._numpad_target.text = v
            # Update numpad bar display label (cached ref for speed)
            lbl = self._numpad_label_ref
            if lbl is None:
                lbl = self.ids.numpad_value_label
                self._numpad_label_ref = lbl
            lbl.text = v
        except (AttributeError, ReferenceError) as e:
            log.debug("_sync_numpad: %s", e)
            self._numpad_label_ref = None

    def numpad_key(self, key: str):
        """Handle numpad key with full state machine, throttle, and overflow guards.
        Perf: frequently accessed self attrs pulled into locals to reduce attr lookups.
        """
        # ── Global lock: block keys during calculate (except DONE) ────────────
        if self._ui_locked and key != "DONE":
            return

        # ── Throttle: ignore events fired < 30 ms apart (button bounce / spam) ──
        now = time.time()
        last_ts = self._last_key_ts
        if key not in ("DONE", "CLEAR") and (now - last_ts) < 0.03:
            return
        self._last_key_ts = now

        # Cache frequently-used attrs as locals — avoid repeated attr lookup cost
        state   = self._input_state
        owner   = self._numpad_owner
        target  = self._numpad_target
        max_len = self._MAX_INPUT_LEN

        # ── DONE / OK ─────────────────────────────────────────────────────────
        if key == "DONE":
            if self._done_pending:
                return                        # DONE spam guard
            if state == "IDLE":
                self.on_calculate()           # recalculate even without active field
                return
            if state != "EDITING":
                return                        # COMMITTING — ignore duplicate DONE
            self._done_pending = True
            state  = "COMMITTING"
            self._end_input_session(commit=True, keep_done_pending=True)
            # Defer calculate one frame so field.text fully propagates
            def _safe_finish(dt, r=self):
                try:
                    if r: r._finish_done()
                except Exception as e:
                    log.error("_finish_done error: %s", e)
            Clock.schedule_once(_safe_finish, 0)
            return

        # ── Block input in COMMITTING state ──────────────────────────────────
        if state == "COMMITTING":
            return

        # ── No active field → ignore ──────────────────────────────────────────
        if target is None or id(target) != owner:
            return

        v = self._numpad_value

        if key == "CLEAR":
            v = ""
        elif key == "DEL":
            v = v[:-1]
        elif key == ".":
            if "." not in v:
                v = "0." if v == "" else v + "."
        elif key == "00":
            if len(v) >= max_len - 1:
                return                        # max length guard
            v = (v + "00") if (v and v != "0") else ("0" if not v else v)
        elif key.isdigit():
            if len(v) >= max_len:
                return                        # max length guard
            v = key if v == "0" else v + key

        self._numpad_value = v
        self._sync_numpad()
        # Live update — on every keystroke recalculate lots (if needed) then
        # push a full risk-estimator preview.  _sync_numpad() already wrote
        # the live buffer to target.text so all reads see the current value.
        _hint = getattr(target, 'hint_text', '')
        if _hint in ('Risk %', 'Account', 'ATR'):
            try:
                self._maybe_recalc_lots()   # lots = f(risk%, account, ATR, sl_mult)
            except Exception as _e:
                log.debug("numpad_key _maybe_recalc_lots: %s", _e)
        if _hint in ('Risk %', 'Account', 'ATR', 'Lots', 'Entry Price (optional)'):
            try:
                self._live_risk_update()    # loss/profit/distances = f(lots, ATR, entry)
            except Exception as _e:
                log.debug("numpad_key _live_risk_update: %s", _e)
        # Haptic: tactile feedback on key press (not DONE/CLEAR — those have visual feedback)
        if key not in ("DONE",):
            self._haptic_tap()

    def _finish_done(self):
        """Called one frame after DONE to run calculate with committed values."""
        self._done_pending = False   # cleared FIRST — numpad never permanently locked
        try:
            hint = getattr(self, '_last_committed_hint', None)
            if hint in ('Risk %', 'Account'):
                self._maybe_recalc_lots()
            self.on_calculate()
        except Exception as _e:
            log.error("_finish_done unexpected: %s", _e)
            self._ui_locked = False   # ensure calculator isn't frozen

    def _maybe_recalc_lots(self):
        """Silently recalculate lot size from current ATR / account / risk %.
        Called automatically after DONE on the Risk % or Account field.
        No-op if any input is missing or invalid.
        """
        try:
            atr_t  = self.ids.atr.text.strip()
            acc_t  = self.ids.account.text.strip()
            risk_t = self.ids.risk.text.strip()
            if not (atr_t and acc_t and risk_t):
                return
            if atr_t == "." or acc_t == "." or risk_t == ".":
                return
            atr = float(atr_t)
            acc = float(acc_t)
            rsk = float(risk_t)
            if not (math.isfinite(atr) and math.isfinite(acc) and math.isfinite(rsk)):
                return
            if atr <= 0 or acc <= 0 or not (0 < rsk <= 100):
                return
            sym  = self.ids.instrument.text.strip()
            lots = calc_auto_lot(atr, acc, rsk, sym, self.sl_mult)
            if math.isfinite(lots) and lots > 0:
                self.ids.lots.text = f"{lots:.2f}"
                log.debug("auto-lot updated: %.2f (risk=%.1f%% acc=%.0f atr=%s)", lots, rsk, acc, atr_t)
        except Exception as e:
            log.debug("_maybe_recalc_lots: %s", e)

    _last_live_upd = 0.0   # monotonic timestamp of last _live_risk_update call

    def _live_risk_update(self):
        """Push a full risk-estimator preview on every relevant numpad keystroke.

        Reads all current field values (including the live-buffered value already
        written by _sync_numpad / _maybe_recalc_lots) and runs calc_setup()
        without locking the UI or committing the active input session.
        Silently no-ops when any required input is missing or invalid.
        Also called by the win-rate slider so EV updates immediately.
        """
        import time as _t2
        _now2 = _t2.monotonic()
        if _now2 - self._last_live_upd < 0.015:   # cap at ~60 fps
            return
        self._last_live_upd = _now2
        try:
            ids    = self.ids
            atr_t  = ids.atr.text.strip()
            lots_t = ids.lots.text.strip()
            sym    = ids.instrument.text.strip()
            if not (atr_t and lots_t and sym):
                return
            if atr_t == "." or lots_t == ".":
                return
            atr  = float(atr_t)
            lots = float(lots_t)
            if not (math.isfinite(atr) and math.isfinite(lots)):
                return
            if atr <= 0 or lots <= 0:
                return
            entry_t = ids.entry.text.strip()
            entry   = None
            if entry_t and entry_t != ".":
                try:
                    entry = float(entry_t)
                    if not math.isfinite(entry):
                        entry = None
                except Exception:
                    entry = None

            setup = calc_setup(atr=atr, lots=lots, symbol=sym,
                               sl_m=self.sl_mult, entry=entry, wr_pct=self.win_rate)
            d = DECIMALS.get(_base_sym(sym), 2)

            def _fmt_val(v):
                if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
                    return "—"
                return f"{v:.{d}f}"

            ids.sl_distance.text    = _fmt_val(setup.sl_dist)
            ids.tp1_distance.text   = _fmt_val(setup.tp1_dist)
            ids.tp2_distance.text   = _fmt_val(setup.tp2_dist)
            ids.tp3_distance.text   = _fmt_val(setup.tp3_dist)
            ids.loss.text           = f"${abs(setup.loss):,.2f}" if (isinstance(setup.loss, (int, float)) and math.isfinite(setup.loss)) else "—"
            ids.tp1.text            = f"${setup.profit1:,.2f}" if (isinstance(setup.profit1, (int, float)) and math.isfinite(setup.profit1)) else "—"
            ids.tp2.text            = f"${setup.profit2:,.2f}" if (isinstance(setup.profit2, (int, float)) and math.isfinite(setup.profit2)) else "—"
            ids.tp3.text            = f"${setup.profit3:,.2f}" if (isinstance(setup.profit3, (int, float)) and math.isfinite(setup.profit3)) else "—"
            ids.blended_profit.text = f"${setup.blended:,.2f}" if (isinstance(setup.blended, (int, float)) and math.isfinite(setup.blended)) else "—"
            ids.expected_value.text = f"${setup.ev:+,.2f}" if (isinstance(setup.ev, (int, float)) and math.isfinite(setup.ev)) else "—"

            if entry and setup.buy and isinstance(setup.buy, dict) and setup.sell and isinstance(setup.sell, dict):
                rr_str = "1:2 / 1:3 / 1:4"
                be_str = _fmt_val(entry)
                ids.buy_entry.text  = _fmt_val(setup.buy.get('entry'))
                ids.buy_sl.text     = _fmt_val(setup.buy.get('sl'))
                ids.buy_tp1.text    = _fmt_val(setup.buy.get('tp1'))
                ids.buy_tp2.text    = _fmt_val(setup.buy.get('tp2'))
                ids.buy_tp3.text    = _fmt_val(setup.buy.get('tp3'))
                ids.buy_rr.text     = rr_str
                ids.buy_be.text     = be_str
                ids.sell_entry.text = _fmt_val(setup.sell.get('entry'))
                ids.sell_sl.text    = _fmt_val(setup.sell.get('sl'))
                ids.sell_tp1.text   = _fmt_val(setup.sell.get('tp1'))
                ids.sell_tp2.text   = _fmt_val(setup.sell.get('tp2'))
                ids.sell_tp3.text   = _fmt_val(setup.sell.get('tp3'))
                ids.sell_rr.text    = rr_str
                ids.sell_be.text    = be_str
        except Exception as e:
            log.debug("_live_risk_update: %s", e)

    # ── levels animation ───────────────────────────────────────────────────
    def _set_levels_visible(self, show: bool):
        try:
            box = self.ids.levels_box
        except Exception as e:
            log.debug("_set_levels_visible ids: %s", e)
            return
        Animation.cancel_all(box)
        if show:
            if getattr(self, "_levels_visible", False):
                return
            self._levels_visible = True
            box.height = dp(333)
            Animation(opacity=1, duration=0.2).start(box)
        else:
            self._levels_visible = False
            def _collapse(*a): box.height = 0
            a = Animation(opacity=0, duration=0.15)
            a.bind(on_complete=_collapse)
            a.start(box)
        Clock.schedule_once(lambda dt: self._apply_mt5_visuals(), 0)

    # ── display reset helpers ──────────────────────────────────────────────
    def _reset_risk_display(self):
        for fid in ("sl_distance", "tp1_distance", "tp2_distance", "tp3_distance",
                    "loss", "tp1", "tp2", "tp3", "blended_profit", "expected_value"):
            self.ids[fid].text = "—"

    def _reset_level_display(self):
        for fid in ("buy_entry", "buy_sl", "buy_tp1", "buy_tp2", "buy_tp3",
                    "buy_be", "buy_rr",
                    "sell_entry", "sell_sl", "sell_tp1", "sell_tp2", "sell_tp3",
                    "sell_be", "sell_rr"):
            self.ids[fid].text = ""

    # ── calculate ──────────────────────────────────────────────────────────
    # ── Theme toggle ───────────────────────────────────────────────────────

