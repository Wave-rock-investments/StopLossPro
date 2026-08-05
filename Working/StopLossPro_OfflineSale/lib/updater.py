# updater.py — Enterprise Auto-Updater for StopLoss Pro
# ─────────────────────────────────────────────────────────────────────────────
# Mirrors the update pattern used by VS Code, Telegram Desktop, and Chrome:
#   1. Silent background version check on every startup
#   2. Background download with real byte-level progress %
#   3. In-app progress notification — never blocks the UI
#   4. "Restart & Update" dialog when download completes
#   5. Atomic apply via external apply_update script (runs after app exits)
#   6. Auto-backup of previous version for rollback
# ─────────────────────────────────────────────────────────────────────────────
#
# ══ ONE-TIME SETUP ════════════════════════════════════════════════════════════
#  1. Go to gist.github.com → New public Gist
#  2. Add a file named  version.json  with this content:
#       {
#         "version":      "1.0.1",
#         "download_url": "https://your-link.com/StoplossApp.zip",
#         "sha256":       "abc123...  (optional but strongly recommended)",
#         "notes":        "What changed in this release"
#       }
#  3. Click [RAW] on that file, copy the URL.
#  4. Paste it into UPDATE_URL below.
#
# ══ HOW TO RELEASE AN UPDATE ══════════════════════════════════════════════════
#  1. Bump APP_VERSION here (e.g. "1.0.0" → "1.0.1")
#  2. Zip the new archive folder → upload anywhere (Google Drive, GitHub, etc.)
#  3. Edit your Gist: update "version" and "download_url", save.
#  4. Done. All customers will see the update dialog next time they open the app.
# ─────────────────────────────────────────────────────────────────────────────

import os, sys, json, threading, zipfile, shutil, logging

log = logging.getLogger("StopLossPro.updater")

# ══ CONFIGURE THESE ═══════════════════════════════════════════════════════════
APP_VERSION = "1.0.0"   # Bump this on every release — must match version.json
UPDATE_URL  = ""        # Paste your Gist raw URL here
# ══════════════════════════════════════════════════════════════════════════════

# ── Internal paths ─────────────────────────────────────────────────────────────
_LIB_DIR    = os.path.dirname(os.path.abspath(__file__))   # archive/lib/
_APP_DIR    = os.path.dirname(_LIB_DIR)                    # archive/
_ZIP_PATH   = os.path.join(_APP_DIR, "_update.zip")        # download staging
_STAGING    = os.path.join(_APP_DIR, "_staging")           # extracted update
_BACKUP_DIR = os.path.join(_APP_DIR, "_backup")            # rollback copy


# ══ State machine ══════════════════════════════════════════════════════════════
class UpdateState:
    IDLE        = "idle"
    CHECKING    = "checking"
    AVAILABLE   = "available"
    DOWNLOADING = "downloading"
    READY       = "ready"       # downloaded, waiting for user to restart
    APPLYING    = "applying"    # user clicked Restart, applying now
    ERROR       = "error"

_state      = UpdateState.IDLE
_state_lock      = threading.Lock()
_remote_ver_lock = threading.Lock()
_remote_ver      = ""

def get_state() -> str:
    return _state

def get_remote_version() -> str:
    with _remote_ver_lock:
        return _remote_ver


# ══ Version comparison ══════════════════════════════════════════════════════════
def _parse_ver(v: str):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)

def _is_newer(remote: str, local: str) -> bool:
    return _parse_ver(remote) > _parse_ver(local)


