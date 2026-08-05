# widgets.py — custom Kivy widgets used by the Root layout.
#   SwitchToggle   : animated iOS-style toggle (drawn in Canvas)
#   PositionCardRV : RecycleView card for the Position Manager list
# ─────────────────────────────────────────────────────────────────────────────
import weakref

from kivy.uix.widget import Widget
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior as _RDVB
from kivy.graphics import Color, RoundedRectangle, Ellipse
from kivy.clock import Clock
from kivy.animation import Animation
from kivy.metrics import dp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty

from kivy.factory import Factory
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.card import MDCard
from kivymd.uix.button import MDRaisedButton
from kivymd.uix.label import MDLabel

from constants import log, DECIMALS, _base_sym

class SwitchToggle(Widget):
    """Canvas-drawn toggle: pill track + white circle thumb, smooth animation."""
    active          = BooleanProperty(False)
    track_color_on  = ListProperty([0.13, 0.59, 0.95, 1])
    track_color_off = ListProperty([0.30, 0.30, 0.32, 1])
    _prog           = NumericProperty(0.0)   # 0 = off, 1 = on (animated)

    def on_kv_post(self, base_widget):
        self._prog = 1.0 if self.active else 0.0
        self.bind(pos=self._draw, size=self._draw, _prog=self._draw,
                  track_color_on=self._draw, track_color_off=self._draw)
        Clock.schedule_once(lambda dt: self._draw(), 0)

    def _draw(self, *_):
        if self.height <= 0:
            return
        self.canvas.clear()
        pad = dp(3)
        ts  = self.height - pad * 2          # thumb diameter
        p   = max(0.0, min(1.0, self._prog))
        # Interpolate track color
        c0, c1 = self.track_color_off, self.track_color_on
        col = [c0[i] + (c1[i] - c0[i]) * p for i in range(4)]
        # Thumb x: slides from left-pad to right-pad
        tx = self.x + pad + (self.width - ts - pad * 2) * p
        r  = self.height / 2
        with self.canvas:
            Color(*col)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[r, r, r, r])
            Color(1, 1, 1, 1)
            Ellipse(pos=(tx, self.y + pad), size=(ts, ts))

    def on_active(self, instance, value):
        target = 1.0 if value else 0.0
        Animation.cancel_all(self, '_prog')
        Animation(_prog=target, duration=0.20, t='out_quad').start(self)

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos) and not self.disabled:
            self.active = not self.active
            return True
        return super().on_touch_up(touch)



