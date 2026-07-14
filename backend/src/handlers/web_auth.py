"""Browser-based Google sign-up for the desktop app, initiated from the device.

POST /devices/web-auth/start  -> UNAUTHENTICATED. The desktop (no account yet)
                                 requests a session code, then opens the user's
                                 system browser to auravoiceapp.com/auth?session=<code>.
POST /devices/web-auth/status -> UNAUTHENTICATED. The desktop polls this with the
                                 same code until Aura-Web (a separate service,
                                 same Firebase project) reports the browser leg
                                 done, then consumes the result exactly once.

This is the same "device authorization" shape as `pairing.py`, but for a
fundamentally different lifecycle: a pairing code is issued by an
ALREADY-AUTHENTICATED party for a KNOWN uid; a web-auth code is issued by a
FULLY UNAUTHENTICATED desktop for an ACCOUNT THAT DOES NOT YET EXIST. That is
why this is a separate module rather than an extension of pairing.py — the
per-uid active-code cap and claim-attempt semantics pairing.py builds around
don't apply here (there is no uid until the browser leg completes).

Security model:
  * The code is `secrets.token_urlsafe(24)` (~192 bits). Unlike pairing's 8-char
    human-typed code, this one is never read or retyped by a person — it only
    ever travels inside a URL (desktop -> browser) and a JSON body (desktop ->
    backend), and BOTH endpoints here are unauthenticated, so the code itself is
    the only gate. The long random token is deliberately much stronger than
    pairing's, matching that higher exposure.
  * `/status` is POST with the code in the body, not a GET query param, for the
    same reason pairing's /claim is POST: it's a bearer credential and query
    params end up in access logs and proxies. (The desktop -> browser leg has no
    such alternative; a URL is the only channel there, same as every device-
    authorization flow in the wild.)
  * Single-use is structural, not conventional: `/status` DELETES the session
    doc in the same transaction that reads a `completed` or `failed` result, so
    a leaked poll response can never be replayed.
  * TTL is 600s (2x pairing's 300s) because this leg requires a full Google OAuth
    round trip in a real browser (choosing an account, maybe 2FA) rather than
    retyping a code already on screen.
  * Known, accepted risk (same class every device-authorization flow carries):
    an attacker could distribute a `/auth?session=<code>` link to a victim to
    hijack their Google sign-in into the attacker's own waiting desktop. This is
    mitigated (short TTL, single-use, unguessable code, on-page copy telling the
    user the window was opened by their own Aura Desktop app) but not eliminated
    for v1, matching pairing.py's own documented risk posture.
  * `/start` has no per-user cap (there is no uid yet) and no per-IP rate limit
    (no such middleware exists anywhere in this backend, per pairing.py's own
    precedent) — just a log-only per-instance velocity alarm on issuance volume,
    the same class of defense pairing.py itself relies on.

Firestore layout (field names are the module constants below):
  web_auth_sessions/{CODE}: status, created_at, expires_at, device_name,
                            uid?, custom_token?, completed_at?,
                            failure_reason?, failed_at?
                            (backend-only; denied to clients in firestore.rules)
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from google.cloud import firestore as gcloud_firestore

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.notifications.device_link_push import send_new_device_linked_push
from .pairing import DEFAULT_DEVICE_NAME, sanitize_device_name

# ── Constants ─────────────────────────────────────────────────────────────────
WEB_AUTH_CODE_NBYTES = 24  # secrets.token_urlsafe(24) -> ~192 bits, URL-safe
WEB_AUTH_SESSION_TTL_SECONDS = 600

WEB_AUTH_START_VELOCITY_WINDOW_SECONDS = 600
WEB_AUTH_START_VELOCITY_THRESHOLD = 100  # log-only, observability, not blocking

# ── Collection / field names (single source of truth for writers AND readers) ─
WEB_AUTH_SESSIONS_COLLECTION = "web_auth_sessions"
USERS_COLLECTION = "users"
LINKED_DEVICES_SUBCOLLECTION = "linked_devices"

FIELD_STATUS = "status"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_DEVICE_NAME = "device_name"
FIELD_UID = "uid"
FIELD_CUSTOM_TOKEN = "custom_token"
FIELD_COMPLETED_AT = "completed_at"
FIELD_FAILURE_REASON = "failure_reason"
FIELD_FAILED_AT = "failed_at"
FIELD_PLATFORM = "platform"
FIELD_LINKED_AT = "linked_at"

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

PLATFORM_WINDOWS = "windows"

# In-memory (per-instance) issuance-volume tracker. Monotonic seconds of each
# /start call; pruned to the window on every record. Log-only anomaly signal,
# mirroring pairing.py's claim-failure velocity alarm — there is no uid to cap
# against here, so this tracks raw call volume instead of failure volume.
_recent_start_seconds: deque[float] = deque()


# ── Pure helpers (unit-testable without Firestore) ───────────────────────────
def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def generate_web_auth_code() -> str:
    return secrets.token_urlsafe(WEB_AUTH_CODE_NBYTES)


def normalise_web_auth_code(raw: object) -> str | None:
    """The code is opaque (never human-typed), so validation is just
    "non-empty string, not absurdly long" — no alphabet/length contract to
    enforce the way pairing's human-facing code has."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 256:
        return None
    return cleaned