# ══ Main entry point ════════════════════════════════════════════════════════════
def check_and_update(
    on_available=None,   # fn(version: str, notes: str)   — update found
    on_progress=None,    # fn(fraction: float, pct: int)  — 0.0 → 1.0 download progress
    on_ready=None,       # fn()                           — download complete, ready to apply
    on_error=None,       # fn(msg: str)                   — non-fatal error
):
    """
    Full enterprise update flow. Non-blocking — runs entirely in daemon threads.
    All callbacks are delivered on the Kivy main thread (Clock.schedule_once).
    Safe to call multiple times — ignores duplicate calls while already running.
    """
    global _state

    if not UPDATE_URL:
        log.debug("[updater] UPDATE_URL not configured — skipping")
        return

    with _state_lock:
        if _state not in (UpdateState.IDLE, UpdateState.ERROR):
            log.debug("[updater] Already in state=%s — skipping duplicate call", _state)
            return
        _state = UpdateState.CHECKING

    def _clock(fn, *args):
        """Deliver callback on Kivy main thread."""
        try:
            from kivy.clock import Clock
            Clock.schedule_once(lambda dt: fn(*args), 0)
        except Exception as e:
            log.debug("[updater] Clock dispatch error: %s", e)

    def _set_state(s):
        global _state
        with _state_lock:
            _state = s

    def _worker():
        global _remote_ver

        try:
            # ── Phase 1: Fetch version manifest ──────────────────────────
            import urllib.request
            log.debug("[updater] Checking %s", UPDATE_URL)
            req = urllib.request.Request(
                UPDATE_URL,
                headers={"User-Agent": f"StopLossPro-Updater/{APP_VERSION}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            remote_ver = str(data.get("version", "")).strip()
            dl_url     = str(data.get("download_url", "")).strip()
            notes           = str(data.get("notes", "")).strip()
            expected_sha256 = str(data.get("sha256", "")).strip().lower()

            if not remote_ver:
                log.debug("[updater] version.json has no 'version' field")
                _set_state(UpdateState.IDLE)
                return

            with _remote_ver_lock:
                _remote_ver = remote_ver

            if not _is_newer(remote_ver, APP_VERSION):
                log.debug("[updater] Up to date (local=%s  remote=%s)", APP_VERSION, remote_ver)
                _set_state(UpdateState.IDLE)
                return

            log.info("[updater] Update available: v%s → v%s", APP_VERSION, remote_ver)
            _set_state(UpdateState.AVAILABLE)

            if on_available:
                _clock(on_available, remote_ver, notes)

            if not dl_url:
                # Update found but no download URL — notify only, can't auto-download
                _set_state(UpdateState.IDLE)
                return

            # ── Phase 2: Download in background ──────────────────────────
            _set_state(UpdateState.DOWNLOADING)

            # Clean stale leftovers from previous attempts
            for p in [_ZIP_PATH, _STAGING]:
                if os.path.exists(p):
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)

            _DOWNLOAD_TIMEOUT_S = 30   # seconds per read — catches hung connections

            log.info("[updater] Downloading from %s", dl_url)
            dl_req = urllib.request.Request(
                dl_url,
                headers={"User-Agent": f"StopLossPro-Updater/{APP_VERSION}"},
                method="GET",
            )
            _last_pct = [-1]
            with urllib.request.urlopen(dl_req, timeout=_DOWNLOAD_TIMEOUT_S) as dl_resp:
                total_size = int(dl_resp.headers.get("Content-Length") or 0)
                downloaded  = 0
                with open(_ZIP_PATH, "wb") as fout:
                    while True:
                        chunk = dl_resp.read(65536)   # 64 KB chunks
                        if not chunk:
                            break
                        fout.write(chunk)
                        downloaded += len(chunk)
                        if on_progress and total_size > 0:
                            frac = min(1.0, downloaded / total_size)
                            pct  = int(frac * 100)
                            if pct - _last_pct[0] >= 2 or pct == 100:
                                _last_pct[0] = pct
                                _clock(on_progress, frac, pct)

            if on_progress:
                _clock(on_progress, 1.0, 100)   # guarantee 100% fires
            log.info("[updater] Download complete — %d KB", downloaded // 1024)

            # ── Phase 3: Validate ZIP ─────────────────────────────────────
            if not zipfile.is_zipfile(_ZIP_PATH):
                raise ValueError(f"Downloaded file is not a valid ZIP: {_ZIP_PATH}")

            # ── Optional SHA256 integrity check ───────────────────────────
            if expected_sha256:
                import hashlib
                sha256_hash = hashlib.sha256()
                with open(_ZIP_PATH, "rb") as fv:
                    for chunk in iter(lambda: fv.read(65536), b""):
                        sha256_hash.update(chunk)
                actual_sha256 = sha256_hash.hexdigest()
                if actual_sha256 != expected_sha256:
                    raise ValueError(
                        f"SHA256 integrity check failed — "
                        f"expected {expected_sha256[:16]}…  got {actual_sha256[:16]}…  "
                        "The download may be corrupted or tampered."
                    )
                log.info("[updater] SHA256 integrity verified ✓")
            else:
                log.debug("[updater] No SHA256 in manifest — skipping integrity check")

            # Pre-extract to staging (so apply_update.bat is instant)
            log.info("[updater] Extracting to staging folder...")
            if os.path.exists(_STAGING):
                shutil.rmtree(_STAGING, ignore_errors=True)
            with zipfile.ZipFile(_ZIP_PATH, "r") as zf:
                zf.extractall(_STAGING)
            os.remove(_ZIP_PATH)  # staging done, ZIP no longer needed

            _set_state(UpdateState.READY)
            log.info("[updater] Update ready — staging at %s", _STAGING)
            if on_ready:
                _clock(on_ready)

        except Exception as exc:
            log.warning("[updater] Update check/download failed: %s", exc)
            _set_state(UpdateState.ERROR)
            # Clean up partial downloads
            for p in [_ZIP_PATH, _STAGING]:
                if os.path.exists(p):
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            if on_error:
                _clock(on_error, str(exc))

    threading.Thread(target=_worker, daemon=True, name="stoploss-updater").start()


# ══ Apply update & restart ══════════════════════════════════════════════════════
def apply_update_and_restart():
    """
    Called when user clicks "Restart & Update".
    Backs up current app, launches the external apply script, then exits.
    The apply script (apply_update.bat / apply_update.sh) runs after we exit,
    copies the staged files in, and relaunches launch.bat/launch.sh.
    """
    global _state

    if not os.path.isdir(_STAGING):
        log.error("[updater] Staging folder missing — cannot apply")
        return False

    with _state_lock:
        if _state != UpdateState.READY:
            return False
        _state = UpdateState.APPLYING

    try:
        # Backup current lib/ for rollback
        src_lib  = os.path.join(_APP_DIR, "lib")
        bak_lib  = os.path.join(_BACKUP_DIR, "lib")
        if os.path.exists(_BACKUP_DIR):
            shutil.rmtree(_BACKUP_DIR, ignore_errors=True)
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        if os.path.isdir(src_lib):
            shutil.copytree(src_lib, bak_lib)
        log.info("[updater] Backup saved to %s", _BACKUP_DIR)

    except Exception as exc:
        log.error("[updater] Backup failed: %s — aborting update", exc)
        with _state_lock:
            _state = UpdateState.READY
        return False

    # Launch apply script as independent process, then exit
    import subprocess
    if sys.platform == "win32":
        script = os.path.join(_APP_DIR, "apply_update.bat")
        subprocess.Popen(
            [script],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    else:
        script = os.path.join(_APP_DIR, "apply_update.sh")
        subprocess.Popen(
            ["bash", script],
            start_new_session=True,
            close_fds=True,
        )

    log.info("[updater] Apply script launched — exiting app to release file locks...")

    try:
        from kivy.app import App
        app = App.get_running_app()
        if app:
            app.stop()
    except Exception:
        pass

    sys.exit(0)