# ═══════════════════════════════════════════════════════════════════════════
# POSITION CARD — RecycleView item (built once, data injected per frame)
# ═══════════════════════════════════════════════════════════════════════════
class PositionCardRV(_RDVB, MDBoxLayout):
    """
    Virtual-scroll position card.
    RecycleView reuses ~7 widget instances for any number of positions —
    only visible cards exist in memory.  refresh_view_attrs() injects data.
    """
    index = None

    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal",
                         size_hint=(1, None), height=dp(238), **kwargs)
        # ── mutable per-card state ────────────────────────────────────────
        self._ticket      = 0
        self._symbol      = ""
        self._volume      = 0.0
        self._ptype       = "BUY"
        self._open_price  = 0.0
        self._sl          = 0.0
        self._tp          = 0.0
        self._app_ref     = lambda: None
        # ── left accent strip ─────────────────────────────────────────────
        self._strip = MDBoxLayout(size_hint_x=None, width=dp(5),
                                  md_bg_color=(0.15, 0.75, 0.35, 1))
        self.add_widget(self._strip)
        # ── main card ─────────────────────────────────────────────────────
        self._card = MDCard(
            orientation="vertical",
            padding=[dp(11), dp(8), dp(11), dp(8)],
            spacing=dp(5),
            radius=[0, dp(14), dp(14), 0],
            elevation=1,
            md_bg_color=(0.08, 0.08, 0.11, 1),
        )
        self.add_widget(self._card)
        # ── Header: symbol | direction+lots | #ticket ─────────────────────
        hdr = MDBoxLayout(orientation="horizontal", size_hint_y=None, height=dp(30))
        self._sym_lbl  = MDLabel(font_size="17sp", bold=True,
                                  theme_text_color="Primary", size_hint_x=0.38)
        self._dir_lbl  = MDLabel(font_size="13sp", bold=True, valign="middle",
                                  size_hint_x=0.38, theme_text_color="Custom",
                                  text_color=(0.18, 0.84, 0.44, 1))
        self._tick_lbl = MDLabel(font_size="10sp", theme_text_color="Secondary",
                                  halign="right", valign="middle", size_hint_x=0.24)
        for w in (self._sym_lbl, self._dir_lbl, self._tick_lbl):
            hdr.add_widget(w)
        self._card.add_widget(hdr)
        # ── Price grid: ENTRY | NOW | SL | TP ────────────────────────────
        price_row = MDBoxLayout(orientation="horizontal",
                                size_hint_y=None, height=dp(44))
        self._price_lbl = {}
        for key, caption in [("ENTRY","ENTRY"),("NOW","NOW"),("SL","SL"),("TP","TP")]:
            col = MDBoxLayout(orientation="vertical", size_hint_x=0.25,
                              spacing=dp(2), padding=[0, dp(3), 0, dp(3)])
            col.add_widget(MDLabel(text=caption, font_size="9sp",
                                   theme_text_color="Secondary", halign="center",
                                   size_hint_y=None, height=dp(14)))
            val = MDLabel(font_size="12sp", theme_text_color="Custom",
                          halign="center", valign="middle",
                          size_hint_y=None, height=dp(22),
                          bold=(key == "NOW"), text_color=(1,1,1,1))
            col.add_widget(val)
            price_row.add_widget(col)
            self._price_lbl[key] = val
        self._card.add_widget(price_row)
        # ── P&L row ───────────────────────────────────────────────────────
        pnl_row = MDBoxLayout(orientation="horizontal",
                              size_hint_y=None, height=dp(26), spacing=dp(6))
        self._pnl_lbl     = MDLabel(font_size="15sp", bold=True,
                                     theme_text_color="Custom", valign="middle",
                                     size_hint_x=0.44, text_color=(0.18,0.84,0.44,1))
        self._swap_lbl    = MDLabel(font_size="10sp", theme_text_color="Secondary",
                                     halign="center", valign="middle", size_hint_x=0.30)
        self._trail_badge = MDLabel(font_size="10sp", theme_text_color="Custom",
                                     text_color=(0.98, 0.72, 0.10, 1),
                                     halign="right", valign="middle", size_hint_x=0.26)
        for w in (self._pnl_lbl, self._swap_lbl, self._trail_badge):
            pnl_row.add_widget(w)
        self._card.add_widget(pnl_row)
        # ── Action row 1: CLOSE | PARTIAL | B/E | MODIFY ─────────────────
        _d = (0.18, 0.18, 0.22, 1)
        row1 = MDBoxLayout(orientation="horizontal",
                           size_hint_y=None, height=dp(36), spacing=dp(4))
        for attr, txt, bg in [
            ("_btn_close",   "CLOSE",   (0.72, 0.07, 0.09, 1)),
            ("_btn_partial", "PARTIAL", _d),
            ("_btn_be",      "B/E",     (0.05, 0.44, 0.20, 1)),
            ("_btn_modify",  "MODIFY",  (0.12, 0.30, 0.64, 1)),
        ]:
            b = MDRaisedButton(text=txt, size_hint_x=1, size_hint_y=None,
                                height=dp(32), md_bg_color=bg,
                                theme_text_color="Custom", text_color=[1,1,1,1],
                                font_size="11sp", elevation=0)
            setattr(self, attr, b)
            row1.add_widget(b)
        self._card.add_widget(row1)
        self._btn_close.bind(on_release=self._on_close)
        self._btn_partial.bind(on_release=self._on_partial)
        self._btn_be.bind(on_release=self._on_be)
        self._btn_modify.bind(on_release=self._on_modify)
        # ── Action row 2: TRAIL toggle | opened time ──────────────────────
        row2 = MDBoxLayout(orientation="horizontal",
                           size_hint_y=None, height=dp(32), spacing=dp(6))
        self._trail_btn = MDRaisedButton(
            text="TRAIL: OFF", size_hint_x=0.42, size_hint_y=None,
            height=dp(30), md_bg_color=_d,
            theme_text_color="Custom", text_color=[1,1,1,1],
            font_size="11sp", elevation=0)
        self._trail_btn.bind(on_release=self._on_trail)
        self._open_lbl = MDLabel(font_size="10sp", theme_text_color="Secondary",
                                  halign="right", valign="middle")
        row2.add_widget(self._trail_btn)
        row2.add_widget(self._open_lbl)
        self._card.add_widget(row2)

    # ── Button callbacks — read instance attrs (safe with RV widget reuse) ──
    def _on_close(self, _):
        r = self._app_ref()
        if r: r.close_position(self._ticket, self._symbol, self._volume)
    def _on_partial(self, _):
        r = self._app_ref()
        if r: r.partial_close_dialog(self._ticket, self._symbol, self._volume)
    def _on_be(self, _):
        r = self._app_ref()
        if r: r.set_break_even(self._ticket, self._symbol, self._open_price, self._ptype)
    def _on_modify(self, _):
        r = self._app_ref()
        if r: r.modify_sl_tp_dialog(self._ticket, self._symbol, self._sl, self._tp)
    def _on_trail(self, _):
        r = self._app_ref()
        if r: r.toggle_atr_trail(self._ticket, self._symbol, self._ptype)

    def refresh_view_attrs(self, rv, index, data):
        """RecycleView calls this each time a card enters the visible viewport."""
        import datetime as _dt
        self.index       = index
        self._ticket     = data.get("ticket",        0)
        self._symbol     = data.get("symbol",        "?")
        # ── Register this card for direct fast-tick label mutations ───────────
        _app_ref = data.get("app_ref")
        _root = _app_ref() if _app_ref else None
        if _root is not None and self._ticket:
            import weakref as _wr
            _root._card_refs[self._ticket] = _wr.ref(self)
        self._ptype      = data.get("type",          "BUY")
        self._volume     = data.get("volume",        0.0)
        self._open_price = data.get("open_price",    0.0)
        self._sl         = data.get("sl",            0.0)
        self._tp         = data.get("tp",            0.0)
        self._app_ref    = data.get("app_ref",       lambda: None)

        profit    = data.get("profit",        0.0)
        swap      = data.get("swap",          0.0)
        cur_price = data.get("current_price", 0.0)
        open_time = data.get("open_time",     0)
        is_dark   = data.get("is_dark",       True)
        trail_act = data.get("trail_active",  False)
        total_pnl = profit + swap

        is_buy  = self._ptype == "BUY"
        pnl_pos = total_pnl >= 0

        type_clr = (0.18, 0.84, 0.44, 1) if is_buy  else (1.00, 0.32, 0.32, 1)
        pnl_clr  = (0.18, 0.84, 0.44, 1) if pnl_pos else (1.00, 0.32, 0.32, 1)
        card_bg  = (0.08, 0.08, 0.11, 1) if is_dark else (0.98, 0.98, 1.00, 1)
        strip_cl = (0.15, 0.75, 0.35, 1) if is_buy  else (0.88, 0.18, 0.18, 1)
        now_clr  = (1.00, 1.00, 1.00, 1) if is_dark else (0.05, 0.05, 0.07, 1)
        ent_clr  = (0.70, 0.70, 0.76, 1)
        sl_clr   = (1.00, 0.58, 0.22, 1)
        tp_clr   = (0.18, 0.84, 0.44, 1)
        btn_dark = (0.18, 0.18, 0.22, 1) if is_dark else (0.80, 0.80, 0.84, 1)

        # Strip + card background
        self._strip.md_bg_color = strip_cl
        self._card.md_bg_color  = card_bg

        # Header
        self._sym_lbl.text        = self._symbol
        dir_sym = "▲" if is_buy else "▼"
        self._dir_lbl.text        = f"{dir_sym} {self._volume:.2f} lots"
        self._dir_lbl.text_color  = type_clr
        self._tick_lbl.text       = "#" + str(self._ticket)[-7:]

        # Prices
        digs = DECIMALS.get(_base_sym(self._symbol), 5)
        fmt  = "{:." + str(digs) + "f}"
        self._price_lbl["ENTRY"].text       = fmt.format(self._open_price)
        self._price_lbl["ENTRY"].text_color = ent_clr
        self._price_lbl["NOW"].text         = fmt.format(cur_price)
        self._price_lbl["NOW"].text_color   = now_clr
        self._price_lbl["SL"].text          = fmt.format(self._sl) if self._sl else "—"
        self._price_lbl["SL"].text_color    = sl_clr
        self._price_lbl["TP"].text          = fmt.format(self._tp) if self._tp else "—"
        self._price_lbl["TP"].text_color    = tp_clr

        # P&L
        sign = "▲" if pnl_pos else "▼"
        self._pnl_lbl.text        = f"{sign} {total_pnl:+,.2f}"
        self._pnl_lbl.text_color  = pnl_clr
        self._swap_lbl.text       = f"swap {swap:+.2f}"
        self._trail_badge.text    = "⚡ TRAIL" if trail_act else ""

        # Trail button
        trail_bg = (0.05, 0.52, 0.22, 1) if trail_act else btn_dark
        self._trail_btn.text         = "⚡ TRAIL ON" if trail_act else "TRAIL: OFF"
        self._trail_btn.md_bg_color  = trail_bg
        self._btn_partial.md_bg_color = btn_dark

        # Open time
        try:
            open_str = (_dt.datetime.fromtimestamp(open_time).strftime("%b %d  %H:%M")
                        if open_time else "—")
        except Exception:
            open_str = "—"
        self._open_lbl.text = f"Opened: {open_str}"

        # No super() — MDBoxLayout has no refresh_view_attrs

# Register PositionCardRV with Kivy Factory so KV viewclass: lookup finds it
Factory.register('PositionCardRV', cls=PositionCardRV)

# ═══════════════════════════════════════════════════════════════════════════
# ROOT WIDGET
# ═══════════════════════════════════════════════════════════════════════════

