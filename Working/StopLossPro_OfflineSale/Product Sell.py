# Product Sell.py — main entry point.
# ─────────────────────────────────────────────────────────────────────────────
# ── Production log lockdown ────────────────────────────────────────────────
# MUST happen before the very first `kivy` import (even `from kivy.config
# import Config` triggers kivy/__init__.py, which wires up the file logger
# using whatever this machine's persisted %USERPROFILE%\.kivy\config.ini
# says — Config.set() calls made afterward are too late to stop it). Kivy
# reads KIVY_NO_FILELOG straight from the environment before any of that
# happens, so this is the only reliable switch.
#
# Without this, %USERPROFILE%\.kivy\logs\ ships a plain-text trail of every
# internal event, licensing check, session token, and order-execution step —
# effectively documenting how the app works to anyone who opens the folder.
# Support/dev sessions can re-enable full diagnostics with STOPLOSSPRO_DEBUG=1.
import os as _os_early
_debug_build = _os_early.environ.get('STOPLOSSPRO_DEBUG') == '1'
if not _debug_build:
    _os_early.environ['KIVY_NO_FILELOG']    = '1'
    _os_early.environ['KIVY_NO_CONSOLELOG'] = '1'

# Config.set() MUST happen before any other kivy import (Kivy rule).
from kivy.config import Config
from kivy.utils import platform as _plat
if _plat not in ('android', 'ios'):
    Config.set('graphics', 'width', '360')
    Config.set('graphics', 'height', '740')
    Config.set('graphics', 'resizable', False)

if _debug_build:
    Config.set('kivy', 'log_level', 'debug')
else:
    Config.set('kivy', 'log_level', 'error')

# ── lib/ path injection (all support files live in lib/ next to this file) ───
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "lib"))

# ── Standard imports ──────────────────────────────────────────────────────────
import os, logging, threading, base64, hashlib, weakref

from kivy.lang import Builder
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.clock import Clock
from kivy.storage.jsonstore import JsonStore
from kivy.factory import Factory
from kivy.properties import DictProperty, StringProperty, NumericProperty, ListProperty
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.menu import MDDropdownMenu

# ── Support modules (same folder, VirtualBox-style) ───────────────────────────
from constants import (
    log, platform,
    CONTRACTS, DECIMALS, DEFAULT_SL, DEFAULT_WR,
    MAX_LOT, MAX_HIST, STORE_FILE,
    _get_dp, _base_sym,
    _MT5_OK, MT5_ORDER_TYPE_LABELS,
)
from mt5_api    import (    # explicit — no wildcard pollution
    mt5_check_status, mt5_place_order, mt5_fetch_candle,
    mt5_get_account, mt5_get_positions, mt5_close_order,
    mt5_close_all_bulk, mt5_modify_sl_tp, mt5_resolve_symbols,
)
from calc       import TradeSetup, calc_setup, calc_auto_lot, recommend_order_type
from activation import (
    _show_activation_blocker, _register_if_new, _is_activated,
    _start_session_heartbeat,   # authenticated licence heartbeat (PHASE 12)
    _resume_session,            # try to silently restore a saved session (PHASE 17)
    get_provider,                # so we can wire on_state_change before first use (PHASE 17)
)

# ── Widget classes (must be imported so KV can reference them) ────────────────
from widgets import SwitchToggle, PositionCardRV   # noqa: F401

# ── Root mixins ───────────────────────────────────────────────────────────────
from mixin_lifecycle   import LifecycleMixin
from mixin_settings    import SettingsMixin
from mixin_history     import HistoryMixin
from mixin_numpad      import NumpadMixin
from mixin_calculator  import CalculatorMixin
from mixin_mt5         import MT5Mixin
from mixin_trading     import TradingMixin
from mixin_orders      import OrdersMixin

# KV layout file (inside lib/)
_KV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "layout.kv")


