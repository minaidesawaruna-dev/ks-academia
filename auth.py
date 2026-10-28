"""Sign-in for KS Academia: nobody sees the app without a username and password.

The app holds students' names, attendance and payments, so it can't be
left open on a public link. Streamlit Cloud's "private app" setting is
not enough on its own: in testing, the running app was still reachable
on its internal /~/+/ address with no account. So the check is done here,
inside the app.

require_login() runs before the database is touched or any name is
shown, and stops the page for anyone not signed in.

The details come from Streamlit's secrets, never from the code:

    [auth]
    cookie_name = "ks_academia_auth"
    cookie_key = "a-long-random-string"
    cookie_expiry_days = 30

    [auth.credentials.usernames.admin]
    name = "Admin"
    password = "$2b$12$....."   # bcrypt hash, never the password itself

Make the hash with `python make_password_hash.py`, which asks for the
password without showing it and prints only the hash.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

import jwt
import streamlit as st
import streamlit_authenticator as stauth


COOKIE_NAME_DEFAULT = "ks_academia_auth"
COOKIE_EXPIRY_DAYS_DEFAULT = 30.0


def _read_cookie_from_browser(model: Any) -> Callable[[], Any]:
    """Return a cookie reader for streamlit-authenticator that also asks the browser.

    The library reads the "stay signed in" cookie from the request headers.
    That works locally, but Streamlit Cloud strips cookies from the headers,
    so everyone had to sign in again on every visit. This reader tries the
    headers first, and if the cookie isn't there it asks the library's own
    cookie component, which reads it in the browser.

    The cookie is still checked the library's way (signed with the secret
    key, not past its expiry date), so a fake or old cookie is rejected.
    `model` is the library's CookieModel; only its cookie name, key and
    component are used.
    """

    def get_cookie() -> Any:
        """Return the signed-in user's details from the cookie, or None if there is no valid cookie.
        """
        if st.session_state.get("logout"):
            return False
        token = None
        try:
            token = st.context.cookies.get(model.cookie_name)
        except Exception:  # noqa: BLE001 - no request context, e.g. in a test
            token = None
        if not token:
            try:
                token = model.cookie_manager.get(model.cookie_name)
            except Exception:  # noqa: BLE001 - component not ready or absent
                token = None
        if not token:
            return None
        try:
            decoded = jwt.decode(token, model.cookie_key, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        if (
            isinstance(decoded, dict)
            and "username" in decoded
            and float(decoded.get("exp_date", 0)) > time.time()
        ):
            return decoded
        return None

    return get_cookie


def _plain(value: Any) -> Any:
    """Turn Streamlit's secrets (AttrDict) into plain dicts, which the library expects."""
    if hasattr(value, "items"):
        return {key: _plain(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _configuration_error(detail: str) -> None:
    """Stop the app with a plain "unavailable" message when sign-in isn't set up.

    The visitor might be a stranger, so the page says nothing about what is
    missing. The details go to the server log for whoever runs the app.
    """
    print(f"[auth] refusing to start: {detail}", file=sys.stderr)
    st.error("KS Academia is unavailable.")
    st.write("Please contact the administrator.")
    st.stop()
    # st.stop() ends a real Streamlit run, but not a test or a plain script.
    # Raise as well, so nothing carries on with a broken setup.
    raise RuntimeError(f"sign-in is not configured: {detail}")


def require_login() -> stauth.Authenticate:
    """Stop the page unless the visitor is signed in, and return the authenticator.

    Fails closed: if the [auth] settings are missing or wrong, nobody gets in.
    """
    # Reading st.secrets raises an error when there are no secrets at all.
    # Treat any error here the same as "not set up".
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
        # The stored passwords are already bcrypt hashes; auto_hash would hash them again
        # and nobody could sign in.
        auto_hash=False,
    )

    # Swap in the cookie reader above, so "stay signed in" works on Streamlit Cloud.
    # If the library is laid out differently, sign-in still works; staying signed in doesn't.
    try:
        cookie_model = authenticator.cookie_controller.cookie_model
        cookie_model.get_cookie = _read_cookie_from_browser(cookie_model)
    except AttributeError as error:  # a streamlit-authenticator that is put together differently
        print(f"[auth] stay-signed-in is off: {error}", file=sys.stderr)

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
    """Show who is signed in and a Sign out button in the sidebar."""
    # Use the library's logout (it deletes the cookie). Clearing session state alone
    # doesn't sign anyone out: the cookie signs them back in on the next rerun.
    # No `with st.sidebar:` here, because logout(location="sidebar") already puts
    # the button in the sidebar. The try/except is for tests run outside Streamlit,
    # where logout raises.
    name = st.session_state.get("name") or st.session_state.get("username")
    if name:
        st.sidebar.caption(f"Signed in as {name}")
    try:
        authenticator.logout("Sign out", location="sidebar")
    except Exception:  # noqa: BLE001 - never break the page over a button
        pass
