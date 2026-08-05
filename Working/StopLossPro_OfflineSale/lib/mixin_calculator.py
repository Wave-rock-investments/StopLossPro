# mixin_calculator.py — on_calculate, auto-lot, theme toggle, risk/level display, monthly stats.
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


class CalculatorMixin:
    def toggle_theme(self):
        """Switch between Dark and Light mode, persist choice, refresh numpad colors."""
        app = MDApp.get_running_app()
        self._theme_style = "Light" if self._theme_style == "Dark" else "Dark"
        app.theme_cls.theme_style = self._theme_style

        # Update topbar icon: sun for dark→light action, moon for light→dark
        try:
            icon = "weather-night" if self._theme_style == "Light" else "weather-sunny"
            self.ids.topbar.right_action_items = [[icon, lambda x: self.toggle_theme(), "Toggle Theme"]]
        except Exception as e:
            log.debug("toggle_theme icon: %s", e)

        # Update ALL hardcoded colors to match new theme
        self._apply_full_theme()

        # Persist
        try:
            self._store.put('ui_prefs',
                theme=self._theme_style,
                numpad_visible=getattr(self, '_numpad_visible', True))
        except Exception as e:
            log.warning("toggle_theme save: %s", e)

        self._show_snackbar(f"{'Light' if self._theme_style=='Light' else 'Dark'} mode enabled")

    def _apply_numpad_theme(self):
        """Alias kept for compatibility — delegates to _apply_full_theme."""
        self._apply_full_theme()

    def _apply_full_theme(self):
        """Apply ALL hardcoded colors for current theme (Dark / Light)."""
        is_dark = (self._theme_style == "Dark")

        # Numpad bar + panel — keep in sync with KV expressions
        bar_bg   = [0.03, 0.03, 0.04, 1] if is_dark else [0.86, 0.86, 0.88, 1]
        panel_bg = [0.05, 0.05, 0.06, 1] if is_dark else [0.92, 0.92, 0.93, 1]
        try:
            self.ids.numpad_bar.md_bg_color   = bar_bg
            self.ids.numpad_panel.md_bg_color = panel_bg
        except Exception as e:
            log.debug("theme numpad: %s", e)

        # BUY card — Dhan-style carbon green tint
        buy_bg = [0.04, 0.10, 0.06, 1] if is_dark else [0.94, 1.0, 0.96, 1]
        try:
            self.ids.buy_card.md_bg_color = buy_bg
        except Exception as e:
            log.debug("theme buy_card: %s", e)

        # SELL card — Dhan-style carbon red tint
        sell_bg = [0.10, 0.04, 0.04, 1] if is_dark else [1.0, 0.94, 0.94, 1]
        try:
            self.ids.sell_card.md_bg_color = sell_bg
        except Exception as e:
            log.debug("theme sell_card: %s", e)


    # ── Numpad minimize / expand ────────────────────────────────────────────
    # Saved row heights for toggle restore
    _np_row_heights = [48, 48, 48, 48]  # dp values, one per row

    def toggle_numpad(self):
        """Slide numpad open or closed. Collapses row heights to 0 to prevent
        Kivy overflow — MDBoxLayout does not clip children, so opacity alone
        would still render rows beyond the panel boundary on Android."""

        self._numpad_animating = False  # reset before starting new animation
        self._numpad_visible = not self._numpad_visible

        # End any active input session before hiding
        if not self._numpad_visible:
            self._end_input_session(commit=True)

        chevron = "chevron-down" if self._numpad_visible else "chevron-up"

        self._numpad_animating = True
        def _anim_done(*a):
            self._numpad_animating = False

        try:
            # Collapse or restore each button row height
            row_ids = ('np_row1', 'np_row2', 'np_row3', 'np_row4')
            for rid in row_ids:
                try:
                    row = self.ids[rid]
                    Animation.cancel_all(row)   # cancel stacked animations first
                    if self._numpad_visible:
                        row.disabled = False
                        Animation(height=dp(48), opacity=1,
                                  duration=0.20, t='out_cubic').start(row)
                    else:
                        Animation(height=dp(0), opacity=0,
                                  duration=0.15, t='in_cubic').start(row)
                        row.disabled = True  # BUG 9: prevent Kivy processing hidden rows
                except Exception as e:
                    log.debug("toggle_numpad row %s: %s", rid, e)

            # Animate panel height (bar only = 38dp, full = 252dp)
            target_h = dp(_KV_NUMPAD_H) if self._numpad_visible else dp(_KV_NUMPAD_BAR)
            Animation.cancel_all(self.ids.numpad_panel)
            _pa = Animation(height=target_h, duration=0.22, t='out_cubic')
            _pa.bind(on_complete=_anim_done)
            _pa.start(self.ids.numpad_panel)

            self.ids.numpad_toggle_btn.icon = chevron

        except Exception as e:
            log.warning("toggle_numpad: %s", e)

        # Persist preference
        try:
            self._store.put('ui_prefs',
                theme=self._theme_style,
                numpad_visible=self._numpad_visible)
        except Exception as e:
            log.warning("toggle_numpad save: %s", e)

    def on_calculate(self):
        log.debug("[EVENT] CALCULATE tapped")
        from time import monotonic
        now = monotonic()
        if now - self._last_calc_time < 0.15:
            log.debug("[EVENT] CALCULATE debounced")
            return
        self._last_calc_time = now
        self._dismiss_keyboard()
        if self._ui_locked:
            return
        if self._input_state == "EDITING":
            self._end_input_session(commit=True)
        if not self._calc_pending:
            self._calc_pending = True
            Clock.schedule_once(self._do_calculate, 0)

    def _do_calculate(self, dt):
        self._calc_pending = False
        self._ui_locked    = True
        try:
            ids    = self.ids
            atr_t  = ids.atr.text.strip().rstrip(".")
            if not atr_t:
                self._set_status("ATR is required", "error")
                return
            lots_t  = ids.lots.text.strip()
            entry_t = ids.entry.text.strip()
            sym     = ids.instrument.text.strip()
            if not sym:
                self._set_status("Select an instrument", "error")
                return

            atr  = float(atr_t)
            lots = float(lots_t) if lots_t else 0.0

            log.debug("[EVENT] CALC_RUN atr=%r lots=%r entry=%r sym=%r", atr_t, lots_t, entry_t, sym)

            entry = None
            if entry_t:
                entry = float(entry_t)

            if lots <= 0:
                self._set_status("Lots must be > 0", "error")
                return

            setup = calc_setup(
                atr=atr, lots=lots, symbol=sym,
                sl_m=self.sl_mult, entry=entry, wr_pct=self.win_rate)
            self._calc_symbol = sym
            self.buy  = setup.buy
            self.sell = setup.sell

            d      = DECIMALS.get(_base_sym(sym), 2)
            rr_str = "1:2 / 1:3 / 1:4"
            be_str = ""
            if entry:
                be_str = f"{entry:.{d}f}"

            ids.sl_distance.text     = f"{setup.sl_dist:.{d}f}"
            ids.tp1_distance.text    = f"{setup.tp1_dist:.{d}f}"
            ids.tp2_distance.text    = f"{setup.tp2_dist:.{d}f}"
            ids.tp3_distance.text    = f"{setup.tp3_dist:.{d}f}"
            ids.loss.text            = f"${setup.loss:,.2f}"
            ids.tp1.text             = f"${setup.profit1:,.2f}"
            ids.tp2.text             = f"${setup.profit2:,.2f}"
            ids.tp3.text             = f"${setup.profit3:,.2f}"
            ids.blended_profit.text  = f"${setup.blended:,.2f}"
            ids.expected_value.text  = f"${setup.ev:+,.2f}"

            if entry and setup.buy:
                self.ids.buy_entry.text  = f"{setup.buy['entry']:.{d}f}"
                self.ids.buy_sl.text     = f"{setup.buy['sl']:.{d}f}"
                self.ids.buy_tp1.text    = f"{setup.buy['tp1']:.{d}f}"
                self.ids.buy_tp2.text    = f"{setup.buy['tp2']:.{d}f}"
                self.ids.buy_tp3.text    = f"{setup.buy['tp3']:.{d}f}"
                self.ids.buy_rr.text     = rr_str
                self.ids.buy_be.text     = be_str
                self.ids.sell_entry.text = f"{setup.sell['entry']:.{d}f}"
                self.ids.sell_sl.text    = f"{setup.sell['sl']:.{d}f}"
                self.ids.sell_tp1.text   = f"{setup.sell['tp1']:.{d}f}"
                self.ids.sell_tp2.text   = f"{setup.sell['tp2']:.{d}f}"
                self.ids.sell_tp3.text   = f"{setup.sell['tp3']:.{d}f}"
                self.ids.sell_rr.text    = rr_str
                self.ids.sell_be.text    = be_str
            else:
                self._reset_level_display()

            Clock.schedule_once(lambda dt2: self._set_levels_visible(True), 0)
            Clock.schedule_once(lambda dt2: self._apply_mt5_visuals(), 0)
            log.debug("[EVENT] CALC_OK sl=%s tp1=%s loss=%s p1=%s",
                      setup.sl_dist, setup.tp1_dist, setup.loss, setup.profit1)
            self._set_status("Calculation complete ✓", "success")
            self._clear_stale()
            # Scroll RISK ESTIMATE into view after calculation
            Clock.schedule_once(
                lambda dt: self.ids.calc_scroll.scroll_to(
                    self.ids.expected_value, padding=dp(20)
                ),
                0.05
            )
            self._save_last_values()
            self._add_to_history(setup)

        except (ValueError, ArithmeticError) as e:
            log.warning("Calculate ValueError: %s", e)
            log.debug("[EVENT] CALC_ERR %s", e)
            self._set_status(str(e) if str(e) else "Invalid number — check all fields", "error")
        except KeyError as e:
            log.warning("Calculate KeyError (unknown symbol): %s", e)
            self._set_status(f"Unknown instrument: {self.ids.instrument.text}", "error")
        except ZeroDivisionError as e:
            log.warning("Calculate ZeroDivisionError: %s", e)
            self._set_status("Division error — check inputs", "error")
        except Exception as e:
            log.warning("Calculate unexpected: %s", e)
            self._set_status(str(e) if str(e) else "Calculation error", "error")
        finally:
            self._ui_locked = False

    # ── auto lot ───────────────────────────────────────────────────────────
    def on_auto_lot(self):
        log.debug("[EVENT] AUTOLOT tapped")
        from time import monotonic
        now = monotonic()
        if now - self._last_autolot_time < 0.15:
            log.debug("[EVENT] AUTOLOT debounced")
            return
        self._last_autolot_time = now
        self._dismiss_keyboard()
        if self._ui_locked:
            return
        if self._input_state == "EDITING":
            self._end_input_session(commit=True)
        if not self._autolot_pending:
            self._autolot_pending = True
            Clock.schedule_once(self._do_auto_lot, 0)

    def _do_auto_lot(self, dt):
        self._autolot_pending = False
        try:
            atr_t  = self.ids.atr.text.strip()
            acc_t  = self.ids.account.text.strip()
            risk_t = self.ids.risk.text.strip()
            if not atr_t:  self._set_status("ATR is required",     "error"); return
            if not acc_t:  self._set_status("Account is required", "error"); return
            if not risk_t: self._set_status("Risk % is required",  "error"); return

            atr = float(atr_t)
            acc = float(acc_t)
            rsk = float(risk_t)
            if not math.isfinite(atr) or atr <= 0:
                self._set_status("ATR must be a valid positive number", "error"); return
            if atr > 100000:
                self._set_status("ATR too large — check instrument", "error"); return
            if not math.isfinite(acc) or acc <= 0:
                self._set_status("Account must be > 0", "error"); return
            if acc > 1_000_000_000:
                self._set_status("Account size exceeds limit ($1B)", "error"); return
            if not (0 < rsk <= 100):
                self._set_status("Risk must be between 0 and 100%", "error"); return

            sym  = self.ids.instrument.text
            lots = calc_auto_lot(atr, acc, rsk, sym, self.sl_mult)
            # calc_auto_lot returns full mathematical lot — clusters handle broker max
            self.ids.lots.text = f"{lots:.2f}"
            self._set_status(f"Auto lot: {lots:.2f}", "success")
            self.on_calculate()

        except ValueError as e:
            log.warning("AutoLot ValueError: %s", e)
            self._set_status(str(e) if str(e) else "Invalid inputs", "error")
        except KeyError as e:
            log.warning("AutoLot KeyError: %s", e)
            self._set_status(f"Unknown instrument: {self.ids.instrument.text}", "error")
        except ZeroDivisionError as e:
            log.warning("AutoLot ZeroDivisionError: %s", e)
            self._set_status("Division by zero — check ATR", "error")

    # ── share / copy ───────────────────────────────────────────────────────
    # ── individual price copy ──────────────────────────────────────────────
    def copy_price(self, price_text: str):
        """Copy a single price value — called by copy icon buttons on each row."""
        val = price_text.strip()
        if not val or val == "—":
            return
        Clipboard.copy(val)
        self._show_snackbar(f"✓ {val} copied")

    # ── close trade ────────────────────────────────────────────────────────


    # ── monthly stats — in-memory, disk is backup only ────────────────────
    def _get_monthly_stats(self) -> dict:
        # BUG 7 FIX: check month rollover on every read, not just on update
        now = datetime.date.today()
        current_key = f"{now.year}_{now.month:02d}"
        if self._stats_key and self._stats_key != current_key:
            self._stats_wins   = 0
            self._stats_losses = 0
            self._stats_key    = current_key
        return {'wins': self._stats_wins, 'losses': self._stats_losses}

    def _update_monthly_stats(self, won: bool) -> dict:
        """Increment in-memory counter, write to disk as backup. Returns updated."""
        try:
            now = datetime.date.today()
            current_key = f"{now.year}_{now.month:02d}"
            if self._stats_key != current_key:
                self._stats_wins   = 0
                self._stats_losses = 0
                self._stats_key    = current_key
            if won:
                self._stats_wins += 1
            else:
                self._stats_losses += 1
            try:
                self._store.put('monthly_stats',
                    key=current_key,
                    wins=self._stats_wins,
                    losses=self._stats_losses)
            except Exception as e:
                log.warning("_update_monthly_stats store: %s", e)
            return {'wins': self._stats_wins, 'losses': self._stats_losses}
        except Exception as e:
            log.warning("_update_monthly_stats: %s", e)
            return {'wins': self._stats_wins, 'losses': self._stats_losses}
    def _apply_mode_visuals(self):
        Clock.schedule_once(lambda dt: self._apply_mt5_visuals(), 0)

    @staticmethod
    def _keystore_encrypt(alias: str, plaintext: str) -> str:
        """Encrypt plaintext using Android Keystore (AES-256-GCM).
        Falls back to plaintext on non-Android or if Keystore unavailable.
        The alias identifies the key in the Keystore — unique per field.
        On failure (e.g. first run), returns plaintext unchanged.
        """
        if platform != 'android' or not plaintext:
            # BUG 15 FIX: warn clearly when storing credentials in plaintext
            if plaintext and platform != 'android':
                log.warning(
                    "SECURITY: Keystore unavailable on %s — "
                    "'%s' stored as PLAINTEXT. "
                    "Credentials are NOT encrypted on this platform.",
                    platform, alias)
            return plaintext
        try:
            from jnius import autoclass  # type: ignore[import]
            KeyGen    = autoclass('javax.crypto.KeyGenerator')
            KeyStore  = autoclass('java.security.KeyStore')
            Cipher    = autoclass('javax.crypto.Cipher')
            KGP       = autoclass('android.security.keystore.KeyGenParameterSpec$Builder')
            KGP_Purp  = autoclass('android.security.keystore.KeyProperties')

            ks = KeyStore.getInstance('AndroidKeyStore')
            ks.load(None)
            if not ks.containsAlias(alias):
                kg = KeyGen.getInstance(
                    KGP_Purp.KEY_ALGORITHM_AES, 'AndroidKeyStore')
                spec = KGP(alias,
                    KGP_Purp.PURPOSE_ENCRYPT | KGP_Purp.PURPOSE_DECRYPT
                ).setBlockModes(KGP_Purp.BLOCK_MODE_GCM
                ).setEncryptionPaddings(KGP_Purp.ENCRYPTION_PADDING_NONE
                ).setKeySize(256).build()
                kg.init(spec)
                kg.generateKey()

            key    = ks.getKey(alias, None)
            cipher = Cipher.getInstance('AES/GCM/NoPadding')
            cipher.init(Cipher.ENCRYPT_MODE, key)
            enc    = cipher.doFinal(plaintext.encode('utf-8'))
            iv     = cipher.getIV()
            # Store iv:ciphertext as Base64
            return base64.b64encode(bytes(iv) + bytes(enc)).decode('ascii')
        except Exception as e:
            log.debug("_keystore_encrypt: %s — storing plaintext", e)
            return plaintext

    @staticmethod
    def _keystore_decrypt(alias: str, ciphertext: str) -> str:
        """Decrypt via Android Keystore. Returns plaintext on any failure."""
        if platform != 'android' or not ciphertext:
            return ciphertext
        try:
            data = base64.b64decode(ciphertext)
            if len(data) < 13:     # too short to be encrypted (IV=12 + tag)
                return ciphertext  # plaintext fallback (first run after upgrade)
            from jnius import autoclass  # type: ignore[import]
            KeyStore = autoclass('java.security.KeyStore')
            Cipher  = autoclass('javax.crypto.Cipher')
            GCMSpec = autoclass('javax.crypto.spec.GCMParameterSpec')
            ks = KeyStore.getInstance('AndroidKeyStore')
            ks.load(None)
            if not ks.containsAlias(alias):
                return ciphertext
            key    = ks.getKey(alias, None)
            iv     = data[:12]
            enc    = data[12:]
            cipher = Cipher.getInstance('AES/GCM/NoPadding')
            spec   = GCMSpec(128, iv)
            cipher.init(Cipher.DECRYPT_MODE, key, spec)
            dec    = cipher.doFinal(enc)
            return bytes(dec).decode('utf-8')
        except Exception as e:
            log.debug('_keystore_decrypt: %s', e)
            return ciphertext

    # BUG 18 NOTE: MT5 methods below should be extracted to a _MT5Mixin class.
    # Root(MDBoxLayout) exceeds 1200 lines. When refactoring, create:
#   class _MT5Mixin: (contains all methods prefixed mt5/on_mt5_)
#   class Root(_MT5Mixin, MDBoxLayout): pass
    # ═══════════════════════════════════════════════════════════════════════════
    # MT5 DIRECT ORDERS — personal mode only
    # Direct MT5 connection via MetaTrader5 Python library
    # ═══════════════════════════════════════════════════════════════════════════


