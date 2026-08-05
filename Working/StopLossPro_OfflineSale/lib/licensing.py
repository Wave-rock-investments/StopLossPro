"""Client-side licensing provider — replaces the Gist / Worker / ntfy stack.

PHASE 12. The old trust model had three remotely-abusable paths:
  * a Cloudflare Worker that authenticated writes on a User-Agent prefix
  * public ntfy topics where an APPROVED message activated the product
  * an approved-IDs allowlist keyed on a hardware fingerprint

All three are gone. Authority now sits entirely on the server; this module
only asks and reports. It can no longer grant itself anything.

DESIGN NOTES
  * The client holds the PUBLIC verification key only. It can check that a
    grant came from the server; it cannot mint one.
  * The session token and device private key live in Windows DPAPI-protected
    storage, not a plaintext file.
  * "Server said revoked" and "server unreachable" are DIFFERENT states.
    Revocation locks immediately. Unreachability starts a bounded grace
    window, after which the app locks anyway.
  * Clock rollback is defended with a monotonic high-water mark: if the system
    clock ever moves backwards relative to the last seen value, the grace
    window is treated as exhausted rather than extended.

The provider is deliberately behind a narrow interface so the backend could be
swapped for a hosted licensing vendor later without touching the risk engine.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("StopLossPro.licensing")

# ── configuration ──────────────────────────────────────────────────────────
API_BASE = os.environ.get("STOPLOSSPRO_API", "https://api.stoplosspro.in/api/v1")

# Ed25519 PUBLIC verification key, base64 PEM. Safe to ship: it can only
# VERIFY grants. Populated at build time; empty means "unconfigured".
SERVER_PUBLIC_KEY_B64 = ""

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".stoplosspro")
_STATE_FILE = os.path.join(_STATE_DIR, "state.bin")

DEFAULT_HEARTBEAT = 90
# 24h — matches server OFFLINE_GRACE_SECONDS (app/config.py). Used only until the
# first successful login/heartbeat, which then overwrites this from the server's
# live "offline_grace_seconds" field — so this constant only matters for the
# very first offline gap before any server contact has happened.
DEFAULT_GRACE = 24 * 3600


# ══════════════════════════════════════════════════════════════════════════
# Windows DPAPI-protected local storage
# ══════════════════════════════════════════════════════════════════════════
def _dpapi(protect: bool, blob: bytes) -> bytes | None:
    """Encrypt/decrypt with the current Windows user account's key.

    Data protected here cannot be read by another user account, and cannot be
    copied to another machine and decrypted. That is what makes copying the
    state file to a second PC useless.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        def _mk(b):
            buf = ctypes.create_string_buffer(b, len(b))
            return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        src, _keep = _mk(blob)
        out = BLOB()
        fn = (ctypes.windll.crypt32.CryptProtectData if protect
              else ctypes.windll.crypt32.CryptUnprotectData)
        ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
        if not ok:
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception as exc:                      # non-Windows or API failure
        log.debug("DPAPI unavailable: %s", exc)
        return None


def _save_state(data: dict) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        raw = json.dumps(data).encode()
        sealed = _dpapi(True, raw)
        if sealed is None:
            # Refuse to silently downgrade to plaintext — that was precisely
            # the old failure mode. Better to re-authenticate next launch.
            log.warning("secure storage unavailable; not persisting session")
            return
        with open(_STATE_FILE, "wb") as f:
            f.write(sealed)
    except Exception as exc:
        log.debug("save_state: %s", exc)


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, "rb") as f:
            sealed = f.read()
        raw = _dpapi(False, sealed)
        return json.loads(raw.decode()) if raw else {}
    except Exception:
        return {}


def _clear_state() -> None:
    try:
        os.remove(_STATE_FILE)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Grant verification (public key only)
# ══════════════════════════════════════════════════════════════════════════
def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_grant(token: str) -> dict:
    """Verify an Ed25519-signed grant. Raises ValueError if untrustworthy."""
    if not SERVER_PUBLIC_KEY_B64:
        raise ValueError("no verification key configured in this build")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    try:
        body_b64, sig_b64 = token.split(".")
        body, sig = _b64u_dec(body_b64), _b64u_dec(sig_b64)
    except Exception as exc:
        raise ValueError("malformed grant") from exc

    pub = serialization.load_pem_public_key(base64.b64decode(SERVER_PUBLIC_KEY_B64))
    try:
        pub.verify(sig, body)          # signature FIRST, before trusting any field
    except InvalidSignature as exc:
        raise ValueError("invalid signature") from exc

    claims = json.loads(body)
    if int(claims.get("exp", 0)) <= int(time.time()):
        raise ValueError("grant expired")
    return claims


