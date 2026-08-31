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

import sys
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


def _configuration_error(detail: str) -> None:
    """Fail closed, telling the visitor nothing about why.

    Whoever hits this may well be a stranger who found the URL, so the page
    says only that the app is unavailable. Which key is missing is a fact
    about how the deployment is put together, and it goes to the server log,
    where the person who can actually fix it will look.
    """
    print(f"[auth] refusing to start: {detail}", file=sys.stderr)
    st.error("KS Academia is unavailable.")
    st.write("Please contact the administrator.")
    st.stop()
    # Unreachable inside a real Streamlit run, where st.stop() raises. Outside
    # one -- a test importing this module, a bare script -- st.stop() does not
    # halt, and execution would carry on into code that assumes a valid
    # configuration and fail later with something misleading. Stopping has to
    # mean stopping in both.
    raise RuntimeError(f"sign-in is not configured: {detail}")


def require_login() -> stauth.Authenticate:
    """Stop the page unless the visitor has signed in.

    Returns the authenticator so the caller can offer a logout button.

    Deliberately fails closed: a missing or malformed ``[auth]`` block stops
    the app rather than letting anyone through. The alternative -- carrying
    on when the configuration looks wrong -- is how an app ends up quietly
    serving 170 students' payment records to the internet.
    """
    # Reading st.secrets raises StreamlitSecretNotFoundError when no secrets
    # exist at all, rather than behaving like an empty mapping -- so a
    # deployment with nothing configured would show a Python traceback to
    # whoever opened it instead of the message below. Treat any failure to
    # read as "not configured", which lands in the same safe place.
    try:
        section = st.secrets["auth"] if "auth" in st.secrets else None
    except Exception:  # noqa: BLE001 - absent, unreadable, malformed: all the same
        section = None
    if section is None:
        _configuration_error("No `[auth]` section was found in the app's secrets.")

    config = _plain(section)
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
    # Two things here are deliberate and easy to "tidy" into breakage:
    #
    # There is no `with st.sidebar:` wrapper, because logout(location=
    # "sidebar") already calls st.sidebar.button internally; wrapping it
    # nests the sidebar inside itself and renders nothing at all.
    #
    # The library's logout is used rather than a hand-rolled button, because
    # dropping the cookie is what actually signs someone out and its cookie
    # controller does that properly. Clearing session state alone achieves
    # nothing -- the cookie signs them straight back in on the next rerun.
    #
    # The try/except keeps a decorative button from being able to take the
    # page down; logout raises if there is no session, which happens when a
    # test imports this module outside a Streamlit run. The real gate is
    # require_login, above.
    name = st.session_state.get("name") or st.session_state.get("username")
    if name:
        st.sidebar.caption(f"Signed in as {name}")
    try:
        authenticator.logout("Sign out", location="sidebar")
    except Exception:  # noqa: BLE001 - never break the page over a button
        pass
