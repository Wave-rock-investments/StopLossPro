# activation.py — licence & registration system.
# Handles: machine fingerprint, Gist-based approval check, GPS location,
# registration notification, heartbeat, revoke listener, activation screen.
# ─────────────────────────────────────────────────────────────────────────────
import os, time, uuid, hashlib, socket, base64, threading, subprocess, logging

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.core.window import Window
from kivy.metrics import dp
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.label import MDLabel
from kivymd.uix.button import MDFlatButton, MDRaisedButton
from kivymd.uix.dialog import MDDialog

from constants import (
    log, platform,
    _NOTIFY_URL, _HB_URL, _APPROVE_URL, _LINK_URL, _GIST_PROXY_URL,
    _SESSIONS_URL, _SESSION_HB_INTERVAL,
    _REG_FILE, _LIC_CACHE, _GPS_CACHE, _CACHE_TTL, _GPS_TTL,
    _CACHED_COORDS,
)

def _get_icon_path() -> str:
    import sys as _s, os as _o
    base = getattr(_s, '_MEIPASS', _o.path.dirname(_o.path.abspath(__file__)))
    return _o.path.join(base, 'app_icon.ico')


def _set_icon(root) -> None:
    try:
        root.iconbitmap(_get_icon_path())
    except Exception:
        pass


def _get_user_sid() -> str:
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
            capture_output=True, text=True, timeout=6, creationflags=0x08000000
        )
        sid = r.stdout.strip()
        if sid.startswith('S-1-'):
            return sid
    except Exception:
        pass
    return os.environ.get('USERNAME') or os.environ.get('USER', 'unknown')


def _get_machine_id() -> str:
    try:
        raw = f"{uuid.getnode()}:{socket.gethostname()}:{_get_user_sid()}".encode()
        return hashlib.sha256(raw).hexdigest()[:16].upper()
    except Exception:
        return "UNKNOWN00000000"


def _check_approved() -> bool:
    """Cache-first approval check — instant on repeat runs, no network delay at startup."""
    import time as _t
    mid = _get_machine_id()
    cached_content = None

    # Serve from cache if fresh — zero network calls on most startups
    try:
        with open(_LIC_CACHE, 'r') as f:
            raw = f.read().strip()
        ts_str, cached_content = raw.split(':', 1)
        if _t.time() - int(ts_str) < _CACHE_TTL:
            approved = {ln.strip().upper() for ln in cached_content.splitlines() if ln.strip()}
            return mid in approved
    except Exception:
        pass

    # Cache stale or missing — fetch from network with short timeout
    content = None
    try:
        import urllib.request as _ur, time as _t2
        _url = f"{_APPROVE_URL}?t={int(_t2.time())}"  # bypass GitHub CDN cache
        with _ur.urlopen(_url, timeout=4) as r:
            content = r.read().decode().strip()
        with open(_LIC_CACHE, 'w') as f:
            f.write(f"{int(_t.time())}:{content}")
    except Exception:
        content = cached_content  # stay approved if network is temporarily down

    if content is None:
        return False
    approved = {ln.strip().upper() for ln in content.splitlines() if ln.strip()}
    return mid in approved


def _is_activated() -> bool:
    return _check_approved()


def _collect_system_info(gps_ready: bool = False) -> dict:
    import platform as _plat_mod, json as _json
    info = {
        'machine_id':   _get_machine_id(),
        'hostname':     socket.gethostname(),
        'username':     os.environ.get('USERNAME') or os.environ.get('USER', 'unknown'),
        'os':           _plat_mod.platform(),
        'os_release':   _plat_mod.release(),
        'os_version':   _plat_mod.version(),
        'cpu':          _plat_mod.processor(),
        'arch':         _plat_mod.machine(),
        'mac':          '', 'manufacturer': '', 'model': '',
        'ram_gb':       '', 'screen':       '',
        'ip': '', 'city': '', 'region': '', 'country': '', 'org': '', 'loc': '',
    }
    # MAC address
    try:
        n = uuid.getnode()
        info['mac'] = ':'.join(f'{(n >> (8*i)) & 0xff:02x}' for i in range(5, -1, -1))
    except Exception:
        pass
    # System manufacturer + model
    try:
        r = subprocess.run(
            ['wmic', 'computersystem', 'get', 'Manufacturer,Model', '/format:csv'],
            capture_output=True, text=True, timeout=8, creationflags=0x08000000)
        for ln in r.stdout.strip().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith('Node'):
                parts = ln.split(',')
                if len(parts) >= 3:
                    info['manufacturer'] = parts[1].strip()
                    info['model']        = parts[2].strip()
                break
    except Exception:
        pass
    # RAM
    try:
        r = subprocess.run(
            ['wmic', 'OS', 'get', 'TotalVisibleMemorySize', '/format:csv'],
            capture_output=True, text=True, timeout=8, creationflags=0x08000000)
        for ln in r.stdout.strip().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith('Node'):
                kb = int(ln.split(',')[-1].strip())
                info['ram_gb'] = f"{kb/1024/1024:.1f} GB"
                break
    except Exception:
        pass
    # Screen resolution
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             'Add-Type -AssemblyName System.Windows.Forms;'
             '[System.Windows.Forms.Screen]::PrimaryScreen.Bounds|'
             'Select-Object Width,Height|ConvertTo-Json'],
            capture_output=True, text=True, timeout=8, creationflags=0x08000000)
        d = _json.loads(r.stdout.strip())
        info['screen'] = f"{d['Width']}x{d['Height']}"
    except Exception:
        pass
    # IP + geo (ipinfo.io) — city, region, country, ISP, fallback coords
    try:
        import urllib.request as _ur
        with _ur.urlopen('https://ipinfo.io/json', timeout=8) as _r:
            geo = _json.loads(_r.read().decode())
        info.update({k: geo.get(k, '') for k in ('ip','city','region','country','org','loc')})
    except Exception:
        pass
    # GPS via Windows Location Services (only when user granted permission)
    if gps_ready:
        try:
            _gps_cmd = (
                'Add-Type -AssemblyName System.Device;'
                '$w=New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::Default);'
                '$w.Start();$t=0;'
                'while($w.Status -ne "Ready" -and $t -lt 16){Start-Sleep -Milliseconds 500;$t++};'
                'if(-not $w.Position.Location.IsUnknown){'
                '  $w.Position.Location.Latitude.ToString("F6")+","+$w.Position.Location.Longitude.ToString("F6")'
                '} else {"UNAVAILABLE"};$w.Stop()'
            )
            r = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', _gps_cmd],
                capture_output=True, text=True, timeout=22, creationflags=0x08000000)
            gps = r.stdout.strip().split('\n')[-1].strip()
            if gps and gps != 'UNAVAILABLE' and ',' in gps:
                info['loc']     = gps
                info['loc_src'] = 'GPS'
            else:
                info['loc_src'] = 'IP'
        except Exception:
            info['loc_src'] = 'IP'
    else:
        info['loc_src'] = 'IP'
    # Update global coords cache so heartbeats can use it without re-querying GPS
    global _CACHED_COORDS
    if info.get('loc'):
        _CACHED_COORDS = {'loc': info['loc'], 'src': info.get('loc_src', 'IP')}
    return info