# ═══════════════════════════════════════════════════════════════════════════════
# Root  — composed from 8 thin mixin modules + own Kivy properties
# ═══════════════════════════════════════════════════════════════════════════════
class Root(
    LifecycleMixin,
    SettingsMixin,
    HistoryMixin,
    NumpadMixin,
    CalculatorMixin,
    MT5Mixin,
    TradingMixin,
    OrdersMixin,
    MDBoxLayout,
):

    buy          = DictProperty({})
    sell         = DictProperty({})
    _calc_symbol = StringProperty("")

    # Settings as plain Python attrs (not Kivy properties) so slider
    # on_value events don't fire during programmatic slider.value assignment
    # while the widgets might not yet have their IDs registered.
    sl_mult  = DEFAULT_SL
    win_rate = DEFAULT_WR

    # ── Core flags ────────────────────────────────────────────────────
    _calc_pending       = False
    _autolot_pending    = False
    _last_calc_time     = 0.0
    _last_autolot_time  = 0.0
    _last_numpad_times  = None   # dict[field_id -> float], set in _init_flags
    _settings_applied   = False
    _results_are_stale  = False
    _app_ready          = False  # True after all startup tasks complete
    # ── Input state machine ────────────────────────────────────────────
    # IDLE -> EDITING -> COMMITTING -> IDLE
    _input_state    = "IDLE"
    _numpad_target  = None            # active MDTextField
    _numpad_value   = ""              # buffer — sole source of truth
    _numpad_owner   = None            # id() prevents wrong-field commit
    _done_pending       = False        # blocks DONE spam
    _last_committed_hint = None         # hint_text of field committed by DONE
    _last_key_ts        = 0.0          # 30ms throttle timestamp
    _ui_locked          = False        # global interaction lock
    _MAX_INPUT_LEN      = 18           # cap buffer: prevents float overflow
    _numpad_prev_value  = ""           # saved on field open; restored on back-cancel
    # ── UI preferences ────────────────────────────────────────────────
    _theme_style     = "Dark"         # "Dark" | "Light"
    _numpad_visible    = True           # numpad expanded or minimized
    _numpad_animating  = False          # True during expand/collapse animation
    # ── Performance caches ─────────────────────────────────────────────
    _history_cache_key  = None
    _history_built      = False
    _history_batch_id   = 0           # incremented on clear: cancels batches
    # ── Monthly stats ──────────────────────────────────────────────────
    _stats_wins     = 0
    _stats_losses   = 0
    _stats_key      = ""              # "YYYY_MM" — month rollover detection
    _last_back_ts   = 0.0             # tracks last back-press for exit confirmation
    # ── MT5 ──────────────────────────────────────────────────────────────
    _mt5_enabled        = False
    _mt5_connected      = False
    _mt5_ping_event     = None
    _mt5_share_on       = False
    _mt5_posting        = False
    _mt5_posting_ts     = 0.0
    _mt5_testing        = False
    _broker_symbol_map  = {}
    # ── Fetch (price + ATR from MT5) ─────────────────────────────────────
    _fetch_pending      = False   # True while a fetch is in-flight
    _fetch_timeout_ev   = None    # Clock event handle for fetch safety timeout
    _auto_fetch_ev      = None    # Clock event for candle-close precise scheduler
    _CANDLE_SECONDS = {           # candle duration in seconds per timeframe
        'M1': 60,    'M5': 300,   'M15': 900,  'M30': 1800,
        'H1': 3600,  'H4': 14400, 'D1': 86400, 'W1': 604800,
    }
    _last_price_data    = None    # cached from last successful fetch
    _atr_timeframe      = "H1"   # timeframe for ATR fetch
    _last_candle_time   = {}     # {sym_tf: unix_ts} — tracks last fetched closed candle
    # ── UI state ─────────────────────────────────────────────────────────
    _popup_dialog       = None    # active MDDialog (dismissed before reopening)
    _mode_switching     = False   # guard: prevent re-entrant mode switches
    _last_tab           = None    # name of last-active nav tab
    # ── Position Manager ─────────────────────────────────────────────────
    _positions_data     = []      # last fetched positions list
    _pos_fetching       = False   # True while positions fetch in-flight
    _pos_refresh_ev     = None    # Clock event: auto positions refresh
    _acct_refresh_ev    = None    # Clock event: auto account refresh
    _trail_ev           = None    # Clock event: ATR trail tick
    _fast_price_tick_ev = None    # Clock event: 2-second price-only tick (MT5-style)
    _card_refs          = {}      # {ticket: weakref(PositionCardRV)} — direct label access
    _active_trails      = {}      # {ticket: {period, mult, direction, symbol}}
    _last_trail_candles = {}      # {ticket: candle_time} — last trail update time
    _levels_visible     = False   # True when levels panel is expanded
    # ── Performance caches (module-level, shared across instances) ────────
    _vibrator_cache   = None          # cached Android Vibrator — resolved once
    _vibrator_ready   = False         # True after _init_vibrator() called
    _imm_cache        = None          # InputMethodManager (pre-warmed at t=2s)
    _window_cache     = None          # Activity Window   (pre-warmed at t=2s)
    _imm_ready        = False         # True once _init_ime() completes
    _numpad_label_ref = None          # cached numpad_value_label widget ref

    # ── init ──────────────────────────────────────────────────────────────
    # ── Widget name map for live touch logging ────────────────────────────
    _WIDGET_NAMES = {
        'atr':               'ATR field',
        'lots':              'Lots field',
        'entry':             'Entry field',
        'account':           'Account field',
        'risk':              'Risk % field',
        'calc_btn':          'CALCULATE button',
        'instrument':        'Instrument dropdown',
        'mt5_buy_btn':       'MT5 BUY button',
        'mt5_sell_btn':      'MT5 SELL button',
        'fetch_btn':         'FETCH button',
        'tf_spinner':        'Timeframe selector',
        'numpad_toggle_btn': 'Numpad toggle (chevron)',
        'mt5_toggle':        'MT5 toggle',
        'win_rate_slider':   'Win Rate slider',
    }



