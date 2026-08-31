"""Turn a password into the hash that goes in the app's secrets.

    python make_password_hash.py

It asks for the password without echoing it, and prints only the hash. The
password itself is never written to a file, never stored, and never appears
on screen -- paste the hash into Streamlit's secrets and the password stays
in the head of whoever chose it.

The output looks like::

    [auth.credentials.usernames.junxi]
    name = "Junxi"
    password = "$2b$12$......"
"""

from __future__ import annotations

import getpass
import re
import secrets
import sys

import streamlit_authenticator as stauth


def main() -> int:
    print(__doc__.split("The output looks like")[0].strip())
    print()

    username = input("Username to sign in with (e.g. junxi): ").strip()
    if not username:
        print("A username is required.")
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        print("Use only letters, numbers, dots, dashes or underscores.")
        return 2

    display = input(f"Display name [{username}]: ").strip() or username

    password = getpass.getpass("Password (not shown as you type): ")
    if len(password) < 8:
        print("Use at least 8 characters.")
        return 2
    if password != getpass.getpass("Type it again to confirm: "):
        print("The two did not match. Nothing was written; run it again.")
        return 2

    hashed = stauth.Hasher.hash(password)
    del password

    first = input("\nIs this the first user for this app? [y/N]: ").strip().lower()

    print()
    print("=" * 68)
    print("Paste this into Settings -> Secrets for the app on Streamlit:")
    print("=" * 68)
    print()
    if first.startswith("y"):
        print('DATABASE_URL = "PUT-YOUR-NEON-POOLED-URL-HERE"')
        print()
        print("[auth]")
        print('cookie_name = "ks_academia_auth"')
        # Signs the "stay signed in" cookie. Anyone holding it could mint a
        # cookie for any user, so it is generated here rather than being a
        # memorable string, and it belongs only in the secrets page.
        print(f'cookie_key = "{secrets.token_urlsafe(32)}"')
        print("cookie_expiry_days = 30")
        print()
    else:
        print("(adding to the existing [auth] block -- keep the cookie_key")
        print(" you already have, changing it signs everybody out)")
        print()
    print(f"[auth.credentials.usernames.{username}]")
    print(f'name = "{display}"')
    print(f'password = "{hashed}"')
    print()
    print("=" * 68)
    print("The hash above is safe to paste. The password is not -- do not")
    print("send it to anyone, and do not put it in a chat window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