def _record_start_call() -> None:
    now_seconds = time.monotonic()
    _recent_start_seconds.append(now_seconds)
    cutoff = now_seconds - WEB_AUTH_START_VELOCITY_WINDOW_SECONDS
    while _recent_start_seconds and _recent_start_seconds[0] < cutoff:
        _recent_start_seconds.popleft()
    if len(_recent_start_seconds) > WEB_AUTH_START_VELOCITY_THRESHOLD:
        logger.error("WebAuth: high session-start velocity", {
            "starts_in_window": len(_recent_start_seconds),
            "window_seconds": WEB_AUTH_START_VELOCITY_WINDOW_SECONDS,
        })


# ── POST /devices/web-auth/start ─────────────────────────────────────────────
async def handle_web_auth_start(request: Request) -> JSONResponse:
    """Issue a fresh, unauthenticated web-auth session code for the desktop to
    embed in the browser URL it's about to open."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    device_name = sanitize_device_name(body.get("device_name"))
    code = generate_web_auth_code()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=WEB_AUTH_SESSION_TTL_SECONDS)

    def _issue() -> None:
        db = admin_firestore()
        code_ref = db.collection(WEB_AUTH_SESSIONS_COLLECTION).document(code)
        # create() (not set) so an astronomically-unlikely code collision errors
        # loudly instead of silently overwriting another pending session.
        code_ref.create({
            FIELD_STATUS: STATUS_PENDING,
            FIELD_CREATED_AT: now,
            FIELD_EXPIRES_AT: expires_at,
            FIELD_DEVICE_NAME: device_name,
        })

    try:
        await asyncio.to_thread(_issue)
    except Exception as exc:
        logger.exception("WebAuth: session start failed", {"error": str(exc)})
        return JSONResponse({"error": "internal"}, status_code=500)

    _record_start_call()
    # Never log the code itself — it is a credential.
    logger.info("WebAuth: session started", {
        "expires_in_seconds": WEB_AUTH_SESSION_TTL_SECONDS,
    })
    return JSONResponse(
        {"code": code, "expires_in_seconds": WEB_AUTH_SESSION_TTL_SECONDS}, status_code=200
    )


# ── POST /devices/web-auth/status ────────────────────────────────────────────
async def handle_web_auth_status(request: Request) -> JSONResponse:
    """Read and consume a web-auth session. Terminal outcomes (completed/failed/
    expired) delete the doc in the same transaction as the read, so a poll
    response can never be replayed."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    code = normalise_web_auth_code(body.get("code"))
    if code is None:
        return JSONResponse({"error": "missing_code"}, status_code=400)

    now = datetime.now(UTC)

    def _check() -> dict:
        db = admin_firestore()
        code_ref = db.collection(WEB_AUTH_SESSIONS_COLLECTION).document(code)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> dict:
            snapshot = code_ref.get(transaction=txn)
            if not snapshot.exists:
                return {"status": "not_found"}

            data = snapshot.to_dict() or {}
            status = data.get(FIELD_STATUS)

            if status == STATUS_PENDING:
                expires_at = data.get(FIELD_EXPIRES_AT)
                if isinstance(expires_at, datetime) and _as_aware(expires_at) <= now:
                    txn.delete(code_ref)
                    return {"status": "expired"}
                return {"status": "pending"}

            if status == STATUS_COMPLETED:
                uid = data.get(FIELD_UID)
                custom_token = data.get(FIELD_CUSTOM_TOKEN)
                txn.delete(code_ref)
                if not isinstance(uid, str) or not uid or not isinstance(custom_token, str):
                    # Malformed completion (shouldn't happen) - treat as a failure,
                    # not a crash; the code is still consumed either way.
                    return {"status": "failed", "reason": "internal"}
                return {
                    "status": "completed",
                    "uid": uid,
                    "custom_token": custom_token,
                    "device_name": data.get(FIELD_DEVICE_NAME) or DEFAULT_DEVICE_NAME,
                }

            if status == STATUS_FAILED:
                reason = data.get(FIELD_FAILURE_REASON) or "other"
                txn.delete(code_ref)
                return {"status": "failed", "reason": reason}

            # Unknown status value - consume it rather than looping a client forever.
            txn.delete(code_ref)
            return {"status": "failed", "reason": "other"}

        return _execute(transaction)

    try:
        result = await asyncio.to_thread(_check)
    except Exception as exc:
        logger.exception("WebAuth: status check failed", {"error": str(exc)})
        return JSONResponse({"error": "internal"}, status_code=500)

    if result["status"] == "completed":
        user_id = result["uid"]
        device_name = result["device_name"]
        logger.info("WebAuth: session completed, signing in", {"user_id": user_id})

        # Record the linked device + fire the same "new device" push pairing's
        # claim does on success. Never fails the response (log and continue) -
        # the token is already valid regardless of whether this succeeds.
        def _write_linked_device() -> str:
            device_ref = (
                admin_firestore()
                .collection(USERS_COLLECTION)
                .document(user_id)
                .collection(LINKED_DEVICES_SUBCOLLECTION)
                .document()
            )
            device_ref.set({
                FIELD_DEVICE_NAME: device_name,
                FIELD_PLATFORM: PLATFORM_WINDOWS,
                FIELD_LINKED_AT: now,
            })
            return device_ref.id

        linked_device_doc_id: str | None = None
        try:
            linked_device_doc_id = await asyncio.to_thread(_write_linked_device)
        except Exception as exc:
            logger.exception("WebAuth: linked_devices write failed (sign-in still succeeds)", {
                "user_id": user_id,
                "error": str(exc),
            })

        if linked_device_doc_id:
            try:
                await send_new_device_linked_push(user_id, device_name, linked_device_doc_id)
            except Exception as exc:
                logger.exception("WebAuth: new-device push failed (sign-in still succeeds)", {
                    "user_id": user_id,
                    "error": str(exc),
                })

        return JSONResponse(
            {"status": "completed", "custom_token": result["custom_token"]}, status_code=200
        )

    if result["status"] == "failed":
        logger.info("WebAuth: session failed", {"reason": result.get("reason")})
        return JSONResponse(
            {"status": "failed", "reason": result.get("reason", "other")}, status_code=200
        )

    if result["status"] == "expired":
        logger.info("WebAuth: session expired unclaimed", {})

    return JSONResponse({"status": result["status"]}, status_code=200)
