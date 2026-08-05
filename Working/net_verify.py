import urllib.request as ur, json, sys, time, os

GIST_ID    = '8a8b52dc14c0ecca38121df01557ec99'
# SECURITY 2026-08-05: hardcoded PAT removed (leaked, revoked — see docs/CREDENTIAL_INCIDENT.md).
# Developer-side only. Never hardcode a token here and never ship one in a client build.
#   PowerShell:  $env:STOPLOSS_GIST_TOKEN = "<token>"
#   cmd:         set STOPLOSS_GIST_TOKEN=<token>
GIST_TOKEN = os.environ.get('STOPLOSS_GIST_TOKEN', '')
if not GIST_TOKEN:
    sys.exit("ERROR: STOPLOSS_GIST_TOKEN environment variable is not set.\n"
             "This is a developer-side diagnostic script. Set the variable in your shell "
             "and re-run. Do not hardcode credentials in this file.")
API_URL    = f'https://api.github.com/gists/{GIST_ID}'
HERE       = os.path.dirname(os.path.abspath(__file__))
OUT_FILE   = os.path.join(HERE, 'net_verify_results.txt')

lines = []
ok_count = 0
fail_count = 0

def log(msg):
    print(msg, flush=True)
    lines.append(msg)

log("=" * 60)
log("  STOPLOSS NETWORK VERIFICATION")
log(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 60)

# ── 1. Gist READ ─────────────────────────────────────────────
log("\n[1] GitHub Gist READ...")
gist_data = {}
try:
    req = ur.Request(API_URL, headers={
        'Authorization': f'token {GIST_TOKEN}',
        'User-Agent': 'StopLossVerify/1'
    })
    with ur.urlopen(req, timeout=12) as r:
        gist_data = json.loads(r.read())
    files = gist_data.get('files', {})
    log(f"    PASS — {len(files)} files in Gist")
    for fname, fdata in files.items():
        content = (fdata or {}).get('content', '') or ''
        n = len([l for l in content.splitlines() if l.strip()])
        log(f"      {fname}: {n} entries")
    ok_count += 1
except Exception as e:
    log(f"    FAIL: {e}")
    fail_count += 1

# ── 2. Gist WRITE ────────────────────────────────────────────
log("\n[2] GitHub Gist WRITE...")
try:
    files = gist_data.get('files', {})
    pend  = (files.get('pending_ids.txt', {}) or {}).get('content', '') or ' '
    patch = json.dumps({'files': {'pending_ids.txt': {'content': pend}}}).encode()
    preq  = ur.Request(API_URL, data=patch, method='PATCH', headers={
        'Authorization': f'token {GIST_TOKEN}',
        'Content-Type':  'application/json',
        'User-Agent':    'StopLossVerify/1'
    })
    with ur.urlopen(preq, timeout=12) as r:
        r.read()
    log(f"    PASS — write confirmed")
    ok_count += 1
except Exception as e:
    log(f"    FAIL: {e}")
    fail_count += 1

# ── 3. ntfy admin ─────────────────────────────────────────────
log("\n[3] ntfy.sh admin channel...")
try:
    with ur.urlopen(ur.Request(
        'https://ntfy.sh/stoploss_dev_h7zltndg',
        data=b'[NETWORK TEST] StopLoss verify OK',
        headers={'Title': 'NetVerify', 'Priority': 'low', 'Content-Type': 'text/plain'},
        method='POST'
    ), timeout=8) as r:
        r.read()
    log(f"    PASS — ntfy admin channel OK")
    ok_count += 1
except Exception as e:
    log(f"    FAIL: {e}")
    fail_count += 1

# ── 4. ntfy per-machine ───────────────────────────────────────
log("\n[4] ntfy.sh per-machine topic...")
try:
    with ur.urlopen(ur.Request(
        'https://ntfy.sh/slcalc_nettest01',
        data=b'[NETWORK TEST] per-machine topic OK',
        headers={'Title': 'Test', 'Content-Type': 'text/plain'},
        method='POST'
    ), timeout=8) as r:
        r.read()
    log(f"    PASS — per-machine topic OK")
    ok_count += 1
except Exception as e:
    log(f"    FAIL: {e}")
    fail_count += 1

# ── 5. TronGrid ──────────────────────────────────────────────
log("\n[5] TronGrid API (P2 payment verify)...")
try:
    with ur.urlopen(ur.Request(
        'https://api.trongrid.io/v1/accounts/TSPy3m6cY4VdqXyAbtfu8Ei5tT5PmQ5K1S',
        headers={'Accept': 'application/json', 'User-Agent': 'StopLossVerify/1'}
    ), timeout=12) as r:
        body = json.loads(r.read())
    data_arr = body.get('data', [])
    status = data_arr[0].get('address', 'found') if data_arr else 'reachable (empty data)'
    log(f"    PASS — TronGrid OK: {status}")
    ok_count += 1
except Exception as e:
    log(f"    FAIL: {e}")
    fail_count += 1

# ── Summary ───────────────────────────────────────────────────
log("\n" + "=" * 60)
total = ok_count + fail_count
result_str = "ALL SYSTEMS GO" if fail_count == 0 else f"{fail_count} CHECK(S) FAILED"
log(f"  RESULT: {ok_count}/{total} PASSED — {result_str}")
log("=" * 60)

with open(OUT_FILE, 'w') as f:
    f.write('\n'.join(lines) + '\n')
print(f"\nSaved to: {OUT_FILE}", flush=True)
sys.exit(0 if fail_count == 0 else 1)
