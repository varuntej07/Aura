"""
Device pairing for the Aura desktop companion.

POST /devices/pair/start  -> authed; the signed-in phone app requests a short-lived
                             8-char pairing code to show the user.
POST /devices/pair/claim  -> UNAUTHENTICATED by design (reviewed decision): the
                             one-time code IS the credential. The desktop exchanges
                             a valid code for a Firebase custom token, exactly once.
POST /devices/unlink      -> authed; remove a linked device, then revoke ALL of the
                             user's refresh tokens (the honest reviewed semantic:
                             unlinking a device signs out every session).

Security model:
  * Codes come from ``secrets`` over a 32-char unambiguous alphabet (no 0/O/1/I),
    expire after 5 minutes, and are single-use. The claim is a Firestore
    TRANSACTION (read -> validate -> mark used), so two racing claims can never
    both win — the loser's retry re-reads ``used: true`` and fails.
  * Every claim failure returns the same ``{"error": "invalid_or_expired"}`` body,
    so the endpoint is not an oracle (a prober cannot distinguish missing vs used
    vs expired vs locked-out codes).
  * Per-code attempt cap (10) plus a per-instance in-memory failure velocity alarm
    (>20 failed claims in 10 minutes logs ERROR).
  * Per-uid cap of 3 live (unexpired + unclaimed) codes, tracked on
    ``users/{uid}/pairing_state/state`` rather than by querying ``pairing_codes``
    on uid + expires_at — that query would need a composite index, and a missing
    index 400s at runtime looking exactly like "no data" (CLAUDE.md). The state
    doc needs zero indexes and is pruned inside the same issuing transaction.

Firestore layout (field names are the module constants below — writer and every
reader reference them, per the CLAUDE.md data-layer discipline):
  pairing_codes/{CODE}:              uid, created_at, expires_at, used, attempts,
                                     claimed_at?, device_name?   (backend-only;
                                     denied to clients in firestore.rules)
  users/{uid}/pairing_state/state:   active_codes: {CODE: expires_at}
  users/{uid}/linked_devices/{id}:   device_name, platform, linked_at
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
from ..services.firebase import admin_auth, admin_firestore
from ..services.notifications import orchestrator
from ..services.notifications.proposal import (
    SOURCE_DEVICE_LINK,
    NotificationProposal,
    ProposalKind,
)
from ..services.request_auth import resolve_user_id_from_request

# ── Pairing constants ────────────────────────────────────────────────────────
# Unambiguous alphabet: A-Z + 2-9 minus the four look-alikes 0/O and 1/I.
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8  # stored raw; the client renders XXXX-XXXX
PAIRING_CODE_TTL_SECONDS = 300
MAX_ACTIVE_CODES_PER_USER = 3
MAX_CLAIM_ATTEMPTS_PER_CODE = 10
DEVICE_NAME_MAX_LENGTH = 64
DEFAULT_DEVICE_NAME = "Windows PC"

CLAIM_FAILURE_VELOCITY_WINDOW_SECONDS = 600
CLAIM_FAILURE_VELOCITY_THRESHOLD = 20

# ── Collection / field names (single source of truth for writers AND readers) ─
PAIRING_CODES_COLLECTION = "pairing_codes"
USERS_COLLECTION = "users"
PAIRING_STATE_SUBCOLLECTION = "pairing_state"
PAIRING_STATE_DOC_ID = "state"
LINKED_DEVICES_SUBCOLLECTION = "linked_devices"

FIELD_UID = "uid"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_USED = "used"
FIELD_ATTEMPTS = "attempts"
FIELD_CLAIMED_AT = "claimed_at"
FIELD_DEVICE_NAME = "device_name"
FIELD_PLATFORM = "platform"
FIELD_LINKED_AT = "linked_at"
FIELD_ACTIVE_CODES = "active_codes"

PLATFORM_WINDOWS = "windows"

# The one uniform claim-failure body: every failure mode (missing, used, expired,
# locked out, malformed) answers identically so the endpoint leaks nothing.
_CLAIM_INVALID_RESPONSE = {"error": "invalid_or_expired"}

# In-memory (per-instance) claim-failure velocity tracker. Monotonic seconds of
# each failed claim; pruned to the window on every record.
_recent_claim_failure_seconds: deque[float] = deque()


class ActiveCodeCapExceededError(Exception):
    """Raised inside the issuing transaction when the uid already has the maximum
    number of live pairing codes."""


# ── Pure helpers (unit-testable without Firestore) ───────────────────────────
def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def normalise_pairing_code(raw: object) -> str | None:
    """Uppercase raw 8-char code, tolerating the client's XXXX-XXXX display format.
    Returns None when the input cannot possibly be a code (wrong length/alphabet)."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.replace("-", "").replace(" ", "").strip().upper()
    if len(cleaned) != PAIRING_CODE_LENGTH:
        return None
    if any(ch not in PAIRING_CODE_ALPHABET for ch in cleaned):
        return None
    return cleaned


