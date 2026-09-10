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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from ...lib.logger import logger
from ...services.research.run_identity import client_run_id_for, retry_salted
from ..voice.interview.models import buddy_owns_conversation
from .notion_backend import (
    DestinationCopy,
    ReauthorizationRequired,
    create_database_backend,
    decide_destination,
    request_backend,
    resolve_spoken_destination,
)
from .transport import await_turn_boundary

_DISPATCH_TIMEOUT_S = 20.0
_POLL_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 10.0
# A 404 is authoritative quickly (the run doc is gone); other failures get a
# longer leash for transient backend blips before the run is dropped.
_POLL_404_EVICT_AFTER = 2
_POLL_ERROR_EVICT_AFTER = 6
# Coalescing floor between non-urgent spoken notes (today only the stall note).
# Terminal and question events bypass it: those are the two things the user is
# actually waiting on.
_MIN_NARRATION_GAP_S = 20.0
# A run whose state_revision has not advanced for this long earns one spoken
# "taking longer than usual" note. Checkpoint narration (clarification,
# terminal receipt, stall) replaced the old per-revision progress notes: a
# healthy run advancing through its stages is not news the user needs read to
# them, and a stalled one is.
_STALL_NOTE_AFTER_S = 180.0

# States where the run has a result (or never will); everything else is live.
_RESULT_TERMINAL_STATES = ("ready", "partial", "failed", "cancelled")
# Rehydration ignores non-terminal runs that stopped updating longer ago than
# this: the framer deadline terminalizes real runs well inside it, so an older
# row is a corpse, and re-tracking it would poll a dead run for the session.
_REHYDRATE_MAX_AGE = timedelta(hours=24)
# Polls to keep waiting for notion_deliver's receipt after the run goes
# result-terminal before narrating without one. 30 x 10s = 5 minutes, which
# covers the deliver stage's own retry backoff (Cloud Tasks min 10s, max 300s
# on the second attempt); past it, narrate on what exists rather than never.
_DELIVERY_RESULT_WAIT_POLLS = 30

_FAILURE_LINE = "I couldn't start that research - try again?"
_RECONNECT_LINE = "Your Notion connection needs a refresh - reconnect it from the dashboard first."
_DELIVER_FAILURE_LINE = "I couldn't save that research to Notion - try again?"

