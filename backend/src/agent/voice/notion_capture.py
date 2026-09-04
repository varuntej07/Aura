"""Voice-triggered capture of the current screen into the user's Notion.

Worker-side orchestration of the section 3 firebreak
(Aura-Desktop/future-features.txt): the tool-free extractor runs HERE so the
screen payload never leaves worker RAM except into that one model call, while
destination resolution and the deterministic write happen on juno-backend via
/notion/* routes, authenticated with the session's own Firebase ID token
(uid-scoped, same header pattern as the MCP server connection).

Structure mirrors screen_saves.py: explicit kwargs in, a frozen result
dataclass out carrying the exact confirmation Buddy may speak, a stable
idempotency id, and the desktop receipt published only after the durable
write. Nothing spoken is ever sourced from the screen; database titles come
from Notion itself.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field

import httpx

from ...config.settings import settings
from ...lib.logger import logger
from ...services.notion.extract import (
    CaptureRecord,
    extract_from_frame,
    extract_from_structured_text,
)
from .screen_saves import capture_item_id
from .transport import current_room, publish_client_event

_RESOLVE_TIMEOUT_S = 15.0
_WRITE_TIMEOUT_S = 25.0

_FAILURE_LINE = "Something went wrong saving that to Notion - try again?"
_NO_SCREEN_LINE = "I can't see your screen right now, so there's nothing to save."
_RECONNECT_LINE = "Your Notion connection needs a refresh - reconnect it from the dashboard and I'll save this."


@dataclass(frozen=True, slots=True)
class NotionCandidate:
    data_source_id: str
    title: str


@dataclass(frozen=True, slots=True)
class SaveToNotionResult:
    """Outcome plus the short confirmation Buddy may speak verbatim."""

    spoken_confirmation: str
    saved: bool = False
    page_url: str | None = None
    database_name: str | None = None
    idempotency_key: str | None = None
    dropped_fields: list[str] = field(default_factory=list)
    already_saved: bool = False
    # Set when the turn ends in a question instead of a write: the model asks
    # the user and calls the tool again with a confirmed choice.
    candidates: list[NotionCandidate] = field(default_factory=list)
    proposed_create_name: str | None = None


class _ReauthorizationRequired(Exception):
    pass


class _BackendCallFailed(Exception):
    pass


def _headers(firebase_id_token: str, session_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {firebase_id_token}",
        "X-Aura-Voice-Session": session_id,
    }


async def _post_backend(
    path: str,
    body: dict,
    *,
    firebase_id_token: str,
    session_id: str,
    timeout_s: float,
) -> dict:
    url = f"{settings.BACKEND_INTERNAL_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(
            url, json=body, headers=_headers(firebase_id_token, session_id)
        )
    if response.status_code == 409:
        raise _ReauthorizationRequired()
    if response.status_code != 200:
        raise _BackendCallFailed(f"{path} -> {response.status_code}")
    return response.json()


async def execute_notion_capture(
    *,
    uid: str,
    session_id: str,
    finalized_message_id: str,
    firebase_id_token: str,
    intent: str,
    destination: str,
    confirmed_data_source_id: str = "",
    confirmed_database_name: str = "",
    create_database_named: str = "",
    structured_text: str | None,
    structured_snapshot_id: str,
    jpeg_bytes: bytes | None,
    frame_id: str,
) -> SaveToNotionResult:
    """One authorized capture into one Notion destination.

    Extraction and resolution run concurrently (extract reads the screen,
    resolve reads the utterance; independent by construction). The write only
    happens against a bound data source; ask/propose outcomes return a
    question instead and write nothing.
    """
    if not uid or not session_id or not finalized_message_id or not firebase_id_token:
        return SaveToNotionResult(spoken_confirmation=_FAILURE_LINE)
    if not structured_text and not jpeg_bytes:
        return SaveToNotionResult(spoken_confirmation=_NO_SCREEN_LINE)

    snapshot_id = structured_snapshot_id if structured_text else frame_id
    idempotency_key = capture_item_id(
        uid=uid,
        session_id=session_id,
        finalized_message_id=finalized_message_id,
        frame_id=snapshot_id,
    )

    async def _extract() -> CaptureRecord | None:
        if structured_text:
            return await extract_from_structured_text(
                intent=intent, rendered_tree=structured_text
            )
        assert jpeg_bytes is not None
        return await extract_from_frame(
            intent=intent,
            jpeg_base64=base64.b64encode(jpeg_bytes).decode("ascii"),
        )

    async def _resolve() -> dict | None:
        if confirmed_data_source_id or create_database_named:
            return None
        return await _post_backend(
            "/notion/resolve",
            {"spoken_destination": destination},
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            timeout_s=_RESOLVE_TIMEOUT_S,
        )

    try:
        record, resolved = await asyncio.gather(_extract(), _resolve())
    except _ReauthorizationRequired:
        return SaveToNotionResult(spoken_confirmation=_RECONNECT_LINE)
    except Exception as exc:
        logger.warn(
            "notion_capture: extract/resolve failed",
            {"user_id": uid, "session_id": session_id, "error": str(exc)},
        )
        return SaveToNotionResult(spoken_confirmation=_FAILURE_LINE)

    if record is None:
        return SaveToNotionResult(
            spoken_confirmation=(
                "I couldn't make out anything on the screen worth saving - try again?"
            )
        )

    # Bind the destination. Only the user's words (or their confirmed choice)
    # ever decide this; nothing screen-derived is a candidate input.
    data_source_id = confirmed_data_source_id
    database_name = confirmed_database_name
    if create_database_named:
        try:
            created = await _post_backend(
                "/notion/create-database",
                {"name": create_database_named},
                firebase_id_token=firebase_id_token,
                session_id=session_id,
                timeout_s=_WRITE_TIMEOUT_S,
            )
        except _ReauthorizationRequired:
            return SaveToNotionResult(spoken_confirmation=_RECONNECT_LINE)
        except Exception as exc:
            logger.warn(
                "notion_capture: database create failed",
                {"user_id": uid, "session_id": session_id, "error": str(exc)},
            )
            return SaveToNotionResult(
                spoken_confirmation="I couldn't create that database in Notion - try again?"
            )
        data_source_id = str(created.get("data_source_id") or "")
        database_name = str(created.get("database_name") or create_database_named)
    elif not data_source_id and resolved is not None:
        outcome = str(resolved.get("outcome") or "")
        if outcome == "bind":
            data_source_id = str(resolved.get("data_source_id") or "")
            database_name = str(resolved.get("title") or destination)
        elif outcome == "ask":
            candidates = [
                NotionCandidate(
                    data_source_id=str(candidate.get("data_source_id") or ""),
                    title=str(candidate.get("title") or ""),
                )
                for candidate in resolved.get("candidates", [])
                if candidate.get("data_source_id")
            ]
            titles = " or ".join(candidate.title for candidate in candidates[:2])
            return SaveToNotionResult(
                spoken_confirmation=f"Did you mean {titles}?",
                candidates=candidates,
            )
        elif outcome in ("propose_create", "no_databases"):
            name = " ".join(destination.split())[:80]
            return SaveToNotionResult(
                spoken_confirmation=(
                    f"I don't see a database like that in your Notion. "
                    f"Want me to create one called {name}?"
                ),
                proposed_create_name=name,
            )

    if not data_source_id:
        return SaveToNotionResult(spoken_confirmation=_FAILURE_LINE)

    try:
        written = await _post_backend(
            "/notion/write",
            {
                "data_source_id": data_source_id,
                "database_name": database_name or "Notion",
                "record": record.model_dump(),
                "idempotency_key": idempotency_key,
                "session_id": session_id,
            },
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except _ReauthorizationRequired:
        return SaveToNotionResult(spoken_confirmation=_RECONNECT_LINE)
    except Exception as exc:
        logger.error(
            "notion_capture: write failed",
            {
                "user_id": uid,
                "session_id": session_id,
                "idempotency_key": idempotency_key,
                "error": str(exc),
            },
        )
        return SaveToNotionResult(spoken_confirmation=_FAILURE_LINE)

    saved_name = str(written.get("database_name") or database_name or "Notion")
    dropped = [str(name) for name in written.get("dropped_fields", [])]
    already = bool(written.get("already_saved"))
    if already:
        spoken = "Already saved it."
    elif dropped:
        spoken = (
            f"Saved to {saved_name}. "
            f"{'One field' if len(dropped) == 1 else 'A few fields'} didn't fit the database, "
            "so I put the details in the page body."
        )
    else:
        spoken = f"Saved to {saved_name}."

    await _publish_notion_saved(
        database_name=saved_name,
        page_url=str(written.get("page_url") or ""),
        page_id=str(written.get("page_id") or ""),
        session_id=session_id,
        user_id=uid,
    )
    logger.info(
        "notion_capture: capture persisted",
        {
            "user_id": uid,
            "session_id": session_id,
            "idempotency_key": idempotency_key,
            "dropped_field_count": len(dropped),
            "already_saved": already,
        },
    )
    return SaveToNotionResult(
        spoken_confirmation=spoken,
        saved=True,
        page_url=written.get("page_url"),
        database_name=saved_name,
        idempotency_key=idempotency_key,
        dropped_fields=dropped,
        already_saved=already,
    )


async def execute_notion_undo(
    *,
    uid: str,
    session_id: str,
    firebase_id_token: str,
    idempotency_key: str,
) -> bool:
    try:
        result = await _post_backend(
            "/notion/undo",
            {"idempotency_key": idempotency_key},
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn(
            "notion_capture: undo failed",
            {"user_id": uid, "session_id": session_id, "error": str(exc)},
        )
        return False
    return bool(result.get("ok"))


async def _publish_notion_saved(
    *, database_name: str, page_url: str, page_id: str, session_id: str, user_id: str
) -> None:
    """Publish the desktop caption after durable persistence; fail soft."""
    await publish_client_event(
        current_room(),
        "notion.saved",
        {
            "database_name": database_name,
            "page_url": page_url,
            "page_id": page_id,
        },
        log_message="notion_capture: notion.saved publish failed",
        log_fields={"session_id": session_id, "user_id": user_id},
    )
