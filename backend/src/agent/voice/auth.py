"""Firebase auth for the voice worker's MCP calls.

Mints a real Firebase ID token per session so the worker can authenticate to the
backend /mcp endpoint on the same verification path as /chat.
"""

from __future__ import annotations

import httpx

from ...config.settings import settings
from ...services.firebase import admin_auth


async def mint_firebase_id_token(user_id: str) -> str:
    """Exchange an Admin-SDK custom token for a real Firebase ID token.

    The /mcp endpoint verifies tokens with admin_auth().verify_id_token, which
    only accepts ID tokens, not custom tokens. To stay on a single auth path
    (same as /chat) the worker mints a custom token and swaps it via the
    identitytoolkit REST endpoint. Requires FIREBASE_WEB_API_KEY.
    """
    if not settings.FIREBASE_WEB_API_KEY:
        raise RuntimeError(
            "FIREBASE_WEB_API_KEY is not configured — voice worker cannot reach /mcp"
        )

    custom_token = admin_auth().create_custom_token(user_id)
    if isinstance(custom_token, bytes):
        custom_token = custom_token.decode("utf-8")

    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
        f"?key={settings.FIREBASE_WEB_API_KEY.strip()}"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            json={"token": custom_token, "returnSecureToken": True},
        )
        resp.raise_for_status()
        body = resp.json()
        id_token = body.get("idToken")
        if not isinstance(id_token, str) or not id_token:
            raise RuntimeError("identitytoolkit response missing idToken")
        return id_token