# Each engine refusal is a different fact, and only one of them is "try again".
# Keyed on the reason the engine returns, never on anything the user said.
_DELIVER_REFUSALS = {
    "still_running": (
        "That research is still going. Ask me again once it's done and I'll put it in Notion."
    ),
    "already_bound": (
        "That one's already headed to Notion - I can't change where it lands once it's set."
    ),
    "not_deliverable": (
        "There's nothing to save from that run - want me to research it fresh into Notion?"
    ),
    "not_found": "I can't find that research run any more.",
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
    # True only on a cache replay: this exact confirmation was already spoken
    # for this finalized message. The per-message cache dedups the backend
    # dispatch, and used to hand the identical line back to the verbatim speech
    # path, which spoke it again word for word. Carried on the result so the
    # tool wrapper can drop `render: verbatim` for the replay alone and leave
    # every first-time envelope exactly as it was.
    already_spoken: bool = False


# The spoken halves of the ask/propose outcomes; the decision tree itself is
# shared with notion_capture via notion_backend.decide_destination.
_DESTINATION_COPY = DestinationCopy(
    ask_format="Which database - {titles}?",
    propose_format=(
        "I don't see a database like that in your Notion. "
        "Want me to create one called {name} for the results?"
    ),
    unspecified_format="Where in Notion should the results go - {titles}?",
    unnamed_format=(
        "You don't have any Notion databases yet - what should I call one for the results?"
    ),
)


async def _backend_request(
    method: str,
    path: str,
    *,
    firebase_id_token: str,
    session_id: str,
    json_body: dict | None = None,
    timeout_s: float = _DISPATCH_TIMEOUT_S,
) -> httpx.Response:
    return await request_backend(
        method,
        path,
        firebase_id_token=firebase_id_token,
        session_id=session_id,
        json_body=json_body,
        timeout_s=timeout_s,
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

    if create_database_named:
        try:
            data_source_id, database_name = await create_database_backend(
                name=create_database_named,
                firebase_id_token=firebase_id_token,
                session_id=session_id,
                timeout_s=_DISPATCH_TIMEOUT_S,
            )
        except ReauthorizationRequired:
            return ResearchDispatchResult(spoken_confirmation=_RECONNECT_LINE)
        except Exception as exc:
            logger.warn(
                "research_dispatch: database create failed",
                {"user_id": uid, "session_id": session_id, "error": str(exc)},
            )
            return ResearchDispatchResult(
                spoken_confirmation="I couldn't create that database in Notion - try again?"
            )
    else:
        resolved: dict | None = None
        if not confirmed_data_source_id:
            try:
                resolved = await resolve_spoken_destination(
                    destination=destination,
                    firebase_id_token=firebase_id_token,
                    session_id=session_id,
                    timeout_s=_DISPATCH_TIMEOUT_S,
                )
            except ReauthorizationRequired:
                return ResearchDispatchResult(spoken_confirmation=_RECONNECT_LINE)
            except Exception as exc:
                logger.warn(
                    "research_dispatch: destination resolve failed",
                    {"user_id": uid, "session_id": session_id, "error": str(exc)},
                )
                return ResearchDispatchResult(spoken_confirmation=_FAILURE_LINE)
        decision = decide_destination(
            resolved,
            destination=destination,
            confirmed_data_source_id=confirmed_data_source_id,
            confirmed_database_name=confirmed_database_name,
            copy=_DESTINATION_COPY,
        )
        if decision.question is not None:
            return ResearchDispatchResult(
                spoken_confirmation=decision.question,
                candidates=decision.candidates,
                proposed_create_name=decision.proposed_create_name,
            )
        data_source_id = decision.data_source_id
        database_name = decision.database_name

    if not data_source_id:
        return ResearchDispatchResult(spoken_confirmation=_FAILURE_LINE)

    # Run identity via the shared helper: the tool name is part of the id so
    # the same request text spoken to start_research and research_to_notion in
    # one session can never collapse onto one run doc (that collision silently
    # dropped the delivery binding). An identical retry of THIS tool replays.
    client_run_id = client_run_id_for(
        scope=f"voice:{session_id}",
        tool_name="research_to_notion",
        request_text=cleaned_request,
    )
    delivery_binding = {
        "data_source_id": data_source_id,
        "database_name": database_name or "Notion",
    }

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
            "delivery": delivery_binding,
        },
    )
    if response.status_code == 200:
        # 200 (not 202) means the deterministic id REPLAYED an existing run.
        # A replay is only acceptable when that run is alive and bound to the
        # same destination; a terminal run or one missing this delivery
        # binding must restart under a salted id, or Buddy says "on it" while
        # nothing will ever deliver.
        payload = response.json()
        replayed_state = str(payload.get("state") or "")
        replayed_delivery = dict(payload.get("delivery") or {})
        if (
            replayed_state in ("failed", "cancelled")
            or replayed_delivery.get("data_source_id") != data_source_id
        ):
            logger.info(
                "research_dispatch: replayed run unusable, restarting salted",
                {
                    "user_id": uid,
                    "session_id": session_id,
                    "replayed_state": replayed_state,
                    "delivery_bound": bool(replayed_delivery.get("data_source_id")),
                },
            )
            response = await _backend_request(
                "POST",
                "/research",
                firebase_id_token=firebase_id_token,
                session_id=session_id,
                json_body={
                    "request": cleaned_request,
                    "depth": "quick",
                    "client_run_id": retry_salted(client_run_id),
                    "origin_surface": "voice",
                    "delivery": delivery_binding,
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
        logger.warn(
            "research_dispatch: refusal body was not JSON",
            {
                "user_id": uid,
                "session_id": session_id,
                "status": response.status_code,
                "body_prefix": response.text[:200],
            },
        )
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


async def deliver_existing_run_to_notion(
    *,
    uid: str,
    session_id: str,
    firebase_id_token: str,
    run_id: str,
    destination: str,
    confirmed_data_source_id: str = "",
    confirmed_database_name: str = "",
    create_database_named: str = "",
) -> ResearchDispatchResult:
    """Send a research run that ALREADY EXISTS into Notion.

    The destination half is resolved by the same two helpers dispatch_research_to_notion
    uses, so disambiguation, the propose-create round trip and the misheard-name rules
    behave identically no matter which of the two the model picked. Only the last step
    differs: this binds the destination onto a finished run instead of creating one.

    Without this the only way to reach Notion was to start a NEW run, so "put that
    research in my CRM" a minute after the brief landed silently paid for the same
    work twice.
    """
    if create_database_named:
        try:
            data_source_id, database_name = await create_database_backend(
                name=create_database_named,
                firebase_id_token=firebase_id_token,
                session_id=session_id,
                timeout_s=_DISPATCH_TIMEOUT_S,
            )
        except ReauthorizationRequired:
            return ResearchDispatchResult(spoken_confirmation=_RECONNECT_LINE)
        except Exception as exc:
            logger.warn(
                "research_dispatch: deliver database create failed",
                {"user_id": uid, "session_id": session_id, "error": str(exc)},
            )
            return ResearchDispatchResult(
                spoken_confirmation="I couldn't create that database in Notion - try again?"
            )
    else:
        resolved: dict | None = None
        if not confirmed_data_source_id:
            try:
                resolved = await resolve_spoken_destination(
                    destination=destination,
                    firebase_id_token=firebase_id_token,
                    session_id=session_id,
                    timeout_s=_DISPATCH_TIMEOUT_S,
                )
            except ReauthorizationRequired:
                return ResearchDispatchResult(spoken_confirmation=_RECONNECT_LINE)
            except Exception as exc:
                logger.warn(
                    "research_dispatch: deliver destination resolve failed",
                    {"user_id": uid, "session_id": session_id, "error": str(exc)},
                )
                return ResearchDispatchResult(spoken_confirmation=_DELIVER_FAILURE_LINE)
        decision = decide_destination(
            resolved,
            destination=destination,
            confirmed_data_source_id=confirmed_data_source_id,
            confirmed_database_name=confirmed_database_name,
            copy=_DESTINATION_COPY,
        )
        if decision.question is not None:
            return ResearchDispatchResult(
                spoken_confirmation=decision.question,
                candidates=decision.candidates,
                proposed_create_name=decision.proposed_create_name,
            )
        data_source_id = decision.data_source_id
        database_name = decision.database_name

    if not data_source_id:
        return ResearchDispatchResult(spoken_confirmation=_DELIVER_FAILURE_LINE)

    resolved_name = database_name or "Notion"
    try:
        response = await _backend_request(
            "POST",
            f"/research/{run_id}/deliver",
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            json_body={
                "data_source_id": data_source_id,
                "database_name": resolved_name,
                "correlation_id": f"voice:{session_id}",
            },
        )
    except Exception as exc:
        logger.warn(
            "research_dispatch: deliver request failed",
            {"session_id": session_id, "run_id": run_id, "error": str(exc)},
        )
        return ResearchDispatchResult(spoken_confirmation=_DELIVER_FAILURE_LINE)

    if response.status_code != 200:
        # The engine names its refusals; each one is a different true sentence, and
        # the wrong one here is Buddy promising a save that will never happen.
        reason = ""
        try:
            reason = str((response.json() or {}).get("error") or "")
        except Exception:
            reason = ""
        logger.info(
            "research_dispatch: deliver refused",
            {
                "session_id": session_id,
                "run_id": run_id,
                "status": response.status_code,
                "reason": reason,
            },
        )
        return ResearchDispatchResult(
            spoken_confirmation=_DELIVER_REFUSALS.get(reason, _DELIVER_FAILURE_LINE)
        )

    return ResearchDispatchResult(
        spoken_confirmation=f"Saving that research into {resolved_name} now.",
        dispatched=True,
        run_id=run_id,
        database_name=resolved_name,
    )


async def cancel_research_run(
    *, session_id: str, firebase_id_token: str, run_id: str
) -> bool:
    """False on any failure, network included: the voice tool speaks
    "I couldn't cancel it just now", never an agent-level stack trace."""
    try:
        response = await _backend_request(
            "POST",
            f"/research/{run_id}/cancel",
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            json_body={"correlation_id": f"voice:{session_id}"},
        )
    except Exception as exc:
        logger.warn(
            "research_dispatch: cancel request failed",
            {"session_id": session_id, "run_id": run_id, "error": str(exc)},
        )
        return False
    return response.status_code == 200


async def answer_research_run(
    *,
    session_id: str,
    firebase_id_token: str,
    run_id: str,
    question_id: str,
    answer_text: str,
) -> bool:
    """False on any failure, network included; the pending question stays set
    so the narrator re-offers it instead of the answer silently vanishing."""
    try:
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
    except Exception as exc:
        logger.warn(
            "research_dispatch: answer request failed",
            {"session_id": session_id, "run_id": run_id, "error": str(exc)},
        )
        return False
    return response.status_code == 200


async def fetch_resumable_research_runs(
    *,
    session_id: str,
    firebase_id_token: str,
) -> list[tuple[str, str, str]]:
    """(run_id, database_name, request) for the user's live Notion-bound runs.

    Session-start rehydration: a user who hung up mid-run and came back had no
    narrator tracking the run, so the "saved to X" receipt never fired for
    them. Newest first, capped by the list route's own limit. Only runs WITH a
    delivery binding are returned - the narrator exists to receipt Notion-bound
    work, and tracking an unbound run would misreport it at terminal. [] on any
    failure: rehydration is best-effort and a failed read must cost nothing.
    """
    try:
        response = await _backend_request(
            "GET",
            "/research?limit=10",
            firebase_id_token=firebase_id_token,
            session_id=session_id,
            timeout_s=_POLL_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn(
            "research_dispatch: rehydration list failed",
            {"session_id": session_id, "error": str(exc)},
        )
        return []
    if response.status_code != 200:
        return []
    try:
        items = list((response.json() or {}).get("items") or [])
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    runs: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("state") or "") in _RESULT_TERMINAL_STATES:
            continue
        delivery = dict(item.get("delivery") or {})
        run_id = str(item.get("run_id") or "")
        if not delivery or not run_id:
            continue
        updated_raw = str(item.get("updated_at") or "")
        if updated_raw:
            try:
                updated_at = datetime.fromisoformat(updated_raw)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if now - updated_at > _REHYDRATE_MAX_AGE:
                    continue
            except ValueError:
                pass  # unparseable timestamps do not disqualify a live state
        runs.append((
            run_id,
            str(delivery.get("database_name") or "your Notion"),
            str(item.get("request") or ""),
        ))
    return runs


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
        # run_id -> the request text captured at track(), so the disambiguation
        # question ("which one - X or Y?") can name runs the way the user did.
        self._descriptions: dict[str, str] = {}
        # run_id -> monotonic time of the last observed revision advance, and
        # the runs that already got their one stall note. One note per run:
        # a genuinely stuck run has its own retry deadline server-side, and
        # repeating "still slow" every three minutes is nagging, not news.
        self._last_advance_at: dict[str, float] = {}
        self._stall_noted: set[str] = set()
        # run_id -> consecutive failed polls; a run that never answers is
        # evicted so a corpse does not get polled every 10s for the session.
        self._poll_failures: dict[str, int] = {}
        # run_id -> polls spent waiting for DELIVERY_RESULT after the run went
        # result-terminal; see the wait in _narrate.
        self._delivery_waits: dict[str, int] = {}
        self._last_spoken_at = 0.0
        self.pending_question: PendingVoiceQuestion | None = None

    @property
    def active_run_ids(self) -> list[str]:
        return list(self._active_runs)

    def run_descriptions(self) -> dict[str, str]:
        """Active run_id -> the request text it was tracked with ("" if unknown)."""
        return {
            run_id: self._descriptions.get(run_id, "")
            for run_id in self._active_runs
        }

    def track(self, run_id: str, database_name: str, description: str = "") -> None:
        if self._closed or not run_id:
            return
        self._active_runs[run_id] = database_name
        self._revisions.setdefault(run_id, -1)
        if description:
            self._descriptions.setdefault(run_id, description)
        self._last_advance_at.setdefault(run_id, time.monotonic())
        self._wake.set()
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"research-narrator-{self._session_id[:8]}"
            )

    def forget(self, run_id: str) -> None:
        self._active_runs.pop(run_id, None)
        self._poll_failures.pop(run_id, None)
        self._delivery_waits.pop(run_id, None)
        self._descriptions.pop(run_id, None)
        self._last_advance_at.pop(run_id, None)
        self._stall_noted.discard(run_id)
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
                failures = self._poll_failures.get(run_id, 0) + 1
                self._poll_failures[run_id] = failures
                cap = (
                    _POLL_404_EVICT_AFTER
                    if response.status_code == 404
                    else _POLL_ERROR_EVICT_AFTER
                )
                logger.warn(
                    "research_narrator: run poll returned non-200",
                    {
                        "session_id": self._session_id,
                        "run_id": run_id,
                        "status": response.status_code,
                        "consecutive_failures": failures,
                    },
                )
                if failures >= cap:
                    logger.warn(
                        "research_narrator: evicting unpollable run",
                        {
                            "session_id": self._session_id,
                            "run_id": run_id,
                            "last_status": response.status_code,
                        },
                    )
                    self.forget(run_id)
                continue
            self._poll_failures.pop(run_id, None)
            projection = response.json()
            revision = int(projection.get("state_revision") or 0)
            if revision <= self._revisions.get(run_id, -1):
                await self._maybe_note_stall(run_id)
                continue
            self._last_advance_at[run_id] = time.monotonic()
            self._revisions[run_id] = revision
            await self._narrate(run_id, projection)

    def _spoken_run_name(self, run_id: str) -> str:
        """"the research on <request>" when a description exists, else generic.

        Naming the run matters the moment two are live: on 2026-09-09 an
        unnamed "research could not be completed" line was followed, one turn
        later, by the model denying that any research it knew about had
        failed. The request text is spoken as data, never matched on.
        """
        description = " ".join(self._descriptions.get(run_id, "").split())[:80]
        if description:
            return f"the research on {description}"
        return "the background research"

    async def _maybe_note_stall(self, run_id: str) -> None:
        """One "taking longer than usual" note per run whose revision stalls.

        Marked noted only after the note was actually spoken: _speak suppresses
        non-urgent lines during ownership handoffs and inside the narration
        gap, and a suppressed note must retry on a later poll rather than be
        silently spent.
        """
        if run_id in self._stall_noted:
            return
        last_advance = self._last_advance_at.get(run_id)
        if last_advance is None:
            return
        if (time.monotonic() - last_advance) < _STALL_NOTE_AFTER_S:
            return
        spoke = await self._speak(
            f"Tell the user briefly, in your own words: {self._spoken_run_name(run_id)} "
            "is still working, just taking longer than usual.",
            urgent=False,
        )
        if spoke:
            self._stall_noted.add(run_id)

    async def _narrate(self, run_id: str, projection: dict) -> None:
        state = str(projection.get("state") or "")
        database_name = self._active_runs.get(run_id) or str(
            (projection.get("delivery") or {}).get("database_name") or "your Notion"
        )
        # Captured BEFORE the terminal branch's forget(), which evicts the
        # description this is built from.
        run_name = self._spoken_run_name(run_id)

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
                f"{run_name[:1].upper()}{run_name[1:]} hit a question and is paused on it. "
                f"Ask the user, in your own words: {question_text}"
                + (f" The options are {numbered}." if numbered else "")
                + " They can answer, or say to just use your best judgment.",
                urgent=True,
            )
            return

        if state in _RESULT_TERMINAL_STATES:
            delivery_result = dict(projection.get("delivery_result") or {})
            if (
                state in ("ready", "partial")
                and dict(projection.get("delivery") or {})
                and not delivery_result
            ):
                # finalize goes result-terminal BEFORE notion_deliver writes
                # its receipt, and every advance bumps state_revision, so a
                # poll in that window sees ready/partial with no receipt yet.
                # Speaking now would misreport and evict the run before the
                # "saved to X" it was tracked for. Rewind the cursor and keep
                # polling; the deliver stage's own advance (or fail_stage's
                # failed receipt) bumps the revision again WITH the receipt.
                # Bounded so registry drift can never poll a run forever.
                waits = self._delivery_waits.get(run_id, 0) + 1
                if waits <= _DELIVERY_RESULT_WAIT_POLLS:
                    self._delivery_waits[run_id] = waits
                    self._revisions[run_id] -= 1
                    return
            self.forget(run_id)
            if state == "cancelled":
                return  # the user did this; telling them is noise
            binding = ""
            if delivery_result.get("page_id"):
                line = (
                    f"{run_name[:1].upper()}{run_name[1:]} is done and saved to "
                    f"{database_name} in their Notion"
                    + (" with some gaps noted" if state == "partial" else "")
                    + "."
                )
            elif state == "failed":
                line = (
                    f"{run_name[:1].upper()}{run_name[1:]} could not be completed. "
                    "The details are in the app."
                )
                # The sentence whose absence produced the 2026-09-09 denial:
                # one turn after this receipt, the model claimed no research
                # had failed, holding only stale "started" envelopes.
                binding = (
                    " Only this one failed: do not claim any other research "
                    "run failed, and do not say this one is still running."
                )
            elif not delivery_result:
                # No delivery receipt AND no failure entry: the run went
                # terminal on a path that never reached the deliver stage
                # (e.g. a fail-derived partial). Saying "saving failed" here
                # would blame Notion for an attempt that never happened.
                line = (
                    f"{run_name[:1].upper()}{run_name[1:]} finished with partial "
                    "results - the brief is in the app."
                )
            else:
                line = (
                    f"{run_name[:1].upper()}{run_name[1:]} finished, but saving it "
                    "to Notion failed - the brief is in the app."
                )
            await self._speak(
                f"Tell the user briefly, in your own words: {line}{binding}",
                urgent=True,
            )
            return

        # Routine state advances (planning, searching, reading, ...) are
        # deliberately not narrated. Checkpoint policy: the clarification and
        # terminal branches above, plus the stall note in _poll_once, are the
        # only interruptions a healthy run earns.

    async def announce(self, line: str) -> None:
        """Speak a receipt for an action whose own turn was cancelled.

        A user barging in mid-dispatch cancels the reply generation, but the
        write is shielded and still lands, so the run really did start and the
        turn that would have said so is gone. Without this the user is never
        told about work they authorized and are paying for. Routed through the
        same boundary-waiting path as progress narration, so it can never talk
        over them.
        """
        await self._speak(
            f"Tell the user briefly, in your own words: {line}", urgent=True
        )

    async def _speak(self, instructions: str, *, urgent: bool) -> bool:
        """True only when the narration was actually generated, so callers with
        one-shot notes (the stall note) can retry a suppressed line later."""
        if self._closed:
            return False
        if not urgent and (time.monotonic() - self._last_spoken_at) < _MIN_NARRATION_GAP_S:
            return False
        if not buddy_owns_conversation(self._session):
            return False
        # A proactive nudge must never talk over the user; wait out both sides.
        await await_turn_boundary(self._session, require_user_idle=True)
        if self._closed or not buddy_owns_conversation(self._session):
            return False
        try:
            speech = self._session.generate_reply(instructions=instructions)
            await speech
            self._last_spoken_at = time.monotonic()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "research_narrator: narration failed",
                {"session_id": self._session_id, "error": str(exc)},
            )
            return False