# ══════════════════════════════════════════════════════════════════════════
# Provider
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class LicenceState:
    authorised: bool = False
    reason: str = "NOT_AUTHENTICATED"
    message: str = "Sign in to continue."
    entitlements: list[str] = field(default_factory=list)
    offline: bool = False
    grace_remaining: int = 0


class LicensingProvider:
    """Narrow interface. Swapping to a hosted vendor means reimplementing
    these methods only — the risk engine never calls anything else."""

    def __init__(self, on_state_change: Callable[[LicenceState], None] | None = None):
        self._state = LicenceState()
        self._cb = on_state_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hb_interval = DEFAULT_HEARTBEAT
        self._grace = DEFAULT_GRACE

    # ── state ──────────────────────────────────────────────────────────────
    @property
    def state(self) -> LicenceState:
        return self._state

    def _set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self._state, k, v)
        if self._cb:
            try:
                self._cb(self._state)
            except Exception as exc:
                log.debug("state callback: %s", exc)

    # ── HTTP ───────────────────────────────────────────────────────────────
    def _post(self, path: str, payload: dict | None = None,
              token: str | None = None, timeout: int = 10) -> tuple[int, dict]:
        url = f"{API_BASE}{path}"
        data = json.dumps(payload or {}).encode()
        headers = {"Content-Type": "application/json",
                   "User-Agent": "StopLossPro/2"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}
        except Exception as exc:
            raise ConnectionError(str(exc)) from exc

    # ── login ──────────────────────────────────────────────────────────────
    def login(self, email: str, password: str, *, device_public_key: str,
              device_name: str, os_name: str, app_version: str,
              takeover: bool = False, totp_code: str | None = None) -> tuple[bool, str, str]:
        """Returns (ok, code, message). `code` is stable and machine-readable."""
        try:
            status, body = self._post("/auth/login", {
                "email": email, "password": password,
                "device_public_key": device_public_key, "device_name": device_name,
                "os_name": os_name, "app_version": app_version,
                "takeover": takeover, "totp_code": totp_code,
            })
        except ConnectionError as exc:
            return False, "NETWORK_UNAVAILABLE", f"Cannot reach the licence server. {exc}"

        if status != 200:
            d = body.get("detail", body) if isinstance(body, dict) else {}
            code = d.get("code", "ERROR") if isinstance(d, dict) else "ERROR"
            msg = d.get("message", "Sign-in failed.") if isinstance(d, dict) else "Sign-in failed."
            return False, code, msg

        self._hb_interval = int(body.get("heartbeat_interval", DEFAULT_HEARTBEAT))
        self._grace = int(body.get("offline_grace_seconds", DEFAULT_GRACE))

        try:
            claims = verify_grant(body["grant"])
        except ValueError as exc:
            # Server reachable but its grant does not verify — treat as hostile.
            return False, "GRANT_INVALID", f"Licence response could not be verified ({exc})."

        now = int(time.time())
        _save_state({
            "session_token": body["session_token"],
            "grant": body["grant"],
            "device_id": body.get("device_id"),
            "last_ok": now,
            "clock_hwm": now,
        })
        self._set(authorised=True, reason="OK", message="",
                  entitlements=list(claims.get("ent", [])), offline=False,
                  grace_remaining=self._grace)
        return True, "OK", ""

    # ── heartbeat ──────────────────────────────────────────────────────────
    def _heartbeat_once(self) -> None:
        st = _load_state()
        token = st.get("session_token")
        if not token:
            self._set(authorised=False, reason="NOT_AUTHENTICATED",
                      message="Sign in to continue.")
            return

        now = int(time.time())
        hwm = int(st.get("clock_hwm", now))

        try:
            status, body = self._post("/session/heartbeat", token=token, timeout=8)
        except ConnectionError:
            # ── UNREACHABLE — not the same as revoked ──────────────────────
            self._enter_grace(st, now, hwm)
            return

        if status == 200:
            try:
                claims = verify_grant(body["grant"])
            except ValueError as exc:
                self._set(authorised=False, reason="GRANT_INVALID",
                          message=f"Licence could not be verified ({exc}).")
                return
            st.update({"grant": body["grant"], "last_ok": now,
                       "clock_hwm": max(hwm, now)})
            _save_state(st)
            self._hb_interval = int(body.get("heartbeat_interval", self._hb_interval))
            self._grace = int(body.get("offline_grace_seconds", self._grace))
            self._set(authorised=True, reason="OK", message="",
                      entitlements=list(claims.get("ent", [])), offline=False,
                      grace_remaining=self._grace)
            return

        # ── SERVER SAID NO — authoritative, lock immediately ───────────────
        d = body.get("detail", {}) if isinstance(body, dict) else {}
        code = d.get("code", "LICENCE_INVALID") if isinstance(d, dict) else "LICENCE_INVALID"
        msg = d.get("message", "Access is no longer available.") if isinstance(d, dict) else ""
        log.warning("authorization withdrawn by server: %s", code)
        _clear_state()
        self._set(authorised=False, reason=code, message=msg,
                  entitlements=[], offline=False, grace_remaining=0)

    def _enter_grace(self, st: dict, now: int, hwm: int) -> None:
        """Bounded offline tolerance with clock-rollback defence."""
        last_ok = int(st.get("last_ok", 0))

        if now < hwm:
            # System clock moved backwards. Someone may be trying to rewind
            # into a fresh grace window. Refuse rather than reward it.
            log.warning("system clock moved backwards; ending offline grace")
            self._set(authorised=False, reason="CLOCK_ROLLBACK",
                      message="System clock inconsistency detected. Reconnect to continue.",
                      offline=True, grace_remaining=0)
            return

        st["clock_hwm"] = max(hwm, now)
        _save_state(st)

        elapsed = now - last_ok
        remaining = self._grace - elapsed

        if remaining <= 0:
            self._set(authorised=False, reason="OFFLINE_GRACE_EXPIRED",
                      message="Offline period exceeded. Connect to the internet to continue.",
                      offline=True, grace_remaining=0)
            return

        # Still inside the window: keep working on the last verified grant.
        try:
            claims = verify_grant(st.get("grant", ""))
            ents = list(claims.get("ent", []))
        except ValueError:
            # Cached grant has expired — normal, they are short-lived. Stay
            # authorised on the strength of the grace window itself.
            ents = self._state.entitlements

        self._set(authorised=True, reason="OFFLINE_GRACE", entitlements=ents,
                  offline=True, grace_remaining=int(remaining),
                  message=f"Working offline. {int(remaining // 3600)}h of grace remaining.")

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop():
            self._heartbeat_once()
            while not self._stop.wait(self._hb_interval):
                self._heartbeat_once()

        self._thread = threading.Thread(target=_loop, daemon=True, name="slp-licence")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def logout(self) -> None:
        st = _load_state()
        tok = st.get("session_token")
        if tok:
            try:
                self._post("/session/logout", token=tok, timeout=5)
            except Exception:
                pass
        _clear_state()
        self._set(authorised=False, reason="LOGGED_OUT", message="Signed out.",
                  entitlements=[], offline=False, grace_remaining=0)

    def has_entitlement(self, key: str) -> bool:
        return self._state.authorised and key in self._state.entitlements


# ── device keypair ─────────────────────────────────────────────────────────
def get_or_create_device_keypair() -> tuple[str, str]:
    """Ed25519 keypair identifying THIS installation.

    Replaces hardware fingerprinting. A customer may replace their SSD, GPU or
    motherboard without losing access; conversely, copying the app folder to
    another machine does not carry the identity across, because the private
    key is sealed to this Windows account by DPAPI.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    st = _load_state()
    if st.get("device_private_key"):
        try:
            priv = serialization.load_pem_private_key(
                base64.b64decode(st["device_private_key"]), password=None)
            pub = priv.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo)
            return st["device_private_key"], base64.b64encode(pub).decode()
        except Exception:
            pass

    priv = ed25519.Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)

    priv_b64 = base64.b64encode(priv_pem).decode()
    st["device_private_key"] = priv_b64
    _save_state(st)
    return priv_b64, base64.b64encode(pub_pem).decode()
