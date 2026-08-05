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


def _resume_session() -> bool:
    """Try to silently re-authorise from a previously saved session (DPAPI
    storage) before deciding whether to show the blocking sign-in screen.

    Without this, every launch showed the sign-in dialog even for a customer
    who had already signed in and whose session was still valid — the dialog
    was unconditional on `_is_activated()`, which only reflects THIS PROCESS's
    in-memory state and is always False the instant a fresh LicensingProvider
    is constructed, regardless of what is sitting in state.bin on disk.
    Call this once, before the `_is_activated()` check in Product Sell.py.
    """
    try:
        return get_provider().resume()
    except Exception as exc:
        log.warning("_resume_session: %s", exc)
        return False


# ══════════════════════════════════════════════════════════════════════════
# Consent copy — interim summaries, NOT final legal text.
#
# The full documents (legal/TERMS_OF_SERVICE_v1.0.md etc.) are explicitly
# marked PLACEHOLDER — LAWYER REVIEW REQUIRED in the repo; shipping that raw
# file to a customer would be worse than this. These summaries are accurate,
# narrow factual statements already documented elsewhere (docs/LEGAL_REVIEW_
# BRIEF.md, docs/DATA_INVENTORY.md) and say nothing the project doesn't
# already assert in writing. Do not add anything here that isn't already
# backed by those docs (refund terms, liability caps, governing law, etc.
# are all still genuinely undecided — do not invent language for them).
# When counsel finalises real text, bump REQUIRED_CONSENTS versions in
# app/services.py — every customer is automatically re-prompted at next
# sign-in, this dialog just needs the real copy swapped in.
# ══════════════════════════════════════════════════════════════════════════
_CONSENT_COPY = {
    "TERMS_OF_SERVICE": (
        "Terms of Service",
        "StopLossPro is a position-sizing and risk-management calculator for "
        "MetaTrader 5. It is not a broker, financial advisor, or signal "
        "service, and never holds your funds — all trades execute in your "
        "own MT5 account through your own broker connection.\n\n"
        "Your licence permits one active device at a time. You can move it "
        "to a new device at any time using your authenticator code. Sharing "
        "your account credentials or redistributing the software is not "
        "permitted.\n\n"
        "Final terms are being completed with legal counsel and will be "
        "presented again for acceptance if updated."
    ),
    "RISK_DISCLOSURE": (
        "Risk Disclosure",
        "Trading leveraged products carries a high risk of loss, including "
        "the possible loss of your entire trading capital. Past performance "
        "does not indicate future results, and no outcome is guaranteed.\n\n"
        "StopLossPro performs mechanical calculations from numbers you "
        "supply — it does not give financial advice or trading "
        "recommendations. Any order it places into MT5 is initiated and "
        "confirmed by you; you are solely responsible for verifying all "
        "values before placing any trade.\n\n"
        "Final risk-disclosure language is being completed with legal "
        "counsel and will be presented again for acceptance if updated."
    ),
    "PRIVACY_NOTICE": (
        "Privacy Notice",
        "We collect only what running your licence requires: your email, a "
        "securely hashed password (never the password itself), licence, "
        "device and session records, and security audit logs including IP "
        "address, kept for security purposes.\n\n"
        "We do not collect your GPS location, MAC address, hostname, "
        "hardware fingerprints, or your MT5 account number, broker, "
        "balance, equity, open positions, or trade history.\n\n"
        "Final privacy terms are being completed with legal counsel and "
        "will be presented again for acceptance if updated."
    ),
}