def _send_registration_notification(info: dict):
    """Notify developer of new install via ntfy with full system details."""
    try:
        import urllib.request as _ur
        mid = info.get('machine_id', '')
        model = f"{info.get('manufacturer','')} {info.get('model','')}".strip() or '—'
        loc_str = f"{info.get('city','')} {info.get('region','')} {info.get('country','')}".strip()
        msg = (
            f"ID:       {mid}\n"
            f"User:     {info.get('username','')}\n"
            f"Host:     {info.get('hostname','')}\n"
            f"MAC:      {info.get('mac','')}\n"
            f"Model:    {model}\n"
            f"CPU:      {info.get('cpu','')}\n"
            f"RAM:      {info.get('ram_gb','')}\n"
            f"Screen:   {info.get('screen','')}\n"
            f"Arch:     {info.get('arch','')}\n"
            f"OS:       {info.get('os','')}\n"
            f"IP:       {info.get('ip','')} — {loc_str}\n"
            f"Org:      {info.get('org','')}\n"
            f"Coords:   {info.get('loc','')} ({info.get('loc_src','IP')})\n\n"
            f"Add ID to approved list to activate."
        )
        req = _ur.Request(
            _NOTIFY_URL,
            data=msg.encode('utf-8'),
            headers={
                'Title':        f"StopLoss Install: {mid}",
                'Priority':     'default',
                'Content-Type': 'text/plain',
            },
            method='POST',
        )
        _ur.urlopen(req, timeout=10)
    except Exception:
        pass


def _gps_check() -> bool:
    """Check if Windows Location Services is on and can provide a position.
    Uses Default accuracy so WiFi/network location works on desktop PCs too."""
    _cmd = (
        'Add-Type -AssemblyName System.Device;'
        '$w=New-Object System.Device.Location.GeoCoordinateWatcher('
        '[System.Device.Location.GeoPositionAccuracy]::Default);'
        '$w.Start();$t=0;'
        'while($w.Status -ne "Ready" -and $t -lt 6)'
        '{Start-Sleep -Milliseconds 500;$t++};'
        'if(-not $w.Position.Location.IsUnknown){'
        '$w.Position.Location.Latitude.ToString("F6")+","'
        '+$w.Position.Location.Longitude.ToString("F6")'
        '} else {"UNAVAILABLE"};$w.Stop()'
    )
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', _cmd],
            capture_output=True, text=True, timeout=22, creationflags=0x08000000)
        val = r.stdout.strip().split('\n')[-1].strip()
        return bool(val and val != 'UNAVAILABLE' and ',' in val)
    except Exception:
        return False


def _require_location():
    """Hard gate — app cannot start without location services enabled.
    Result cached for 24 hours so the check only runs once per day."""
    import sys, time as _t
    # Check cache first — skip GPS check if confirmed within 24 hours
    try:
        with open(_GPS_CACHE, 'r') as _f:
            ts = float(_f.read().strip())
        if _t.time() - ts < _GPS_TTL:
            return  # GPS was confirmed recently — skip check entirely
    except Exception:
        pass

    try:
        import tkinter as tk
    except ImportError:
        return

    # Silent quick check — no dialog if location is already enabled
    if _gps_check():
        try:
            with open(_GPS_CACHE, 'w') as _f:
                _f.write(str(_t.time()))
        except Exception:
            pass
        return

    # Location is off — show mandatory enable screen
    root = tk.Tk()
    root.title("StopLoss Calculator")
    _set_icon(root)
    root.geometry("380x400")
    root.resizable(False, False)
    root.configure(bg='#0d0d0f')
    root.attributes('-topmost', True)
    root.eval('tk::PlaceWindow . center')

    tk.Label(root, text="📍", font=('Segoe UI Emoji', 36),
             bg='#0d0d0f').pack(pady=(30, 8))

    tk.Label(root, text='"StopLoss Calculator"\nrequires Location Access',
             font=('Segoe UI', 13, 'bold'), bg='#0d0d0f', fg='white',
             justify='center').pack()

    tk.Frame(root, bg='#1c1c1e', height=1).pack(fill='x', pady=16)

    tk.Label(root,
             text="Location services must be ON to use this app.\n"
                  "This is required by our service agreement for\n"
                  "identity verification and compliance.",
             font=('Segoe UI', 9), bg='#0d0d0f', fg='#666',
             justify='center').pack(padx=30)

    status_var = tk.StringVar(value="")
    status_lbl = tk.Label(root, textvariable=status_var,
                          font=('Segoe UI', 9), bg='#0d0d0f', fg='#555')
    status_lbl.pack(pady=(10, 0))

    btn_frame = tk.Frame(root, bg='#0d0d0f')
    btn_frame.pack(pady=14, fill='x', padx=30)

    step = [0]  # 0=initial, 1=waiting for user to enable

    def rebuild_buttons():
        for w in btn_frame.winfo_children():
            w.destroy()
        if step[0] == 0:
            tk.Button(btn_frame, text="Enable Location",
                      command=on_enable,
                      bg='#1565c0', fg='white', relief='flat',
                      font=('Segoe UI', 11, 'bold'),
                      cursor='hand2').pack(fill='x', pady=(0, 8))
            tk.Button(btn_frame, text="Exit App",
                      command=on_exit,
                      bg='#1a1a1c', fg='#555', relief='flat',
                      font=('Segoe UI', 10),
                      cursor='hand2').pack(fill='x')
        else:
            tk.Button(btn_frame, text="I've Enabled It — Continue",
                      command=on_check,
                      bg='#1565c0', fg='white', relief='flat',
                      font=('Segoe UI', 11, 'bold'),
                      cursor='hand2').pack(fill='x', pady=(0, 8))
            tk.Button(btn_frame, text="Exit App",
                      command=on_exit,
                      bg='#1a1a1c', fg='#555', relief='flat',
                      font=('Segoe UI', 10),
                      cursor='hand2').pack(fill='x')

    def on_enable():
        step[0] = 1
        status_var.set("Turn on Location Access in Settings, then come back here.")
        status_lbl.config(fg='#888')
        subprocess.Popen(
            ['powershell', '-NoProfile', '-Command',
             'Start-Process ms-settings:privacy-location'],
            creationflags=0x08000000)
        rebuild_buttons()

    def on_check():
        import time as _t
        status_var.set("Checking location…")
        status_lbl.config(fg='#888')
        root.update()
        if _gps_check():
            try:
                with open(_GPS_CACHE, 'w') as _f:
                    _f.write(str(_t.time()))
            except Exception:
                pass
            status_var.set("Location enabled. Starting app…")
            status_lbl.config(fg='#4caf50')
            root.after(700, root.destroy)
        else:
            status_var.set("Location is still off. Enable it in Settings and try again.")
            status_lbl.config(fg='#ff7043')

    def on_exit():
        root.destroy()
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", on_exit)
    rebuild_buttons()
    root.mainloop()


