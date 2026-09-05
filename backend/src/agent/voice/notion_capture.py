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

from ...lib.logger import logger
from ...services.notion.extract import (
    CaptureRecord,
    extract_from_frame,
    extract_from_structured_text,
)
from .notion_backend import (
    DestinationCopy,
    ReauthorizationRequired as _ReauthorizationRequired,
    create_database_backend,
    decide_destination,
    post_backend,
    resolve_spoken_destination,
)
from .screen_context_control import spoken_no_screen_line
from .screen_saves import capture_item_id
from .transport import current_room, publish_client_event

_RESOLVE_TIMEOUT_S = 20.0
# Must exceed the backend's worst honest case: schema fetch plus a page create
# that rides the connector's full retry discipline (up to 3 sends with
# Retry-After sleeps capped at 5s each). 25s sat exactly at that boundary and
# timed out on pages that were landing.
_WRITE_TIMEOUT_S = 45.0
# Hard ceiling on the extract+resolve phase as a whole. The extraction model
# call is otherwise bounded only by model_provider's per-attempt timeout times
# its retries (minutes), and this runs behind a live voice turn: past this,
# the user hears the failure line instead of silence.
_CAPTURE_PHASE_WALL_CLOCK_S = 30.0

_FAILURE_LINE = "Something went wrong saving that to Notion - try again?"
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
    # Machine-readable failure cause, populated on every failure path so the
    # voice action receipt records WHY, not just the spoken line.
    error_code: str | None = None
    dropped_fields: list[str] = field(default_factory=list)
    already_saved: bool = False
    # Set when the turn ends in a question instead of a write: the model asks
    # the user and calls the tool again with a confirmed choice.
    candidates: list[NotionCandidate] = field(default_factory=list)
    proposed_create_name: str | None = None


