"""Pre-release verification gate.

    python verify_release.py

Run this BEFORE signing and BEFORE shipping any build. It fails loudly rather
than warning quietly, because every check here corresponds to a defect that has
actually occurred in this project at least once.

Exit code 0 = safe to sign and ship. Anything else = do not ship.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIB = ROOT / "lib"

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN  {msg}")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


SELF = Path(__file__).name


def source_files() -> list[Path]:
    """Files that actually ship inside the exe.

    Excludes this verifier itself — it necessarily contains every banned
    pattern as a search string, and would otherwise flag itself forever.
    """
    out = []
    for pat in ("*.py", "*.kv", "*.bat", "*.spec"):
        for p in list(ROOT.glob(pat)) + list(LIB.rglob(pat)):
            if any(x in p.parts for x in ("build", "dist", "__pycache__", "tests")):
                continue
            if p.name == SELF:
                continue
            out.append(p)
    return out


# ── 1. no credentials of any kind ──────────────────────────────────────────
def check_no_secrets() -> None:
    print("\n[1] Credentials in shipped source")
    patterns = {
        "GitHub PAT": r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
        "private key block": r"BEGIN (RSA |OPENSSH |EC |)PRIVATE KEY",
        "AWS key": r"AKIA[0-9A-Z]{16}",
        "signing private key var": r"SIGNING_PRIVATE_KEY",
    }
    hits = 0
    for p in source_files():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, rx in patterns.items():
            if re.search(rx, txt):
                fail(f"{label} found in {p.name}")
                hits += 1
    if not hits:
        ok("no credentials, private keys or tokens in shipped source")


# ── 2. dead trust paths must stay dead ─────────────────────────────────────
def check_no_legacy_trust_paths() -> None:
    print("\n[2] Legacy trust paths (must be absent)")
    banned = {
        "ntfy.sh": "public pub/sub — activation and kill-switch by anyone",
        "gist.githubusercontent.com": "client-trusted licence allowlist",
        "api.github.com/gists": "direct gist writes from the client",
        "workers.dev": "unauthenticated Cloudflare write oracle",
        "ipinfo.io": "geolocation lookup with no licensing purpose",
        "GeoCoordinateWatcher": "GPS collection",
    }
    hits = 0
    for p in source_files():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for needle, why in banned.items():
            for ln in txt.splitlines():
                if needle in ln:
                    s = ln.strip()
                    # allow documentation of what was removed
                    if s.startswith("#") or "REMOVED" in ln or "PHASE 12" in ln:
                        continue
                    fail(f"{p.name}: live reference to {needle} ({why})")
                    hits += 1
    if not hits:
        ok("no live references to ntfy, gist, worker, ipinfo or GPS")


# ── 3. privacy ─────────────────────────────────────────────────────────────
def check_no_privacy_invasive_collection() -> None:
    print("\n[3] Privacy — banned data collection")
    banned = ["GeoCoordinate", "Win32_NetworkAdapterConfiguration",
              "MACAddress", "getnode()"]
    hits = 0
    for p in source_files():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for b in banned:
            for ln in txt.splitlines():
                if b in ln and not ln.strip().startswith("#") and "REMOVED" not in ln:
                    fail(f"{p.name}: collects {b}")
                    hits += 1
    if not hits:
        ok("no GPS, MAC address or hardware-fingerprint collection")


# ── 4. verification key present, private key absent ────────────────────────
def check_signing_key_config() -> None:
    print("\n[4] Grant verification key")
    lic = LIB / "licensing.py"
    if not lic.exists():
        fail("lib/licensing.py missing")
        return
    txt = lic.read_text(encoding="utf-8", errors="ignore")

    m = re.search(r'SERVER_PUBLIC_KEY_B64\s*=\s*"([^"]*)"', txt)
    if not m:
        fail("SERVER_PUBLIC_KEY_B64 not found in licensing.py")
    elif not m.group(1).strip():
        fail("SERVER_PUBLIC_KEY_B64 is EMPTY — the client cannot verify grants. "
             "Populate it from `GET /api/v1/pubkey` before building.")
    else:
        ok("public verification key is embedded")

    if "PRIVATE KEY" in txt.upper().replace("PRIVATE KEY MATERIAL", ""):
        if re.search(r"BEGIN.*PRIVATE KEY", txt):
            fail("licensing.py appears to contain PRIVATE key material")
    else:
        ok("no private key material in the client")


# ── 5. logging locked down ─────────────────────────────────────────────────
def check_log_lockdown() -> None:
    print("\n[5] Production logging")
    main = ROOT / "Product Sell.py"
    txt = main.read_text(encoding="utf-8", errors="ignore")
    if "KIVY_NO_FILELOG" in txt and "STOPLOSSPRO_DEBUG" in txt:
        ok("file logging disabled by default, debug behind an env flag")
    else:
        fail("Product Sell.py does not disable Kivy file logging")

    if os.environ.get("STOPLOSSPRO_DEBUG") == "1":
        warn("STOPLOSSPRO_DEBUG=1 is set in this shell — do not build with it set")


# ── 6. risk engine untouched ───────────────────────────────────────────────
def check_risk_engine_baseline() -> None:
    print("\n[6] Risk engine regression baseline")
    t = ROOT / "tests" / "test_risk_engine_baseline.py"
    if not t.exists():
        fail("risk engine baseline test missing")
        return
    import subprocess
    r = subprocess.run([sys.executable, str(t)], capture_output=True, text=True)
    if r.returncode == 0:
        ok("risk engine baseline passes (65 checks)")
    else:
        fail("RISK ENGINE REGRESSION — do not ship")
        print(r.stdout[-1500:])


# ── 7. build config ────────────────────────────────────────────────────────
def check_build_config() -> None:
    print("\n[7] Build configuration")
    b = ROOT / "build.bat"
    if not b.exists():
        fail("build.bat missing")
        return
    txt = b.read_text(encoding="utf-8", errors="ignore")
    for flag, why in [("--version-file", "exe metadata (blank metadata looks like malware)"),
                      ("--noupx", "UPX packing raises AV false positives")]:
        ok(f"{flag} present — {why}") if flag in txt else warn(f"{flag} missing — {why}")

    if (ROOT / "version_info.txt").exists():
        ok("version_info.txt present")
    else:
        warn("version_info.txt missing")


def main() -> int:
    print("=" * 72)
    print("StopLossPro — pre-release verification")
    print("=" * 72)
    for fn in (check_no_secrets, check_no_legacy_trust_paths,
               check_no_privacy_invasive_collection, check_signing_key_config,
               check_log_lockdown, check_risk_engine_baseline, check_build_config):
        try:
            fn()
        except Exception as exc:
            fail(f"{fn.__name__} crashed: {exc}")

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} BLOCKING FAILURE(S) — DO NOT SHIP")
        for f in FAILURES:
            print(f"  - {f}")
        if WARNINGS:
            print(f"\n{len(WARNINGS)} warning(s):")
            for w in WARNINGS:
                print(f"  - {w}")
        return 1

    if WARNINGS:
        print(f"RESULT: PASSED with {len(WARNINGS)} warning(s)")
        for w in WARNINGS:
            print(f"  - {w}")
        return 0

    print("RESULT: ALL CHECKS PASSED — safe to sign and ship")
    return 0


if __name__ == "__main__":
    sys.exit(main())
