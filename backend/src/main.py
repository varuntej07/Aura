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

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token
from livekit.api import AccessToken, VideoGrants
from starlette.middleware.base import BaseHTTPMiddleware

from .config.settings import settings
from .handlers.account import handle_delete_account
from .handlers.billing import (
    handle_billing_checkout,
    handle_billing_portal,
    handle_billing_webhook,
)
from .handlers.calendar import get_upcoming_calendar
from .handlers.entitlement import handle_get_entitlement
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
from .handlers.briefing import (
    handle_get_today_briefing,
    handle_post_generate_briefing,
    handle_post_world_briefing,
)
from .handlers.buddy_pills import handle_refresh_buddy_pills
from .handlers.keyboard import handle_keyboard_draft, handle_keyboard_vocab
from .handlers.aura import (
    handle_consolidate_session,
    handle_delete_memory,
    handle_get_memory,
)
from .handlers.history import (
    handle_delete_session,
    handle_get_session_detail,
    handle_list_sessions,
)
from .handlers.screen_saves import (
    handle_delete_screen_save,
    handle_list_screen_saves,
)
from .handlers.drafts import (
    handle_delete_draft,
    handle_list_drafts,
)
from .handlers.memories import (
    handle_callback_card,
    handle_delete_memory,
    handle_list_memories,
    handle_patch_memory,
)
from .handlers.onboarding_profile import handle_onboarding_profile
from .handlers.draft_outbound import handle_draft_outbound_refine
from .handlers.dashboard_link import (
    handle_dashboard_link_claim,
    handle_dashboard_link_start,
)
from .handlers.pairing import (
    handle_pair_claim,
    handle_pair_start,
    handle_unlink_device,
)
from .handlers.web_auth import handle_web_auth_start, handle_web_auth_status
from .handlers.scheduler import handle_scheduler_tick
from .handlers.signal_content_ingest import handle_signal_content_ingest
from .handlers.signal_events import handle_signal_events
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
                # Per-platform latency feed for the ops dashboard. Clients send
                # X-Aura-Platform (android/ios/windows); requests without it
                # (cron, internal, old app builds) are deliberately excluded so
                # the metric measures real client-observed backend latency.
                # A GCP log-based DISTRIBUTION metric extracts duration_ms
                # labeled by platform from these lines (ops/README.md has the
                # one-time gcloud command); Cloud Run's built-in
                # request_latencies metric cannot see custom headers.
                platform = request.headers.get("X-Aura-Platform", "")
                if platform:
                    logger.info("request_metric", {
                        "platform": platform[:24],
                        "app_version": request.headers.get("X-Aura-App-Version", "")[:24],
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

# CORS, added AFTER RequestLoggingMiddleware so Starlette makes it the
# OUTERMOST layer (Starlette wraps in reverse add order — the last middleware
# added runs first on the way in, last on the way out). It has to be
# outermost to intercept a browser's preflight OPTIONS before anything else
# sees it, and so every response (including errors from deeper middleware)
# still carries the right Access-Control-* headers. Explicit origin allowlist
# from settings, no wildcard. allow_credentials stays False on purpose: every
# authenticated route here reads a manually-set `Authorization: Bearer
# <Firebase ID token>` header, not a cookie, so there is nothing for CORS
# "credentials" mode to protect — enabling it would only add the
# wildcard-plus-credentials footgun for no benefit.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


# Launch surfaces the voice worker understands (voice_agent._KNOWN_SURFACES). Anything
# else collapses to "app", the neutral default, so a bad query param never changes behavior.
_VOICE_SURFACES = frozenset({"app", "keyboard", "desktop"})


@app.get("/voice/token")
async def voice_token(request: Request) -> JSONResponse:
    """Return a LiveKit room token for the authenticated user.

    `?surface=keyboard` marks a quick tap from the Buddy Keyboard; it is stamped into the
    token's participant metadata so the worker can keep that session short and task-focused.
    The in-app voice orb passes nothing and gets the default "app" experience.
    """
    claims = decode_firebase_claims(request.headers)
    if not claims:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id: str = claims.get("uid") or claims.get("sub") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    surface = request.query_params.get("surface", "app")
    if surface not in _VOICE_SURFACES:
        surface = "app"

    room_name = f"voice-{user_id}"
    token = (
        AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(user_id)
        .with_name(user_id)
        .with_metadata(json.dumps({"surface": surface}))
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


# Desktop pairing: the signed-in phone app requests a short-lived one-time code.
@app.post("/devices/pair/start")
async def devices_pair_start_endpoint(request: Request) -> JSONResponse:
    return await handle_pair_start(request)


# Desktop pairing: the desktop exchanges the code for a Firebase custom token.
# UNAUTHENTICATED by design (reviewed decision) — the one-time code IS the credential.
@app.post("/devices/pair/claim")
async def devices_pair_claim_endpoint(request: Request) -> JSONResponse:
    return await handle_pair_claim(request)


# Remove a linked desktop, then revoke ALL refresh tokens (honest full sign-out).
@app.post("/devices/unlink")
async def devices_unlink_endpoint(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    return await handle_unlink_device(request, background_tasks)


# Dashboard-link handshake: the signed-in desktop app requests a short-lived
# token to open a signed-in web dashboard without a second login.
@app.post("/devices/dashboard-link/start")
async def devices_dashboard_link_start_endpoint(request: Request) -> JSONResponse:
    return await handle_dashboard_link_start(request)


# Dashboard-link handshake: the web dashboard exchanges the token for a Firebase
# custom token. UNAUTHENTICATED by design (reviewed decision) -- the one-time
# token IS the credential.
@app.post("/devices/dashboard-link/claim")
async def devices_dashboard_link_claim_endpoint(request: Request) -> JSONResponse:
    return await handle_dashboard_link_claim(request)


# Web sign-up handshake: desktop opens a browser to /auth?session=<code>;
# Aura-Web completes Google sign-in server-side and writes the result here.
# UNAUTHENTICATED by design on BOTH endpoints (reviewed decision, mirrors
# pairing's "the code IS the credential" model) — there is no uid yet at
# issuance time, so auth can't be required the way pair/start requires it.
@app.post("/devices/web-auth/start")
async def devices_web_auth_start_endpoint(request: Request) -> JSONResponse:
    return await handle_web_auth_start(request)


@app.post("/devices/web-auth/status")
async def devices_web_auth_status_endpoint(request: Request) -> JSONResponse:
    return await handle_web_auth_status(request)


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


@app.post("/aura/consolidate-session")
async def aura_consolidate_session_endpoint(request: Request) -> JSONResponse:
    return await handle_consolidate_session(request)


@app.get("/aura/memory")
async def aura_get_memory_endpoint(request: Request) -> JSONResponse:
    return await handle_get_memory(request)


@app.delete("/aura/memory/{atom_id}")
async def aura_delete_memory_endpoint(request: Request, atom_id: str) -> JSONResponse:
    return await handle_delete_memory(request, atom_id)


@app.get("/history/sessions")
async def history_list_sessions_endpoint(request: Request) -> JSONResponse:
    return await handle_list_sessions(request)


@app.get("/history/sessions/{session_id}")
async def history_get_session_endpoint(request: Request, session_id: str) -> JSONResponse:
    return await handle_get_session_detail(request, session_id)


@app.delete("/history/sessions/{session_id}")
async def history_delete_session_endpoint(request: Request, session_id: str) -> JSONResponse:
    return await handle_delete_session(request, session_id)


@app.get("/screen-saves")
async def screen_saves_list_endpoint(request: Request) -> JSONResponse:
    return await handle_list_screen_saves(request)


@app.delete("/screen-saves/{item_id}")
async def screen_saves_delete_endpoint(request: Request, item_id: str) -> JSONResponse:
    return await handle_delete_screen_save(request, item_id)


@app.get("/drafts")
async def drafts_list_endpoint(request: Request) -> JSONResponse:
    return await handle_list_drafts(request)


@app.delete("/drafts/{draft_id}")
async def drafts_delete_endpoint(request: Request, draft_id: str) -> JSONResponse:
    return await handle_delete_draft(request, draft_id)


# Visible memory (v0.1.7): the desktop daily catch-up card + the dashboard's
# "What Buddy remembers" page. /memories/callback is registered before
# /memories/{memory_id} so "callback" can never be captured as a memory id.
@app.get("/memories/callback")
async def memories_callback_endpoint(request: Request) -> JSONResponse:
    return await handle_callback_card(request)


@app.get("/memories")
async def memories_list_endpoint(request: Request) -> JSONResponse:
    return await handle_list_memories(request)


@app.delete("/memories/{memory_id}")
async def memories_delete_endpoint(request: Request, memory_id: str) -> JSONResponse:
    return await handle_delete_memory(request, memory_id)


@app.patch("/memories/{memory_id}")
async def memories_patch_endpoint(request: Request, memory_id: str) -> JSONResponse:
    return await handle_patch_memory(request, memory_id)


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
        logger.error("scheduler_auth: OIDC token REJECTED, internal endpoint is 401ing (scheduler/tasks outage)", {
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


# Reactive orchestrate (internal — Cloud Tasks only). Coalesced per-user wake from the
# outbox relay / inline presence dispatch. Drains the user's event inbox -> reconcile ->
# deterministic policy -> dispatch agents through the self-heal envelope -> guard -> funnel.
@app.post("/internal/orchestrate")
async def orchestrate_endpoint(
    request: Request,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    from .handlers.orchestrate import handle_orchestrate

    body = await request.json()
    result = await handle_orchestrate(body)
    return JSONResponse(content=result)


# Durable chat completion (internal — Cloud Tasks only). Finishes a chat turn the client
# disconnected from and pushes "Buddy replied". See services/chat_completion/completion.py.
@app.post("/internal/chat/complete")
async def chat_complete_endpoint(
    request: Request,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    from .services.chat_completion.completion import complete_turn

    body = await request.json()
    raw_session = str(body.get("session_id", "") or "")
    status = await complete_turn(
        str(body.get("user_id", "")),
        str(body.get("client_message_id", "")),
        raw_session or None,
    )
    return JSONResponse(content={"status": status})


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


# Buddy Keyboard: memory-aware draft suggestions for the in-keyboard Buddy bar
# (reply/continue/rewrite in the user's voice; grammar/translate/tone utility).
@app.post("/keyboard/draft")
async def keyboard_draft_endpoint(request: Request) -> JSONResponse:
    return await handle_keyboard_draft(request)


# Buddy Keyboard: the user's consent-gated known-word hints (interest subjects + storyline
# entities) the on-device keyboard caches so it never flags / autocorrects them.
@app.get("/keyboard/vocab")
async def keyboard_vocab_endpoint(request: Request) -> JSONResponse:
    return await handle_keyboard_vocab(request)


# Buddy Drafts refine: reworks an existing outbound draft (text-only; new drafts
# are minted and metered inside the voice worker, never here).
@app.post("/desktop/draft-outbound/refine")
async def desktop_draft_outbound_refine_endpoint(request: Request) -> JSONResponse:
    return await handle_draft_outbound_refine(request)


# Signal engine — user events, scoring tick, content ingest.
@app.post("/events")
async def signal_events_endpoint(request: Request) -> JSONResponse:
    return await handle_signal_events(request)


# Daily Briefing: the signed-in user's synthesized morning digest for today.
@app.get("/briefing/today")
async def briefing_today_endpoint(request: Request) -> JSONResponse:
    return await handle_get_today_briefing(request)


# Generate (and persist) today's briefing on demand: first open before the morning
# tick, and the in-app refresh button (force regenerate).
@app.post("/briefing/generate")
async def briefing_generate_endpoint(request: Request) -> JSONResponse:
    return await handle_post_generate_briefing(request)


# On-demand "Catch me up on the world" snapshot (cold-start fill + refresh icon).
@app.post("/briefing/world")
async def briefing_world_endpoint(request: Request) -> JSONResponse:
    return await handle_post_world_briefing(request)


# Signal scoring (internal — the Cloud Task enqueued by content-ingest, or an
# authenticated manual recovery call). The body optionally carries the 4-hour
# generation_id; an empty/absent body derives the current bucket.
@app.post("/internal/signal-engine/tick")
async def signal_engine_tick_endpoint(
    request: Request,
    _: None = Depends(_verify_scheduler_token),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = await handle_signal_tick(body if isinstance(body, dict) else {})
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


@app.get("/calendar/upcoming")
async def calendar_upcoming_endpoint(request: Request) -> JSONResponse:
    return await get_upcoming_calendar(request)


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


@app.get("/entitlement")
async def entitlement_endpoint(request: Request) -> JSONResponse:
    return await handle_get_entitlement(request)


@app.post("/billing/checkout")
async def billing_checkout_endpoint(request: Request) -> JSONResponse:
    return await handle_billing_checkout(request)


# No Firebase auth here: the Standard Webhooks signature is the auth (handler).
@app.post("/billing/webhook")
async def billing_webhook_endpoint(request: Request) -> JSONResponse:
    return await handle_billing_webhook(request)


@app.get("/billing/portal")
async def billing_portal_endpoint(request: Request) -> JSONResponse:
    return await handle_billing_portal(request)


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
        "DODO_CONFIGURED": settings.dodo_configured,
        "DODO_WEBHOOK_SECRET": bool(settings.DODO_WEBHOOK_SECRET),
        "DODO_API_BASE": settings.DODO_API_BASE,
        "ENV": settings.ENV,
    }

    logger.info("Juno backend starting", checks)

    if not settings.ANTHROPIC_API_KEY:
        logger.warn("ANTHROPIC_API_KEY is not set, /chat will fail")
    if not settings.livekit_configured:
        logger.warn("LiveKit not fully configured, voice sessions will fail...")


# on_event is deprecated but intentional here: it is part of the same "all or nothing"
# group as the MCP session-manager handlers in handlers/mcp.py Do not migrate this one without the others.
# See the NOTE in mcp.register_mcp and lessons-learnt 2026-05-29.
@app.on_event("startup")  # pyright: ignore[reportDeprecated]
async def on_startup() -> None:
    _check_env()


@app.on_event("shutdown")  # pyright: ignore[reportDeprecated]
async def on_shutdown() -> None:
    # Drain any still-queued Langfuse telemetry before the container stops; the
    # SDK's atexit hook covers hard exits, this covers the clean path. Never
    # raises (llm_telemetry swallows everything).
    from .services.analytics.llm_telemetry import flush as flush_llm_telemetry
    flush_llm_telemetry()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.VOICE_GATEWAY_HOST,
        port=settings.VOICE_GATEWAY_PORT,
        reload=settings.ENV == "development",
        log_level="info",
    )