# The spoken halves of the ask/propose outcomes; the decision tree itself is
# shared with research_dispatch via notion_backend.decide_destination.
_DESTINATION_COPY = DestinationCopy(
    ask_format="Did you mean {titles}?",
    propose_format=(
        "I don't see a database like that in your Notion. "
        "Want me to create one called {name}?"
    ),
)


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
    screen_unavailable_reason: str = "",
) -> SaveToNotionResult:
    """One authorized capture into one Notion destination.

    Extraction and resolution run concurrently (extract reads the screen,
    resolve reads the utterance; independent by construction). The write only
    happens against a bound data source; ask/propose outcomes return a
    question instead and write nothing.
    """
    if not uid or not session_id or not finalized_message_id or not firebase_id_token:
        return SaveToNotionResult(
            spoken_confirmation=_FAILURE_LINE, error_code="invalid_arguments"
        )
    if not structured_text and not jpeg_bytes:
        # The desktop's screen_context.unavailable signal (when one arrived
        # this turn) turns the generic "can't see your screen" into the actual
        # reason plus the user's next step; see screen_context_control.
        return SaveToNotionResult(
            spoken_confirmation=spoken_no_screen_line(screen_unavailable_reason),
            error_code=(
                f"no_screen:{screen_unavailable_reason}"
                if screen_unavailable_reason
                else "no_screen_available"
            ),
        )

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
        return await resolve_spoken_destination(
            destination=destination,
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            timeout_s=_RESOLVE_TIMEOUT_S,
        )

    # Extraction and resolution fail differently and must be told apart in the
    # logs: "the model couldn't read the screen" and "the backend refused the
    # resolve" are different root causes wearing the same spoken line.
    try:
        record_outcome, resolve_outcome = await asyncio.wait_for(
            asyncio.gather(_extract(), _resolve(), return_exceptions=True),
            timeout=_CAPTURE_PHASE_WALL_CLOCK_S,
        )
    except TimeoutError:
        logger.warn(
            "notion_capture: extract/resolve exceeded wall clock",
            {
                "user_id": uid,
                "session_id": session_id,
                "idempotency_key": idempotency_key,
                "wall_clock_s": _CAPTURE_PHASE_WALL_CLOCK_S,
            },
        )
        return SaveToNotionResult(
            spoken_confirmation=_FAILURE_LINE,
            error_code="capture_wall_clock_expired",
            idempotency_key=idempotency_key,
        )

    if isinstance(resolve_outcome, _ReauthorizationRequired):
        return SaveToNotionResult(
            spoken_confirmation=_RECONNECT_LINE,
            error_code="reauthorization_required",
            idempotency_key=idempotency_key,
        )
    if isinstance(resolve_outcome, BaseException):
        logger.warn(
            "notion_capture: destination resolve failed",
            {
                "user_id": uid,
                "session_id": session_id,
                "idempotency_key": idempotency_key,
                "error": str(resolve_outcome),
            },
        )
        return SaveToNotionResult(
            spoken_confirmation=_FAILURE_LINE,
            error_code="resolve_failed",
            idempotency_key=idempotency_key,
        )
    if isinstance(record_outcome, BaseException):
        logger.warn(
            "notion_capture: extraction failed",
            {
                "user_id": uid,
                "session_id": session_id,
                "idempotency_key": idempotency_key,
                "error": str(record_outcome),
            },
        )
        return SaveToNotionResult(
            spoken_confirmation=_FAILURE_LINE,
            error_code="extraction_failed",
            idempotency_key=idempotency_key,
        )
    record, resolved = record_outcome, resolve_outcome

    if record is None:
        return SaveToNotionResult(
            spoken_confirmation=(
                "I couldn't make out anything on the screen worth saving - try again?"
            ),
            error_code="nothing_extracted",
            idempotency_key=idempotency_key,
        )

    # Bind the destination via the shared decision tree. Only the user's words
    # (or their confirmed choice) ever decide this; nothing screen-derived is
    # a candidate input.
    if create_database_named:
        try:
            data_source_id, database_name = await create_database_backend(
                name=create_database_named,
                firebase_id_token=firebase_id_token,
                session_id=session_id,
                timeout_s=_WRITE_TIMEOUT_S,
            )
        except _ReauthorizationRequired:
            return SaveToNotionResult(
                spoken_confirmation=_RECONNECT_LINE,
                error_code="reauthorization_required",
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            logger.warn(
                "notion_capture: database create failed",
                {
                    "user_id": uid,
                    "session_id": session_id,
                    "idempotency_key": idempotency_key,
                    "error": str(exc),
                },
            )
            return SaveToNotionResult(
                spoken_confirmation="I couldn't create that database in Notion - try again?",
                error_code="database_create_failed",
                idempotency_key=idempotency_key,
            )
    else:
        decision = decide_destination(
            resolved,
            destination=destination,
            confirmed_data_source_id=confirmed_data_source_id,
            confirmed_database_name=confirmed_database_name,
            copy=_DESTINATION_COPY,
        )
        if decision.question is not None:
            return SaveToNotionResult(
                spoken_confirmation=decision.question,
                candidates=[
                    NotionCandidate(data_source_id=candidate_id, title=title)
                    for candidate_id, title in decision.candidates
                ],
                proposed_create_name=decision.proposed_create_name,
            )
        data_source_id = decision.data_source_id
        database_name = decision.database_name

    if not data_source_id:
        return SaveToNotionResult(
            spoken_confirmation=_FAILURE_LINE,
            error_code="unresolved_destination",
            idempotency_key=idempotency_key,
        )

    try:
        written = await post_backend(
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
        return SaveToNotionResult(
            spoken_confirmation=_RECONNECT_LINE,
            error_code="reauthorization_required",
            idempotency_key=idempotency_key,
        )
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
        return SaveToNotionResult(
            spoken_confirmation=_FAILURE_LINE,
            error_code="write_failed",
            idempotency_key=idempotency_key,
        )

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
        result = await post_backend(
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