def sanitize_device_name(raw: object) -> str:
    """Cap at 64 chars, strip control characters. Empty/absent -> default name."""
    if not isinstance(raw, str):
        return DEFAULT_DEVICE_NAME
    printable = "".join(ch for ch in raw if ch.isprintable()).strip()
    return printable[:DEVICE_NAME_MAX_LENGTH] or DEFAULT_DEVICE_NAME


def evaluate_claim(code_data: dict | None, now: datetime) -> str | None:
    """Pure validity check for a claim. Returns the owning uid when the code is
    claimable, else None. Deliberately one combined answer: the endpoint must not
    reveal WHICH check failed."""
    if code_data is None:
        return None
    if code_data.get(FIELD_USED):
        return None
    if int(code_data.get(FIELD_ATTEMPTS, 0) or 0) >= MAX_CLAIM_ATTEMPTS_PER_CODE:
        return None
    expires_at = code_data.get(FIELD_EXPIRES_AT)
    if not isinstance(expires_at, datetime) or _as_aware(expires_at) <= now:
        return None
    uid = code_data.get(FIELD_UID)
    if not isinstance(uid, str) or not uid:
        return None
    return uid


def _record_claim_failure() -> None:
    """Per-instance brute-force alarm: >20 failed claims inside 10 minutes on this
    instance logs ERROR. In-memory by design (reviewed) — a restart resets it, and
    that is acceptable for an alarm whose job is to make a hammering run visible."""
    now_seconds = time.monotonic()
    _recent_claim_failure_seconds.append(now_seconds)
    cutoff = now_seconds - CLAIM_FAILURE_VELOCITY_WINDOW_SECONDS
    while _recent_claim_failure_seconds and _recent_claim_failure_seconds[0] < cutoff:
        _recent_claim_failure_seconds.popleft()
    if len(_recent_claim_failure_seconds) > CLAIM_FAILURE_VELOCITY_THRESHOLD:
        logger.error("Pairing: high claim failure velocity", {
            "failures_in_window": len(_recent_claim_failure_seconds),
            "window_seconds": CLAIM_FAILURE_VELOCITY_WINDOW_SECONDS,
        })


