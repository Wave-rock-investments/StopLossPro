# mixin_orders.py — Trade popup (2-button UX), pre-flight check, order dispatch, reset.
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


class OrdersMixin:
    def _confirm_dialog(self, title, text, on_ok):
        """Generic confirm / cancel dialog."""
        if self._popup_dialog:
            try: self._popup_dialog.dismiss()
            except Exception: pass
        dlg = MDDialog(
            title=title, text=text,
            buttons=[
                MDFlatButton(text="CANCEL",  on_release=lambda x: dlg.dismiss()),
                MDRaisedButton(text="CONFIRM",
                               md_bg_color=(0.60, 0.05, 0.07, 1),
                               theme_text_color="Custom", text_color=[1,1,1,1],
                               on_release=lambda x: (dlg.dismiss(), on_ok())),
            ],
        )
        self._popup_dialog = dlg
        dlg.open()

    # ── Trade Popup ────────────────────────────────────────────────────────────────────

    def _show_trade_popup(self, side, data):
        """Unified MT5 order popup: order type selection + TP selection + PLACE ORDER.

        side : 'BUY' or 'SELL'
        data : dict with keys entry, sl, tp1, tp2, tp3, loss, profit1, profit2, profit3
        All possible order types shown as tap-to-select buttons.
        """
        _app = MDApp.get_running_app()
        if _app is None:
            log.warning("_show_trade_popup: MDApp not running"); return

        sym  = self._calc_symbol or self.ids.instrument.text.strip()
        pd   = self._last_price_data or {}
        bid  = pd.get('bid', 0.0)
        ask  = pd.get('ask', 0.0)
        digs = pd.get('digits', DECIMALS.get(_base_sym(sym), 2))
        fmt  = '{:.' + str(digs) + 'f}'

        entry = float(data.get('entry')  or 0)
        sl    = float(data.get('sl')     or 0)
        tp1   = float(data.get('tp1')    or 0)
        tp2   = float(data.get('tp2')    or 0)
        tp3   = float(data.get('tp3')    or 0)
        try:
            lots = float(self.ids.lots.text.strip() or '0.01')
        except (ValueError, TypeError):
            lots = 0.01
        loss = abs(float(data.get('loss',    0)))
        p1   = float(data.get('profit1', 0))
        p2   = float(data.get('profit2', 0))
        p3   = float(data.get('profit3', 0))

        # Auto-recommend order type; fallback to market if no live price
        ot_default = (recommend_order_type(side, entry, bid, ask, digs)
                      if (bid or ask) else
                      ('MARKET_BUY' if side == 'BUY' else 'MARKET_SELL'))

        if side == 'BUY':
            order_types = [
                ('MARKET_BUY',     'Market Buy'),
                ('BUY_LIMIT',      'Buy Limit'),
                ('BUY_STOP',       'Buy Stop'),
                ('BUY_STOP_LIMIT', 'Stop Limit'),
            ]
        else:
            order_types = [
                ('MARKET_SELL',     'Market Sell'),
                ('SELL_LIMIT',      'Sell Limit'),
                ('SELL_STOP',       'Sell Stop'),
                ('SELL_STOP_LIMIT', 'Stop Limit'),
            ]

        default_tp = 'tp1' if tp1 > 0 else 'none'
        _state  = {'ot': ot_default, 'tp': default_tp}
        ot_btns = {}
        tp_btns = {}

        theme    = _app.theme_cls
        _acc     = theme.accent_color
        _bg      = theme.bg_dark
        _sel_ot  = [0.06, 0.48, 0.20, 1] if side == 'BUY' else [0.56, 0.06, 0.06, 1]
        _sel_tp  = theme.primary_color

        content = MDBoxLayout(orientation='vertical', spacing=dp(8),
                              padding=[dp(12), dp(8), dp(12), dp(8)],
                              size_hint_y=None)
        content.bind(minimum_height=content.setter('height'))

        # ── Header: symbol + entry/SL/lots ───────────────────────────────
        hdr = MDBoxLayout(orientation='vertical', spacing=dp(2),
                          size_hint_y=None, height=dp(72))
        hdr.add_widget(MDLabel(
            text='[b]' + side + '  ' + sym + '[/b]',
            markup=True, font_style='H6', halign='center',
            size_hint_y=None, height=dp(32)))
        hdr.add_widget(MDLabel(
            text=('Entry: ' + fmt.format(entry) + '   SL: ' + fmt.format(sl) + '\n' +
                  'Lots: ' + '{:.2f}'.format(lots) + '   Risk: $' + '{:.2f}'.format(loss)),
            halign='center', size_hint_y=None, height=dp(40), font_style='Body2'))
        # ── Live price context: bid/ask + reason why type was auto-selected ───
        if bid > 0 or ask > 0:
            _ot_reasons = {
                'MARKET_BUY':      'Entry ≈ Ask — market fill',
                'BUY_LIMIT':       'Entry < Ask — buy on pullback',
                'BUY_STOP':        'Entry > Ask — buy on breakout',
                'BUY_STOP_LIMIT':  'Entry > Ask — stop-limit buy',
                'MARKET_SELL':     'Entry ≈ Bid — market fill',
                'SELL_LIMIT':      'Entry > Bid — sell on rally',
                'SELL_STOP':       'Entry < Bid — sell on breakdown',
                'SELL_STOP_LIMIT': 'Entry < Bid — stop-limit sell',
            }
            _reason = _ot_reasons.get(ot_default, '')
            _price_ctx = (('Bid: ' + fmt.format(bid) + '   Ask: ' + fmt.format(ask)) +
                          ('   ·   ' + _reason if _reason else ''))
            hdr.add_widget(MDLabel(
                text='[i]' + _price_ctx + '[/i]',
                markup=True, halign='center', font_style='Caption',
                theme_text_color='Secondary',
                size_hint_y=None, height=dp(22)))
            hdr.height = dp(94)
        content.add_widget(hdr)

        # ── Divider ───────────────────────────────────────────────────────
        def _div():
            return MDBoxLayout(size_hint_y=None, height=dp(1), md_bg_color=[.4,.4,.4,.4])

        content.add_widget(_div())

        # ── Order execution: 2 plain-English buttons ─────────────────────
        # "FILL NOW" = market, "AT ENTRY" = correct pending type auto-picked.
        # User never needs to know Limit vs Stop vs Stop-Limit.
        _mkt_key     = 'MARKET_BUY' if side == 'BUY' else 'MARKET_SELL'
        _mkt_label   = 'Market Buy' if side == 'BUY' else 'Market Sell'
        # pending type is already in ot_default (recommend_order_type ran above)
        _pend_key    = ot_default if ot_default not in (_mkt_key,) else (
                        'BUY_LIMIT' if side == 'BUY' else 'SELL_LIMIT')
        _is_market   = ot_default == _mkt_key
        # Human-readable reason for the pending type
        _pend_reason = {
            'BUY_LIMIT':       'entry < ask — buy on pullback',
            'BUY_STOP':        'entry > ask — buy on breakout',
            'BUY_STOP_LIMIT':  'entry > ask — stop-limit buy',
            'SELL_LIMIT':      'entry > bid — sell on rally',
            'SELL_STOP':       'entry < bid — sell on breakdown',
            'SELL_STOP_LIMIT': 'entry < bid — stop-limit sell',
        }.get(_pend_key, '')

        # hint label (updated when user taps a button)
        _hint_lbl = MDLabel(
            text='', halign='center', font_style='Caption',
            theme_text_color='Secondary',
            size_hint_y=None, height=dp(18))

        def _set_hint(is_mkt):
            if is_mkt:
                _hint_lbl.text = 'Fills instantly at current market price'
            else:
                _hint_lbl.text = (f'Pending at {fmt.format(entry)}  ·  {_pend_reason}'
                                  if entry else f'Pending order  ·  {_pend_reason}')

        def _tap_now(inst):
            _state['ot'] = _mkt_key
            btn_now.md_bg_color  = _sel_ot
            btn_pend.md_bg_color = _bg
            _set_hint(True)

        def _tap_pend(inst):
            _state['ot'] = _pend_key
            btn_now.md_bg_color  = _bg
            btn_pend.md_bg_color = _sel_ot
            _set_hint(False)

        content.add_widget(MDLabel(
            text='When to execute:', halign='left',
            size_hint_y=None, height=dp(22), font_style='Subtitle2'))

        ot_row = MDBoxLayout(orientation='horizontal', spacing=dp(8),
                             size_hint_y=None, height=dp(52))

        btn_now = MDRaisedButton(
            text='FILL NOW\n' + _mkt_label,
            size_hint_x=1, size_hint_y=None, height=dp(52),
            font_size='11sp', halign='center',
            md_bg_color=_sel_ot if _is_market else _bg)
        btn_now.bind(on_release=_tap_now)

        _pend_price_str = fmt.format(entry) if entry else 'entry price'
        btn_pend = MDRaisedButton(
            text='WAIT FOR\n' + _pend_price_str,
            size_hint_x=1, size_hint_y=None, height=dp(52),
            font_size='11sp', halign='center',
            md_bg_color=_sel_ot if not _is_market else _bg)
        if entry <= 0:
            btn_pend.disabled = True   # no entry set → pending impossible
        btn_pend.bind(on_release=_tap_pend)

        ot_row.add_widget(btn_now)
        ot_row.add_widget(btn_pend)
        content.add_widget(ot_row)
        _set_hint(_is_market)
        content.add_widget(_hint_lbl)
        content.add_widget(_div())

        # keep ot_btns dict alive for pre-existing code that references it
        ot_btns = {}

        # ── Take Profit buttons ───────────────────────────────────────────
        content.add_widget(MDLabel(
            text='Take Profit:', halign='left',
            size_hint_y=None, height=dp(22), font_style='Subtitle2'))

        place_btn = [None]   # mutable ref so _upd() can access before assignment

        def _upd():
            sel      = _state['tp']
            tp_price = {'tp1': tp1, 'tp2': tp2, 'tp3': tp3}.get(sel, 0)
            profit   = {'tp1': p1,  'tp2': p2,  'tp3': p3 }.get(sel, 0)
            if place_btn[0]:
                if sel == 'none':
                    place_btn[0].text = 'PLACE ORDER  \u00b7  No TP'
                else:
                    place_btn[0].text = ('PLACE ORDER  \u00b7  ' + sel.upper() + ': ' +
                                         fmt.format(tp_price) + '  +$' + '{:.0f}'.format(profit))

        def _tap_tp(key):
            _state['tp'] = key
            for k, b in tp_btns.items():
                b.md_bg_color = _sel_tp if k == key else _bg
            _upd()

        def _make_tp_btn(key, price, profit, rr):
            lbl = (key.upper() + ':  ' + fmt.format(price) +
                   '   +$' + '{:.0f}'.format(profit) + '   (' + rr + ')')
            b = MDRaisedButton(text=lbl, size_hint_x=1, size_hint_y=None, height=dp(44),
                               md_bg_color=_sel_tp if key == _state['tp'] else _bg)
            b.bind(on_release=lambda inst, k=key: _tap_tp(k))
            tp_btns[key] = b
            return b

        # No TP option
        b_no = MDRaisedButton(
            text='No TP', size_hint_x=1, size_hint_y=None, height=dp(44),
            md_bg_color=_sel_tp if _state['tp'] == 'none' else _bg)
        b_no.bind(on_release=lambda inst: _tap_tp('none'))
        tp_btns['none'] = b_no
        content.add_widget(b_no)

        if tp1 > 0: content.add_widget(_make_tp_btn('tp1', tp1, p1, '1:2'))
        if tp2 > 0: content.add_widget(_make_tp_btn('tp2', tp2, p2, '1:3'))
        if tp3 > 0: content.add_widget(_make_tp_btn('tp3', tp3, p3, '1:4'))

        # ── Place Order button ────────────────────────────────────────────
        content.add_widget(MDBoxLayout(size_hint_y=None, height=dp(6)))
        pb = MDRaisedButton(
            text='PLACE ORDER', size_hint_x=1, size_hint_y=None,
            height=dp(52), md_bg_color=_acc)
        place_btn[0] = pb
        _upd()

        def _on_place(inst):
            if self._popup_dialog:
                self._popup_dialog.dismiss()
            self._place_from_popup(side, data, _state['ot'], _state['tp'])

        pb.bind(on_release=_on_place)
        content.add_widget(pb)

        dlg = MDDialog(title='', type='custom', content_cls=content,
                       buttons=[MDFlatButton(text='CANCEL',
                               on_release=lambda x: dlg.dismiss())])
        self._popup_dialog = dlg
        dlg.bind(on_dismiss=lambda inst: setattr(self, '_popup_dialog', None))
        dlg.open()

    _PENDING_ORDER_TYPES = {"BUY_LIMIT","BUY_STOP","SELL_LIMIT","SELL_STOP",
                            "BUY_STOP_LIMIT","SELL_STOP_LIMIT"}

    def _place_from_popup(self, side, data, order_type, tp_key):
        log.debug("[EVENT] PLACE_POPUP side=%s ot=%s tp=%s ui_locked=%s posting=%s", side, order_type, tp_key, self._ui_locked, self._mt5_posting)
        if self._ui_locked:
            self._show_snackbar("UI busy — wait for fetch to finish"); return
        if self._mt5_posting:
            self._show_snackbar("Order already in progress"); return
        if not self._mt5_enabled:
            self._show_snackbar("MT5 disabled — enable in Settings"); return
        # Pending orders require a valid entry price
        if order_type in self._PENDING_ORDER_TYPES:
            try:
                entry = float(data.get('entry') or 0)
            except (ValueError, TypeError):
                entry = 0.0
            if entry <= 0:
                self._show_snackbar("Enter a valid entry price for pending orders"); return
        # Validate lot size before sending
        try:
            lots = float(self.ids.lots.text.strip() or '0')
        except (ValueError, TypeError):
            lots = 0.0
        if lots < 0.01:
            self._show_snackbar("Minimum lot size is 0.01"); return
        self._execute_mt5_order(side, data, order_type, tp_key)

    def _execute_mt5_order(self, direction, data, order_type, tp_key):
        import time as _time
        if self._mt5_posting: return
        self._mt5_posting    = True
        self._mt5_posting_ts = _time.monotonic()

        # ── Guard: symbol lookup can raise — must release posting lock ────────
        try:
            base = (self._calc_symbol or self.ids.instrument.text).strip().upper()
            sym  = self._broker_sym(base)
        except Exception as _e:
            self._mt5_posting = False
            self._show_snackbar(f"Symbol error: {_e}"); return
        dec = DECIMALS.get(_base_sym(sym), 2)

        try:
            volume = float(self.ids.lots.text.strip() or "0.01")
        except ValueError:
            self._mt5_posting = False
            self._show_snackbar("Invalid lot size"); return

        try:
            price = float(data.get('entry') or 0)
            sl    = float(data.get('sl')    or 0)
            tp    = {"tp1": float(data.get('tp1') or 0),
                     "tp2": float(data.get('tp2') or 0),
                     "tp3": float(data.get('tp3') or 0)}.get(tp_key, 0.0)
        except (ValueError, TypeError):
            self._mt5_posting = False
            self._show_snackbar("Invalid price/SL/TP value"); return

        # ── Pre-flight: re-validate order type against CURRENT live price ──────
        # Price can move while the user sits in the dialog.
        #
        # Rule:
        #   pending → different pending  → auto-correct silently (no slippage risk)
        #   pending → market             → BLOCK: warn user, let them choose
        #
        # We NEVER force a market order — slippage on XAUUSD can be significant.
        if order_type in self._PENDING_ORDER_TYPES and price > 0:
            pd_now  = self._last_price_data or {}
            bid_now = pd_now.get('bid', 0.0)
            ask_now = pd_now.get('ask', 0.0)
            if bid_now > 0 or ask_now > 0:
                _side  = 'BUY' if 'BUY' in order_type else 'SELL'
                _fixed = recommend_order_type(_side, price, bid_now, ask_now, dec)
                if _fixed != order_type:
                    _fmt = '{:.' + str(dec) + 'f}'
                    if _fixed in self._PENDING_ORDER_TYPES:
                        # Safe pending→pending flip (e.g., Sell Limit→Sell Stop)
                        _old_lbl = order_type.replace('_', ' ').title()
                        _new_lbl = _fixed.replace('_', ' ').title()
                        log.debug("ORDER pending-correct %s→%s bid=%s ask=%s entry=%s",
                                  _old_lbl, _new_lbl, bid_now, ask_now, price)
                        order_type = _fixed
                        self._show_snackbar(
                            f"⚡ Price moved — auto-corrected to {_new_lbl}")
                    else:
                        # Would require market fill — block, show price context
                        _mkt = _fmt.format(bid_now if 'SELL' in order_type else ask_now)
                        _ent = _fmt.format(price)
                        log.debug("ORDER blocked: %s→market bid=%s ask=%s entry=%s",
                                  order_type, bid_now, ask_now, price)
                        self._mt5_posting = False
                        self._show_snackbar(
                            f"Entry {_ent} is at market ({('Bid' if 'SELL' in order_type else 'Ask')} {_mkt})"
                            f" — tap Market {'Sell' if 'SELL' in order_type else 'Buy'} if you accept slippage")
                        return

        log.debug("[EVENT] ORDER_SEND sym=%s type=%s vol=%s price=%s sl=%s tp=%s tp_key=%s", sym, order_type, volume, price, sl, tp, tp_key)
        self._show_snackbar(f"Sending {order_type.replace('_',' ')}…")
        _r = weakref.ref(self)

        def _ok(res):
            def _ui(dt):
                r = _r()
                if not r: return
                r._mt5_posting = False
                ticket = res.get('ticket', '?')
                log.debug("[EVENT] ORDER_OK ticket=%s", ticket)
                r._show_snackbar(f"✅  #{ticket} placed in MT5")
            Clock.schedule_once(_ui, 0)

        def _err(msg):
            def _ui(dt):
                r = _r()
                if not r: return
                r._mt5_posting = False
                log.debug("[EVENT] ORDER_ERR %s", msg)
                r._show_snackbar(f"MT5: {msg}")
            Clock.schedule_once(_ui, 0)

        # ── Dispatch — narrow try so any crash here still clears posting lock ─
        try:
            mt5_place_order(
                symbol=sym, order_type=order_type, volume=round(volume,2),
                price=round(price,dec), sl=round(sl,dec), tp=round(tp,dec),
                comment="SL Calculator", on_success=_ok, on_error=_err,
            )
        except Exception as _e:
            self._mt5_posting = False
            log.error("mt5_place_order dispatch failed: %s", _e)
            self._show_snackbar(f"Order error: {_e}")

    # ── reset ──────────────────────────────────────────────────────────────
    def reset(self):
        if self._mt5_posting:
            self._show_snackbar("Cannot reset while posting — please wait")
            return
        # End input session (discards uncommitted buffer on reset)
        self._end_input_session(commit=False)
        self._dismiss_keyboard()
        self._history_cache_key = None   # force history rebuild after reset
        for fid in ("atr", "lots", "entry", "account", "risk"):
            self.ids[fid].text = ""
        self._reset_risk_display()
        self._reset_level_display()
        self.buy = {}
        self.sell = {}
        self._calc_symbol = ""
        self._set_levels_visible(False)
        Clock.schedule_once(lambda dt: self._apply_mt5_visuals(), 0)
        self._clear_stale()
        self._set_status("Ready")

    # ── instrument change ──────────────────────────────────────────────────
    # called by app.set_item — clears trade fields, keeps account/risk
    def on_instrument_change(self, text: str):
        if not self._app_ready:
            return
        try:
            for fid in ("atr", "lots", "entry"):
                self.ids[fid].text = ""
        except Exception as e:
            log.debug("on_instrument_change ids: %s", e); return
        self._reset_risk_display()
        self._reset_level_display()
        self.buy = {}
        self.sell = {}
        self._calc_symbol = text           # keep consistent
        self._last_candle_time.clear()    # new symbol = fresh fetch, no dedup block
        self._set_levels_visible(False)
        self._clear_stale()
        self._set_status(f"Instrument: {text}")
        self._update_broker_sym_label()
        # Fire one auto-fetch immediately for the new symbol (timer keeps same interval)
        if self._mt5_connected and self._mt5_enabled:
            Clock.schedule_once(self._auto_fetch_tick, 0.5)


# ═══════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════

