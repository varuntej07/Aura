"""
Helpers for authenticating REST requests with Firebase ID tokens.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request

from ..config.settings import settings
from ..lib.logger import logger
from .firebase import admin_auth


def _normalise_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def decode_firebase_claims(headers: Mapping[str, Any] | None) -> dict[str, Any] | None:
    normalised = _normalise_headers(headers)
    auth_header = normalised.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:].strip()
    if not token:
        return None

    try:
        return admin_auth().verify_id_token(token)
    except Exception as exc:
        logger.warn("REST auth: Firebase token verification failed", {
            "error": type(exc).__name__,
            "detail": str(exc),
        })
        return None


def resolve_user_id(
    headers: Mapping[str, Any] | None,
    *,
    explicit_user_id: str | None = None,
) -> str | None:
    claims = decode_firebase_claims(headers)
    if claims:
        uid = claims.get("uid") or claims.get("sub")
        if isinstance(uid, str) and uid:
            return uid

    if not settings.is_production:
        if explicit_user_id:
            return explicit_user_id

        normalised = _normalise_headers(headers)
        fallback = normalised.get("x-juno-user-id", "")
        if fallback:
            logger.warn("REST auth: using dev fallback x-juno-user-id", {
                "user_id": fallback,
            })
            return fallback

    return None


def resolve_user_id_from_request(
    request: Request,
    *,
    explicit_user_id: str | None = None,
) -> str | None:
    return resolve_user_id(request.headers, explicit_user_id=explicit_user_id)


