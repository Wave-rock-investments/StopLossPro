#!/usr/bin/env python3
"""One-off script: sends ONE sample Sterling_Room Premium call, using the
already-finished premium-desk design, straight to your real Telegram
channel. Nothing else — no server, no worker, no database.

Reads STERLING_TELEGRAM_BOT_TOKEN and STERLING_TELEGRAM_PREMIUM_CHAT_ID
from the .env file already sitting next to this script (or from your shell
environment). Never prints the token.

Run it from a machine with real internet access (your PC — this sandbox
and the device bridge both can't reach api.telegram.org):

    cd SterlingRoom/backend
    python3 send_test_call_to_channel.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env manually (no extra dependency) so this runs standalone.
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.environ.get("STERLING_TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("STERLING_TELEGRAM_PREMIUM_CHAT_ID", "")

if not BOT_TOKEN or not CHAT_ID:
    print("Missing STERLING_TELEGRAM_BOT_TOKEN or STERLING_TELEGRAM_PREMIUM_CHAT_ID "
          "(checked .env and environment). Nothing sent.")
    sys.exit(1)

from app import telegram_bot as tb
from app.models import Call, CallDirection, CallStatus
import uuid

sample = Call(
    id=uuid.uuid4(), trade_id="SR-TEST-001", source_call_id="manual-test",
    instrument="BTCUSD", direction=CallDirection.BUY,
    entry_min=63209.70, entry_max=None, stop_loss=63123.75,
    tp1=63381.60, tp2=63467.55, tp3=63553.50,
    risk_percent=1.5, status=CallStatus.ACTIVE,
)
text = tb.render_entry_message(sample)

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
body = json.dumps({"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}).encode()
req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    result = json.loads(e.read().decode())

if result.get("ok"):
    print(f"Sent. Telegram message_id={result['result']['message_id']} in chat {CHAT_ID}.")
else:
    print(f"Telegram rejected it: {result.get('description')}")
