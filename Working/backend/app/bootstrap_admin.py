"""Create the first administrator. Run once, interactively.

    python -m app.bootstrap_admin

Deliberately interactive: no default password, no seeded account, no
credentials passed as command-line arguments (they would land in shell history
and process listings).

MFA is mandatory. An admin compromise would compromise every customer licence,
so there is no path here that creates an admin without a second factor.
"""
from __future__ import annotations

import getpass
import sys

import pyotp
from sqlalchemy import select

from app import security
from app.admin import AdminRole, AdminUser
from app.database import SessionLocal


def main() -> int:
    db = SessionLocal()
    try:
        if db.execute(select(AdminUser)).scalars().first():
            print("An administrator already exists. Refusing to create another here.")
            print("Add further admins from the admin panel once RBAC is enabled.")
            return 1

        print("=" * 66)
        print("Create the first StopLossPro administrator")
        print("=" * 66)

        email = input("Email: ").strip().lower()
        if "@" not in email:
            print("That does not look like an email address.")
            return 1

        pw = getpass.getpass("Password (min 12 chars): ")
        if len(pw) < 12:
            print("Too short. Use at least 12 characters.")
            return 1
        if pw != getpass.getpass("Confirm password: "):
            print("Passwords do not match.")
            return 1

        secret = security.new_totp_secret()
        uri = security.totp_provisioning_uri(secret, email, issuer="StopLossPro Admin")

        print("\nAdd this to your authenticator app now:")
        print(f"\n  Secret : {secret}")
        print(f"  URI    : {uri}\n")

        code = input("Enter the 6-digit code to confirm: ").strip()
        if not pyotp.TOTP(secret).verify(code, valid_window=1):
            print("That code did not verify. Nothing was created — run again.")
            return 1

        admin = AdminUser(
            email=email,
            password_hash=security.hash_password(pw),
            totp_secret_encrypted=security.encrypt_totp_secret(secret),
            totp_confirmed=True,
            role=AdminRole.SUPER_ADMIN.value,
        )
        db.add(admin)
        db.commit()

        print("\nAdministrator created. Sign in at /admin/login")
        print("The TOTP secret above is not recoverable — if you lose your")
        print("authenticator you will need database access to reset it.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
