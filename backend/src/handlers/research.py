"""Authenticated research API plus the internal Cloud Tasks step endpoint.

Deliberately narrow. Everything a user could reach (create, list, answer, cancel, delete,
read) is a later phase, and adding it here would mean shipping a user-facing surface
before the desktop client that can render it exists. What this file provides is the
target a Cloud Task hits, and nothing else.

The status-code discipline is the important part, because Cloud Tasks reads it as an
instruction:

* **200 on ``already_leased``.** Another worker owns the stage. Returning an error would
  make Cloud Tasks hammer a stage that is actively running elsewhere; the lease TTL is
  the recovery path, not the retry.
* **200 on a terminal or cancelled run.** Late tasks are expected and harmless. Terminal
  research states are absorbing, so the stage does no provider work.
* **500 only when a retry could genuinely help**, because the queue's maxAttempts is 3
  and each attempt can spend real provider budget.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.request_auth import resolve_user_id_from_request
from ..services.research import fields as F
from ..services.research.engine import StepRef, get_research_engine


def _projection(detail: dict[str, Any]) -> dict[str, Any]:
    run = dict(detail.get("run") or {})
    plan = dict(detail.get("plan") or {})
    claims = []
    for raw in list(detail.get("claims") or []):
        claim = dict(raw or {})
        claims.append({
            "claim_id": str(claim.get("claim_id") or claim.get("doc_id") or ""),
            "text": str(claim.get("text") or ""),
            "confidence": str(claim.get("confidence") or ""),
            "evidence": [
                {
                    "url": str(item.get("url") or ""),
                    "excerpt": str(item.get("excerpt") or ""),
                    "source_class": str(item.get("source_class") or ""),
                }
                for item in list(claim.get("evidence") or [])
                if isinstance(item, dict)
            ],
        })
    return {
        "run_id": str(run.get(F.RUN_ID) or ""),
        "request": str(run.get(F.REQUEST_TEXT) or ""),
        "preset": str(run.get(F.PRESET) or "quick"),
        "state": str(run.get(F.STATE) or ""),
        "processing_stage": str(run.get(F.PROCESSING_STAGE) or ""),
        "state_revision": int(run.get(F.STATE_REVISION, 0)),
        "failure_code": str(run.get(F.FAILURE_CODE) or ""),
        "pending_question": dict(run.get(F.PENDING_QUESTION) or {}),
        "current_plan_version": int(run.get(F.CURRENT_PLAN_VERSION, 0)),
        "admitted_plan_version": int(run.get(F.ADMITTED_PLAN_VERSION, 0)),
        "auto_admit_requested": bool(run.get(F.AUTO_ADMIT_REQUESTED)),
        "brief": dict(run.get(F.BRIEF) or {}),
        "gaps": list(run.get(F.GAPS) or []),
        "source_count": int(run.get(F.SOURCE_COUNT, 0)),
        "claim_count": int(run.get(F.CLAIM_COUNT, 0)),
        "created_at": str(run.get(F.CREATED_AT) or ""),
        "updated_at": str(run.get(F.UPDATED_AT) or ""),
        "plan": {
            "objective": str(plan.get("objective") or ""),
            "assumptions": list(plan.get("assumptions") or []),
            "sub_questions": list(plan.get("sub_questions") or []),
        },
        "claims": claims,
    }


def _activity_projection(detail: dict[str, Any]) -> dict[str, Any]:
    run = dict(detail.get("run") or {})
    sources = []
    for raw in list(detail.get("sources") or [])[:50]:
        source = dict(raw or {})
        sources.append({
            "source_id": str(source.get("source_id") or source.get("doc_id") or ""),
            "query": str(source.get("discovered_by_query") or "")[:500],
            "title": str(source.get("title") or "")[:300],
            "domain": str(source.get("domain") or "")[:253],
            "url": str(source.get("url") or "")[:2_048],
            "final_url": str(source.get("final_url") or "")[:2_048],
            "state": str(source.get("state") or "pending")[:32],
            "read_state": str(source.get("read_state") or "")[:32],
            "source_class": str(source.get("source_class") or "unknown")[:32],
            "candidate_count": max(0, int(source.get("candidate_count") or 0)),
            "gap_reason": str(source.get("gap_reason") or "")[:80],
            "injection_suspected": bool(source.get("injection_suspected")),
        })
    return {
        "run_id": str(run.get(F.RUN_ID) or ""),
        "state": str(run.get(F.STATE) or ""),
        "processing_stage": str(run.get(F.PROCESSING_STAGE) or ""),
        "state_revision": int(run.get(F.STATE_REVISION, 0)),
        "source_count": int(run.get(F.SOURCE_COUNT, 0)),
        "claim_count": int(run.get(F.CLAIM_COUNT, 0)),
        "updated_at": str(run.get(F.UPDATED_AT) or ""),
        "sources": sources,
    }


async def handle_create(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    text = str(body.get("request") or "").strip()
    client_run_id = str(body.get("client_run_id") or "").strip()
    preset = str(body.get("depth") or "quick").strip().lower()
    if not text or len(text) > 2_000 or not client_run_id or len(client_run_id) > 128:
        return JSONResponse({"error": "Invalid research request."}, status_code=400)
    if preset != "quick":
        return JSONResponse(
            {"detail": {"code": F.RESEARCH_DEPTH_CODE}}, status_code=400
        )
    handle = await get_research_engine().start(
        uid,
        {
            "request": text,
            "preset": preset,
            "origin_surface": str(body.get("origin_surface") or "dashboard"),
        },
        client_run_id=client_run_id,
    )
    detail = await get_research_engine().detail(uid, handle.run_id)
    return JSONResponse(
        _projection(detail or {"run": {F.RUN_ID: handle.run_id, F.STATE: handle.state}}),
        status_code=200 if handle.replayed else 202,
    )


async def handle_list(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        limit = max(1, min(F.LIST_LIMIT, int(request.query_params.get("limit", F.LIST_LIMIT))))
    except ValueError:
        return JSONResponse({"error": "Invalid limit."}, status_code=400)
    runs = await get_research_engine().list_runs(uid, limit=limit)
    items = []
    for run in runs:
        detail = await get_research_engine().detail(uid, str(run.get(F.RUN_ID) or ""))
        if detail:
            items.append(_projection(detail))
    return JSONResponse({"items": items})


async def handle_get(request: Request, run_id: str) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = await get_research_engine().detail(uid, run_id)
    if detail is None:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return JSONResponse(_projection(detail))


async def handle_activity(request: Request, run_id: str) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = await get_research_engine().activity(uid, run_id)
    if detail is None:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return JSONResponse(_activity_projection(detail))


async def handle_signal(request: Request, run_id: str, kind: str) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    signal: dict[str, Any] = {
        "kind": kind,
        "correlation_id": str(body.get("correlation_id") or ""),
    }
    if kind == "answer":
        signal.update({
            "question_id": str(body.get("question_id") or ""),
            "answer": dict(body.get("answer") or {}),
        })
    elif kind == "admit":
        signal.update({"plan_version": int(body.get("plan_version") or 0), "preset": "quick"})
    try:
        status = await get_research_engine().signal(uid, run_id, signal)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if kind == "delete":
        return JSONResponse({"ok": True})
    detail = await get_research_engine().detail(uid, status.run_id)
    if detail is None:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return JSONResponse(_projection(detail))


async def handle_research_step(request: Request) -> JSONResponse:
    """Run exactly one bounded research stage. Cloud Tasks only."""
    try:
        payload: Any = await request.json()
    except Exception:
        # A malformed body will never become well-formed on retry.
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=200)

    if not isinstance(payload, dict):
        # `[1,2]`, `"x"` and `5` are all VALID json, so the parse above succeeds and the
        # field reads below would raise AttributeError into a 500 - telling Cloud Tasks
        # to retry a body that can never become well-formed. Same answer as a parse
        # failure, for the same reason.
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=200)

    uid = str(payload.get("user_id") or "")
    run_id = str(payload.get("run_id") or "")
    stage_id = str(payload.get("stage_id") or "")
    if not (uid and run_id and stage_id):
        # Also the shape the Phase 3 OIDC probe sends ({"probe": true}). Answering 200
        # keeps a probe from being retried three times against a real queue.
        return JSONResponse(
            {"ok": False, "error": "missing_fields"}, status_code=200
        )

    engine = get_research_engine()
    try:
        outcome = await engine.advance(
            uid, StepRef(uid=uid, run_id=run_id, stage_id=stage_id)
        )
    except Exception as exc:
        # An unexpected fault IS worth a retry: the lease will have expired or been
        # released, and the engine's own attempt cap bounds how often this can repeat.
        logger.error(
            "research.step: stage raised",
            {
                "run_id": run_id,
                "stage_id": stage_id,
                "error": str(exc),
                "error_code": "research_step_failed",
            },
        )
        return JSONResponse(
            {"ok": False, "error": "stage_failed"}, status_code=500
        )

    body = {
        "ok": True,
        "disposition": outcome.disposition,
        "state": outcome.state,
        "stage_kind": outcome.stage_kind,
    }
    # The engine decides whether a retry can help; the handler only translates it.
    status = 500 if outcome.retryable else 200
    return JSONResponse(body, status_code=status)
