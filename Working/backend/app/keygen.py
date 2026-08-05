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

Also generates the independent TOTP-secret encryption key (see app/config.py
for why this must be a separate secret from the signing key, not derived from
it). Rotating one must never force rotation of the other:
  - rotating the SIGNING key changes what future grants are signed with; it
    has no effect on already-encrypted TOTP secrets (STOPLOSS_TOTP_ENCRYPTION
    _KEY_B64 is untouched).
  - rotating the TOTP key requires re-encrypting existing stored TOTP secrets
    under the new key (it is symmetric encryption — there is no public half
    to swap in). Do this as a maintenance pass: decrypt every
    mfa_credentials.secret_encrypted with the OLD key, encrypt with the NEW
    key, then cut over the environment variable. Until that pass runs, keep
    the old key available (e.g. as a second env var) so admins can still
    complete MFA during the migration window.
"""
import base64
import secrets

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


def generate_totp_key() -> str:
    """256-bit random secret, independent of the signing keypair.

    Not a PEM/asymmetric key — it is raw key material fed through SHA-256 in
    app/security.py to derive a Fernet key. Any 32+ bytes of high-entropy
    randomness is sufficient; base64 is just the transport encoding.
    """
    return base64.b64encode(secrets.token_bytes(32)).decode()


def main() -> None:
    priv_b64, pub_b64 = generate()
    totp_b64 = generate_totp_key()

    print("=" * 74)
    print("Ed25519 signing keypair  (signs authorization grants ONLY)")
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
    print()
    print("=" * 74)
    print("TOTP encryption key  (protects MFA secrets ONLY — independent secret)")
    print("=" * 74)
    print()
    print("Set this in the SERVER environment too (never in a file in this repo):")
    print()
    print(f"STOPLOSS_TOTP_ENCRYPTION_KEY_B64={totp_b64}")
    print()
    print("-" * 74)
    print("This key never leaves the server and is never embedded in the client.")
    print("It must NOT equal the signing private key above — that would silently")
    print("re-couple the two security domains this migration was meant to split.")
    print("Rotating this key requires re-encrypting stored TOTP secrets (see the")
    print("module docstring); it does NOT require rotating the signing key.")
    print("=" * 74)


if __name__ == "__main__":
    main()