# ═══════════════════════════════════════════════════════════════════════════════
# StopLossApp  — MDApp subclass
# ═══════════════════════════════════════════════════════════════════════════════
class StopLossApp(MDApp):

    def build(self):
        self.theme_cls.primary_palette = "Blue"
        self.theme_cls.accent_palette  = "LightBlue"
        self.title = "StopLoss Calculator"
        self.icon  = self._resolve_icon()

        # Restore persisted theme before UI builds — prevents dark→light flash
        try:
            _path = os.path.join(self.user_data_dir, STORE_FILE)
            if os.path.exists(_path):
                _s = JsonStore(_path)
                if _s.exists('ui_prefs'):
                    _theme = _s.get('ui_prefs').get('theme', 'Dark')
                    self.theme_cls.theme_style = _theme
                else:
                    self.theme_cls.theme_style = "Dark"
            else:
                self.theme_cls.theme_style = "Dark"
        except Exception:
            self.theme_cls.theme_style = "Dark"

        Window.softinput_mode = ""
        if platform == 'android':
            try:
                from jnius import autoclass as _ac  # type: ignore[import]
                _ac('org.kivy.android.PythonActivity').mActivity.getWindow().setSoftInputMode(51)
            except Exception as e:
                log.debug("build MIUI IME: %s", e)

        root = Builder.load_file(_KV_FILE)

        menu_items = [
            {"text": sym, "height": dp(48),
             "on_release": lambda x=sym: self.set_item(x)}
            for sym in CONTRACTS
        ]
        self.menu = MDDropdownMenu(
            caller=root.ids.instrument,
            items=menu_items,
            width=dp(200),
            max_height=dp(320),
        )
        return root

    def _resolve_icon(self):
        import sys as _s, os as _o
        base = getattr(_s, '_MEIPASS', _o.path.dirname(_o.path.abspath(__file__)))
        path = _o.path.join(base, 'app_icon.ico')
        return path if _o.path.exists(path) else ''

    def rebuild_instrument_menu(self, symbols):
        """Rebuild the instrument dropdown — called on MT5 connect/disconnect.
        Symbols are broker-specific names when connected, base names when not.
        """
        try:
            try:
                self.menu.dismiss()
            except Exception:
                pass
            caller = self.root.ids.instrument
            menu_items = [
                {"text": sym, "height": dp(48),
                 "on_release": lambda x=sym: self.set_item(x)}
                for sym in symbols
            ]
            self.menu = MDDropdownMenu(
                caller=caller,
                items=menu_items,
                width=dp(200),
                max_height=dp(320),
            )
            log.debug("instrument menu rebuilt: %d symbols", len(symbols))
        except Exception as e:
            log.warning("rebuild_instrument_menu: %s", e)

    def set_item(self, text: str):
        # Preserve broker name casing — broker symbols can be case-sensitive (XAUUSDm)
        self.root.ids.instrument.text = text
        self.menu.dismiss()
        self.root.on_instrument_change(text)

    def on_pause(self):
        """Android: keep process alive when app goes to background.
        Must return True — otherwise Android kills the process.
        CRITICAL: do NOT use deferred writes here — Android may suspend
        before the Clock event fires. Write synchronously.
        """
        try:
            root = self.root
            if not root:
                return True
            # Synchronous snapshot — no Clock deferral
            snapshot = dict(
                atr=root.ids.atr.text,
                lots=root.ids.lots.text,
                entry=root.ids.entry.text,
                account=root.ids.account.text,
                risk=root.ids.risk.text,
                instrument=root.ids.instrument.text,
            )
            root._store.put('last', **snapshot)
        except Exception as e:
            log.warning("on_pause save: %s", e)
        return True   # CRITICAL: tell Android to keep us alive

    def on_start(self):
        """Start the authenticated licence heartbeat.

        PHASE 12: the previous startup path also began polling a public ntfy
        topic for REVOKE messages and posted MT5 account data to another public
        topic. Both are gone. Revocation now arrives over the authenticated
        heartbeat inside LicensingProvider, and nothing about the customer's
        trading account leaves this machine.
        """
        _start_session_heartbeat()

    def on_resume(self):
        """Android: re-sync UI after returning from background.
        Called when user brings app back to foreground.
        """
        try:
            root = self.root
            if not root:
                return
            # Re-suppress keyboard — Android resets softinput_mode on resume
            Window.softinput_mode = ""
            Window.release_all_keyboards()
            Clock.schedule_once(lambda dt: Window.release_all_keyboards(), 0.1)
            # Clean up any stale input session from before suspend
            # (widget references may be invalid after Android recreates views)
            if root._input_state == "EDITING":
                root._end_input_session(commit=False)  # discard stale buffer
            try:
                root.ids.numpad_field_label.text = "Tap a field to enter value"
                root.ids.numpad_value_label.text  = ""
            except Exception as e:
                log.debug("on_resume numpad bar: %s", e)
            # Re-apply mode visuals (theme may reset on some devices)
            Clock.schedule_once(lambda dt: root._apply_mode_visuals(), 0.1)

            # Restore close trade panel if a signal was active before suspend
            # H1 FIX: 200ms deferred IME re-arm (Gboard/MIUI brief flash on resume)
            # MIUI can briefly show keyboard during resume even with setSoftInputMode(51)
            # A second deferred call 200ms later catches this re-show reliably
            if platform == 'android':
                Clock.schedule_once(lambda dt: root._hide_android_ime(), 0.2)

        except Exception as e:
            log.warning("on_resume: %s", e)



    def on_low_memory(self):
        """Android: system is critically low on memory.
        Called before the process may be killed (lower priority than on_pause).
        Save state defensively — we may not get on_stop.
        """
        log.warning("on_low_memory — emergency state save")
        try:
            root = self.root
            if root:
                snapshot = dict(
                    atr=root.ids.atr.text,
                    lots=root.ids.lots.text,
                    account=root.ids.account.text,
                    risk=root.ids.risk.text,
                    instrument=root.ids.instrument.text,
                )
                root._store.put('last', **snapshot)
        except Exception as e:
            log.debug("on_low_memory save: %s", e)

    def on_stop(self):
        """Called before process death — flush state to disk.
        Android may skip on_pause on force-close; on_stop is guaranteed.
        """
        try:
            if self.root:
                self.root._stop_mt5_ping()
        except Exception: pass
        try:
            root = self.root
            if not root:
                return
            # Ensure storage dir exists (may not if app closed before first calc)
            _path = os.path.join(self.user_data_dir, STORE_FILE)
            try:
                os.makedirs(os.path.dirname(_path), exist_ok=True)
            except Exception as e:
                log.debug("on_stop makedirs: %s", e)
            snapshot = dict(
                atr=root.ids.atr.text,
                lots=root.ids.lots.text,

                entry=root.ids.entry.text,
                account=root.ids.account.text,
                risk=root.ids.risk.text,
                instrument=root.ids.instrument.text,
            )
            JsonStore(_path).put('last', **snapshot)
        except Exception as e:
            log.debug('on_stop: %s', e)


