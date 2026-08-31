"""Sign-in gate for KS Academia.

The app holds real students' names, attendance and payment records, so it
cannot simply be put on a URL and left there.

The host's own "private app" setting is not enough on its own. Set on
Streamlit Community Cloud, it redirects the app's front door to a sign-in
page but still serves the running app on its internal ``/~/+/`` path, which
was reachable from a phone on mobile data with no account at all. Access
control therefore lives here, in the app, where it does not depend on how the
host happens to route a request.

``require_login()`` runs before anything reads the database or draws a
student's name, and stops the script outright for anyone not admitted.

Two separate questions
----------------------

Google answers *who you are*. It does not answer *who is allowed in* -- an
app that only calls ``st.login()`` admits anybody on earth with a Google
account. So there are two checks here: sign in with Google, and then be on
the list. The list is a plain set of email addresses in the app's secrets,
and adding an admin means adding their address to it.

Configuration, in the app's secrets (never in the repository)::

    [auth]
    redirect_uri = "https://<your-app>.streamlit.app/oauth2callback"
    cookie_secret = "a-long-random-string"

    [auth.google]
    client_id = "....apps.googleusercontent.com"
    client_secret = "...."
    server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

    [access]
    emails = ["you@gmail.com", "your.admin@gmail.com"]

``[auth]`` is Streamlit's own section and it is strict about what may appear
inside it, which is why the allowlist lives under ``[access]`` instead.
"""

from __future__ import annotations

import sys

import streamlit as st


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


def _allowed_emails() -> set[str]:
    """The addresses permitted in, lowercased.

    Reading st.secrets raises when a deployment has no secrets at all rather
    than behaving like an empty mapping, so any failure to read is treated as
    "nobody is configured" -- which fails closed.
    """
    try:
        access = st.secrets["access"] if "access" in st.secrets else None
    except Exception:  # noqa: BLE001 - absent, unreadable, malformed: all the same
        access = None
    if access is None:
        return set()
    raw = access.get("emails") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(entry).strip().lower() for entry in raw if str(entry).strip()}


def _is_logged_in() -> bool:
    """Whether Streamlit has an identity for this visitor.

    Accessing st.user raises rather than returning empty when the [auth]
    section is missing or malformed, so this doubles as the check that OIDC
    is configured at all.
    """
    try:
        return bool(st.user.is_logged_in)
    except Exception as exc:  # noqa: BLE001
        _configuration_error(f"OIDC is not configured correctly: {exc!r}")
        return False  # unreachable; st.stop() raised


def require_login() -> None:
    """Stop the page unless the visitor signed in and is on the list.

    Deliberately fails closed. An empty or missing allowlist admits nobody
    rather than everybody: getting this the wrong way round is how an app
    ends up quietly serving 170 students' payment records to the internet.
    """
    allowed = _allowed_emails()
    if not allowed:
        _configuration_error(
            "No addresses under [access] emails, so nobody can be admitted."
        )

    if not _is_logged_in():
        st.title("KS Academia")
        st.write("Please sign in to continue.")
        st.button("Sign in with Google", type="primary", on_click=st.login,
                  args=("google",))
        st.stop()

    email = str(getattr(st.user, "email", "") or "").strip().lower()
    if email not in allowed:
        # Signed in as somebody, just not somebody allowed. Say so plainly --
        # this person authenticated, so they are not an anonymous passer-by,
        # and a vague message would only send them to the admin confused.
        print(f"[auth] refused: {email or '(no email)'} is not on the list",
              file=sys.stderr)
        st.error("This account does not have access to KS Academia.")
        st.write("Ask the administrator to add your address, then sign in again.")
        st.button("Sign out", on_click=st.logout)
        st.stop()


def logout_button() -> None:
    """A way out, in the sidebar, plus who is currently signed in.

    Worth having on a shared machine at the front desk: without it whoever
    signed in last stays signed in.
    """
    name = str(getattr(st.user, "name", "") or getattr(st.user, "email", "") or "")
    if name:
        st.sidebar.caption(f"Signed in as {name}")
    st.sidebar.button("Sign out", on_click=st.logout, key="ks_sign_out")