def _try_gps_background():
    """Silent background GPS collection — never blocks app startup.
    Populates _CACHED_COORDS so heartbeats include real GPS coords in admin panel map.
    Falls back to IP-based location automatically if GPS unavailable."""
    import threading as _thr, time as _t2
    def _run():
        global _CACHED_COORDS
        try:
            # Check 24-hr file cache — skip expensive PowerShell if confirmed recently
            try:
                with open(_GPS_CACHE, 'r') as _fc:
                    _ts = float(_fc.read().strip())
                if _t2.time() - _ts < _GPS_TTL:
                    return  # confirmed recently; coords populated by _collect_system_info
            except Exception:
                pass
            # Quick status check first (fast, just reads Windows location status)
            if not _gps_check():
                return  # Location Services off — heartbeat falls back to IP
            # Location available — fetch actual coordinates
            _cmd = (
                'Add-Type -AssemblyName System.Device;'
                '$w=New-Object System.Device.Location.GeoCoordinateWatcher('
                '[System.Device.Location.GeoPositionAccuracy]::Default);'
                '$w.Start();$t=0;'
                'while($w.Status -ne "Ready" -and $t -lt 10){Start-Sleep -m 500;$t++};'
                'if($w.Position.Location.IsUnknown){"UNAVAILABLE"}else{'
                '$w.Position.Location.Latitude.ToString()+" "+'
                '$w.Position.Location.Longitude.ToString()};$w.Stop()'
            )
            import subprocess as _sp2
            r = _sp2.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', _cmd],
                capture_output=True, text=True, timeout=20, creationflags=0x08000000)
            gps = r.stdout.strip().split('\n')[-1].strip().replace(' ', ',')
            if gps and gps != 'UNAVAILABLE' and ',' in gps:
                _CACHED_COORDS['loc'] = gps
                _CACHED_COORDS['src'] = 'GPS'
                try:
                    with open(_GPS_CACHE, 'w') as _fc:
                        _fc.write(str(_t2.time()))
                except Exception:
                    pass
        except Exception:
            pass
    _thr.Thread(target=_run, daemon=True).start()


def _send_heartbeat():
    """Silent heartbeat — sends full MT5 account snapshot to developer dashboard."""
    try:
        import urllib.request as _ur
        mid = _get_machine_id()
        lines = [f"mid:{mid}", "trades:0"]
        try:
            import MetaTrader5 as _mt5hb
            _mt5hb.initialize()
            acc = _mt5hb.account_info()
            pos = _mt5hb.positions_get()
            if acc:
                lines = [
                    f"mid:{mid}",
                    f"balance:{acc.balance:.2f}",
                    f"equity:{acc.equity:.2f}",
                    f"profit:{acc.profit:.2f}",
                    f"currency:{acc.currency}",
                    f"server:{acc.server}",
                    f"login:{acc.login}",
                    f"trades:{len(pos) if pos else 0}",
                ]
            if pos:
                type_map = {0: 'BUY', 1: 'SELL'}
                for p in pos:
                    t = type_map.get(p.type, str(p.type))
                    lines.append(f"pos:{p.symbol},{t},{p.volume},{p.profit:.2f},{p.price_open:.5f},{p.price_current:.5f}")
        except Exception:
            pass
        # Append GPS/location coords so admin panel map shows real device position
        _coords = _CACHED_COORDS.get('loc', '')
        _csrc   = _CACHED_COORDS.get('src', 'IP')
        if not _coords:
            # Fallback: quick IP-based lookup if GPS cache not yet populated
            try:
                import json as _jh
                with _ur.urlopen('https://ipinfo.io/json', timeout=4) as _rh:
                    _geo = _jh.loads(_rh.read().decode())
                _coords = _geo.get('loc', '')
            except Exception:
                pass
        if _coords:
            lines.append(f"Coords: {_coords} ({_csrc})")
        body = '\n'.join(lines)
        req = _ur.Request(
            _HB_URL,
            data=body.encode(),
            headers={'Title': f'HB:{mid}', 'Priority': 'min', 'Tags': 'heartbeat'},
            method='POST',
        )
        _ur.urlopen(req, timeout=4)
    except Exception:
        pass


