"""Activation shim — PHASE 12 replacement.

The previous implementation (1,179 lines, recoverable from git at the
phase0-clean-baseline commit) is GONE, along with every mechanism it relied on:

  REMOVED  Gist `approved_ids.txt` allowlist          — client-trusted licence state
  REMOVED  Gist `active_sessions.txt` session claim   — read-modify-write on a shared file
  REMOVED  Cloudflare Worker writes                   — authenticated on a User-Agent prefix
  REMOVED  ntfy `APPROVED` activation listener        — public topic; curl = free licence
  REMOVED  ntfy `REVOKE` kill listener                — public topic; anyone could kill an app
  REMOVED  ntfy heartbeat telemetry                   — published MT5 login, balance, equity,
                                                        open positions, P/L and GPS to a
                                                        world-readable URL
  REMOVED  GPS / Windows Location Services collection — collected with no consent gate
  REMOVED  ipinfo.io geolocation lookups              — no licensing purpose
  REMOVED  MAC address, hostname, username, hardware  — collected far beyond need
  REMOVED  `~/.slcalc_cache` plaintext licence cache  — user-writable = self-authorisation
  REMOVED  hardware-fingerprint machine ID as identity

This module now delegates entirely to `licensing.LicensingProvider`. It keeps
the old public names so the rest of the app imports unchanged, but the
behaviour behind them is server-authoritative. Nothing here can grant access.

The risk engine is untouched by any of this.
"""
from __future__ import annotations

import hashlib
import logging
import platform as _platform

log = logging.getLogger("StopLossPro.activation")

from licensing import (  # noqa: E402
    LicenceState,
    LicensingProvider,
    get_or_create_device_keypair,
)

_provider: LicensingProvider | None = None


def get_provider() -> LicensingProvider:
    global _provider
    if _provider is None:
        _provider = LicensingProvider()
    return _provider


# ══════════════════════════════════════════════════════════════════════════
# Compatibility surface — same names the app already imports
# ══════════════════════════════════════════════════════════════════════════
def _get_machine_id() -> str:
    """Retained ONLY as a weak advisory label shown in the admin panel.

    This is no longer an identity or an authorisation input. Device identity
    is now an Ed25519 keypair sealed to the Windows account (see
    licensing.get_or_create_device_keypair). A customer can replace their
    motherboard without losing access, and copying the install folder to
    another machine does not carry identity with it.
    """
    try:
        return f"{_platform.node()}/{_platform.system()}"[:64]
    except Exception:
        return "unknown"


def _is_activated() -> bool:
    """True only if the SERVER most recently said so, or we are inside the
    bounded offline grace window. There is no local flag that grants access."""
    return get_provider().state.authorised


def _register_if_new() -> None:
    """No-op. Registration telemetry has been removed entirely.

    Previously this collected hostname, username, MAC, CPU, RAM, screen
    resolution, OS build, public IP, ISP, city/region/country and GPS
    coordinates, then published all of it to a public ntfy topic on every
    install. None of it served a licensing purpose.
    """
    return None


def _send_heartbeat() -> None:
    """No-op. Superseded by the authenticated heartbeat in LicensingProvider.

    The old version posted MT5 broker, account login number, balance, equity,
    currency, every open position (symbol, direction, volume, P/L, entry and
    current price) plus GPS coordinates to a world-readable URL.
    """
    return None


def _start_revoke_listener() -> None:
    """No-op. Revocation now arrives through the authenticated heartbeat.

    The old listener polled a public ntfy topic whose name was derived from
    the machine ID and terminated the app on any message containing 'REVOKE',
    so anyone who knew a customer's machine ID could kill their application
    repeatedly.
    """
    return None


def _start_session_heartbeat() -> None:
    """Start the authenticated heartbeat loop.

    Single-active-session enforcement now lives in the database as a partial
    unique index, not in a shared text file edited by every client.
    """
    get_provider().start()