import sys as _sys


def _get_icon_path() -> str:
    import sys as _s, os as _o
    base = getattr(_s, '_MEIPASS', _o.path.dirname(_o.path.abspath(__file__)))
    return _o.path.join(base, 'app_icon.ico')


def _set_icon(root):
    try:
        root.iconbitmap(_get_icon_path())
    except Exception:
        pass


def _show_splash():
    try:
        import tkinter as tk
        splash = tk.Tk()
        splash.title('StopLoss Calculator')
        _set_icon(splash)
        splash.configure(bg='#0d0d0f')
        w, h = 300, 120
        sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
        splash.geometry(f'{w}x{h}+{(sw-w)//2}+{(sh-h)//2}')
        splash.resizable(False, False)
        splash.attributes('-topmost', True)
        tk.Label(splash, text='StopLoss Calculator',
                 font=('Segoe UI', 16, 'bold'), bg='#0d0d0f', fg='white').pack(pady=(30, 6))
        tk.Label(splash, text='Loading…',
                 font=('Segoe UI', 9), bg='#0d0d0f', fg='#444').pack()
        splash.update()
        return splash
    except Exception:
        return None


_splash = _show_splash()
if _splash:
    try: _splash.destroy()
    except Exception: pass

# ── Admin activate/deactivate must reach an already-open session, not just
# block the NEXT launch (PHASE 17). Without this, an admin suspending or
# revoking a customer's licence from the panel only took effect once that
# customer happened to close and reopen the app — a suspended account could
# keep trading indefinitely in the meantime. The background heartbeat
# thread (started below via _start_session_heartbeat) already polls the
# server every ~90s and correctly flips LicenceState.authorised to False the
# moment the server reports the account is no longer ACTIVE; this callback
# is what makes that transition actually DO something while the app is open.
_was_authorised = {"v": False}


