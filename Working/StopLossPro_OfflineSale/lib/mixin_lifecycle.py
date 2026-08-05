# mixin_lifecycle.py — Startup, on_kv_post, touch routing, Android IME, keep-screen-on.
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
from updater import check_and_update, apply_update_and_restart, APP_VERSION
from calc import (
    TradeSetup, calc_setup, calc_auto_lot, recommend_order_type,
    _update_dynamic_aliases, _cluster_lots,
)
from mt5_api import (
    mt5_check_status, mt5_place_order, mt5_fetch_candle,
    mt5_get_account, mt5_get_positions, mt5_close_order,
    mt5_close_all_bulk, mt5_modify_sl_tp, mt5_resolve_symbols,
)


class LifecycleMixin:
    def on_touch_up(self, touch):
        result = super().on_touch_up(touch)
        if log.isEnabledFor(logging.DEBUG):
            try:
                scroll = getattr(touch, 'is_mouse_scrolling', False)
                dx = abs(touch.x - touch.ox)
                dy = abs(touch.y - touch.oy)
                log.debug("[RAW_TOUCH] pos=(%.0f,%.0f) dx=%.0f dy=%.0f scroll=%s",
                          touch.x, touch.y, dx, dy, scroll)
                if not scroll and dx <= 10 and dy <= 10:
                    matched = False
                    for wid, name in self._WIDGET_NAMES.items():
                        w = self.ids.get(wid)
                        if w and w.collide_point(*touch.pos):
                            log.debug("[TOUCH] %s", name)
                            matched = True
                            break
                    if not matched:
                        log.debug("[TOUCH] no-match at (%.0f,%.0f)", touch.x, touch.y)
            except Exception as e:
                log.debug("[TOUCH_ERR] %s", e)
        return result

    def on_kv_post(self, base_widget):
        try:
            self._keep_screen_on()
        except Exception as e:
            log.debug("on_kv_post _keep_screen_on: %s", e)
        try:
            self._load_settings_values()
        except Exception as e:
            log.warning("on_kv_post _load_settings_values: %s", e)
        # Suppress system keyboard — release on startup + every field focus
        try:
            Window.softinput_mode = ""
            Window.release_all_keyboards()
            Window.bind(on_keyboard=self._on_back_key)
        except Exception as e:
            log.debug("on_kv_post Window setup: %s", e)
        Clock.schedule_once(lambda dt: self._load_last_values(), 0)
        # Single deferred task — collapses 4 startup steps into 1 frame
        # 0.15s gives _load_last_values time to complete on slow devices
        Clock.schedule_once(lambda dt: self._startup_complete(), 0.15)
        # Layer 4 (MIUI/Gboard): set window-level ALWAYS_HIDDEN at startup
        if platform == 'android':
            Clock.schedule_once(lambda dt: self._hide_android_ime(), 0.2)
            # Pre-warm Android Vibrator at 2s so first numpad tap has no jnius lag
            Clock.schedule_once(lambda dt: self._init_vibrator(), 2.0)
            Clock.schedule_once(lambda dt: self._init_ime(), 2.0)
        # MT5 — load persisted settings; start ping if already enabled
        self._load_mt5_settings()
        if self._mt5_enabled:
            Clock.schedule_once(lambda dt: self._start_mt5_ping(), 1.2)
        # Auto-update check — runs 3s after startup, non-blocking, silent on error
        Clock.schedule_once(lambda dt: self._check_for_update(), 3.0)

    def _check_for_update(self):
        """
        Enterprise update flow — mirrors VS Code / Telegram:
        1. Silent background check
        2. If update found: snackbar "Downloading v1.x.x..."
        3. Progress tracked in background (throttled to every 2%)
        4. When ready: MDDialog with "RESTART & UPDATE" button
        5. On confirm: apply_update script runs, app restarts cleanly
        """
        from updater import check_and_update, apply_update_and_restart, APP_VERSION

        # Per-instance update state — safe because only one flow runs at a time
        self._upd_dialog     = None
        self._upd_status_lbl = None
        self._upd_version    = ""

        # ── Callback 1: update found, download starting ─────────────────
        def _on_available(ver, notes):
            self._upd_version = ver
            try:
                self._show_snackbar(f"Downloading update v{ver}…  0%")
            except Exception:
                pass
            log.info("[updater] Downloading v%s (current: v%s)", ver, APP_VERSION)

        # ── Callback 2: real-time download progress ──────────────────────
        def _on_progress(frac, pct):
            try:
                bar_filled = int(frac * 20)
                bar = "█" * bar_filled + "░" * (20 - bar_filled)
                msg = f"Downloading v{self._upd_version}  [{bar}]  {pct}%"
                self._show_snackbar(msg)
            except Exception:
                pass

        # ── Callback 3: download complete, prompt restart ────────────────
        def _on_ready():
            try:
                # Dismiss any leftover snackbar
                if self._upd_dialog:
                    try:
                        self._upd_dialog.dismiss()
                    except Exception:
                        pass

                ver = self._upd_version

                # Status label inside dialog (updates if anything is still running)
                self._upd_status_lbl = MDLabel(
                    text=(
                        f"StopLoss Pro v{ver} has been downloaded and is ready to install.\n\n"
                        "The app will close, apply the update automatically, "
                        "and restart — just like a professional software update."
                    ),
                    theme_text_color="Secondary",
                    halign="left",
                    size_hint_y=None,
                    height=dp(80),
                )

                def _do_restart(btn):
                    self._upd_dialog.dismiss()
                    # Change label so user knows something is happening
                    try:
                        self._upd_status_lbl.text = "Applying update…"
                    except Exception:
                        pass
                    # Short delay so dialog can close visually before exit
                    Clock.schedule_once(lambda dt: apply_update_and_restart(), 0.3)

                self._upd_dialog = MDDialog(
                    title=f"✓  Update Ready — v{ver}",
                    type="custom",
                    content_cls=self._upd_status_lbl,
                    buttons=[
                        MDFlatButton(
                            text="LATER",
                            on_release=lambda x: self._upd_dialog.dismiss(),
                        ),
                        MDRaisedButton(
                            text="RESTART & UPDATE",
                            on_release=_do_restart,
                        ),
                    ],
                )
                self._upd_dialog.open()
                log.info("[updater] Ready dialog shown — v%s", ver)

            except Exception as e:
                log.debug("[updater] Ready dialog error (non-fatal): %s", e)

        # ── Callback 4: any error — silent, never crash the app ──────────
        def _on_error(msg):
            log.debug("[updater] Background error (non-fatal): %s", msg)

        check_and_update(
            on_available = _on_available,
            on_progress  = _on_progress,
            on_ready     = _on_ready,
            on_error     = _on_error,
        )

    def _startup_complete(self):
        """Single deferred startup task — runs all post-load initialization at once.
        Called 0.15s after on_kv_post, after _load_last_values has had time to run.
        Replaces 4 separate Clock.schedule_once calls (was 500ms delay, now 150ms).
        """
        self._app_ready = True   # set FIRST — no timing window for dropped taps
        # Belt-and-suspenders: ensure instrument always shows a value
        try:
            if not self.ids.instrument.text.strip():
                sym = self._calc_symbol or 'XAUUSD'
                self.ids.instrument.text = sym
                self._calc_symbol = sym
        except Exception as e:
            log.debug("startup instrument fallback: %s", e)
        self._apply_mode_visuals()
        self._apply_startup_prefs()

    def _clear_auto_lots_on_startup(self):
        """Kept for compatibility — _startup_complete now handles this."""
        pass

    def _apply_startup_prefs(self):
        """Apply theme and numpad visibility after all widgets are ready."""
        app = MDApp.get_running_app()
        # Apply theme_cls first
        if self._theme_style != "Dark":
            app.theme_cls.theme_style = self._theme_style
        # Apply all hardcoded colors (numpad, buy/sell cards, etc.)
        self._apply_full_theme()
        # Set topbar icon to match current theme
        try:
            icon = "weather-night" if self._theme_style == "Light" else "weather-sunny"
            self.ids.topbar.right_action_items = [
                [icon, lambda x: self.toggle_theme(), "Toggle Theme"]
            ]
        except Exception as e:
            log.debug("startup theme icon: %s", e)
        # Numpad visibility
        if not self._numpad_visible:
            try:
                panel = self.ids.numpad_panel
                panel.height = dp(38)
                # Collapse row heights to 0 — prevents Kivy overflow rendering
                for rid in ('np_row1', 'np_row2', 'np_row3', 'np_row4'):
                    try:
                        row = self.ids[rid]
                        row.height  = dp(0)
                        row.opacity = 0
                    except Exception as e:
                        log.debug("_apply_startup_prefs: %s", e)
                self.ids.numpad_toggle_btn.icon = "chevron-up"
            except Exception as e:
                log.debug("startup numpad state: %s", e)

    _numpad_prev_value = ""   # stores value before EDITING started (for cancel)

    def _on_back_key(self, window, key, *args):
        """Android back button — layered dismiss logic (standard Android UX).

        Priority 1: If numpad is active → commit cancel, close session.
        Priority 2: If close trade panel is open → hide panel only (not reset).
        Priority 3: Double-back within 2s → exit. Single back → snackbar warning.
        """
        if key == 27:  # ESC / Android back
            # Priority 1: cancel active numpad input
            if self._input_state == "EDITING":
                self._end_input_session(commit=False)
                return True

            # Cancel any in-progress numpad animations
            try:
                Animation.cancel_all(self.ids.numpad_panel)
                self._numpad_animating = False
            except Exception as e:
                log.debug("_on_back_key anim cancel: %s", e)

            # Priority 2: double-back exit confirmation (standard Android UX)
            now = time.time()
            if (now - self._last_back_ts) < 2.0:
                return False   # second back press — Android exits the app
            self._last_back_ts = now
            self._show_snackbar("Press back again to exit")
            return True

        return False

    # ── Android keyboard / screen helpers ────────────────────────────────
    @staticmethod
    def _hide_android_ime():
        """Force-hide Android IME."""
        try:
            from jnius import autoclass  # type: ignore[import]
            activity = autoclass('org.kivy.android.PythonActivity').mActivity
            Context  = autoclass('android.content.Context')
            imm      = activity.getSystemService(Context.INPUT_METHOD_SERVICE)
            window   = activity.getWindow()
            token = window.getDecorView().getWindowToken()
            imm.hideSoftInputFromWindow(token, 0)
            window.setSoftInputMode(51)
        except Exception as e:
            log.debug("_hide_android_ime: %s", e)

    def _suppress_ime(self, field):
        """Called on touch_down for numpad fields — defocus immediately so the
        Android keyboard never gets a chance to appear before open_numpad runs."""
        try:
            field.focus = False
        except Exception:
            pass
        Window.release_all_keyboards()
        if platform == 'android':
            self._hide_android_ime()

    @staticmethod
    def _restore_ime_mode():
        """Reset window soft-input mode for fields that need the real keyboard."""
        try:
            from jnius import autoclass  # type: ignore[import]
            activity = autoclass('org.kivy.android.PythonActivity').mActivity
            activity.getWindow().setSoftInputMode(49)
        except Exception as e:
            log.debug("_restore_ime_mode: %s", e)

    @staticmethod
    def _keep_screen_on():
        if platform == 'android':
            try:
                from jnius import autoclass  # type: ignore[import]
                act = autoclass('org.kivy.android.PythonActivity').mActivity
                act.getWindow().addFlags(128)
            except Exception as e:
                log.debug("?: %s", e)

    # ── storage ────────────────────────────────────────────────────────────
    @property
    def _store(self) -> JsonStore:
        if not hasattr(self, '_store_instance'):
            app  = MDApp.get_running_app()
            path = os.path.join(app.user_data_dir, STORE_FILE)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError:
                pass
            self._store_instance = JsonStore(path)
        return self._store_instance

    # ── settings — values only (no widget touch) ───────────────────────────
