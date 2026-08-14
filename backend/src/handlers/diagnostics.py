"""Startup-failure ingest for app launches that die before they can report.

POST /diagnostics/startup -> UNAUTHENTICATED. An Android client whose PREVIOUS
                             launch never rendered a frame posts what the OS
                             recorded about that death.

Why this endpoint exists at all
-------------------------------
A user reported the app closing back to the launcher immediately on open, five
times running, surviving a reboot and a reinstall. Crashlytics had nothing and
Play Console's Android vitals had nothing. Every reporting channel the app owned
had the same blind spot: they all need the app to survive long enough to use
them.

  * Crashlytics must be initialised from Dart. A process that dies before that
    point has no reporter.
  * Android vitals only aggregates from users who opted into sharing diagnostics,
    and needs volume before it surfaces anything.
  * Any authenticated backend call needs a Firebase session. The reporting user
    had never completed a sign-in on Android at all — the app died first.

So the client sends this from Kotlin, with a raw HttpURLConnection, before
Flutter starts. That is what makes it useful, and it is also why it cannot be
authenticated: there is no session, no Firebase, and no Flutter engine at the
moment the report has to leave the device.

Security model
--------------
  * UNAUTHENTICATED by necessity, following the same reviewed precedent as
    `web_auth.py` and `pairing.py`, which are unauthenticated for the same
    structural reason (no uid exists yet at the moment of the call).
  * Write-only and read-never for clients. Nothing here is served back, so there
    is no oracle to probe. `firestore.rules` denies the collection to clients
    entirely; only the backend admin SDK writes it.
  * Contains NO user identity. The install id is a random UUID generated on the
    device, tied to nothing, and dies with an uninstall. There is no uid, no
    email, no advertising id, no hardware id, and no free-text user content.
  * Bounded by construction rather than by trust: the body is size-capped before
    parsing, every field is copied through an explicit allowlist with per-field
    type and length limits, and unknown fields are dropped rather than stored.
    A hostile client cannot make this collection grow in a shape we did not
    choose.
  * Rate limited per install id, so a device stuck in a genuine crash loop
    reports its first few launches and then goes quiet instead of writing on
    every single launch forever.
  * Log-only per-instance velocity alarm on total volume, mirroring
    `web_auth.py`'s own defence: there is no per-IP rate limiting middleware
    anywhere in this backend, and this endpoint does not introduce one.

Cost note: a healthy install sends nothing, ever — the client only posts when the
previous launch failed to reach a first frame. Volume is therefore proportional
to broken launches, not to installs. The per-install cap bounds the worst case
for a device in a permanent crash loop, which is exactly the case that motivated
this endpoint.

Firestore layout (field names are the module constants below):
  startup_diagnostics/{auto-id}: install_id, previous_stage,
                                 consecutive_failed_launches, launch_count,
                                 device{...}, exits[...], received_at, expires_at
                                 (backend-only; denied to clients)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.firebase import admin_firestore

# ── Limits ────────────────────────────────────────────────────────────────────
# Enforced BEFORE json parsing. A generous report with the full 10 exit records
# lands around 4 KB; 16 KB leaves headroom without letting a hostile client
# stream megabytes into the parser.
MAX_BODY_BYTES = 16_384

MAX_EXIT_RECORDS = 10
MAX_ABI_ENTRIES = 8
MAX_STRING_LENGTH = 256
# ApplicationExitInfo.description is the OS's own explanation and is the single
# most valuable field here, so it gets more room than other strings.
MAX_DESCRIPTION_LENGTH = 1024

# Reports are a debugging aid with a short useful life, not a data asset.
DIAGNOSTICS_TTL_DAYS = 30

# Per-install cap. A device in a permanent crash loop reports its first few
# launches — enough to establish the pattern and the consecutive-failure count —
# then stops. Without this, one broken device writes on every launch forever.
MAX_REPORTS_PER_INSTALL_WINDOW = 20
INSTALL_WINDOW_SECONDS = 3600

# Log-only volume alarm, same shape as web_auth.py's issuance alarm.
VELOCITY_WINDOW_SECONDS = 600
VELOCITY_THRESHOLD = 500

# ── Collection / field names (single source of truth for writers AND readers) ─
STARTUP_DIAGNOSTICS_COLLECTION = "startup_diagnostics"

FIELD_INSTALL_ID = "install_id"
FIELD_PREVIOUS_STAGE = "previous_stage"
FIELD_CONSECUTIVE_FAILURES = "consecutive_failed_launches"
FIELD_LAUNCH_COUNT = "launch_count"
FIELD_DEVICE = "device"
FIELD_EXITS = "exits"
FIELD_RECEIVED_AT = "received_at"
FIELD_EXPIRES_AT = "expires_at"

# Explicit allowlists. Anything not named here is dropped, so the stored shape is
# decided by this file and never by the client.
_DEVICE_STRING_FIELDS = (
    "manufacturer",
    "brand",
    "model",
    "device",
    "release",
    "app_version",
    "installer",
)
_DEVICE_INT_FIELDS = ("sdk_int", "app_build")

_EXIT_STRING_FIELDS = ("reason", "process_name")
_EXIT_INT_FIELDS = (
    "reason_code",
    "status",
    "importance",
    "timestamp_ms",
    "pss_kb",
    "rss_kb",
)

# In-memory, per-instance. Cloud Run runs several instances, so these are
# approximate by design — they are a backstop against runaway volume, not an
# accounting system. Same tradeoff web_auth.py already accepts.
_recent_report_seconds: deque[float] = deque()
_install_report_seconds: dict[str, deque[float]] = {}


# ── Pure helpers (inspectable without Firestore) ─────────────────────────────
def _clean_string(raw: object, max_length: int = MAX_STRING_LENGTH) -> str | None:
    """A trimmed, length-capped string, or None if the value isn't usable."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def _clean_int(raw: object) -> int | None:
    """An int, or None. Bools are rejected explicitly: in Python `isinstance(True,
    int)` is True, and a bool landing in an int column is a silent data bug."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def sanitize_install_id(raw: object) -> str | None:
    """The install id is a client-generated UUID. Validated as "plausible opaque
    token" rather than strictly as a UUID, so a client that changes its id format
    keeps reporting instead of going silent — the id is only ever used to group
    reports, never to authorize anything."""
    cleaned = _clean_string(raw, max_length=64)
    if cleaned is None or len(cleaned) < 8:
        return None
    if not all(character.isalnum() or character in "-_" for character in cleaned):
        return None
    return cleaned


def sanitize_device(raw: object) -> dict:
    """Device identity, allowlisted field by field."""
    if not isinstance(raw, dict):
        return {}
    device: dict = {}
    for field in _DEVICE_STRING_FIELDS:
        value = _clean_string(raw.get(field))
        if value is not None:
            device[field] = value
    for field in _DEVICE_INT_FIELDS:
        value = _clean_int(raw.get(field))
        if value is not None:
            device[field] = value
    abis = raw.get("supported_abis")
    if isinstance(abis, list):
        cleaned_abis = [
            value
            for value in (_clean_string(entry) for entry in abis[:MAX_ABI_ENTRIES])
            if value is not None
        ]
        if cleaned_abis:
            device["supported_abis"] = cleaned_abis
    return device


def sanitize_exits(raw: object) -> list[dict]:
    """The ApplicationExitInfo records, allowlisted and capped."""
    if not isinstance(raw, list):
        return []
    exits: list[dict] = []
    for entry in raw[:MAX_EXIT_RECORDS]:
        if not isinstance(entry, dict):
            continue
        record: dict = {}
        for field in _EXIT_STRING_FIELDS:
            value = _clean_string(entry.get(field))
            if value is not None:
                record[field] = value
        description = _clean_string(
            entry.get("description"), max_length=MAX_DESCRIPTION_LENGTH
        )
        if description is not None:
            record["description"] = description
        for field in _EXIT_INT_FIELDS:
            value = _clean_int(entry.get(field))
            if value is not None:
                record[field] = value
        if record:
            exits.append(record)
    return exits


def build_report_doc(body: dict, install_id: str, now: datetime) -> dict:
    """The exact document written to Firestore. Pure, so the stored shape can be
    inspected directly without a database."""
    return {
        FIELD_INSTALL_ID: install_id,
        FIELD_PREVIOUS_STAGE: _clean_string(body.get("previous_stage")),
        FIELD_CONSECUTIVE_FAILURES: _clean_int(
            body.get("consecutive_failed_launches")
        )
        or 0,
        FIELD_LAUNCH_COUNT: _clean_int(body.get("launch_count")) or 0,
        FIELD_DEVICE: sanitize_device(body.get("device")),
        FIELD_EXITS: sanitize_exits(body.get("exits")),
        FIELD_RECEIVED_AT: now,
        FIELD_EXPIRES_AT: now + timedelta(days=DIAGNOSTICS_TTL_DAYS),
    }


def _install_is_over_quota(install_id: str) -> bool:
    """True once this install has reported too often in the window. Prunes as it
    goes, and drops empty deques so a long-lived instance doesn't accumulate one
    entry per install id it has ever seen."""
    now_seconds = time.monotonic()
    cutoff = now_seconds - INSTALL_WINDOW_SECONDS
    timestamps = _install_report_seconds.setdefault(install_id, deque())
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= MAX_REPORTS_PER_INSTALL_WINDOW:
        return True
    timestamps.append(now_seconds)

    # Opportunistic sweep so the dict stays bounded without a background task.
    if len(_install_report_seconds) > 10_000:
        for stale_id in [
            key for key, value in _install_report_seconds.items() if not value
        ]:
            _install_report_seconds.pop(stale_id, None)
    return False


def _record_report_call() -> None:
    now_seconds = time.monotonic()
    _recent_report_seconds.append(now_seconds)
    cutoff = now_seconds - VELOCITY_WINDOW_SECONDS
    while _recent_report_seconds and _recent_report_seconds[0] < cutoff:
        _recent_report_seconds.popleft()
    if len(_recent_report_seconds) > VELOCITY_THRESHOLD:
        logger.error("Diagnostics: high startup-failure report velocity", {
            "reports_in_window": len(_recent_report_seconds),
            "window_seconds": VELOCITY_WINDOW_SECONDS,
        })


# ── POST /diagnostics/startup ────────────────────────────────────────────────
async def handle_startup_diagnostics(request: Request) -> JSONResponse:
    """Record one startup-failure report.

    Always answers 200 for any well-formed-enough request, including one that is
    rate limited. The client is a fire-and-forget beacon that ignores the
    response, and a device that is already failing to launch must never be given
    a reason to retry.
    """
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        logger.warn("Diagnostics: oversized startup report rejected", {
            "bytes": len(raw_body),
            "limit": MAX_BODY_BYTES,
        })
        return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    install_id = sanitize_install_id(body.get(FIELD_INSTALL_ID))
    if install_id is None:
        # No id means the report cannot be grouped or rate limited, which makes
        # it both useless and unbounded. Drop it.
        return JSONResponse({"ok": False, "error": "invalid_install_id"}, status_code=400)

    if _install_is_over_quota(install_id):
        logger.info("Diagnostics: startup report throttled", {
            "install_id": install_id,
            "window_seconds": INSTALL_WINDOW_SECONDS,
        })
        return JSONResponse({"ok": True, "throttled": True}, status_code=200)

    now = datetime.now(UTC)
    document = build_report_doc(body, install_id, now)

    def _write() -> None:
        db = admin_firestore()
        db.collection(STARTUP_DIAGNOSTICS_COLLECTION).document().set(document)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.exception("Diagnostics: startup report write failed", {
            "error": str(exc),
        })
        # Still 200: the client cannot act on this and must not retry.
        return JSONResponse({"ok": False, "error": "internal"}, status_code=200)

    _record_report_call()

    exits = document[FIELD_EXITS]
    latest_exit = exits[0] if exits else {}
    device = document[FIELD_DEVICE]
    # Logged at error level deliberately. A launch that never rendered a frame is
    # the failure this whole mechanism exists to surface, and it should be loud
    # enough to alert on rather than sitting quietly in a collection nobody reads.
    logger.error("Diagnostics: app failed to reach first frame", {
        "install_id": install_id,
        "previous_stage": document[FIELD_PREVIOUS_STAGE],
        "consecutive_failures": document[FIELD_CONSECUTIVE_FAILURES],
        "exit_reason": latest_exit.get("reason"),
        "exit_description": latest_exit.get("description"),
        "device_model": device.get("model"),
        "device_manufacturer": device.get("manufacturer"),
        "sdk_int": device.get("sdk_int"),
        "app_version": device.get("app_version"),
        "installer": device.get("installer"),
    })

    return JSONResponse({"ok": True}, status_code=200)
