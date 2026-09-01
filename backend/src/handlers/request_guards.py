"""Shared request guards and response hygiene for the desktop-surface handlers.

The ``resolve_user_id_from_request`` + hand-written 401 and the Content-Type
check existed as dozens of byte-identical copies across these handlers; one
forgotten copy silently changes an auth or caching contract. The guards raise
typed exceptions that ``main.py`` maps to byte-identical responses via
``@app.exception_handler`` - chosen over ``Depends`` so none of the 126 flat
route signatures change and the 401 body is centrally guaranteed identical.

``no_store_json`` owns the ``Cache-Control: no-store`` stamp - a privacy
invariant for every route in this surface (credentials, transcripts, resumes
must never be cacheable).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..services.request_auth import resolve_user_id_from_request


class UnauthorizedError(Exception):
    """Raised by require_user; main.py maps it to the canonical 401 body."""


class NotJsonError(Exception):
    """Raised by require_json; main.py maps it to the canonical 400 body."""


def unauthorized_response() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def not_json_response() -> JSONResponse:
    return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)


def require_user(request: Request) -> str:
    uid = resolve_user_id_from_request(request)
    if not uid:
        raise UnauthorizedError()
    return uid


def require_json(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise NotJsonError()


def no_store_json(payload: Any, *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


# One home for the SSE header set (previously an inline dict per handler).
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
