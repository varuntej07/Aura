"""
Juno backend — FastAPI application.

Routes:
  GET  /health -> liveness probe
  GET  /voice/token -> LiveKit room token for Flutter client
  POST /chat -> text conversation (Claude)
  POST /notification-reply -> notification reply -> chat
  POST /scheduler/tick -> deliver due reminders (call from cron)

Local dev:
  uvicorn src.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token
from livekit.api import AccessToken, VideoGrants
from starlette.middleware.base import BaseHTTPMiddleware

from .config.settings import settings
from .agents.orchestrator import orchestrate_all_agents, run_agent_for_user
from .handlers.account import handle_delete_account
from .handlers.chat import handle_chat_stream
from .handlers.connectors import (
    connect_gmail,
    connect_google_calendar,
    disconnect_gmail,
    disconnect_google_calendar,
    get_connectors,
    google_calendar_webhook,
    sync_google_calendar,
)
from .handlers.daily_notification import handle_send_nudge
from .handlers.devices import register_device
from .handlers.engagement import (
    handle_engagement_notify,
    handle_engagement_orchestrate,
    handle_engagement_responded,
)
from .handlers.mcp import register_mcp
from .handlers.notification_reply import handle_notification_reply_request
from .handlers.buddy_pills import handle_refresh_buddy_pills
from .handlers.onboarding_profile import handle_onboarding_profile
from .handlers.scheduler import handle_scheduler_tick
from .handlers.signal_content_ingest import handle_signal_content_ingest
from .handlers.signal_events import handle_signal_events
from .handlers.signal_feed import handle_signal_feed
from .handlers.signal_tick import handle_signal_tick
from .handlers.threads import handle_thread_messages, handle_thread_reply
from .lib.logger import logger
from .services.request_auth import decode_firebase_claims

app = FastAPI(title="Juno Backend", version="1.0.0")

# MCP server (POST /mcp) exposes ToolExecutor tools to the LiveKit voice worker over MCP streamable HTTP. 
# Lifespan starts inside register_mcp via the parent app's startup/shutdown events.
register_mcp(app)


# Request / Response logging middleware
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        start = time.monotonic()

        # Skip noisy health checks
        if request.url.path != "/health":
            logger.info("→ HTTP request", {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            })

        try:
            response = await call_next(request)
            if request.url.path != "/health":
                duration_ms = int((time.monotonic() - start) * 1000)
                level_fn = logger.error if response.status_code >= 500 else (
                    logger.warn if response.status_code >= 400 else logger.info
                )
                level_fn("← HTTP response", {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                })
            return response
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.exception("← HTTP unhandled exception", {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
                "error": str(exc),
            })
            raise


app.add_middleware(RequestLoggingMiddleware)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/voice/token")
async def voice_token(request: Request) -> JSONResponse:
    """Return a LiveKit room token for the authenticated user."""
    claims = decode_firebase_claims(request.headers)
    if not claims:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id: str = claims.get("uid") or claims.get("sub") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    room_name = f"voice-{user_id}"
    token = (
        AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(user_id)
        .with_name(user_id)
        .with_grants(VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    return JSONResponse({"token": token, "url": settings.LIVEKIT_URL, "room": room_name})


# REST endpoints
def _to_handler_event(request: Request, body: bytes) -> dict:
    """Convert FastAPI request data into the legacy handler event shape."""
    claims = decode_firebase_claims(request.headers) or {}
    return {
        "body": body.decode("utf-8"),
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": claims
                }
            }
        },
        "headers": dict(request.headers),
    }


def _handler_response(result: dict) -> JSONResponse:
    """
    Legacy handlers return {"statusCode": int, "body": str}.
    result["body"] is already a JSON string — parse it back to a dict so
    JSONResponse doesn't double-encode it into a JSON-wrapped string.
    """
    body_str = result.get("body", "{}")
    try:
        body_dict = json.loads(body_str) if isinstance(body_str, str) else body_str
    except (json.JSONDecodeError, TypeError):
        body_dict = {"raw": body_str}
    return JSONResponse(content=body_dict, status_code=result.get("statusCode", 500))


@app.delete("/account")
async def account_delete_endpoint(request: Request) -> JSONResponse:
    return await handle_delete_account(request)


@app.post("/devices/register")
async def devices_register_endpoint(request: Request) -> JSONResponse:
    return await register_device(request)


@app.post("/chat")
async def chat_endpoint(request: Request) -> StreamingResponse:
    body = await request.body()
    event = _to_handler_event(request, body)
    return await handle_chat_stream(event)


@app.post("/notification-reply")
async def notification_reply_endpoint(request: Request) -> JSONResponse:
    body = await request.body()
    event = _to_handler_event(request, body)
    result = await handle_notification_reply_request(event)
    return _handler_response(result)


@app.post("/threads/reply")
async def threads_reply_endpoint(request: Request) -> JSONResponse:
    return await handle_thread_reply(request)


@app.get("/threads/{thread_id}/messages")
async def threads_messages_endpoint(thread_id: str, request: Request) -> JSONResponse:
    return await handle_thread_messages(request, thread_id)


_google_auth_transport = GoogleRequest()


def _verify_scheduler_token(request: Request) -> None:
    """Allow only Cloud Scheduler / Cloud Tasks calls signed by the juno-scheduler
    service account. The token's audience is checked against every hostname that
    routes to this service (settings.scheduler_oidc_audience_list) so a Cloud Run
    URL-format change can't 401 the scheduler — see the 2026-06-04 audience-drift
    outage. verify_oauth2_token accepts a list and matches membership."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.warn("scheduler_auth: missing bearer token", {"path": request.url.path})
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth_header.removeprefix("Bearer ")
    try:
        claims = verify_oauth2_token(
            token, _google_auth_transport, audience=settings.scheduler_oidc_audience_list
        )
    except Exception as exc:
        # ERROR, not WARN: a rejected token means an internal endpoint (reminders,
        # notifications, ingest) is silently 401ing — an outage. The accepted list
        # is logged inline so the next audience drift is one grep away from the fix.
        logger.error("scheduler_auth: OIDC token REJECTED — internal endpoint is 401ing (scheduler/tasks outage)", {
            "path": request.url.path,
            "error": str(exc),
            "accepted_audiences": settings.scheduler_oidc_audience_list,
        })
        raise HTTPException(status_code=401, detail="Invalid OIDC token")
    caller_email = claims.get("email", "")
    if caller_email != settings.SCHEDULER_SA_EMAIL:
        logger.warn("scheduler_auth: forbidden service account", {
            "path": request.url.path,
            "caller_email": caller_email,
            "expected_email": settings.SCHEDULER_SA_EMAIL,
        })
        raise HTTPException(status_code=403, detail="Forbidden service account")