def _show_activation_blocker() -> bool:
    """Blocking sign-in dialog. Returns True if the app may continue.

    Presents the three states the customer can actually be in, and keeps them
    distinct rather than collapsing everything into 'not licensed':
      * wrong credentials / suspended / expired  -> server said no
      * already active on another device         -> offer MFA-gated takeover
      * server unreachable                       -> say so honestly
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        log.error("tkinter unavailable — cannot show sign-in dialog")
        return False

    provider = get_provider()
    _, device_pub = get_or_create_device_keypair()
    result = {"ok": False}

    root = tk.Tk()
    root.title("StopLoss Pro — Sign in")
    root.geometry("400x470")
    root.resizable(False, False)
    root.configure(bg="#0d0d0f")
    root.attributes("-topmost", True)
    root.eval("tk::PlaceWindow . center")

    def lbl(text, size=10, fg="#cfcfcf", pady=(0, 0)):
        tk.Label(root, text=text, bg="#0d0d0f", fg=fg,
                 font=("Segoe UI", size)).pack(pady=pady)

    lbl("StopLoss Pro", 16, "#ffffff", (26, 2))
    lbl("Sign in to activate this device", 9, "#8a8a8a", (0, 14))

    frm = tk.Frame(root, bg="#0d0d0f")
    frm.pack(padx=30, fill="x")

    def field(label, show=None):
        tk.Label(frm, text=label, bg="#0d0d0f", fg="#8a8a8a",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 2))
        e = tk.Entry(frm, show=show, bg="#16161a", fg="#ffffff",
                     insertbackground="#ffffff", relief="flat", font=("Segoe UI", 11))
        e.pack(fill="x", ipady=6)
        return e

    e_email = field("Email")
    e_pass = field("Password", show="•")

    totp_frame = tk.Frame(root, bg="#0d0d0f")
    tk.Label(totp_frame, text="Authenticator code (to switch device)", bg="#0d0d0f",
             fg="#fbbf24", font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 2))
    e_totp = tk.Entry(totp_frame, bg="#16161a", fg="#ffffff", insertbackground="#ffffff",
                      relief="flat", font=("Segoe UI", 11), justify="center")
    e_totp.pack(fill="x", ipady=6)

    status = tk.StringVar(value="")
    tk.Label(root, textvariable=status, bg="#0d0d0f", fg="#f87171",
             font=("Segoe UI", 8), wraplength=340, justify="center").pack(pady=(12, 0))

    takeover_wanted = {"v": False}

    def attempt():
        email, pw = e_email.get().strip(), e_pass.get()
        if not email or not pw:
            status.set("Enter your email and password.")
            return

        btn.config(state="disabled")
        status.set("Contacting licence server…")
        root.update_idletasks()

        ok, code, msg = provider.login(
            email, pw,
            device_public_key=device_pub,
            # Non-identifying label: distinguishes devices in the admin panel
            # without leaking the real Windows hostname (which often embeds
            # the owner's name — see docs/OFFLINE_GRACE_ANALYSIS.md sibling
            # finding in the Step 14 privacy audit; DATA_INVENTORY.md
            # explicitly promises hostname is NOT collected).
            device_name=f"Device-{hashlib.sha256(device_pub.encode()).hexdigest()[:8]}",
            os_name=f"{_platform.system()} {_platform.release()}",
            app_version="1.0.0",
            takeover=takeover_wanted["v"],
            totp_code=(e_totp.get().strip() or None),
        )
        btn.config(state="normal")

        if ok:
            result["ok"] = True
            root.destroy()
            return

        if code == "SESSION_ACTIVE_ELSEWHERE":
            takeover_wanted["v"] = True
            totp_frame.pack(padx=30, fill="x")
            btn.config(text="Switch to this device")
            status.set("This account is active on another device. Enter your "
                       "authenticator code to move your session here.")
        elif code in ("MFA_REQUIRED", "MFA_INVALID"):
            takeover_wanted["v"] = True
            totp_frame.pack(padx=30, fill="x")
            status.set(msg)
        elif code == "NETWORK_UNAVAILABLE":
            status.set("Cannot reach the licence server. Check your internet "
                       "connection and try again.")
        else:
            status.set(msg or "Sign-in failed.")

    btn = tk.Button(root, text="Sign in", command=attempt, bg="#2563eb", fg="#ffffff",
                    relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2")
    btn.pack(padx=30, fill="x", ipady=8, pady=(16, 0))
    e_pass.bind("<Return>", lambda _e: attempt())
    e_totp.bind("<Return>", lambda _e: attempt())

    tk.Label(root, text="Your licence allows one active device at a time.\n"
                        "You can move it here at any time using your authenticator.",
             bg="#0d0d0f", fg="#6b6b6b", font=("Segoe UI", 8),
             justify="center").pack(side="bottom", pady=14)

    e_email.focus_set()
    root.mainloop()
    return result["ok"]
