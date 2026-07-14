"""
Dashboard-link handshake: the desktop app opens a signed-in web dashboard
without a second login.

POST /devices/dashboard-link/start  -> authed; the signed-in desktop app mints a
                                        short-lived token to hand to the browser.
POST /devices/dashboard-link/claim  -> UNAUTHENTICATED by design (mirrors
                                        pairing.py's reviewed decision): the token
                                        IS the credential. The web dashboard
                                        exchanges it for a Firebase custom token,
                                        exactly once.

Security model:
  * Tokens come from ``secrets.token_urlsafe(32)`` (256 bits), not pairing.py's
    human-typed 8-char alphabet -- this token is minted by the desktop app and
    POSTed straight back by the browser it opens; a person never reads or types
    it. TTL is 60 seconds (vs pairing's 300): a synchronous mint-open-claim round
    trip, not something a person needs time to enter.
  * The claim is a Firestore TRANSACTION (read -> validate -> mark used), so two
    racing claims can never both win -- the loser's retry re-reads ``used: true``
    and fails.
  * Every claim failure returns the same ``{"error": "invalid_or_expired"}`` body,
    so the endpoint is not an oracle (a prober cannot distinguish missing vs used
    vs expired vs malformed tokens).
  * Deliberately skips pairing.py's per-uid active-code cap (and its
    ``users/{uid}/pairing_state`` doc), attempts counter, and claim-failure-
    velocity alarm: this endpoint is only ever called by the desktop app's own
    tray action, not something a user can mash a button on, and the 256-bit token
    space plus 60s TTL make brute-forcing meaningfully harder than pairing's
    8-char code -- that machinery isn't buying anything here.

Firestore layout (field names borrowed from pairing.py's constants -- same
meaning, shared source of truth):
  dashboard_link_codes/{TOKEN}: uid, created_at, expires_at, used   (backend-only;
                                denied to clients in firestore.rules)

Deliberately NOT done here (unlike pairing.py's claim): no ``linked_devices``
write, no new-device-linked push. Opening your own dashboard isn't linking a
device.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from google.cloud import firestore as gcloud_firestore

from ..lib.logger import logger
from ..services.firebase import admin_auth, admin_firestore
from ..services.request_auth import resolve_user_id_from_request
from .pairing import FIELD_CREATED_AT, FIELD_EXPIRES_AT, FIELD_UID, FIELD_USED

DASHBOARD_LINK_CODES_COLLECTION = "dashboard_link_codes"
DASHBOARD_LINK_TOKEN_TTL_SECONDS = 60

# secrets.token_urlsafe(32) always produces 43 base64url characters (256 bits,
# no padding) drawn from A-Za-z0-9-_. A raw claim token outside this shape
# (wrong length, or containing e.g. "/") cannot be one we minted.
_TOKEN_LENGTH = 43
_TOKEN_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

# The one uniform claim-failure body: every failure mode (missing, used, expired,
# malformed) answers identically so the endpoint leaks nothing.
_CLAIM_INVALID_RESPONSE = {"error": "invalid_or_expired"}


# ── Pure helpers (unit-testable without Firestore) ───────────────────────────
def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def generate_dashboard_link_token() -> str:
    return secrets.token_urlsafe(32)


def normalise_dashboard_link_token(raw: object) -> str | None:
    """Returns the token when its shape matches what we mint, else None. Rejects
    before ever touching Firestore (a malformed value must never reach a document
    path -- e.g. a stray "/" would otherwise resolve to a subcollection)."""
    if not isinstance(raw, str):
        return None
    if len(raw) != _TOKEN_LENGTH:
        return None
    if any(ch not in _TOKEN_ALPHABET for ch in raw):
        return None
    return raw


def evaluate_dashboard_link_claim(code_data: dict | None, now: datetime) -> str | None:
    """Pure validity check for a claim, mirrors pairing.evaluate_claim minus the
    attempts cap (this collection has no attempts field). Returns the owning uid
    when the token is claimable, else None -- one combined answer, the endpoint
    must not reveal which check failed."""
    if code_data is None:
        return None
    if code_data.get(FIELD_USED):
        return None
    expires_at = code_data.get(FIELD_EXPIRES_AT)
    if not isinstance(expires_at, datetime) or _as_aware(expires_at) <= now:
        return None
    uid = code_data.get(FIELD_UID)
    if not isinstance(uid, str) or not uid:
        return None
    return uid


# ── POST /devices/dashboard-link/start ───────────────────────────────────────
async def handle_dashboard_link_start(request: Request) -> JSONResponse:
    """Issue a fresh dashboard-link token for the authenticated user."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse(
            {"error": "Unauthorized: valid Firebase ID token required."}, status_code=401
        )

    token = generate_dashboard_link_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=DASHBOARD_LINK_TOKEN_TTL_SECONDS)

    def _issue() -> None:
        db = admin_firestore()
        token_ref = db.collection(DASHBOARD_LINK_CODES_COLLECTION).document(token)
        # create() (not set) so an astronomically-unlikely token collision errors
        # loudly instead of silently overwriting another user's live token.
        token_ref.create({
            FIELD_UID: user_id,
            FIELD_CREATED_AT: now,
            FIELD_EXPIRES_AT: expires_at,
            FIELD_USED: False,
        })

    try:
        await asyncio.to_thread(_issue)
    except Exception as exc:
        logger.exception("DashboardLink: token issue failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse({"error": "internal"}, status_code=500)

    # Never log the token itself -- it is a credential.
    logger.info("DashboardLink: token issued", {
        "user_id": user_id,
        "expires_in_seconds": DASHBOARD_LINK_TOKEN_TTL_SECONDS,
    })
    return JSONResponse(
        {"code": token, "expires_in_seconds": DASHBOARD_LINK_TOKEN_TTL_SECONDS}, status_code=200
    )


# ── POST /devices/dashboard-link/claim ───────────────────────────────────────
async def handle_dashboard_link_claim(request: Request) -> JSONResponse:
    """Exchange a live dashboard-link token for a Firebase custom token.
    UNAUTHENTICATED by design (mirrors pairing.py): the token is the credential."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    token = normalise_dashboard_link_token(body.get("code"))
    if token is None:
        # Malformed token: same anonymous response as every other failure, and it
        # never touches Firestore.
        return JSONResponse(_CLAIM_INVALID_RESPONSE, status_code=400)

    now = datetime.now(UTC)

    def _claim() -> str | None:
        """Atomic claim. Returns the owning uid on success, None on any invalid
        token. Two racing claims serialize on the transaction: the loser re-reads
        used=true."""
        db = admin_firestore()
        token_ref = db.collection(DASHBOARD_LINK_CODES_COLLECTION).document(token)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> str | None:
            snapshot = token_ref.get(transaction=txn)
            code_data = snapshot.to_dict() if snapshot.exists else None
            uid = evaluate_dashboard_link_claim(code_data, now)
            if uid is None:
                return None
            txn.update(token_ref, {FIELD_USED: True})
            return uid

        return _execute(transaction)

    try:
        user_id = await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.exception("DashboardLink: claim transaction failed", {"error": str(exc)})
        return JSONResponse({"error": "internal"}, status_code=500)

    if user_id is None:
        return JSONResponse(_CLAIM_INVALID_RESPONSE, status_code=400)

    # Mint the custom token (mirrors pairing.handle_pair_claim).
    def _mint() -> str:
        minted = admin_auth().create_custom_token(user_id)
        if isinstance(minted, bytes):
            minted = minted.decode("utf-8")
        return minted

    try:
        custom_token = await asyncio.to_thread(_mint)
    except Exception as exc:
        logger.error("DashboardLink: custom token mint failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse({"error": "internal"}, status_code=500)

    logger.info("DashboardLink: claimed", {"user_id": user_id})
    return JSONResponse({"custom_token": custom_token}, status_code=200)