def _show_consent_dialog(root, provider, email: str, outstanding: list[dict]) -> bool:
    """Blocking dialog listing every outstanding required document. Returns
    True only once every one of them has been accepted server-side."""
    import tkinter as tk

    win = tk.Toplevel(root)
    win.title("Required agreements")
    win.configure(bg="#0d0d0f")
    win.geometry("460x560")
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()
    win.attributes("-topmost", True)

    tk.Label(win, text="A few things before you continue", bg="#0d0d0f", fg="#ffffff",
             font=("Segoe UI", 13, "bold")).pack(pady=(18, 2))
    tk.Label(win, text="Please read and accept each of the following.", bg="#0d0d0f",
             fg="#8a8a8a", font=("Segoe UI", 9)).pack(pady=(0, 10))

    body = tk.Frame(win, bg="#0d0d0f")
    body.pack(fill="both", expand=True, padx=20)

    checks: dict[str, tk.BooleanVar] = {}
    for item in outstanding:
        doc = item["document"]
        title, text = _CONSENT_COPY.get(doc, (doc, "(document text unavailable)"))

        tk.Label(body, text=title, bg="#0d0d0f", fg="#ffffff",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
        txt = tk.Text(body, height=6, wrap="word", bg="#16161a", fg="#cfcfcf",
                      relief="flat", font=("Segoe UI", 9), padx=8, pady=6)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.pack(fill="x")

        var = tk.BooleanVar(value=False)
        checks[doc] = var
        tk.Checkbutton(body, text="I have read and agree", variable=var, bg="#0d0d0f",
                       fg="#cfcfcf", selectcolor="#16161a", activebackground="#0d0d0f",
                       font=("Segoe UI", 9), command=lambda: _refresh()).pack(anchor="w", pady=(2, 6))

    status = tk.StringVar(value="")
    tk.Label(win, textvariable=status, bg="#0d0d0f", fg="#f87171",
             font=("Segoe UI", 8), wraplength=400, justify="center").pack(pady=(4, 0))

    result = {"ok": False}

    def _refresh():
        continue_btn.config(state="normal" if all(v.get() for v in checks.values()) else "disabled")

    def _continue():
        continue_btn.config(state="disabled")
        status.set("Recording your acceptance…")
        win.update_idletasks()
        for item in outstanding:
            doc = item["document"]
            if not provider.accept_consent(email, doc, item["version"]):
                status.set("Could not record your acceptance. Check your connection and try again.")
                continue_btn.config(state="normal")
                return
        result["ok"] = True
        win.destroy()

    continue_btn = tk.Button(win, text="Continue", command=_continue, state="disabled",
                             bg="#2563eb", fg="#ffffff", relief="flat",
                             font=("Segoe UI", 10, "bold"), cursor="hand2")
    continue_btn.pack(padx=20, fill="x", ipady=8, pady=(10, 18))

    root.wait_window(win)
    return result["ok"]


def _show_registration_dialog(root, provider) -> None:
    """Self-serve signup. Creates a PENDING account server-side — cannot log
    in until an admin approves it in the panel. Never sets local state,
    never touches _save_state — this is not an authentication event."""
    import tkinter as tk

    win = tk.Toplevel(root)
    win.title("Create account")
    win.configure(bg="#0d0d0f")
    win.geometry("360x420")
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()
    win.attributes("-topmost", True)

    tk.Label(win, text="Create your account", bg="#0d0d0f", fg="#ffffff",
             font=("Segoe UI", 13, "bold")).pack(pady=(20, 2))
    tk.Label(win, text="An administrator reviews and activates new accounts.",
             bg="#0d0d0f", fg="#8a8a8a", font=("Segoe UI", 8), wraplength=300,
             justify="center").pack(pady=(0, 12))

    frm = tk.Frame(win, bg="#0d0d0f")
    frm.pack(padx=30, fill="x")

    def field(label, show=None):
        tk.Label(frm, text=label, bg="#0d0d0f", fg="#8a8a8a",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 2))
        e = tk.Entry(frm, show=show, bg="#16161a", fg="#ffffff",
                     insertbackground="#ffffff", relief="flat", font=("Segoe UI", 11))
        e.pack(fill="x", ipady=6)
        return e

    e_name = field("Full name (optional)")
    e_email = field("Email")
    e_pass = field("Password (min 12 characters)", show="•")
    e_pass2 = field("Confirm password", show="•")

    status = tk.StringVar(value="")
    tk.Label(win, textvariable=status, bg="#0d0d0f", fg="#f87171",
             font=("Segoe UI", 8), wraplength=300, justify="center").pack(pady=(10, 0))

    def submit():
        name, email = e_name.get().strip(), e_email.get().strip()
        pw, pw2 = e_pass.get(), e_pass2.get()
        if not email or not pw:
            status.set("Enter your email and a password.")
            return
        if pw != pw2:
            status.set("Passwords do not match.")
            return

        btn.config(state="disabled")
        status.set("Creating your account…")
        win.update_idletasks()
        ok, code, msg = provider.register(email, pw, name or None)
        btn.config(state="normal")

        if ok:
            status.set("")
            for w in frm.winfo_children():
                w.destroy()
            tk.Label(win, text="Account created.", bg="#0d0d0f", fg="#4ade80",
                     font=("Segoe UI", 11, "bold")).pack(pady=(30, 6))
            tk.Label(win, text="An administrator will review and activate it "
                              "shortly. You'll be able to sign in once that's "
                              "done — no need to keep this window open.",
                     bg="#0d0d0f", fg="#cfcfcf", font=("Segoe UI", 9),
                     wraplength=300, justify="center").pack(padx=20)
            btn.config(text="Close", command=win.destroy)
        elif code == "EMAIL_ALREADY_REGISTERED":
            status.set("An account with this email already exists. Try signing in instead.")
        elif code == "WEAK_PASSWORD":
            status.set(msg)
        elif code == "NETWORK_UNAVAILABLE":
            status.set("Cannot reach the licence server. Check your internet connection.")
        else:
            status.set(msg or "Could not create the account.")

    btn = tk.Button(win, text="Create account", command=submit, bg="#2563eb", fg="#ffffff",
                    relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2")
    btn.pack(padx=30, fill="x", ipady=8, pady=(16, 0))
    e_pass2.bind("<Return>", lambda _e: submit())

    e_name.focus_set()


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
        elif code == "CONSENT_REQUIRED":
            status.set("Reviewing required agreements…")
            root.update_idletasks()
            got, outstanding, fetch_msg = provider.fetch_required_consents(email)
            if not got or not outstanding:
                status.set(fetch_msg or "Could not load the required agreements. Try again.")
                return
            accepted = _show_consent_dialog(root, provider, email, outstanding)
            if not accepted:
                status.set("You need to accept the required agreements to continue.")
                return
            status.set("Thanks — signing you in…")
            root.update_idletasks()
            attempt()   # outstanding consents are now clear; retry the same login
        else:
            status.set(msg or "Sign-in failed.")

    btn = tk.Button(root, text="Sign in", command=attempt, bg="#2563eb", fg="#ffffff",
                    relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2")
    btn.pack(padx=30, fill="x", ipady=8, pady=(16, 0))
    e_pass.bind("<Return>", lambda _e: attempt())
    e_totp.bind("<Return>", lambda _e: attempt())

    create_link = tk.Label(root, text="New here? Create an account",
                           bg="#0d0d0f", fg="#60a5fa", font=("Segoe UI", 9, "underline"),
                           cursor="hand2")
    create_link.pack(pady=(12, 0))
    create_link.bind("<Button-1>", lambda _e: _show_registration_dialog(root, provider))

    tk.Label(root, text="Your licence allows one active device at a time.\n"
                        "You can move it here at any time using your authenticator.",
             bg="#0d0d0f", fg="#6b6b6b", font=("Segoe UI", 8),
             justify="center").pack(side="bottom", pady=14)

    e_email.focus_set()
    root.mainloop()
    return result["ok"]
