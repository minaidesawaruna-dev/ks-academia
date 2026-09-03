"""Check the sign-in details before blaming the app.

    python check_sign_in.py

Reads the secrets you pasted into Streamlit -- from secrets-to-paste.txt if
it is still here, otherwise from .streamlit/secrets.toml -- and checks the
shape of it: that it parses, that the pieces the app needs are present, and
that each stored password is a bcrypt hash rather than a password typed in
by mistake.

It then offers to test a password against a user's hash, so "it will not let
me in" can be answered here in a second instead of by editing the live app
and waiting for it to redeploy.

Everything happens on this computer. Nothing is sent anywhere, the password
is not echoed as you type, and it is not written down.
"""

from __future__ import annotations

import getpass
import sys
import tomllib
from pathlib import Path

import streamlit_authenticator as stauth

HERE = Path(__file__).resolve().parent
CANDIDATES = [HERE / "secrets-to-paste.txt", HERE / ".streamlit" / "secrets.toml"]

OK = "  OK   "
BAD = " PROBLEM "


def main() -> int:
    source = next((p for p in CANDIDATES if p.exists()), None)
    if source is None:
        print("Could not find your settings to check. Looked for:")
        for path in CANDIDATES:
            print(f"  {path}")
        print()
        print("Run 'Set up sign-in.bat' first, or paste what is currently in")
        print("the app's Secrets box into secrets-to-paste.txt and try again.")
        return 2

    print(f"Checking: {source}")
    print()

    try:
        config = tomllib.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} This is not valid TOML, so Streamlit will reject it.")
        print(f"         {exc}")
        print()
        print("Most likely something was lost or doubled when copying. Run")
        print("'Set up sign-in.bat' again and copy the whole file.")
        return 1
    print(f"{OK} Parses correctly.")

    problems = 0

    if config.get("DATABASE_URL", "").startswith("postgresql://"):
        pooled = "-pooler" in config["DATABASE_URL"]
        note = "" if pooled else " (but is NOT the -pooler one)"
        print(f"{OK} DATABASE_URL is set{note}.")
    else:
        print(f"{BAD} DATABASE_URL is missing or does not start with postgresql://")
        print("         Without it the app opens an empty database.")
        problems += 1

    auth = config.get("auth")
    if not isinstance(auth, dict):
        print(f"{BAD} No [auth] section, so nobody can sign in.")
        return 1

    if auth.get("cookie_key"):
        print(f"{OK} cookie_key is set.")
    else:
        print(f"{BAD} cookie_key is missing from [auth].")
        problems += 1

    users = (auth.get("credentials") or {}).get("usernames") or {}
    if not users:
        print(f"{BAD} No users under [auth.credentials.usernames.<name>]")
        return 1

    print(f"{OK} {len(users)} user(s): {', '.join(sorted(users))}")
    print()

    for username, details in sorted(users.items()):
        stored = str((details or {}).get("password", ""))
        if not stored:
            print(f"{BAD} '{username}' has no password line.")
            problems += 1
        elif not stauth.Hasher.is_hash(stored):
            print(f"{BAD} '{username}' has a plain password, not a hash.")
            print("         The app expects the hash that Set up sign-in.bat")
            print("         printed. A typed-in password can never match.")
            problems += 1
        else:
            print(f"{OK} '{username}' has a valid password hash.")

    print()
    if problems:
        print(f"Found {problems} thing(s) to fix above.")
        print("Fix them, paste the corrected block into Settings -> Secrets,")
        print("and save. The app restarts on its own.")
        return 1

    print("The settings look right. Now test the password itself.")
    print()
    default = sorted(users)[0]
    username = input(f"Which username? [{default}]: ").strip() or default
    if username not in users:
        print(f"{BAD} '{username}' is not in the list above.")
        print("         Usernames are case sensitive -- type it exactly.")
        return 1

    password = getpass.getpass("Password (not shown as you type): ")
    if stauth.Hasher.check_pw(password, str(users[username]["password"])):
        print()
        print(f"{OK} That password matches '{username}'.")
        print()
        print("So the details are correct. If the app still refuses, the")
        print("Secrets box on Streamlit does not hold what this file holds --")
        print("paste it again, making sure to replace everything in the box.")
        return 0

    print()
    print(f"{BAD} That password does not match '{username}'.")
    print()
    print("Either it is not the password typed into Set up sign-in.bat, or")
    print("the block was regenerated afterwards. Run it again to set a new")
    print("one, then paste the new block into Settings -> Secrets.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
