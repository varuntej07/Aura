"""
Shared Google OAuth helper.

Exchanges a server auth code (obtained on-device via the Google Sign-In
authorization flow) for access/refresh tokens. Used by every Google connector
(Calendar, Gmail) so the token-exchange logic lives in one place.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..config.settings import settings
from ..lib.logger import logger

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def exchange_server_auth_code(auth_code: str) -> dict[str, Any]:
    """Exchange a Google server auth code for tokens.

    The granted scopes are encoded in the auth code itself, so no scope
    argument is needed here. Raises ValueError on a non-2xx token response.
    """
    form_fields: dict[str, str] = {
        "code": auth_code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "grant_type": "authorization_code",
    }
    if settings.GOOGLE_REDIRECT_URI:
        form_fields["redirect_uri"] = settings.GOOGLE_REDIRECT_URI
    form = urllib.parse.urlencode(form_fields).encode("utf-8")

    request = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Google OAuth code exchange failed", {
            "status": exc.code,
            "body": body[:300],
        })
        raise ValueError(f"Google token exchange failed ({exc.code}): {body[:200]}") from exc
    except Exception as exc:
        logger.exception("Google OAuth code exchange failed", {"error": str(exc)})
        raise