def _start_revoke_listener():
    """Poll personal ntfy topic every 10s — REVOKE message exits app within seconds."""
    import urllib.request as _ur, json as _json, time as _t
    mid = _get_machine_id()
    topic = f"slcalc_{mid[:10].lower()}"
    since = [int(_t.time())]

    def _run():
        while True:
            _t.sleep(10)
            try:
                url = f"https://ntfy.sh/{topic}/json?poll=1&since={since[0]}"
                with _ur.urlopen(url, timeout=6) as r:
                    for raw in r:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            data = _json.loads(raw)
                            since[0] = max(since[0], int(data.get('time', since[0])))
                            if 'REVOKE' in data.get('message', '').upper():
                                try: os.remove(_LIC_CACHE)
                                except Exception: pass
                                from kivy.clock import Clock as _Ck
                                from kivy.app import App as _Ap
                                _Ck.schedule_once(lambda dt: _Ap.get_running_app().stop(), 0)
                                return
                        except Exception:
                            pass
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()


def _start_session_heartbeat():
    """Single-active-session enforcement.

    Each running instance claims its machine ID in active_sessions.txt with a
    random per-process token, then re-affirms that claim every
    _SESSION_HB_INTERVAL seconds. active_sessions.txt holds one line per
    machine ID across every customer, so each write is a read-modify-write of
    the whole file (same pattern the admin dashboard uses for approved_ids).

    If a re-read finds our machine ID's token no longer matches ours, another
    instance has claimed the same machine ID more recently than us — this
    session lost the race and exits. Writes route through the CF Worker
    proxy (_GIST_PROXY_URL) so no GitHub PAT is ever embedded client-side.

    Reads go through the Gist API (api.github.com), NOT the
    gist.githubusercontent.com raw-content CDN. The raw CDN can lag several
    seconds to minutes behind a just-completed PATCH (Fastly edge propagation),
    which previously caused a fresh launch's own claim to read back as
    "missing/stale" on the very first heartbeat cycle — kicking the app the
    customer had just opened. api.github.com reflects writes immediately.
    A single retry-with-delay guards against any remaining transient blip
    before we ever conclude we've actually been superseded.

    ROOT CAUSE FOUND 2026-08-04: active_sessions.txt lives in the same shared
    Gist (8a8b52dc14c0ecca38121df01557ec99) as the retired P1/P2 products.
    Machine ID is derived from hardware, not per-product, so on a dev/test
    machine that ran P1 or P2 earlier, their session-lock code (which writes
    full uuid4() tokens, not our short hex ones) was still asserting a claim
    on the bare `mid` key — evicting every StopLossPro launch within one
    heartbeat cycle even though StopLossPro itself was the only "real"
    instance running. This isn't just a dev artifact: since P1/P2 hardcode
    the same Gist ID forever, any future P1/P2 relaunch would do the same
    thing to a live paying customer. Fix: namespace the session key per
    product so StopLossPro's lock can never collide with P1/P2's entries in
    the same file.
    """
    import urllib.request as _ur, json as _json, time as _t

    mid   = _get_machine_id()
    skey  = f"SLP_{mid}"   # product-scoped session key — isolates from P1/P2 entries sharing this Gist
    token = uuid.uuid4().hex[:12]
    _GIST_ID  = '8a8b52dc14c0ecca38121df01557ec99'
    _GIST_API = f'https://api.github.com/gists/{_GIST_ID}'

    def _parse_sessions(content: str) -> dict:
        out = {}
        for ln in content.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            parts = ln.split(':')
            if len(parts) >= 3:
                out[parts[0]] = (parts[1], parts[2])
        return out

    def _read_sessions() -> dict:
        try:
            req = _ur.Request(_GIST_API, headers={'User-Agent': 'StopLossCalc/2'})
            with _ur.urlopen(req, timeout=8) as r:
                gist = _json.loads(r.read())
            content = (gist.get('files', {}).get('active_sessions.txt', {}) or {}).get('content', '') or ''
        except Exception:
            return {}
        return _parse_sessions(content)

    def _write_sessions(sessions: dict):
        lines = [f"{m}:{t}:{ts}" for m, (t, ts) in sessions.items()]
        body = ('\n'.join(lines) + '\n') if lines else "# format: MID:SESSION_TOKEN:UNIX_TS\n"
        try:
            patch_data = _json.dumps({'files': {'active_sessions.txt': {'content': body}}}).encode()
            req = _ur.Request(_GIST_PROXY_URL, data=patch_data, method='POST',
                               headers={'Content-Type': 'application/json'})
            _ur.urlopen(req, timeout=10)
        except Exception:
            pass

    def _kick():
        log.warning("[SESSION_HB] kicked — mid=%s token=%s superseded, stopping app", mid, token)
        from kivy.clock import Clock as _Ck
        from kivy.app import App as _Ap
        def _stop(dt):
            app = _Ap.get_running_app()
            if app:
                app.stop()
        _Ck.schedule_once(_stop, 0)

    def _run():
        # Claim immediately on start — a fresh launch always takes precedence
        # over whatever (possibly stale) entry is already on record.
        log.debug("[SESSION_HB] starting — skey=%s token=%s interval=%ss", skey, token, _SESSION_HB_INTERVAL)
        sessions = _read_sessions()
        sessions[skey] = (token, str(int(_t.time())))
        _write_sessions(sessions)

        while True:
            _t.sleep(_SESSION_HB_INTERVAL)
            sessions = _read_sessions()
            existing = sessions.get(skey)
            log.debug("[SESSION_HB] cycle — read existing=%s ours=%s", existing, token)
            if existing is not None and existing[0] != token:
                # Mismatch — could be a genuine takeover, or a transient API
                # blip. Re-verify once after a short delay before killing the
                # customer's session over a one-off glitch.
                log.debug("[SESSION_HB] mismatch on first read — re-verifying in 5s")
                _t.sleep(5)
                sessions = _read_sessions()
                existing = sessions.get(skey)
                log.debug("[SESSION_HB] re-verify — existing=%s ours=%s", existing, token)
                if existing is not None and existing[0] != token:
                    # Someone else claimed this machine ID more recently — yield.
                    _kick()
                    return
            # Still ours (or the read failed/was inconclusive) — refresh the claim.
            sessions[skey] = (token, str(int(_t.time())))
            _write_sessions(sessions)

    threading.Thread(target=_run, daemon=True).start()


