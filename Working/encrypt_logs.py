# encrypt_logs.py — one-time (and re-runnable) cleanup: AES-encrypt every
# existing StopLossPro/Kivy debug log instead of deleting them.
#
# Why: %USERPROFILE%\.kivy\logs\ accumulates plain-text debug logs that can
# contain session tokens, machine IDs, and internal event traces. Deleting
# them destroys history that might be useful later; leaving them as plain
# text is a readable blueprint of the app's internals. This script encrypts
# each file in place (AES-128 via Fernet) and removes the plaintext original,
# so the data is preserved but unreadable without the key.
#
# Safe to re-run: only touches files that aren't already *.enc.
#
# Usage:   double-click run_encrypt_logs.bat  (installs 'cryptography' then
#          runs this script against %USERPROFILE%\.kivy\logs)

import os
import glob
import sys

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("ERROR: 'cryptography' package not installed. Run via run_encrypt_logs.bat")
    sys.exit(1)

LOGS_DIR = os.path.join(os.path.expanduser('~'), '.kivy', 'logs')
KEY_FILE = os.path.join(os.path.expanduser('~'), '.kivy', 'logs.key')


def get_or_create_key() -> bytes:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_FILE, 'wb') as f:
        f.write(key)
    try:
        os.system(f'attrib +h "{KEY_FILE}"')  # hide the key file from casual browsing
    except Exception:
        pass
    return key


def main():
    if not os.path.isdir(LOGS_DIR):
        print(f"No logs directory found at {LOGS_DIR} — nothing to do.")
        return

    key = get_or_create_key()
    fernet = Fernet(key)

    candidates = [
        p for p in glob.glob(os.path.join(LOGS_DIR, '*'))
        if os.path.isfile(p) and not p.endswith('.enc') and not p.endswith('.key')
    ]

    if not candidates:
        print("No plaintext log files found — already encrypted or empty.")
        return

    encrypted = 0
    failed = 0
    for path in candidates:
        try:
            with open(path, 'rb') as f:
                data = f.read()
            token = fernet.encrypt(data)
            enc_path = path + '.enc'
            with open(enc_path, 'wb') as f:
                f.write(token)
            os.remove(path)
            encrypted += 1
        except Exception as e:
            print(f"  FAILED: {os.path.basename(path)} — {e}")
            failed += 1

    print(f"\nEncrypted {encrypted} file(s) in {LOGS_DIR}")
    if failed:
        print(f"Failed to encrypt {failed} file(s) (see above).")
    print(f"Key stored at: {KEY_FILE} (hidden attribute set)")
    print("Keep this key file safe — without it, the .enc files cannot be read back.")
    print("Re-run this script any time to sweep up newly created plaintext logs.")


if __name__ == '__main__':
    main()
    input("\nPress Enter to close...")
