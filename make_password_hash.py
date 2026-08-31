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
import subprocess
import sys
from pathlib import Path

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

    first = input("\nIs this the first admin for this app? [y/N]: ").strip().lower()

    lines: list[str] = []
    if first.startswith("y"):
        print()
        print("Paste the database URL from Neon (the one containing '-pooler').")
        print("Right-click to paste in this window. Press Enter to skip and")
        print("fill it in yourself later.")
        database_url = input("Neon URL: ").strip()
        lines.append(
            f'DATABASE_URL = "{database_url}"'
            if database_url
            else 'DATABASE_URL = "PUT-YOUR-NEON-POOLED-URL-HERE"'
        )
        lines.append("")
        lines.append("[auth]")
        lines.append('cookie_name = "ks_academia_auth"')
        # Signs the "stay signed in" cookie. Anyone holding it could mint a
        # cookie for any user, so it is generated here rather than being a
        # memorable string, and it belongs only in the secrets page.
        lines.append(f'cookie_key = "{secrets.token_urlsafe(32)}"')
        lines.append("cookie_expiry_days = 30")
        lines.append("")
    lines.append(f"[auth.credentials.usernames.{username}]")
    lines.append(f'name = "{display}"')
    lines.append(f'password = "{hashed}"')
    block = "\n".join(lines) + "\n"

    # Written to a file as well as printed: copying cleanly out of a console
    # window is fiddly, and this has to arrive in the Secrets box byte for
    # byte or the app will not start. Gitignored -- it holds the database URL
    # and a password hash.
    out = Path(__file__).resolve().parent / "secrets-to-paste.txt"
    out.write_text(block, encoding="utf-8")

    print()
    print("=" * 68)
    print(block, end="")
    print("=" * 68)
    print()
    if not first.startswith("y"):
        print("Add these three lines under the [auth] block you already have.")
        print("Do not change the existing cookie_key -- that signs everyone out.")
        print()
    print(f"Also saved to: {out}")
    print()
    print("Next: open that file, select all, copy, and paste it into")
    print("Settings -> Secrets for the app on share.streamlit.io, then Save.")
    print()
    print("The hash is safe to paste. Your password is not -- do not send it")
    print("to anyone, and do not put it in a chat window.")

    # Only when a person is actually sitting there. Run from a script or a
    # pipe, opening an editor would hang waiting for a window nobody sees.
    if sys.stdin.isatty():
        try:
            subprocess.run(["notepad.exe", str(out)], check=False)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