def _register_if_new():
    if os.path.exists(_REG_FILE):
        return
    def _run():
        try:
            info = _collect_system_info(gps_ready=True)  # GPS confirmed on by _require_location()
            _send_registration_notification(info)
            with open(_REG_FILE, 'w') as f:
                f.write(_get_machine_id())
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _show_activation_blocker() -> bool:
    """Activation blocker — handles both P1 (direct, no TX) and P2 (online USDT, auto-verify on-chain)."""
    try:
        import tkinter as tk
    except ImportError:
        return False

    mid = _get_machine_id()
    _result = [False]

    root = tk.Tk()
    root.title("StopLoss Calculator — Activation")
    _set_icon(root)
    root.update_idletasks()
    w, h = 440, 500
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.resizable(False, False)
    root.configure(bg='#0d0d0f')
    root.attributes('-topmost', True)

    # ── Title ────────────────────────────────────────────────────────────
    tk.Label(root, text="Activate StopLoss Calculator",
             font=('Segoe UI', 14, 'bold'), bg='#0d0d0f', fg='white').pack(pady=(24, 2))
    tk.Label(root, text="Paid online? Verify TX hash below.  Got it directly? Request access.",
             font=('Segoe UI', 9), bg='#0d0d0f', fg='#666').pack(pady=(0, 16))

    # ── Machine ID display ───────────────────────────────────────────────
    tk.Label(root, text="Your Machine ID",
             font=('Segoe UI', 8), bg='#0d0d0f', fg='#444').pack()
    id_frame = tk.Frame(root, bg='#1a1a1c')
    id_frame.pack(padx=40, fill='x', pady=(2, 12))
    id_var = tk.StringVar(value=mid)
    tk.Entry(id_frame, textvariable=id_var, font=('Courier New', 12, 'bold'),
             justify='center', state='readonly',
             readonlybackground='#1a1a1c', fg='#4fc3f7',
             relief='flat', bd=6).pack(fill='x')

    # ── TX Hash input ────────────────────────────────────────────────────
    tk.Label(root, text="Transaction Hash (from checkout page)",
             font=('Segoe UI', 8), bg='#0d0d0f', fg='#888').pack(anchor='w', padx=40)
    tx_frame = tk.Frame(root, bg='#1a1a1c', highlightbackground='#2a2a32',
                        highlightthickness=1)
    tx_frame.pack(padx=40, fill='x', pady=(4, 4))
    tx_var = tk.StringVar()
    tx_entry = tk.Entry(tx_frame, textvariable=tx_var,
                        font=('Courier New', 9), justify='left',
                        bg='#1a1a1c', fg='#e0e0e0', relief='flat',
                        bd=8, insertbackground='white')
    tx_entry.pack(fill='x')
    tx_entry.insert(0, "Paste your 64-character TX hash here…")
    tx_entry.config(fg='#555')

    def _on_focus_in(event):
        if tx_entry.get() == "Paste your 64-character TX hash here…":
            tx_entry.delete(0, 'end')
            tx_entry.config(fg='#e0e0e0')

    def _on_focus_out(event):
        if not tx_entry.get().strip():
            tx_entry.insert(0, "Paste your 64-character TX hash here…")
            tx_entry.config(fg='#555')

    tx_entry.bind('<FocusIn>',  _on_focus_in)
    tx_entry.bind('<FocusOut>', _on_focus_out)

    # ── Status label ─────────────────────────────────────────────────────
    status_var = tk.StringVar(value="")
    status_lbl = tk.Label(root, textvariable=status_var,
                          font=('Segoe UI', 9), bg='#0d0d0f', fg='#555',
                          wraplength=360)
    status_lbl.pack(pady=(6, 2))

    # ── Core backend function — handles P1 and P2 ────────────────────────
    def _call_link_endpoint(tx_hash, p1_mode=False):
        """
        p1_mode=True  → P1: no TX hash, submit Machine ID to pending queue for admin approval.
        p1_mode=False → P2: verify USDT TRC20 tx on-chain via TronGrid, auto-approve if valid.
                            Falls back to pending queue if TronGrid is unreachable.
        """
        import urllib.request as _ur, json as _json, time as _t2
        GIST_ID  = '8a8b52dc14c0ecca38121df01557ec99'
        API_URL  = f'https://api.github.com/gists/{GIST_ID}'
        # GIST_TOKEN intentionally removed from EXE — PAT lives only in the
        # Cloudflare Worker at _GIST_PROXY_URL (CF env var, never in client code).
        # GET requests work unauthenticated for public Gists (60 req/hour — adequate).
        # PATCH (write) requests POST to the CF Worker which re-signs with the PAT.
        WALLET        = 'TSPy3m6cY4VdqXyAbtfu8Ei5tT5PmQ5K1S'
        WALLET_HEX    = 'b430c38c8e0662959b7e82eaa7dda97ac4b91989'  # TronGrid returns hex, not base58
        USDT_CONTRACT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'   # USDT TRC20 mainnet
        AMOUNT_MIN    = 250_000_000                               # 250 USDT × 10^6

        try:
            # ── Collect system info ─────────────────────────────────────────────
            try:
                si = _collect_system_info(gps_ready=False)
            except Exception:
                si = {}
            model_str = f"{si.get('manufacturer','')} {si.get('model','')}".strip()
            loc_str   = f"{si.get('city','')} {si.get('region','')} {si.get('country','')}".strip()
            coords    = f"{si.get('loc','')} ({si.get('loc_src','IP')})" if si.get('loc') else '—'

            # ── GET current Gist (single round-trip for all files) ──────────────
            # No Authorization header needed — public Gist reads are unauthenticated.
            get_req = _ur.Request(API_URL, headers={
                'User-Agent': 'StopLossCalc/2'
            })
            with _ur.urlopen(get_req, timeout=10) as r:
                gist = _json.loads(r.read())
            files            = gist.get('files', {})
            pending_content  = (files.get('pending_txns.txt',  {}) or {}).get('content', '') or ''
            approved_content = (files.get('approved_ids.txt',  {}) or {}).get('content', '') or ''
            used_content     = (files.get('used_txns.txt',     {}) or {}).get('content', '') or ''

            # ═══════════════════════════════════════════════════════════════════
            # P1 PATH — direct customer, no TX hash needed
            # ═══════════════════════════════════════════════════════════════════
            if p1_mode:
                # Duplicate check
                for _ln in pending_content.splitlines():
                    _ln = _ln.strip()
                    if not _ln: continue
                    try:
                        if _json.loads(_ln).get('mid','').upper() == mid:
                            return 'pending', 'Already submitted — waiting for admin approval…'
                    except Exception:
                        pass

                entry_dict = {
                    'tx': 'P1-DIRECT', 'mid': mid, 'ts': int(_t2.time()),
                    'user': si.get('username',''), 'host': si.get('hostname',''),
                    'mac': si.get('mac',''), 'model': model_str,
                    'cpu': si.get('cpu',''), 'ram': si.get('ram_gb',''),
                    'screen': si.get('screen',''), 'arch': si.get('arch',''),
                    'os': si.get('os',''), 'ip': si.get('ip',''),
                    'city': si.get('city',''), 'region': si.get('region',''),
                    'country': si.get('country',''), 'org': si.get('org',''),
                    'loc': si.get('loc',''), 'loc_src': si.get('loc_src','IP'),
                    'p1': True,
                }
                entry_line  = _json.dumps(entry_dict, separators=(',', ':'))
                new_pending = (pending_content.rstrip('\n') + '\n' + entry_line + '\n') if pending_content.strip() else entry_line + '\n'
                patch_data  = _json.dumps({'files': {'pending_txns.txt': {'content': new_pending}}}).encode()
                # P1 write: POST to CF Worker proxy (holds GitHub PAT in env variables)
                patch_req   = _ur.Request(_GIST_PROXY_URL, data=patch_data, method='POST', headers={
                    'Content-Type': 'application/json',
                    'User-Agent':   'StopLossCalc/2'
                })
                with _ur.urlopen(patch_req, timeout=15) as r:
                    _json.loads(r.read())

                try:
                    ntfy_msg = (
                        f"[P1 Direct]\n"
                        f"ID:      {mid}\n"
                        f"User:    {si.get('username','')}\n"
                        f"Host:    {si.get('hostname','')}\n"
                        f"MAC:     {si.get('mac','')}\n"
                        f"Model:   {model_str}\n"
                        f"CPU:     {si.get('cpu','')}\n"
                        f"RAM:     {si.get('ram_gb','')}\n"
                        f"Screen:  {si.get('screen','')}\n"
                        f"Arch:    {si.get('arch','')}\n"
                        f"OS:      {si.get('os','')}\n"
                        f"IP:      {si.get('ip','')} — {loc_str}\n"
                        f"Org:     {si.get('org','')}\n"
                        f"Coords:  {coords}\n\n"
                        f"Approve in admin panel to activate."
                    )
                    ntfy_req = _ur.Request(
                        _NOTIFY_URL, data=ntfy_msg.encode('utf-8'),
                        headers={'Title': f'P1 Request: {mid}', 'Priority': 'high',
                                 'Tags': 'hourglass_flowing_sand', 'Content-Type': 'text/plain'},
                        method='POST'
                    )
                    _ur.urlopen(ntfy_req, timeout=8)
                except Exception:
                    pass

                return 'pending', 'Access requested! Admin will approve shortly — this window polls automatically.'

            # ═══════════════════════════════════════════════════════════════════
            # P2 PATH — verify USDT TRC20 payment on-chain
            # ═══════════════════════════════════════════════════════════════════

            # Replay-attack guard — TX already used? (case-insensitive)
            tx_hash = tx_hash.lower()
            for _ln in used_content.splitlines():
                if _ln.strip().lower() == tx_hash:
                    return 'error', '❌ This TX hash has already been used to activate another machine.'

            # Duplicate pending check
            for _ln in pending_content.splitlines():
                _ln = _ln.strip()
                if not _ln: continue
                try:
                    if _json.loads(_ln).get('tx', '').lower() == tx_hash:
                        return 'pending', 'Already submitted — waiting for approval…'
                except Exception:
                    if _ln.startswith(tx_hash + ':'):
                        return 'pending', 'Already submitted — waiting for approval…'

            # ── On-chain verification via TronGrid ──────────────────────────────
            verified = None   # None = network error, True = paid, False = not paid
            try:
                events_req = _ur.Request(
                    f'https://api.trongrid.io/v1/transactions/{tx_hash}/events',
                    headers={'User-Agent': 'StopLossCalc/2', 'Accept': 'application/json'}
                )
                with _ur.urlopen(events_req, timeout=12) as r:
                    events_data = _json.loads(r.read())

                for event in events_data.get('data', []):
                    ca = event.get('contract_address', event.get('caller_contract_address', ''))
                    if ca.lower() != USDT_CONTRACT.lower():
                        continue
                    res     = event.get('result', {})
                    to_addr = res.get('to', res.get('1', ''))
                    value   = int(res.get('value', res.get('2', 0)) or 0)
                    # TronGrid returns addresses in hex (0x…), base58 comparison would always fail
                    addr_norm = to_addr.lower().lstrip('0x') if to_addr.startswith('0x') else to_addr.lower()
                    if (addr_norm == WALLET_HEX.lower() or to_addr.upper() == WALLET.upper()) and value >= AMOUNT_MIN:
                        verified = True
                        break
                if verified is None:
                    verified = False   # got a response but no matching transfer
            except Exception:
                verified = None        # TronGrid unreachable — fall back to pending

            # ── Verified → AUTO-APPROVE ─────────────────────────────────────────
            if verified:
                approved_ids = {ln.strip().upper() for ln in approved_content.splitlines() if ln.strip()}
                new_approved = approved_content.rstrip('\n') + '\n' + mid + '\n' if mid not in approved_ids else approved_content
                new_used     = used_content.rstrip('\n') + '\n' + tx_hash + '\n'

                patch_data = _json.dumps({
                    'files': {
                        'approved_ids.txt': {'content': new_approved.lstrip('\n')},
                        'used_txns.txt':    {'content': new_used.lstrip('\n')},
                    }
                }).encode()
                # P2 auto-approve write: POST to CF Worker proxy
                patch_req = _ur.Request(_GIST_PROXY_URL, data=patch_data, method='POST', headers={
                    'Content-Type': 'application/json',
                    'User-Agent':   'StopLossCalc/2'
                })
                with _ur.urlopen(patch_req, timeout=15) as r:
                    _json.loads(r.read())

                # APPROVED ntfy → per-machine topic (EXE poller picks this up)
                try:
                    topic = f"slcalc_{mid[:10].lower()}"
                    _ur.urlopen(_ur.Request(
                        f'https://ntfy.sh/{topic}',
                        data=f'StopLoss license APPROVED. Machine: {mid}'.encode(),
                        headers={'Title': f'APPROVED:{mid}', 'Priority': 'high',
                                 'Content-Type': 'text/plain'},
                        method='POST'
                    ), timeout=8)
                except Exception:
                    pass

                # Notify admin (informational — no action needed)
                try:
                    ntfy_msg = (
                        f"[AUTO-APPROVED — P2]\n"
                        f"TX:      {tx_hash[:20]}…\n"
                        f"ID:      {mid}\n"
                        f"User:    {si.get('username','')}\n"
                        f"Host:    {si.get('hostname','')}\n"
                        f"MAC:     {si.get('mac','')}\n"
                        f"Model:   {model_str}\n"
                        f"IP:      {si.get('ip','')} — {loc_str}\n"
                        f"Coords:  {coords}\n\n"
                        f"Payment verified on-chain. Auto-activated — no action needed."
                    )
                    _ur.urlopen(_ur.Request(
                        _NOTIFY_URL, data=ntfy_msg.encode('utf-8'),
                        headers={'Title': f'Auto-Approved: {mid}', 'Priority': 'default',
                                 'Tags': 'white_check_mark', 'Content-Type': 'text/plain'},
                        method='POST'
                    ), timeout=8)
                except Exception:
                    pass

                return 'approved', '✅ Payment verified on-chain! Activating your license…'

            # ── Not verified (wrong TX / wrong amount) ──────────────────────────
            if verified is False:
                return 'error', '❌ Payment not found — TX exists but no $250 USDT to our wallet. Check your TX hash and try again.'

            # ── TronGrid unreachable — fall back to pending (admin reviews) ─────
            entry_dict = {
                'tx': tx_hash, 'mid': mid, 'ts': int(_t2.time()),
                'user': si.get('username',''), 'host': si.get('hostname',''),
                'mac': si.get('mac',''), 'model': model_str,
                'cpu': si.get('cpu',''), 'ram': si.get('ram_gb',''),
                'screen': si.get('screen',''), 'arch': si.get('arch',''),
                'os': si.get('os',''), 'ip': si.get('ip',''),
                'city': si.get('city',''), 'region': si.get('region',''),
                'country': si.get('country',''), 'org': si.get('org',''),
                'loc': si.get('loc',''), 'loc_src': si.get('loc_src','IP'),
            }
            entry_line  = _json.dumps(entry_dict, separators=(',', ':'))
            new_pending = (pending_content.rstrip('\n') + '\n' + entry_line + '\n') if pending_content.strip() else entry_line + '\n'
            patch_data  = _json.dumps({'files': {'pending_txns.txt': {'content': new_pending}}}).encode()
            # P2 TronGrid-unreachable fallback: POST to CF Worker proxy
            patch_req   = _ur.Request(_GIST_PROXY_URL, data=patch_data, method='POST', headers={
                'Content-Type': 'application/json',
                'User-Agent':   'StopLossCalc/2'
            })
            with _ur.urlopen(patch_req, timeout=15) as r:
                _json.loads(r.read())

            try:
                ntfy_msg = (
                    f"[PENDING — TronGrid unreachable]\n"
                    f"TX:      {tx_hash[:20]}…\n"
                    f"ID:      {mid}\n"
                    f"User:    {si.get('username','')}\n"
                    f"Host:    {si.get('hostname','')}\n"
                    f"MAC:     {si.get('mac','')}\n"
                    f"Model:   {model_str}\n"
                    f"IP:      {si.get('ip','')} — {loc_str}\n"
                    f"Coords:  {coords}\n\n"
                    f"Verify TX manually on TronScan and approve."
                )
                _ur.urlopen(_ur.Request(
                    _NOTIFY_URL, data=ntfy_msg.encode('utf-8'),
                    headers={'Title': f'Manual Review: {mid}', 'Priority': 'high',
                             'Tags': 'hourglass_flowing_sand', 'Content-Type': 'text/plain'},
                    method='POST'
                ), timeout=8)
            except Exception:
                pass

            return 'pending', 'Could not reach TronGrid — request submitted for manual review. Admin will approve shortly.'

        except Exception as e:
            return 'error', f'Could not submit: {e}'

    # ── P2: Verify & Activate button ─────────────────────────────────────
    def activate_clicked():
        raw_tx = tx_var.get().strip()
        placeholder = "Paste your 64-character TX hash here…"
        if not raw_tx or raw_tx == placeholder or len(raw_tx) < 20:
            status_var.set("⚠ Please paste your Transaction Hash first.")
            status_lbl.config(fg='#ff7043')
            tx_entry.focus_set()
            return

        activate_btn.config(state='disabled', text='Verifying on-chain…')
        request_btn.config(state='disabled')
        status_var.set("Checking blockchain…")
        status_lbl.config(fg='#888')
        root.update()

        def _run():
            result, msg = _call_link_endpoint(raw_tx, p1_mode=False)
            if result == 'approved':
                def _direct_activate():
                    status_var.set(msg)
                    status_lbl.config(fg='#4caf50')
                    _result[0] = True
                    root.after(1500, root.destroy)
                root.after(0, _direct_activate)
            elif result == 'pending':
                root.after(0, lambda: (
                    status_var.set("⏳ " + msg),
                    status_lbl.config(fg='#4fc3f7'),
                    activate_btn.config(state='disabled', text='Pending…'),
                    request_btn.config(state='normal'),
                ))
            else:
                root.after(0, lambda: (
                    status_var.set(msg or "❌ Connection error. Check your internet."),
                    status_lbl.config(fg='#ff5252'),
                    activate_btn.config(state='normal', text='Verify & Activate →'),
                    request_btn.config(state='normal'),
                ))

        threading.Thread(target=_run, daemon=True).start()

    activate_btn = tk.Button(root, text="Verify & Activate →", command=activate_clicked,
                             bg='#14cc42', fg='#000', relief='flat',
                             font=('Segoe UI', 11, 'bold'), padx=20, pady=9,
                             cursor='hand2', activebackground='#1de84e')
    activate_btn.pack(pady=(8, 2))

    # ── Divider ──────────────────────────────────────────────────────────
    tk.Frame(root, bg='#1e1e22', height=1).pack(fill='x', padx=40, pady=(10, 6))
    tk.Label(root, text="— received directly from developer (no TX hash)? —",
             font=('Segoe UI', 8), bg='#0d0d0f', fg='#333').pack()

    # ── P1: Request Access button ─────────────────────────────────────────
    def request_access_clicked():
        request_btn.config(state='disabled', text='Submitting…')
        activate_btn.config(state='disabled')
        status_var.set("Submitting access request…")
        status_lbl.config(fg='#888')
        root.update()

        def _run():
            result, msg = _call_link_endpoint('', p1_mode=True)
            if result == 'pending':
                root.after(0, lambda: (
                    status_var.set("⏳ " + msg),
                    status_lbl.config(fg='#4fc3f7'),
                    request_btn.config(state='disabled', text='Request sent ✓'),
                    activate_btn.config(state='normal', text='Verify & Activate →'),
                ))
            else:
                root.after(0, lambda: (
                    status_var.set(msg or "❌ Could not submit. Check your internet."),
                    status_lbl.config(fg='#ff5252'),
                    request_btn.config(state='normal', text='Request Access →'),
                    activate_btn.config(state='normal', text='Verify & Activate →'),
                ))

        threading.Thread(target=_run, daemon=True).start()

    request_btn = tk.Button(root, text="Request Access →", command=request_access_clicked,
                            bg='#1565c0', fg='white', relief='flat',
                            font=('Segoe UI', 11, 'bold'), padx=20, pady=9,
                            cursor='hand2', activebackground='#1976d2')
    request_btn.pack(pady=(6, 2))

    # ── Divider ──────────────────────────────────────────────────────────
    tk.Frame(root, bg='#1e1e22', height=1).pack(fill='x', padx=40, pady=(10, 6))
    tk.Label(root, text="— already submitted? check approval status below —",
             font=('Segoe UI', 8), bg='#0d0d0f', fg='#333').pack()

    # ── Secondary buttons ────────────────────────────────────────────────
    def check_again():
        status_var.set("Checking…")
        status_lbl.config(fg='#888')
        root.update()
        def _do_check():
            try:
                os.remove(_LIC_CACHE)
            except Exception:
                pass
            ok = _check_approved()
            def _update():
                if ok:
                    _result[0] = True
                    status_var.set("✓ Access granted!")
                    status_lbl.config(fg='#4caf50')
                    root.after(900, root.destroy)
                else:
                    status_var.set("Not approved yet — admin is reviewing your request.")
                    status_lbl.config(fg='#ff7043')
            root.after(0, _update)
        threading.Thread(target=_do_check, daemon=True).start()

    def copy_id():
        root.clipboard_clear()
        root.clipboard_append(mid)
        status_var.set("Machine ID copied!")
        status_lbl.config(fg='#4fc3f7')

    btn_frame = tk.Frame(root, bg='#0d0d0f')
    btn_frame.pack(pady=8)
    tk.Button(btn_frame, text="Copy ID", command=copy_id,
              bg='#1a1a1c', fg='#4fc3f7', relief='flat',
              font=('Segoe UI', 9), padx=12, pady=6,
              cursor='hand2').pack(side='left', padx=4)
    tk.Button(btn_frame, text="Check Again", command=check_again,
              bg='#333', fg='#aaa', relief='flat',
              font=('Segoe UI', 9), padx=12, pady=6,
              cursor='hand2').pack(side='left', padx=4)

    root.protocol("WM_DELETE_WINDOW", root.destroy)

    # ── ntfy instant-activation poller ───────────────────────────────────
    def _instant_approval_poll():
        """Poll per-machine ntfy topic every 10 s — auto-activate on APPROVED push."""
        import urllib.request as _ur, json as _json, time as _t
        topic = f"slcalc_{mid[:10].lower()}"
        since = [int(_t.time())]
        while True:
            _t.sleep(10)
            try:
                url = f"https://ntfy.sh/{topic}/json?poll=1&since={since[0]}"
                with _ur.urlopen(url, timeout=6) as r:
                    for raw in r:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            data = _json.loads(raw)
                            since[0] = max(since[0], int(data.get('time', since[0])))
                            if 'APPROVED' in data.get('title', ''):
                                # Instant push-approval — write local cache, auto-close blocker
                                try:
                                    import time as _t3
                                    with open(_LIC_CACHE, 'w') as _cf:
                                        _cf.write(f"{int(_t3.time())}:{mid.upper()}")
                                except Exception:
                                    pass
                                def _approve_ui():
                                    try:
                                        _result[0] = True
                                        status_var.set("✅ Access Approved! Loading…")
                                        status_lbl.config(fg='#4caf50')
                                        root.after(1200, root.destroy)
                                    except Exception:
                                        pass
                                root.after(0, _approve_ui)
                                return  # stop polling — activation confirmed
                        except Exception:
                            pass
            except Exception:
                pass

    threading.Thread(target=_instant_approval_poll, daemon=True).start()

    root.mainloop()
    return _result[0]
