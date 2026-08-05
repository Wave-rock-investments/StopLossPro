# decrypt_log.py — read back one encrypted log for support/debugging.
# Usage: python decrypt_log.py "C:\path\to\kivy_26-08-04_77.txt.enc"
# Requires logs.key to still exist at %USERPROFILE%\.kivy\logs.key

import os
import sys

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("ERROR: 'cryptography' package not installed. Run: pip install cryptography")
    sys.exit(1)

KEY_FILE = os.path.join(os.path.expanduser('~'), '.kivy', 'logs.key')


def main():
    if len(sys.argv) != 2:
        print("Usage: python decrypt_log.py <path-to-.enc-file>")
        sys.exit(1)
    enc_path = sys.argv[1]
    if not os.path.exists(KEY_FILE):
        print(f"Key file not found at {KEY_FILE} — cannot decrypt.")
        sys.exit(1)
    with open(KEY_FILE, 'rb') as f:
        key = f.read()
    fernet = Fernet(key)
    with open(enc_path, 'rb') as f:
        token = f.read()
    data = fernet.decrypt(token)
    out_path = enc_path[:-4] if enc_path.endswith('.enc') else enc_path + '.decrypted'
    with open(out_path, 'wb') as f:
        f.write(data)
    print(f"Decrypted -> {out_path}")


if __name__ == '__main__':
    main()