@app.post("/scheduler/tick")
async def scheduler_tick_endpoint(
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    result = await handle_scheduler_tick()
    return _handler_response(result)


# Domain agents — fan-out tick + per-user run (called by Cloud Tasks)
@app.post("/internal/agents/tick")
async def agents_tick_endpoint(
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    result = await orchestrate_all_agents()
    return JSONResponse(content=result)


@app.post("/internal/agents/{agent_id}/run/{user_id}")
async def agents_run_endpoint(
    agent_id: str,
    user_id: str,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    await run_agent_for_user(agent_id, user_id)
    return JSONResponse(content={"ok": True})


# Engagement endpoints (internal — Cloud Tasks only)
@app.post("/internal/engage/orchestrate")
async def engage_orchestrate_endpoint(
    request: Request,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    body = await request.json()
    result = await handle_engagement_orchestrate(body)
    return JSONResponse(content=result)


@app.post("/internal/engage/notify")
async def engage_notify_endpoint(
    request: Request,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    body = await request.json()
    result = await handle_engagement_notify(body)
    return JSONResponse(content=result)


# Daily notification — meeting reminder delivery only (discovery handled by signal engine)
@app.post("/internal/daily-notify/send")
async def daily_notify_send_endpoint(
    request: Request,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    body = await request.json()
    result = await handle_send_nudge(body)
    status_code = result.pop("status_code", 200)
    return JSONResponse(content=result, status_code=status_code)


# Onboarding: seed declared interests into UserAura (consent-gated).
@app.post("/onboarding/profile")
async def onboarding_profile_endpoint(request: Request) -> JSONResponse:
    return await handle_onboarding_profile(request)


# On Main Buddy text chat: regenerates personalized suggestion pills after a session
# (fired by the client on app background when the user did something this session).
@app.post("/chat/buddy-pills/refresh")
async def refresh_buddy_pills_endpoint(request: Request) -> JSONResponse:
    return await handle_refresh_buddy_pills(request)


# Signal engine — user events, ranked feed, scoring tick, content ingest.
@app.post("/events")
async def signal_events_endpoint(request: Request) -> JSONResponse:
    return await handle_signal_events(request)


@app.get("/feed/recommend")
async def signal_feed_endpoint(request: Request) -> JSONResponse:
    return await handle_signal_feed(request)


@app.post("/internal/signal-engine/tick")
async def signal_engine_tick_endpoint(
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    result = await handle_signal_tick()
    return JSONResponse(content=result)


@app.post("/internal/signal-engine/content-ingest")
async def signal_engine_content_ingest_endpoint(
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    result = await handle_signal_content_ingest()
    return JSONResponse(content=result)


@app.post("/internal/engage/responded")
async def engage_responded_endpoint(request: Request) -> JSONResponse:
    claims = decode_firebase_claims(request.headers)
    if not claims:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id: str = claims.get("uid") or claims.get("sub") or ""
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    engagement_id: str = body.get("engagement_id", "")
    result = await handle_engagement_responded(user_id, engagement_id)
    status = 404 if result.get("error") == "not_found" else 200
    return JSONResponse(content=result, status_code=status)


@app.get("/connectors")
async def connectors_endpoint(request: Request) -> JSONResponse:
    return await get_connectors(request)


@app.post("/connectors/google-calendar/connect")
async def connectors_google_calendar_connect_endpoint(request: Request) -> JSONResponse:
    return await connect_google_calendar(request)


@app.post("/connectors/google-calendar/disconnect")
async def connectors_google_calendar_disconnect_endpoint(request: Request) -> JSONResponse:
    return await disconnect_google_calendar(request)


@app.post("/connectors/google-calendar/sync")
async def connectors_google_calendar_sync_endpoint(request: Request) -> JSONResponse:
    return await sync_google_calendar(request)


@app.post("/connectors/gmail/connect")
async def connectors_gmail_connect_endpoint(request: Request) -> JSONResponse:
    return await connect_gmail(request)


@app.post("/connectors/gmail/disconnect")
async def connectors_gmail_disconnect_endpoint(request: Request) -> JSONResponse:
    return await disconnect_gmail(request)


@app.post("/integrations/google-calendar/webhook", name="google_calendar_webhook")
async def google_calendar_webhook_endpoint(request: Request) -> JSONResponse:
    return await google_calendar_webhook(request)


def _check_env() -> None:
    """Log the status of every critical env var so you can spot missing config instantly."""
    checks = {
        "ANTHROPIC_API_KEY": bool(settings.ANTHROPIC_API_KEY),
        "ANTHROPIC_CHAT_MODEL": settings.ANTHROPIC_CHAT_MODEL,
        "LIVEKIT_URL": bool(settings.LIVEKIT_URL),
        "LIVEKIT_API_KEY": bool(settings.LIVEKIT_API_KEY),
        "LIVEKIT_CONFIGURED": settings.livekit_configured,
        "DEEPGRAM_API_KEY": bool(settings.DEEPGRAM_API_KEY),
        "CARTESIA_API_KEY": bool(settings.CARTESIA_API_KEY),
        "GOOGLE_CALENDAR": settings.google_calendar_configured,
        "GOOGLE_CALENDAR_WEBHOOK_URL": bool(settings.GOOGLE_CALENDAR_WEBHOOK_URL),
        "GMAIL": settings.gmail_configured,
        "GEMINI_API_KEY": settings.gemini_configured,
        "GEMINI_MODEL": settings.GEMINI_MODEL,
        "ENV": settings.ENV,
    }

    logger.info("Juno backend starting", checks)

    if not settings.ANTHROPIC_API_KEY:
        logger.warn("ANTHROPIC_API_KEY is not set — /chat will fail")
    if not settings.livekit_configured:
        logger.warn("LiveKit not fully configured, voice sessions will fail...")


# on_event is deprecated but intentional here: it is part of the same "all or nothing"
# group as the MCP session-manager handlers in handlers/mcp.py Do not migrate this one without the others. 
# See the NOTE in mcp.register_mcp and lessons-learnt 2026-05-29.
@app.on_event("startup")  # pyright: ignore[reportDeprecated]
async def on_startup() -> None:
    _check_env()
    # Initialize Langfuse — reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from env
    try:
        from langfuse import Langfuse
        Langfuse()
        logger.info("Langfuse initialized")
    except Exception as exc:
        logger.warn("Langfuse initialization failed — tracing disabled", {"error": str(exc)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.VOICE_GATEWAY_HOST,
        port=settings.VOICE_GATEWAY_PORT,
        reload=settings.ENV == "development",
        log_level="info",
    )
