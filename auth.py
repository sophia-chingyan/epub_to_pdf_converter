"""Google OAuth (app-level) and session helpers.

Auth is enforced in-app so the single login/library/access-denied pages match
the provided template designs. Sign-in is restricted to the ALLOWED_EMAILS
allowlist; for a single-user deployment that is just your own address.
"""
from __future__ import annotations

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request

import config

oauth = OAuth()
oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID or None,
    client_secret=config.GOOGLE_CLIENT_SECRET or None,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def current_user(request: Request) -> dict | None:
    """Return the signed-in user dict from the session, or None."""
    return request.session.get("user")


def is_allowed(email: str | None) -> bool:
    if not email:
        return False
    # An empty allowlist denies everyone (fail closed).
    return email.lower() in config.ALLOWED_EMAILS


def redirect_uri() -> str:
    return f"{config.BASE_URL}/auth"