# ── POST /devices/pair/start ─────────────────────────────────────────────────
async def handle_pair_start(request: Request) -> JSONResponse:
    """Issue a fresh pairing code for the authenticated user."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse(
            {"error": "Unauthorized: valid Firebase ID token required."}, status_code=401
        )

    code = generate_pairing_code()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)

    def _issue() -> None:
        db = admin_firestore()
        code_ref = db.collection(PAIRING_CODES_COLLECTION).document(code)
        state_ref = (
            db.collection(USERS_COLLECTION)
            .document(user_id)
            .collection(PAIRING_STATE_SUBCOLLECTION)
            .document(PAIRING_STATE_DOC_ID)
        )
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> None:
            snapshot = state_ref.get(transaction=txn)
            recorded: dict = (snapshot.to_dict() or {}).get(FIELD_ACTIVE_CODES, {}) or {}

            # Prune expired entries AND garbage-collect their code docs in the same
            # transaction, so pairing_codes never accumulates dead unclaimed codes.
            live_codes: dict[str, datetime] = {}
            for existing_code, existing_expiry in recorded.items():
                if isinstance(existing_expiry, datetime) and _as_aware(existing_expiry) > now:
                    live_codes[existing_code] = existing_expiry
                else:
                    txn.delete(db.collection(PAIRING_CODES_COLLECTION).document(existing_code))

            if len(live_codes) >= MAX_ACTIVE_CODES_PER_USER:
                raise ActiveCodeCapExceededError()

            # create() (not set) so an astronomically-unlikely code collision errors
            # loudly instead of silently overwriting another user's live code.
            txn.create(code_ref, {
                FIELD_UID: user_id,
                FIELD_CREATED_AT: now,
                FIELD_EXPIRES_AT: expires_at,
                FIELD_USED: False,
                FIELD_ATTEMPTS: 0,
            })
            live_codes[code] = expires_at
            txn.set(state_ref, {FIELD_ACTIVE_CODES: live_codes})

        _execute(transaction)

    try:
        await asyncio.to_thread(_issue)
    except ActiveCodeCapExceededError:
        logger.warn("Pairing: active-code cap hit", {
            "user_id": user_id,
            "cap": MAX_ACTIVE_CODES_PER_USER,
        })
        return JSONResponse(
            {
                "error": "too_many_active_codes",
                "detail": (
                    f"You already have {MAX_ACTIVE_CODES_PER_USER} active pairing codes. "
                    "Wait a few minutes for one to expire, then try again."
                ),
            },
            status_code=429,
        )
    except Exception as exc:
        logger.exception("Pairing: code issue failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse({"error": "internal"}, status_code=500)

    # Never log the code itself — it is a credential.
    logger.info("Pairing: code issued", {
        "user_id": user_id,
        "expires_in_seconds": PAIRING_CODE_TTL_SECONDS,
    })
    return JSONResponse(
        {"code": code, "expires_in_seconds": PAIRING_CODE_TTL_SECONDS}, status_code=200
    )


# ── POST /devices/pair/claim ─────────────────────────────────────────────────
async def handle_pair_claim(request: Request) -> JSONResponse:
    """Exchange a live pairing code for a Firebase custom token. UNAUTHENTICATED
    by design (reviewed): the code is the credential."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    code = normalise_pairing_code(body.get("code"))
    device_name = sanitize_device_name(body.get("device_name"))

    if code is None:
        # Malformed code: same anonymous response as every other failure (no oracle),
        # and it still counts toward the velocity alarm.
        _record_claim_failure()
        return JSONResponse(_CLAIM_INVALID_RESPONSE, status_code=400)

    now = datetime.now(UTC)

    def _claim() -> str | None:
        """Atomic claim. Returns the owning uid on success, None on any invalid code.
        Two racing claims serialize on the transaction: the loser re-reads used=true."""
        db = admin_firestore()
        code_ref = db.collection(PAIRING_CODES_COLLECTION).document(code)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> str | None:
            snapshot = code_ref.get(transaction=txn)
            code_data = snapshot.to_dict() if snapshot.exists else None
            uid = evaluate_claim(code_data, now)
            if uid is None:
                # Count the failed attempt on the doc when it exists (missing docs
                # have nothing to count on — brute force there is covered by the
                # velocity alarm + the 32^8 code space).
                if code_data is not None:
                    attempts_so_far = int(code_data.get(FIELD_ATTEMPTS, 0) or 0)
                    txn.update(code_ref, {FIELD_ATTEMPTS: attempts_so_far + 1})
                return None

            txn.update(code_ref, {
                FIELD_USED: True,
                FIELD_CLAIMED_AT: now,
                FIELD_DEVICE_NAME: device_name,
            })
            # Free the uid's cap slot: a claimed code is no longer "live".
            # set(merge=True) + DELETE_FIELD never NotFounds if the state doc is gone.
            state_ref = (
                db.collection(USERS_COLLECTION)
                .document(uid)
                .collection(PAIRING_STATE_SUBCOLLECTION)
                .document(PAIRING_STATE_DOC_ID)
            )
            txn.set(
                state_ref,
                {FIELD_ACTIVE_CODES: {code: gcloud_firestore.DELETE_FIELD}},
                merge=True,
            )
            return uid

        return _execute(transaction)

    try:
        user_id = await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.exception("Pairing: claim transaction failed", {"error": str(exc)})
        return JSONResponse({"error": "internal"}, status_code=500)

    if user_id is None:
        _record_claim_failure()
        client = getattr(request, "client", None)
        logger.warn("Pairing: claim rejected", {
            "client_ip": client.host if client else "unknown",
        })
        return JSONResponse(_CLAIM_INVALID_RESPONSE, status_code=400)

    # Mint the custom token (mirrors src/agent/voice/auth.py:28).
    def _mint() -> str:
        token = admin_auth().create_custom_token(user_id)
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token

    try:
        custom_token = await asyncio.to_thread(_mint)
    except Exception as exc:
        # EXACT string — the deploy-check tripwire for missing IAM signBlob on the
        # Cloud Run service account. Do not reword.
        logger.error("Pairing: custom token mint failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse({"error": "internal"}, status_code=500)

    # Record the linked device. Failure here must not fail the claim (the token is
    # already minted and valid) — log loudly and continue.
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
        logger.exception("Pairing: linked_devices write failed (claim still succeeds)", {
            "user_id": user_id,
            "error": str(exc),
        })

    # "New device linked" security push. Never fails the claim (log and continue).
    if linked_device_doc_id:
        try:
            await _send_new_device_linked_push(user_id, device_name, linked_device_doc_id)
        except Exception as exc:
            logger.exception("Pairing: new-device push failed (claim still succeeds)", {
                "user_id": user_id,
                "error": str(exc),
            })

    logger.info("Pairing: device linked", {
        "user_id": user_id,
        "device_name": device_name,
        "linked_device_doc_id": linked_device_doc_id,
    })
    return JSONResponse({"custom_token": custom_token}, status_code=200)


async def _send_new_device_linked_push(
    user_id: str, device_name: str, linked_device_doc_id: str
) -> None:
    """Security alert through the notification funnel: COMMITTED lane, so it sends
    inline on submit (freshness + dedup only, never held or arbitrated)."""
    proposal = NotificationProposal(
        user_id=user_id,
        source=SOURCE_DEVICE_LINK,
        kind=ProposalKind.COMMITTED,
        dedup_key=f"device_link:{linked_device_doc_id}",
        title="New device linked",
        body=(
            f"A Windows PC ('{device_name}') just linked to your Aura account. "
            "Wasn't you? Unlink in Settings."
        ),
    )
    await orchestrator.submit(proposal)


# ── POST /devices/unlink ─────────────────────────────────────────────────────
async def handle_unlink_device(request: Request) -> JSONResponse:
    """Delete a linked device, then revoke ALL of the user's refresh tokens.

    Yes, all sessions — that is the honest reviewed semantic: Firebase revocation
    is per-user, not per-device, and pretending otherwise would leave the unlinked
    desktop silently signed in."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse(
            {"error": "Unauthorized: valid Firebase ID token required."}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    device_id = str(body.get("device_id", "") or "").strip()
    if not device_id or "/" in device_id or device_id in {".", ".."}:
        return JSONResponse({"error": "device_id is required."}, status_code=400)

    def _delete_linked_device() -> bool:
        device_ref = (
            admin_firestore()
            .collection(USERS_COLLECTION)
            .document(user_id)
            .collection(LINKED_DEVICES_SUBCOLLECTION)
            .document(device_id)
        )
        snapshot = device_ref.get()
        if not snapshot.exists:
            return False
        device_ref.delete()
        return True

    try:
        deleted = await asyncio.to_thread(_delete_linked_device)
        if not deleted:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await asyncio.to_thread(lambda: admin_auth().revoke_refresh_tokens(user_id))
    except Exception as exc:
        logger.exception("Pairing: unlink failed", {
            "user_id": user_id,
            "device_id": device_id,
            "error": str(exc),
        })
        return JSONResponse({"error": "internal"}, status_code=500)

    logger.info("Pairing: device unlinked, all refresh tokens revoked", {
        "user_id": user_id,
        "device_id": device_id,
    })
    return JSONResponse({"ok": True}, status_code=200)
