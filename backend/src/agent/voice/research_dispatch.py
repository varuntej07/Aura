"""Voice-dispatched background research delivered into the user's Notion.

Dispatch is synchronous inside one voice turn and tiny by design: resolve the
spoken destination (utterance only - the Phase 1 firebreak), then POST
/research with the delivery binding. The run doc plus the first Cloud Tasks
stage ARE the dispatch, so the run survives hang-up by construction;
juno-backend executes everything.

RunNarrator is the live half, and it POLLS - deliberately. No backend-to-room
push exists in this system and the worker holds no Firestore listeners; a
10-second authed GET of the run projection, cursored on state_revision, is
the in-pattern mechanism (notion_capture's HTTP client, GuideRuntime's
loop/close discipline). It speaks only from TYPED fields (state, counts, the
pending question's own text) at turn boundaries with the user idle, and it
claims "saved" only after the delivery receipt exists on the run. Researched
content never enters the voice context. A narration failure is silent: a
proactive nudge must never become a spoken apology.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

import httpx

from ...config.settings import settings
from ...lib.logger import logger
from ..voice.interview.models import buddy_owns_conversation
from .transport import await_turn_boundary

_DISPATCH_TIMEOUT_S = 20.0
_POLL_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 10.0
# Coalescing floor between spoken progress updates. Terminal and question
# events bypass it: those are the two things the user is actually waiting on.
_MIN_NARRATION_GAP_S = 20.0

_FAILURE_LINE = "I couldn't start that research - try again?"
_RECONNECT_LINE = "Your Notion connection needs a refresh - reconnect it from the dashboard first."

# Spoken labels for engine states. Typed field -> fixed phrase; nothing
# model-generated and nothing content-derived.
_STATE_LABELS = {
    "planning": "planning it out",
    "queued": "queued up",
    "searching": "searching sources",
    "reading": "reading sources",
    "verifying": "verifying what it found",
    "synthesizing": "writing it up",
}


@dataclass(frozen=True, slots=True)
class ResearchDispatchResult:
    """Outcome plus the confirmation or question Buddy may speak verbatim."""

    spoken_confirmation: str
    dispatched: bool = False
    run_id: str | None = None
    database_name: str | None = None
    candidates: list[tuple[str, str]] = field(default_factory=list)
    proposed_create_name: str | None = None


def _headers(firebase_id_token: str, session_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {firebase_id_token}",
        "X-Aura-Voice-Session": session_id,
    }


async def _backend_request(
    method: str,
    path: str,
    *,
    firebase_id_token: str,
    session_id: str,
    json_body: dict | None = None,
    timeout_s: float = _DISPATCH_TIMEOUT_S,
) -> httpx.Response:
    url = f"{settings.BACKEND_INTERNAL_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await client.request(
            method, url, json=json_body, headers=_headers(firebase_id_token, session_id)
        )


async def dispatch_research_to_notion(
    *,
    uid: str,
    session_id: str,
    firebase_id_token: str,
    request: str,
    destination: str,
    confirmed_data_source_id: str = "",
    confirmed_database_name: str = "",
    create_database_named: str = "",
) -> ResearchDispatchResult:
    """Bind the destination, then create the durable run. ~1-1.3s warm."""
    cleaned_request = " ".join((request or "").split())
    if not cleaned_request or len(cleaned_request) > 2_000:
        return ResearchDispatchResult(spoken_confirmation=_FAILURE_LINE)

    data_source_id = confirmed_data_source_id
    database_name = confirmed_database_name

    if create_database_named:
        response = await _backend_request(
            "POST",
            "/notion/create-database",
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            json_body={"name": create_database_named},
        )
        if response.status_code == 409:
            return ResearchDispatchResult(spoken_confirmation=_RECONNECT_LINE)
        if response.status_code != 200:
            return ResearchDispatchResult(
                spoken_confirmation="I couldn't create that database in Notion - try again?"
            )
        created = response.json()
        data_source_id = str(created.get("data_source_id") or "")
        database_name = str(created.get("database_name") or create_database_named)
    elif not data_source_id:
        response = await _backend_request(
            "POST",
            "/notion/resolve",
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            json_body={"spoken_destination": destination},
        )
        if response.status_code == 409:
            return ResearchDispatchResult(spoken_confirmation=_RECONNECT_LINE)
        if response.status_code != 200:
            return ResearchDispatchResult(spoken_confirmation=_FAILURE_LINE)
        resolved = response.json()
        outcome = str(resolved.get("outcome") or "")
        if outcome == "bind":
            data_source_id = str(resolved.get("data_source_id") or "")
            database_name = str(resolved.get("title") or destination)
        elif outcome == "ask":
            candidates = [
                (str(item.get("data_source_id") or ""), str(item.get("title") or ""))
                for item in resolved.get("candidates", [])
                if item.get("data_source_id")
            ]
            titles = " or ".join(title for _, title in candidates[:2])
            return ResearchDispatchResult(
                spoken_confirmation=f"Which database - {titles}?",
                candidates=candidates,
            )
        else:
            name = " ".join(destination.split())[:80]
            return ResearchDispatchResult(
                spoken_confirmation=(
                    f"I don't see a database like that in your Notion. "
                    f"Want me to create one called {name} for the results?"
                ),
                proposed_create_name=name,
            )

    if not data_source_id:
        return ResearchDispatchResult(spoken_confirmation=_FAILURE_LINE)

    # Salted run identity, mirroring tool_executor's fix: constant per session
    # so an identical retry replays, distinct per request so two topics in one
    # call never collapse onto one run doc.
    request_digest = hashlib.sha256(cleaned_request.casefold().encode("utf-8")).hexdigest()[:16]
    client_run_id = f"voice:{session_id}:{request_digest}"

    response = await _backend_request(
        "POST",
        "/research",
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        json_body={
            "request": cleaned_request,
            "depth": "quick",
            "client_run_id": client_run_id,
            "origin_surface": "voice",
            "delivery": {
                "data_source_id": data_source_id,
                "database_name": database_name or "Notion",
            },
        },
    )
    if response.status_code in (200, 202):
        payload = response.json()
        return ResearchDispatchResult(
            spoken_confirmation=(
                f"On it. I'll research that and save it to {database_name or 'your Notion'}, "
                "and keep you posted."
            ),
            dispatched=True,
            run_id=str(payload.get("run_id") or ""),
            database_name=database_name,
        )

    detail: dict = {}
    try:
        detail = dict(response.json().get("detail") or {})
    except Exception:
        pass
    code = str(detail.get("code") or "")
    if code == "research_cap_reached":
        spoken = "You've used up today's research runs - I can do this one tomorrow."
    elif code == "research_requires_paid":
        spoken = "Background research needs a paid plan, so I can't run this one."
    elif response.status_code == 409:
        spoken = "One research run at a time - want me to cancel the current one first?"
    else:
        spoken = _FAILURE_LINE
    logger.warn(
        "research_dispatch: run create refused",
        {"user_id": uid, "session_id": session_id, "status": response.status_code, "code": code},
    )
    return ResearchDispatchResult(spoken_confirmation=spoken)


async def cancel_research_run(
    *, session_id: str, firebase_id_token: str, run_id: str
) -> bool:
    response = await _backend_request(
        "POST",
        f"/research/{run_id}/cancel",
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        json_body={"correlation_id": f"voice:{session_id}"},
    )
    return response.status_code == 200


async def answer_research_run(
    *,
    session_id: str,
    firebase_id_token: str,
    run_id: str,
    question_id: str,
    answer_text: str,
) -> bool:
    response = await _backend_request(
        "POST",
        f"/research/{run_id}/answer",
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        json_body={
            "question_id": question_id,
            "answer": {"text": answer_text, "via": "voice"},
            "correlation_id": f"voice:{session_id}",
        },
    )
    return response.status_code == 200


@dataclass(slots=True)
class PendingVoiceQuestion:
    run_id: str
    question_id: str
    text: str
    choices: list[str]


class RunNarrator:
    """Per-session progress narration for this session's dispatched runs.

    GuideRuntime's ownership shape: an owned object with start()/close(), one
    named task, silent failure. The loop wakes every _POLL_INTERVAL_S while
    runs are live and sleeps on an Event otherwise.
    """

    def __init__(
        self,
        *,
        session,
        session_id: str,
        user_id: str,
        firebase_id_token: str,
    ) -> None:
        self._session = session
        self._session_id = session_id
        self._user_id = user_id
        self._firebase_id_token = firebase_id_token
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._closed = False
        # run_id -> last narrated state_revision
        self._revisions: dict[str, int] = {}
        self._active_runs: dict[str, str] = {}  # run_id -> database_name
        self._last_spoken_at = 0.0
        self.pending_question: PendingVoiceQuestion | None = None

    @property
    def active_run_ids(self) -> list[str]:
        return list(self._active_runs)

    def track(self, run_id: str, database_name: str) -> None:
        if self._closed or not run_id:
            return
        self._active_runs[run_id] = database_name
        self._revisions.setdefault(run_id, -1)
        self._wake.set()
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"research-narrator-{self._session_id[:8]}"
            )

    def forget(self, run_id: str) -> None:
        self._active_runs.pop(run_id, None)
        if self.pending_question and self.pending_question.run_id == run_id:
            self.pending_question = None

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._closed:
            if not self._active_runs:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warn(
                    "research_narrator: poll failed",
                    {"session_id": self._session_id, "error": str(exc)},
                )
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _poll_once(self) -> None:
        for run_id in list(self._active_runs):
            response = await _backend_request(
                "GET",
                f"/research/{run_id}",
                firebase_id_token=self._firebase_id_token,
                session_id=self._session_id,
                timeout_s=_POLL_TIMEOUT_S,
            )
            if response.status_code != 200:
                continue
            projection = response.json()
            revision = int(projection.get("state_revision") or 0)
            if revision <= self._revisions.get(run_id, -1):
                continue
            self._revisions[run_id] = revision
            await self._narrate(run_id, projection)

    async def _narrate(self, run_id: str, projection: dict) -> None:
        state = str(projection.get("state") or "")
        database_name = self._active_runs.get(run_id) or str(
            (projection.get("delivery") or {}).get("database_name") or "your Notion"
        )

        if state == "awaiting_clarification":
            pending = dict(projection.get("pending_question") or {})
            question_text = str(pending.get("text") or "").strip()
            choices = [str(item) for item in (pending.get("choices") or []) if str(item)]
            if not question_text:
                return
            self.pending_question = PendingVoiceQuestion(
                run_id=run_id,
                question_id=str(pending.get("question_id") or ""),
                text=question_text,
                choices=choices,
            )
            numbered = "; ".join(
                f"option {index + 1}: {choice}" for index, choice in enumerate(choices)
            )
            await self._speak(
                "The background research hit a question and is paused on it. "
                f"Ask the user, in your own words: {question_text}"
                + (f" The options are {numbered}." if numbered else "")
                + " They can answer, or say to just use your best judgment.",
                urgent=True,
            )
            return

        if state in ("ready", "partial", "failed", "cancelled"):
            self.forget(run_id)
            if state == "cancelled":
                return  # the user did this; telling them is noise
            delivery_result = dict(projection.get("delivery_result") or {})
            if delivery_result.get("page_id"):
                line = (
                    f"The research is done and saved to {database_name} in their Notion"
                    + (" with some gaps noted" if state == "partial" else "")
                    + "."
                )
            elif state == "failed":
                line = "The background research could not be completed. The details are in the app."
            else:
                line = (
                    "The research finished, but saving it to Notion failed - "
                    "the brief is in the app."
                )
            await self._speak(
                f"Tell the user briefly, in your own words: {line}", urgent=True
            )
            return

        label = _STATE_LABELS.get(state)
        if label is None:
            return
        source_count = int(projection.get("source_count") or 0)
        detail = f", {source_count} sources so far" if source_count else ""
        await self._speak(
            "Give the user a one-sentence progress note in your own words: "
            f"the background research is {label}{detail}.",
            urgent=False,
        )

    async def _speak(self, instructions: str, *, urgent: bool) -> None:
        if self._closed:
            return
        if not urgent and (time.monotonic() - self._last_spoken_at) < _MIN_NARRATION_GAP_S:
            return
        if not buddy_owns_conversation(self._session):
            return
        # A proactive nudge must never talk over the user; wait out both sides.
        await await_turn_boundary(self._session, require_user_idle=True)
        if self._closed or not buddy_owns_conversation(self._session):
            return
        try:
            speech = self._session.generate_reply(instructions=instructions)
            await speech
            self._last_spoken_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "research_narrator: narration failed",
                {"session_id": self._session_id, "error": str(exc)},
            )