def _on_licence_state_change(state) -> None:
    """Runs on the background heartbeat thread — never touch Kivy/Tk widgets
    directly here, only schedule work onto the main thread via Clock."""
    was, now = _was_authorised["v"], state.authorised
    _was_authorised["v"] = now
    if was and not now and state.reason not in ("NOT_AUTHENTICATED", "LOGGED_OUT"):
        # A previously-authorised session just lost authorisation — the
        # server said so authoritatively (suspended, revoked, expired), not
        # merely "unreachable" (that case keeps working inside offline grace
        # and never reaches here with authorised=False). Kick the user out
        # now instead of waiting for them to close the app on their own.
        _msg = state.message or "Your access to StopLoss Pro has changed."
        Clock.schedule_once(lambda _dt: _force_signed_out(_msg), 0)


def _force_signed_out(message: str) -> None:
    try:
        import tkinter as _tk
        from tkinter import messagebox as _messagebox
        _root = _tk.Tk()
        _root.withdraw()
        _root.attributes("-topmost", True)
        _messagebox.showwarning("StopLoss Pro", f"{message}\n\nThe application will now close.",
                                 parent=_root)
        _root.destroy()
    except Exception as exc:
        log.warning("_force_signed_out: could not show dialog: %s", exc)
    try:
        MDApp.get_running_app().stop()
    except Exception:
        _sys.exit(0)


get_provider(on_state_change=_on_licence_state_change)   # must be the FIRST
                                                          # call to get_provider()
                                                          # in the process so
                                                          # the singleton is
                                                          # constructed with
                                                          # this wired in.

_register_if_new()        # record first install / re-install

_resume_session()         # try to silently restore a saved session (PHASE 17) —
                           # must run before the activation check below, since
                           # _is_activated() only reflects this process's
                           # in-memory state until resume() has had a chance
                           # to reload + revalidate it from disk (state.bin).

if not _is_activated():
    _activated = _show_activation_blocker()
    if not _activated:
        _sys.exit(0)

StopLossApp().run()
