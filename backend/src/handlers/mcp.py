"""
MCP server exposing ToolExecutor tools over HTTP for the LiveKit voice worker.

Mounted at POST /mcp on the FastAPI app. Auth is identical to /chat: a Firebase
ID token in the Authorization header is verified by admin_auth().verify_id_token,
and the resulting uid is used to build a per-request ToolExecutor.

The voice worker (a separate LiveKit process) cannot present a user-issued ID token, 
so it mints an Admin-SDK custom token for the uid and exchanges it for a real ID token via Firebase identitytoolkit REST. 
That keeps this endpoint on a single auth path (verify_id_token) without introducing a parallel verifier.

Discovery handshake:

    curl -i -X POST http://localhost:8000/mcp \
        -H "Authorization: Bearer <firebase-id-token>" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
             "params":{"protocolVersion":"2025-03-26",
                       "capabilities":{},
                       "clientInfo":{"name":"curl","version":"0"}}}'
"""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..lib.logger import logger
from ..services.request_auth import decode_firebase_claims
from ..services.tool_executor import ToolExecutor

# Each MCP request runs the AuthMiddleware first, which sets this ContextVar
# to the verified Firebase uid for the duration of the request. The MCP tool
# handlers below resolve it back when they construct a ToolExecutor,
# so the tools stay stateless and reusable across users.
_current_uid: ContextVar[str | None] = ContextVar("mcp_request_uid", default=None)


def _executor_for_request() -> ToolExecutor:
    uid = _current_uid.get()
    if not uid:
        raise PermissionError("MCP: tool invoked with no authenticated user")
    
    return ToolExecutor(uid, created_via="voice")


# streamable_http_path="/" so when this app is mounted at /mcp on the parent
# FastAPI the wire URL is exactly /mcp (the MCP TS spec defaults).
#
# DNS rebinding protection is disabled because Cloud Run routes requests with
# Host: <run-url>, which FastMCP's default allowlist (127.0.0.1/localhost) rejects with 421.
# The Firebase _FirebaseAuthMiddleware below already authenticates every request with a verified ID token,
# making DNS rebinding protection redundant here.
#
# stateless_http=True is REQUIRED for Cloud Run. The default stateful mode keeps the
# MCP session (Mcp-Session-Id) in memory on the single instance that handled `initialize`.
# Cloud Run runs up to --max-instances and load-balances every request independently, so a
# follow-up tool call routed to a different instance finds no session and returns 404, which
# the client surfaces as "Session terminated". Stateless mode makes each request fully
# self-contained, so it no longer matters which instance serves it.
# json_response=True returns a single JSON body per tool call instead of holding an SSE
# stream open, which suits one-shot voice tool calls and avoids load-balancer stream cutoffs.
mcp_server = FastMCP(
    "juno-voice-tools",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    stateless_http=True,
    json_response=True,
)

# Hard cap for every tool call from the voice worker.
# /mcp is voice-only; 8s fits a calendar API sync while keeping the call feeling alive.
_VOICE_TOOL_TIMEOUT_S = 8.0


