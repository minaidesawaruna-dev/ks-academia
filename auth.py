"""Sign-in gate for KS Academia.

The app holds real students' names, attendance and payment records, so it
cannot simply be put on a URL and left there.

The host's own "private app" setting is not enough on its own. Set on
Streamlit Community Cloud, it redirects the app's front door to a sign-in
page but still serves the running app on its internal ``/~/+/`` path, which
was reachable from a phone on mobile data with no account at all. Access
control therefore lives here, in the app, where it does not depend on how the
host happens to route a request.

``require_login()`` is called before anything reads the database or draws a
student's name, and stops the script outright for anyone not signed in.

Credentials come from Streamlit's secrets, never from the repository::

    [auth]
    cookie_name = "ks_academia_auth"
    cookie_key = "a-long-random-string"
    cookie_expiry_days = 30

    [auth.credentials.usernames.junxi]
    name = "Junxi"
    password = "$2b$12$....."   # bcrypt hash, never the password itself

Generate the hash with ``python make_password_hash.py``, which asks for a
password without echoing it and prints only the hash. Passwords themselves
should never be typed into a settings page, a file, or a chat window.
"""

from __future__ import annotations

from typing import Any

import streamlit as st
import streamlit_authenticator as stauth


COOKIE_NAME_DEFAULT = "ks_academia_auth"
COOKIE_EXPIRY_DAYS_DEFAULT = 30.0


def _plain(value: Any) -> Any:
    """Streamlit's secrets come back as AttrDict; the library wants dicts."""
    if hasattr(value, "items"):
        return {key: _plain(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _configuration_error(message: str) -> None:
    """Fail closed, and say what to fix without leaking anything."""
    st.error(
        f"KS Academia is not configured for sign-in, so it will not open.\n\n"
        f"{message}"
    )
    st.caption(
        "Add the details under Settings → Secrets for this app. "
        "Until then nobody can get in, which is the safe way to be wrong."
    )
    st.stop()


def require_login() -> stauth.Authenticate:
    """Stop the page unless the visitor has signed in.

    Returns the authenticator so the caller can offer a logout button.

    Deliberately fails closed: a missing or malformed ``[auth]`` block stops
    the app rather than letting anyone through. The alternative -- carrying
    on when the configuration looks wrong -- is how an app ends up quietly
    serving 170 students' payment records to the internet.
    """
    if "auth" not in st.secrets:
        _configuration_error("No `[auth]` section was found in the app's secrets.")

    config = _plain(st.secrets["auth"])
    credentials = config.get("credentials")
    if not credentials or not credentials.get("usernames"):
        _configuration_error(
            "The `[auth]` section has no users under "
            "`[auth.credentials.usernames.<name>]`."
        )

    cookie_key = config.get("cookie_key")
    if not cookie_key:
        _configuration_error("`cookie_key` is missing from the `[auth]` section.")

    authenticator = stauth.Authenticate(
        credentials=credentials,
        cookie_name=config.get("cookie_name", COOKIE_NAME_DEFAULT),
        cookie_key=cookie_key,
        cookie_expiry_days=float(
            config.get("cookie_expiry_days", COOKIE_EXPIRY_DAYS_DEFAULT)
        ),
        # The stored passwords are already bcrypt hashes. Leaving auto_hash on
        # would hash the hash and nobody would ever be able to sign in.
        auto_hash=False,
    )

    authenticator.login(
        location="main",
        fields={
            "Form name": "KS Academia",
            "Username": "Username",
            "Password": "Password",
            "Login": "Sign in",
        },
    )

    status = st.session_state.get("authentication_status")
    if status is False:
        st.error("That username and password do not match.")
        st.stop()
    if status is None:
        st.info("Please sign in to continue.")
        st.stop()

    return authenticator


def logout_button(authenticator: stauth.Authenticate) -> None:
    """A way out, in the sidebar, plus who is currently signed in.

    Worth having on a shared machine at the front desk: without it the cookie
    keeps whoever logged in last signed in for a month.
    """
    # Only draw it for a session that actually holds a login. The button
    # raises if asked to log out someone who was never logged in, and this is
    # decoration -- it must never be the thing that takes the page down.
    # Outside a Streamlit run (a test importing this module, a script) there
    # is no session at all, and st.stop() in require_login does not halt the
    # way it does in a real run, so execution can reach here unauthenticated.
    # Drawn unconditionally rather than gated on a session-state flag. When
    # the session is restored from the cookie instead of the form, neither
    # `authentication_status` nor `username` is reliably set on the run that
    # reaches here, and gating on either made the sign-out button vanish for
    # exactly the people who never see the login screen -- which is everyone,
    # most mornings.
    #
    # The try/except is what makes that safe: the button raises when asked to
    # log out a session that never logged in (a test importing this module, a
    # bare script), and this is decoration. It must never be the thing that
    # takes the page down. The real gate is require_login, above.
    # Note there is no `with st.sidebar:` block around this: logout() already
    # calls st.sidebar.button internally when told location="sidebar", and
    # wrapping it nests the sidebar inside itself.
    #
    # The library's own logout is used rather than a hand-rolled button
    # because deleting the cookie is the part that actually signs someone
    # out, and its cookie controller does that correctly. Clearing session
    # state alone achieves nothing -- the cookie simply signs them straight
    # back in on the rerun.
    name = st.session_state.get("name") or st.session_state.get("username")
    if name:
        st.sidebar.caption(f"Signed in as {name}")
    try:
        authenticator.logout("Sign out", location="sidebar")
    except Exception:  # noqa: BLE001 - never break the page over a button
        pass
