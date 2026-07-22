"""The v0.1.7 visible-memory endpoints: the desktop daily catch-up card and
the dashboard "What Buddy remembers" page.

- GET    /memories/callback?date=YYYY-MM-DD  - today's stored callback line +
  the current memory chips, or {} when there is nothing to show. A miss kicks
  off async generation so a LATER summon the same day gets the card; the
  request itself never waits on an LLM (Cloud Run cold start + a model pass
  cannot reliably beat the desktop's 1500ms render budget).
- GET    /memories                           - full row list (dashboard).
- DELETE /memories/{memory_id}?date=...      - forget one row (chip X /
  dashboard delete). Also invalidates the stored line for that date: a deleted
  memory must not survive in a line any surface can still read.
- PATCH  /memories/{memory_id}?date=...      - dashboard edit (value only,
  200-char cap, sanitized - edited text flows into the voice prompt exactly
  like the original did). Also invalidates the stored line.

Reads/writes ``users/{uid}/memories`` - the same rows tool_executor's
store_memory writes and the voice agent's fetch_memory_summary injects, so a
chip delete here is gone from the next call's prompt with no extra plumbing.
Stored lines live in ``users/{uid}/callback_lines/{date}`` keyed by the
CLIENT-reported local date (the desktop's definition of "today" is the only
one; see the design doc).

Kill switch: CALLBACK_CARD_ENABLED=false makes the callback endpoint always
return {} - every client silently shows nothing, no desktop update needed.

Auth matches handlers/drafts.py: Firebase ID token, owner-only, not
consent-gated (a user can always see and delete their own stored data).
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.memory.graph_store import delete_node
from ..services.model_provider import ModelProvider
from ..services.request_auth import resolve_user_id_from_request

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Chips are the audit view: most recently updated rows first, hard cap.
_MAX_CHIPS = 5
# Rows older than this never drive the callback LINE (staleness rule); they
# may still appear among the chips.
_LINE_MAX_AGE_DAYS = 30
# Lines self-scored below this are not stored - silence beats generic.
_CALLBACK_SPECIFICITY_MIN = int(os.getenv("CALLBACK_SPECIFICITY_MIN", "60"))
_VALUE_MAX_CHARS = 200
_MAX_GENERATION_ATTEMPTS = 3
_GENERATION_RETRY_DELAY = timedelta(minutes=5)


class _CallbackLine(BaseModel):
    line: str = Field(min_length=1, max_length=140)
    specificity: int = Field(ge=0, le=100)


def _callback_enabled() -> bool:
    return os.getenv("CALLBACK_CARD_ENABLED", "true").strip().lower() != "false"


def _memories_ref(uid: str):
    return admin_firestore().collection("users").document(uid).collection("memories")


def _line_ref(uid: str, date: str):
    return (
        admin_firestore()
        .collection("users").document(uid)
        .collection("callback_lines").document(date)
    )


def _sanitize_value(raw: str) -> str:
    """Strip markdown-ish characters and collapse whitespace; this text flows
    into the voice prompt, so it gets the same hygiene a fresh row would."""
    cleaned = re.sub(r"[*_`#>\[\]]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_VALUE_MAX_CHARS]


def _chip(doc_id: str, row: dict[str, Any]) -> dict[str, Any]:
    category = str(row.get("category", "")).strip().lower()
    return {
        "id": doc_id,
        "key": str(row.get("key", "")).strip(),
        "value": str(row.get("value", "")).strip(),
        "source": "screen" if category == "screen" else "conversation",
        "updated_at": str(row.get("updated_at", "")),
    }


def _recent_rows(uid: str, limit: int) -> list[tuple[str, dict[str, Any]]]:
    coll = _memories_ref(uid)
    try:
        docs = list(coll.order_by("updated_at", direction="DESCENDING").limit(limit).stream())
    except Exception:
        docs = list(coll.limit(limit).stream())
    return [(d.id, d.to_dict() or {}) for d in docs]


async def _generate_line(uid: str, date: str, *, retry_count: int = 0) -> None:
    """Background generation of one day's callback line. Writes either
    {line, specificity} or {empty: true} to the line doc; the desktop's next
    GET that day picks up whichever landed. Failures leave the doc in
    "generating" state, which a later GET treats as retryable."""
    try:
        rows = await asyncio.to_thread(_recent_rows, uid, 20)
        cutoff = (datetime.now(UTC) - timedelta(days=_LINE_MAX_AGE_DAYS)).isoformat()
        fresh = [
            (key, value)
            for _id, row in rows
            if (key := str(row.get("key", "")).strip())
            and (value := str(row.get("value", "")).strip())
            and str(row.get("updated_at", "")) >= cutoff
        ]
        result: dict[str, Any]
        if not fresh:
            result = {"empty": True}
        else:
            facts = "\n".join(f"- {k}: {v}" for k, v in fresh[:10])
            prompt = (
                "You are Buddy, a warm desktop companion, greeting a returning user "
                "with ONE short opening line that proves you remember their life.\n\n"
                "Known facts about them (from past conversations):\n"
                f"{facts}\n\n"
                "Rules, all hard:\n"
                "- Reference only durable facts or ongoing projects. SKIP anything "
                "task-like or reminder-like entirely (a stored 'wants to call mom' "
                "must never become 'how was your call with mom?').\n"
                "- Never presume an event happened or completed.\n"
                "- One sentence, under 140 characters, conversational, no emoji, "
                "no exclamation marks, never use an em dash.\n"
                "- Rate your own line's specificity 0-100: 100 means it could only "
                "be about this exact person; 0 means it fits anyone.\n"
                "- If nothing specific and safe exists, return specificity 0."
            )
            provider = ModelProvider()
            scored = await provider.cheap(prompt, response_model=_CallbackLine, temperature=0.4)
            line = _sanitize_value(scored.line) if isinstance(scored, _CallbackLine) else ""
            spec = scored.specificity if isinstance(scored, _CallbackLine) else 0
            if line and spec >= _CALLBACK_SPECIFICITY_MIN:
                result = {"line": line, "specificity": spec}
            else:
                result = {"empty": True}
        result["generated_at"] = datetime.now(UTC).isoformat()
        await asyncio.to_thread(_line_ref(uid, date).set, result)
        logger.info("Memories: callback line generated", {
            "user_id": uid, "date": date, "empty": bool(result.get("empty")),
        })
    except Exception as exc:
        logger.warn("Memories: callback line generation failed", {
            "user_id": uid,
            "date": date,
            "error_type": type(exc).__name__,
            "attempt": retry_count + 1,
        })
        try:
            next_count = retry_count + 1
            if next_count >= _MAX_GENERATION_ATTEMPTS:
                failure = {
                    "empty": True,
                    "status": "exhausted",
                    "retry_count": next_count,
                    "generated_at": datetime.now(UTC).isoformat(),
                }
            else:
                failure = {
                    "status": "retry_wait",
                    "retry_count": next_count,
                    "retry_after": (
                        datetime.now(UTC) + _GENERATION_RETRY_DELAY
                    ).isoformat(),
                    "generated_at": datetime.now(UTC).isoformat(),
                }
            await asyncio.to_thread(_line_ref(uid, date).set, failure)
        except Exception:
            pass


async def handle_callback_card(request: Request) -> JSONResponse:
    """GET /memories/callback?date=YYYY-MM-DD. Returns {line, chips} or {}.
    {} is the silent-fallback contract: gate failed, nothing stored yet, bad
    input, or the kill switch - the desktop renders nothing for all of them."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not _callback_enabled():
        return JSONResponse({})
    date = (request.query_params.get("date") or "").strip()
    if not _DATE_RE.match(date):
        return JSONResponse({})

    snap = await asyncio.to_thread(lambda: _line_ref(user_id, date).get())
    stored = snap.to_dict() if snap.exists else None

    if stored is None:
        # Claim the doc so rapid summons don't fan out duplicate generations,
        # then generate in the background; this request returns nothing.
        await asyncio.to_thread(
            _line_ref(user_id, date).set,
            {"status": "generating", "generated_at": datetime.now(UTC).isoformat()},
        )
        asyncio.create_task(_generate_line(user_id, date, retry_count=0))
        return JSONResponse({})

    if stored.get("status") == "generating":
        # In flight (or a crashed attempt: retry if the claim is stale).
        claimed_at = str(stored.get("generated_at", ""))
        stale_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        if claimed_at < stale_cutoff:
            retry_count = int(stored.get("retry_count", 0) or 0)
            await asyncio.to_thread(
                _line_ref(user_id, date).set,
                {
                    "status": "generating",
                    "retry_count": retry_count,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            asyncio.create_task(
                _generate_line(user_id, date, retry_count=retry_count)
            )
        return JSONResponse({})

    if stored.get("status") == "retry_wait":
        retry_count = int(stored.get("retry_count", 0) or 0)
        retry_after = str(stored.get("retry_after", ""))
        if retry_count < _MAX_GENERATION_ATTEMPTS and retry_after <= datetime.now(UTC).isoformat():
            await asyncio.to_thread(
                _line_ref(user_id, date).set,
                {
                    "status": "generating",
                    "retry_count": retry_count,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            asyncio.create_task(
                _generate_line(user_id, date, retry_count=retry_count)
            )
        return JSONResponse({})

    line = str(stored.get("line", "")).strip()
    if not line or stored.get("empty"):
        return JSONResponse({})

    rows = await asyncio.to_thread(_recent_rows, user_id, _MAX_CHIPS)
    chips = [c for doc_id, row in rows if (c := _chip(doc_id, row))["key"] and c["value"]]
    return JSONResponse({"line": line, "chips": chips})


async def handle_list_memories(request: Request) -> JSONResponse:
    """GET /memories - every row, newest first, for the dashboard page."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    rows = await asyncio.to_thread(_recent_rows, user_id, 200)
    items = [c for doc_id, row in rows if (c := _chip(doc_id, row))["key"] and c["value"]]
    logger.info("Memories: listed", {"user_id": user_id, "total": len(items)})
    return JSONResponse({"items": items})


def _invalidate_line(uid: str, date: str) -> None:
    if _DATE_RE.match(date):
        _line_ref(uid, date).delete()


async def handle_delete_memory(request: Request, memory_id: str) -> JSONResponse:
    """DELETE /memories/{memory_id}?date=... - forget one row, owner-only.
    Hard delete, and today's stored callback line goes with it."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    memory_id = (memory_id or "").strip()
    if not memory_id:
        return JSONResponse({"error": "Missing memory id."}, status_code=400)
    date = (request.query_params.get("date") or "").strip()

    def _delete() -> None:
        _memories_ref(user_id).document(memory_id).delete()
        _invalidate_line(user_id, date)

    await asyncio.to_thread(_delete)
    asyncio.create_task(_delete_graph_node_fail_open(user_id, memory_id))
    logger.info("Memories: deleted", {"user_id": user_id, "memory_id": memory_id})
    return JSONResponse({"ok": True})


async def _delete_graph_node_fail_open(uid: str, node_id: str) -> None:
    try:
        await delete_node(uid, node_id)
    except Exception as exc:
        logger.warn("Memories: graph delete failed open", {
            "user_id": uid,
            "node_id": node_id,
            "error": str(exc),
        })


async def handle_patch_memory(request: Request, memory_id: str) -> JSONResponse:
    """PATCH /memories/{memory_id}?date=... with body {value} - dashboard
    edit. Value only (the key names the fact, the value is the fact), capped
    and sanitized because it flows into the voice prompt."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    memory_id = (memory_id or "").strip()
    if not memory_id:
        return JSONResponse({"error": "Missing memory id."}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid body."}, status_code=400)
    value = _sanitize_value(str(body.get("value", "")))
    if not value:
        return JSONResponse({"error": "Value is required."}, status_code=400)
    date = (request.query_params.get("date") or "").strip()

    def _update() -> bool:
        doc_ref = _memories_ref(user_id).document(memory_id)
        if not doc_ref.get().exists:
            return False
        doc_ref.update({"value": value, "updated_at": datetime.now(UTC).isoformat()})
        _invalidate_line(user_id, date)
        return True

    ok = await asyncio.to_thread(_update)
    if not ok:
        return JSONResponse({"error": "Not found."}, status_code=404)
    logger.info("Memories: edited", {"user_id": user_id, "memory_id": memory_id})
    return JSONResponse({"ok": True})