async def _run_tool(tool_name: str, args: dict) -> dict:
    """Execute a tool with a voice-appropriate timeout. Returns a user_message error dict on failure."""
    try:
        return await asyncio.wait_for(
            _executor_for_request().execute(tool_name, args),
            timeout=_VOICE_TOOL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warn("MCP: tool timed out", {"tool": tool_name})
        _TIMEOUT_MESSAGES = {
            "get_upcoming_events": "Your calendar is taking too long to respond. Try again in a moment.",
            "create_calendar_event": "Couldn't reach your calendar in time. Try again.",
            "get_user_context": "That's taking too long. Try again in a moment.",
            "web_surf": "Couldn't reach the web in time. Try again in a sec.",
            "track_topic": "Uh ohh!, couldn't set that up in time. Try again in a sec.",
        }
        return {
            "error": True,
            "user_message": _TIMEOUT_MESSAGES.get(tool_name, "That took too long. Try again in a moment."),
        }


# Reminders ---------------------------------------------------------------

@mcp_server.tool()
async def set_reminder(message: str, scheduled_at: str, priority: str = "normal") -> dict[str, Any]:
    """Set a reminder. scheduled_at must be ISO 8601 with timezone offset (e.g. '2026-06-02T09:00:00+05:30')."""
    return await _run_tool("set_reminder", {"message": message, "scheduled_at": scheduled_at, "priority": priority})


@mcp_server.tool()
async def list_reminders(status_filter: str = "pending") -> dict[str, Any]:
    """List the user's reminders. status_filter: 'pending', 'all', 'fired', 'dismissed'."""
    return await _run_tool("list_reminders", {"status_filter": status_filter})


@mcp_server.tool()
async def cancel_reminder(reminder_id: str) -> dict[str, Any]:
    """Cancel (dismiss) a reminder by its ID."""
    return await _run_tool("cancel_reminder", {"reminder_id": reminder_id})


# Calendar ----------------------------------------------------------------

@mcp_server.tool()
async def create_calendar_event(
    title: str,
    start_time: str,
    end_time: str = "",
    description: str = "",
    location: str = "",
) -> dict[str, Any]:
    """Create a Google Calendar event. start_time and end_time are ISO 8601 strings."""
    return await _run_tool("create_calendar_event", {
        "title": title,
        "start_time": start_time,
        "end_time": end_time or None,
        "description": description or None,
        "location": location or None,
    })


@mcp_server.tool()
async def get_upcoming_events(
    range_name: str = "recent",
    hours_ahead: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Fetch the user's calendar events from Firestore.

    range_name:
      - 'recent' (default) — most recent events by start time, past or future.
        Use when the user asks open-ended things like 'what's on my calendar',
        'any meetings', 'what do I have coming up'.
      - 'today', 'tomorrow', 'this_week' — only when the user gives an explicit
        timeframe in those words.
    """
    return await _run_tool("get_upcoming_events", {"range_name": range_name, "hours_ahead": hours_ahead, "limit": limit})


# Memory ------------------------------------------------------------------

@mcp_server.tool()
async def store_memory(key: str, value: str, category: str) -> dict[str, Any]:
    """Store a memory about the user. category: 'personal', 'preference', 'fact', etc."""
    return await _run_tool("store_memory", {"key": key, "value": value, "category": category})


@mcp_server.tool()
async def query_memory(query: str, category_filter: str = "all") -> dict[str, Any]:
    """Search the user's memories. category_filter: 'all' or a specific category."""
    return await _run_tool("query_memory", {"query": query, "category_filter": category_filter})


# Web surf ----------------------------------------------------------------

@mcp_server.tool()
async def web_surf(query: str, recency: str = "any") -> dict[str, Any]:
    """Search the live web for current information, news, prices, scores, or any time-sensitive fact.

    Use this when the user asks about:
      - news, events, or anything that happened recently
      - live data (sports scores, stock prices, weather, status pages)
      - facts that may have changed since training (releases, rosters, regulations)
      - things you are not certain about and need to verify

    Do NOT use this for things you already know (general knowledge, math, stable facts,
    the user's own data — that's in other tools).

    query: a natural-language search query. Be specific.
    recency: 'fresh' for time-sensitive queries (news/scores/prices). 'any' (default)
             for stable lookups.
    """
    return await _run_tool("web_surf", {"query": query, "recency": recency})


# User context ------------------------------------------------------------

@mcp_server.tool()
async def get_user_context(
    include_memories: bool = True,
    include_reminders: bool = True,
    include_events: bool = True,
) -> dict[str, Any]:
    """Get a snapshot of the user's memories, reminders, and upcoming calendar events."""
    return await _run_tool("get_user_context", {
        "include_memories": include_memories,
        "include_reminders": include_reminders,
        "include_events": include_events,
    })


# Feedback ----------------------------------------------------------------

@mcp_server.tool()
async def report_feedback(
    category: str,
    about: str,
    summary: str,
    verbatim_quote: str,
    severity: str = "medium",
) -> dict[str, Any]:
    """Silently record product feedback about the Aura app itself: a complaint, a feature/behaviour
    request, confusion about how Aura works, praise, or a hint the user may stop using it. Call it in
    the same turn as your spoken reply, NEVER announce it or tell the user you logged anything. Do
    NOT call it for ordinary requests, questions, or chit-chat that isn't about the app.

    category: complaint | feature_request | confusion | bug | praise | churn_risk | other
    about:    notifications | voice | chat | reminders | memory | calendar | email | general
    severity: low | medium | high
    """
    return await _run_tool("report_feedback", {
        "category": category,
        "about": about,
        "summary": summary,
        "verbatim_quote": verbatim_quote,
        "severity": severity,
    })


# Topic tracking ----------------------------------------------------------

@mcp_server.tool()
async def track_topic(request: str) -> dict[str, Any]:
    """Subscribe the user to ONGOING live updates about an event, topic, or developing
    situation they want to stay posted on over time — a sports tournament or league, a
    team's season, an election, a product launch, a court case, a person or company in
    the news. Setup is instant; Buddy researches the topic and schedules the updates
    (before / during / after key moments) in the background, sends only genuinely-new
    updates, and stops on its own when it concludes.

    Use whenever the user asks to be KEPT POSTED or NOTIFIED about how something unfolds
    ('keep me posted on…', 'let me know how X goes', 'follow Y for me until it's done').
    Do NOT use for a one-off reminder at a fixed time (use set_reminder) or a single
    current lookup (use web_surf).

    request: what to keep them posted on, in their own words, including any specifics they
    gave (which team, league, region, etc.), e.g. "USA's matches at the 2026 World Cup".
    Confirm warmly in your own words from what they said; do not wait on or read back any
    title the tool returns.
    """
    return await _run_tool("track_topic", {"request": request})


# Auth middleware ---------------------------------------------------------

class _FirebaseAuthMiddleware(BaseHTTPMiddleware):
    """Verifies the Authorization Bearer ID token against Firebase Admin and
    binds the uid to a ContextVar for the duration of the request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        claims = decode_firebase_claims(request.headers)
        if not claims:
            logger.warn("MCP: unauthorized request", {"path": request.url.path})
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        uid = claims.get("uid") or claims.get("sub")
        if not isinstance(uid, str) or not uid:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        token = _current_uid.set(uid)
        try:
            return await call_next(request)
        finally:
            _current_uid.reset(token)


# Mount + lifespan glue ---------------------------------------------------

_mcp_asgi_app = None
_mcp_lifespan_stack: contextlib.AsyncExitStack | None = None


def _build_mcp_asgi_app():
    global _mcp_asgi_app
    if _mcp_asgi_app is None:
        inner = mcp_server.streamable_http_app()
        inner.add_middleware(_FirebaseAuthMiddleware)
        _mcp_asgi_app = inner
    return _mcp_asgi_app


def register_mcp(app: FastAPI) -> None:
    """Mount the MCP app at /mcp and drive its session-manager lifespan from
    the parent FastAPI startup/shutdown events.

    Starlette does not propagate lifespan into mounted sub-apps, so we run
    the FastMCP session manager ourselves via an AsyncExitStack stored on
    the module.

    NOTE: using @app.on_event (deprecated) here is deliberate, not an oversight.
    Switching to a FastAPI `lifespan` is all-or-nothing: it disables EVERY
    on_event app-wide, including main.on_startup (env checks). It
    is also behavior-identical for our case (the session manager already runs for
    the whole container lifetime, which is what keeps voice tool calls off the
    2026-05-29 Cloud Run 404/hang path, together with stateless_http=True). And the
    parent-app boot of this session manager is NOT covered by tests
    (test_mcp_stateless.py runs the manager directly, bypassing this wiring). So
    migrate only when bumping FastAPI/Starlette, bundled with a lifespan-boot test
    that hits /mcp and a dark-deploy voice smoke test. See lessons-learnt 2026-05-29.
    """
    app.mount("/mcp", _build_mcp_asgi_app())

    @app.on_event("startup")  # pyright: ignore[reportDeprecated]
    async def _start_mcp_session_manager() -> None:  # pyright: ignore[reportUnusedFunction]
        global _mcp_lifespan_stack
        _mcp_lifespan_stack = contextlib.AsyncExitStack()
        await _mcp_lifespan_stack.enter_async_context(mcp_server.session_manager.run())
        logger.info("MCP: streamable_http session manager started at /mcp")

    @app.on_event("shutdown")  # pyright: ignore[reportDeprecated]
    async def _stop_mcp_session_manager() -> None:  # pyright: ignore[reportUnusedFunction]
        global _mcp_lifespan_stack
        if _mcp_lifespan_stack is not None:
            await _mcp_lifespan_stack.aclose()
            _mcp_lifespan_stack = None
            logger.info("MCP: streamable_http session manager stopped")
