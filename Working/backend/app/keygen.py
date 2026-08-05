"""Ed25519 signing keypair generator.

    python -m app.keygen

Ed25519 rather than RSA: 32-byte keys, fast verification on the client, and no
padding-scheme choices to get wrong. Signature verification in the desktop app
must stay cheap because it runs on every heartbeat.

CRITICAL DISTRIBUTION RULE
  PRIVATE key -> server environment only. Never in the repo, never in the exe,
                 never in an installer, never in a build artefact.
  PUBLIC  key -> safe to embed in the desktop client. That is its whole purpose:
                 the client verifies grants it receives but can never mint one.

This prints to stdout only. It deliberately does not write a file, so a private
key cannot be left lying in the working tree by accident.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def generate() -> tuple[str, str]:
    private = ed25519.Ed25519PrivateKey.generate()
    public = private.public_key()

    priv_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return (
        base64.b64encode(priv_pem).decode(),
        base64.b64encode(pub_pem).decode(),
    )


def main() -> None:
    priv_b64, pub_b64 = generate()
    print("=" * 74)
    print("Ed25519 signing keypair")
    print("=" * 74)
    print()
    print("Set these in the SERVER environment (never in a file in this repo):")
    print()
    print(f"STOPLOSS_SIGNING_PRIVATE_KEY_B64={priv_b64}")
    print()
    print(f"STOPLOSS_SIGNING_PUBLIC_KEY_B64={pub_b64}")
    print()
    print("-" * 74)
    print("PRIVATE key: server only. If it ever reaches a client build, every")
    print("             licence in circulation can be forged and you must rotate.")
    print("PUBLIC  key: embed this one in the desktop client.")
    print("Rotation:    generate a new pair, bump STOPLOSS_SIGNING_KEY_ID, ship a")
    print("             client update that accepts both, then retire the old key.")
    print("=" * 74)


if __name__ == "__main__":
    main()
