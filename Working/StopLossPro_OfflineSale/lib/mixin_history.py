# mixin_history.py — Trade history cards, monthly stats, tab switch, status bar, snackbar.
# ─────────────────────────────────────────────────────────────────────────────
import os, time, math, logging, weakref, datetime, threading, base64, hashlib

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.uix.widget import Widget
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
_SNACKBAR_NEW_API = MDSnackbarText is not None

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


class HistoryMixin:
    def _add_to_history(self, setup: TradeSetup):
        try:
            entries = []
            if self._store.exists('history'):
                entries = list(self._store.get('history').get('entries', []))
            d = DECIMALS.get(_base_sym(setup.symbol), 2)
            def _sf(v, default=0.0):
                """Safe float — returns default if v is not finite."""""
                try: return v if (v is not None and math.isfinite(v)) else default
                except Exception: return default
            entries.insert(0, {
                'ts':        setup.timestamp,
                'sym':       setup.symbol,
                'atr':       setup.atr,
                'lots':      setup.lots,
                'entry_fmt': f"{setup.entry:.{d}f}" if setup.entry else "",
                'loss':      setup.loss,
                'p1':        setup.profit1,
                'p2':        setup.profit2,
                'p3':        setup.profit3,
                'rr1':       2.0,
                'rr2':       3.0,
                'ev':        setup.ev,
                'buy':       setup.buy,
                'sell':      setup.sell,
            })
            self._store.put('history', entries=entries[:MAX_HIST])
            self._history_cache_key = None   # invalidate cache
            self._history_built     = False  # BUG 12: force rebuild on next view
        except Exception as e:
            log.warning("_add_to_history: %s", e)

    def _refresh_history(self, force: bool = False):
        """Rebuild history list — cache-guarded to skip redundant rebuilds."""
        try:
            lst = self.ids.history_list
        except Exception:
            return

        # BUG 12 FIX: fast-exit without touching _store if already built
        # _history_built is cleared only by clear_history() and _add_to_history()
        if not force and self._history_built:
            return   # already up-to-date — skip store read entirely

        # ── Cache check — skip full rebuild if data unchanged ──────────────
        try:
            raw = (self._store.get('history').get('entries', [])
                   if self._store.exists('history') else [])
        except Exception:
            raw = []

        cache_key = (len(raw), raw[0].get('ts', 0) if raw else 0)
        if cache_key == self._history_cache_key:
            self._history_built = True
            return   # data unchanged
        self._history_cache_key = cache_key
        self._history_built     = True

        # ── Full rebuild ───────────────────────────────────────────────────
        lst.clear_widgets()

        if not raw:
            self._add_empty_history_label(lst, mode='both')
            return

        self._add_history_cards_batched(lst, raw, batch_id=self._history_batch_id)

    def _add_history_cards_batched(self, lst, entries, batch_size: int = 8,
                                      batch_id: int = 0):
        """Add history cards in batches with cancellation token.
        batch_id is checked against self._history_batch_id — stale batches abort.
        """
        # Cancellation: a clear/refresh increments _history_batch_id
        if batch_id != self._history_batch_id:
            return
        # Debounce: skip re-render if called within 100 ms of last render start
        import time as _time
        _now = _time.monotonic()
        _last = getattr(self, '_last_render_ts', 0.0)
        if (_now - _last) < 0.1 and _last != 0.0:
            return
        self._last_render_ts = _now
        if not entries:
            return
        # Guard: list detached (tab switched away, history cleared)
        try:
            if lst.parent is None:
                return
        except Exception:
            return
        batch = entries[:batch_size]
        rest  = entries[batch_size:]
        for h in batch:
            try:
                lst.add_widget(self._build_history_card(h))
            except Exception as e:
                log.warning("history card render: %s", e)
        if rest:
            bid = batch_id   # capture for lambda closure
            Clock.schedule_once(
                lambda dt: self._add_history_cards_batched(
                    lst, rest, batch_size, bid),
                0.016
            )

    def _add_empty_history_label(self, parent, mode='both'):
        text = {
            'both':     "No calculations yet.\nUse the Calculate tab to get started.",
            'signal':   "No signals generated yet.\nSwitch to Signal Mode to post signals.",
            'personal': "No personal calculations yet.\nUse Personal Mode to calculate.",
        }.get(mode, "No calculations yet.")
        parent.add_widget(MDLabel(
            text=text, halign="center", theme_text_color="Secondary",
            font_style="Caption", size_hint_y=None, height=dp(56),
        ))

    def _build_history_card(self, h: dict):
        # Lazy imports — cached after first call, zero cost on subsequent calls

        def _divider():
            w = Widget(size_hint_y=None, height=dp(1))
            with w.canvas:
                Color(1, 1, 1, 0.08)
                rect = Rectangle(pos=w.pos, size=w.size)
            w.bind(pos=lambda s, v: setattr(rect, 'pos', v),
                   size=lambda s, v: setattr(rect, 'size', v))
            return w

        app    = MDApp.get_running_app()
        # ── Schema validation: sanitize every field before rendering ──────────
        def _sf(v, d=0.0):
            try:
                f = float(v)
                return f if math.isfinite(f) else d
            except (TypeError, ValueError):
                return d
        def _ss(v, d=''):
            return str(v) if v is not None else d

        ts = _sf(h.get('ts', 0), 0)
        try:
            dt_str = datetime.datetime.fromtimestamp(ts).strftime("%b %d  %H:%M")
        except (OSError, OverflowError, ValueError):
            dt_str = "—"
        sym    = _ss(h.get('sym'),  '?')
        loss   = _sf(h.get('loss'))
        p1     = _sf(h.get('p1'))
        p2     = _sf(h.get('p2'))
        p3     = _sf(h.get('p3'))
        ev     = _sf(h.get('ev'))
        ef     = _ss(h.get('entry_fmt'))
        atr    = _sf(h.get('atr'))
        lots   = _sf(h.get('lots'))
        buy    = h.get('buy',  {}) if isinstance(h.get('buy'),  dict) else {}
        sell   = h.get('sell', {}) if isinstance(h.get('sell'), dict) else {}
        has_levels = bool(buy and sell)
        rr_str = "1:2 / 1:3 / 1:4"
        card = MDCard(
            orientation='vertical', size_hint_y=None,
            padding=[dp(12), dp(10), dp(12), dp(8)],
            radius=[dp(10)], elevation=1,
            md_bg_color=app.theme_cls.bg_dark,
        )
        card.bind(minimum_height=card.setter('height'))

        def _lbl(text, **kw):
            return MDLabel(text=text, font_style='Caption',
                           size_hint_y=None, height=dp(22), **kw)

        def _col_lbl(text, col, align='center'):
            l = _lbl(text, halign=align)
            l.theme_text_color = 'Custom'
            l.text_color = col
            return l

        # ── Row 1: symbol + datetime ──────────────────────────────────────
        r1 = MDBoxLayout(size_hint_y=None, height=dp(24))
        sym_lbl = _lbl(f"[b]{sym}[/b]", markup=True)
        hint = "  ▾ tap" if has_levels else ""
        dt_lbl = _lbl(dt_str + hint, theme_text_color='Secondary', halign='right')
        r1.add_widget(sym_lbl)
        r1.add_widget(dt_lbl)
        card.add_widget(r1)
        card.add_widget(_divider())

        # ── Row 2: ATR / lots / entry ─────────────────────────────────────
        entry_txt = f"Entry: {ef}" if ef else "No entry"
        card.add_widget(_lbl(
            f"ATR {atr:.2f}  |  {lots:.2f} lots  |  {entry_txt}",
            theme_text_color='Secondary'))

        # ── Row 3: loss / tp1 / tp2 / tp3 ──────────────────────────────
        r3 = MDBoxLayout(size_hint_y=None, height=dp(24), spacing=dp(4))
        r3.add_widget(_col_lbl(f"-${loss:,.2f}",  (1.0, 0.26, 0.26, 1) if app.theme_cls.theme_style == "Dark" else (0.82, 0.10, 0.10, 1)))
        r3.add_widget(_col_lbl(f"+${p1:,.2f}",    (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)))
        r3.add_widget(_col_lbl(f"+${p2:,.2f}",    (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)))
        r3.add_widget(_col_lbl(f"+${p3:,.2f}",    (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)))
        card.add_widget(r3)

        # ── Row 4: R:R + EV ──────────────────────────────────────────────
        r4 = MDBoxLayout(size_hint_y=None, height=dp(22))
        ev_c = (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1) if ev >= 0 else (1.0, 0.26, 0.26, 1) if app.theme_cls.theme_style == "Dark" else (0.82, 0.10, 0.10, 1)
        r4.add_widget(_lbl(f"R:R  {rr_str}",
                           theme_text_color='Secondary'))
        ev_l = _lbl(f"EV: ${ev:+,.2f}", halign='right')
        ev_l.theme_text_color = 'Custom'
        ev_l.text_color = ev_c
        r4.add_widget(ev_l)
        card.add_widget(r4)

        # ── Expandable levels section (hidden until tapped) ───────────────
        if has_levels:
            card.add_widget(_divider())

            levels_box = MDBoxLayout(
                orientation='vertical',
                size_hint_y=None,
                height=0,
                opacity=0,
            )

            # Determine decimal precision from symbol
            d = DECIMALS.get(_base_sym(sym), 2)

            # Two-column sub-layout: BUY left, SELL right
            cols = MDBoxLayout(size_hint_y=None, height=dp(112), spacing=dp(8))

            def _level_col(title, title_col, data):
                col = MDBoxLayout(orientation='vertical', size_hint_x=1)
                t = _lbl(title, halign='center')
                t.theme_text_color = 'Custom'
                t.text_color = title_col
                col.add_widget(t)
                try:
                    tp3_val = f"{_sf(data.get('tp3')):.{d}f}" if data.get('tp3') else "—"
                    rows = [
                        ("Entry", f"{_sf(data.get('entry')):.{d}f}", [1, 1, 1, 0.9]),
                        ("SL",    f"{_sf(data.get('sl')):.{d}f}",    (1.0, 0.26, 0.26, 1) if app.theme_cls.theme_style == "Dark" else (0.82, 0.10, 0.10, 1)),
                        ("TP1",   f"{_sf(data.get('tp1')):.{d}f}",   (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)),
                        ("TP2",   f"{_sf(data.get('tp2')):.{d}f}",   (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)),
                        ("TP3",   tp3_val,                            (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1)),
                    ]
                except Exception:
                    rows = []
                for label_text, val_text, col_color in rows:
                    row = MDBoxLayout(size_hint_y=None, height=dp(20))
                    lk = _lbl(label_text, theme_text_color='Secondary')
                    lv = _lbl(val_text, halign='right')
                    lv.theme_text_color = 'Custom'
                    lv.text_color = col_color
                    row.add_widget(lk)
                    row.add_widget(lv)
                    col.add_widget(row)
                return col

            cols.add_widget(_level_col(
                "▲ BUY",  (0.15, 0.79, 0.36, 1) if app.theme_cls.theme_style == "Dark" else (0.10, 0.64, 0.28, 1), buy))
            cols.add_widget(_level_col(
                "▼ SELL", (1.0, 0.26, 0.26, 1) if app.theme_cls.theme_style == "Dark" else (0.82, 0.10, 0.10, 1),   sell))

            levels_box.add_widget(cols)
            card.add_widget(levels_box)

            # Tap toggles expand/collapse
            _expanded = [False]

            def _on_tap(instance, touch):
                if not instance.collide_point(*touch.pos):
                    return
                Animation.cancel_all(levels_box)
                if not _expanded[0]:
                    _expanded[0] = True
                    levels_box.height = dp(112)
                    Animation(opacity=1, duration=0.12).start(levels_box)
                    # flip hint arrow
                    try:
                        r1.children[0].text = r1.children[0].text.replace(
                            "▾", "▴")
                    except Exception as e:
                        log.debug("?: %s", e)
                else:
                    _expanded[0] = False
                    def _collapse(*a): levels_box.height = 0
                    a = Animation(opacity=0, duration=0.15)
                    a.bind(on_complete=_collapse)
                    a.start(levels_box)
                    try:
                        r1.children[0].text = r1.children[0].text.replace(
                            "▴", "▾")
                    except Exception as e:
                        log.debug("?: %s", e)

            card.bind(on_touch_down=_on_tap)

        return card

    def clear_history(self):
        try:
            self._store.put('history', entries=[])
        except Exception as e:
            log.warning("clear_history: %s", e)
        self._history_cache_key = None
        self._history_built     = False
        self._history_batch_id += 1      # cancels any in-progress batch render
        self._refresh_history(force=True)
        self._show_snackbar("History cleared")

    # ── tab switch ─────────────────────────────────────────────────────────
    # FIX #3 — explicit (tab_item, name) signature; no *args guesswork
    def _update_topbar_title(self, tab_name=None):
        """Always shows current mode in top bar title."""
        if tab_name is None:
            try:
                tab_name = self.ids.nav.current
            except Exception:
                tab_name = "calc"
        base = {
            "calc":     "SL Calculator",
            "history":  "Trade History",
            "settings": "Settings",
        }.get(tab_name, "SL Calculator")
        try:
            self.ids.topbar.title = base
        except Exception as e:
            log.debug("topbar title: %s", e)

    def _on_tab_switch(self, tab_item, name):
        self._update_topbar_title(name)

        # End input session only if EDITING — skip the call entirely when IDLE
        # (avoids 6+ self. attribute lookups + label update on every tab switch)
        if self._input_state != "IDLE":
            self._end_input_session(commit=True)

        # Cancel any in-progress history batch only when leaving history tab
        if getattr(self, "_last_tab", None) == "history":
            self._history_batch_id += 1
        # RC-2: leaving settings re-arms keyboard suppression
        # TG fields call _restore_ime_mode(setSoftInputMode=49) arming keyboard.
        # Re-suppress now before user can tap any numpad field on calc tab.
        if getattr(self, "_last_tab", None) == "settings" and platform == "android":
            Clock.schedule_once(lambda dt: self._hide_android_ime(), 0)
        self._last_tab = name

        if name == "history":
            # Schedule on next frame — IDs guaranteed ready, batch ID is fresh
            Clock.schedule_once(lambda dt: self._refresh_history(), 0)

        if name == "settings" and not self._settings_applied:
            self._settings_applied = True
            Clock.schedule_once(lambda dt: self._apply_settings_to_widgets(), 0)

    # ── status / snackbar ──────────────────────────────────────────────────
    def _set_status(self, text: str, color: str = "secondary"):
        try:
            lbl = self.ids.status
            lbl.text = text
            lbl.theme_text_color = "Custom"
            _is_dark = MDApp.get_running_app().theme_cls.theme_style == "Dark"
            lbl.text_color = {
                "secondary": [1, 1, 1, 0.55] if _is_dark else [0.1, 0.1, 0.1, 0.6],
                "error":     [1.0, 0.26, 0.26, 1],
                "success":   [0.15, 0.79, 0.36, 1],
            }.get(color, [1, 1, 1, 0.55] if _is_dark else [0.1, 0.1, 0.1, 0.6])
        except Exception as e:
            log.debug("_set_status widget: %s", e)

    # FIX #4 — try new MDSnackbar API first, fall back to old API
    def _show_snackbar(self, text: str):
        """Show snackbar using pre-imported module-level MDSnackbar (fast path)."""
        try:
            if _SNACKBAR_NEW_API:
                MDSnackbar(
                    MDSnackbarText(text=text),
                    y=dp(24), pos_hint={"center_x": 0.5},
                    size_hint_x=0.9, duration=2,
                ).open()
            elif MDSnackbar is not None:
                MDSnackbar(text=text, duration=2).open()
            else:
                self._set_status(text, "success")
        except Exception:
            self._set_status(text, "success")

    # ── keyboard ───────────────────────────────────────────────────────────

