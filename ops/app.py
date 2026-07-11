"""Aura ops dashboard, deployed and passcode-gated.

Security model:
  - The HTML page is public and harmless; it holds no user data.
  - EVERY data route depends on require_passcode, which constant-time-compares the caller's
    passcode against OPS_PASSCODE. No/!wrong passcode -> 401, so no data ever leaves.
  - The passcode is a shared secret: treat it like a password. Anyone who has it gets in.
  - Cloud Run is --allow-unauthenticated so a browser can load the page; the app (the
    passcode) is the gate. The service URL is guessable, so the passcode is the real lock.

firebase_admin is initialized only to READ Firestore via the Admin SDK (ADC), not for auth.
"""
from __future__ import annotations

import hmac
import os

import functools

import anyio
import firebase_admin
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import panels

load_dotenv()

PROJECT_ID = os.environ.get("GCP_PROJECT", "juno-2ea45")
PASSCODE = os.environ.get("OPS_PASSCODE", "")

if not firebase_admin._apps:
    firebase_admin.initialize_app(options={"projectId": PROJECT_ID})

app = FastAPI(title="Aura Ops", docs_url=None, redoc_url=None)


async def require_passcode(authorization: str = Header(default="")) -> None:
    """Gate every data route on the shared passcode (constant-time compare).

    401 on a missing/wrong passcode. Fails CLOSED when OPS_PASSCODE is unset, an
    unconfigured gate must never mean "open to everyone".
    """
    if not PASSCODE:
        raise HTTPException(status_code=503, detail="ops passcode not configured")
    supplied = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else ""
    if not (supplied and hmac.compare_digest(supplied, PASSCODE)):
        raise HTTPException(status_code=401, detail="wrong passcode")


_VALID_RANGES = {"today", "7d", "30d"}


def _clamp_range(range_key: str) -> str:
    return range_key if range_key in _VALID_RANGES else "7d"


@app.get("/api/dashboard")
async def dashboard(_gate: None = Depends(require_passcode)) -> JSONResponse:
    data = await anyio.to_thread.run_sync(panels.build_dashboard)
    return JSONResponse(data)


@app.get("/api/overview/analytics")
async def overview_analytics(_gate: None = Depends(require_passcode)) -> JSONResponse:
    """The slower Overview panels (retention, funnels, default LLM views);
    the UI fetches this lazily after first paint on a 5-minute cadence."""
    data = await anyio.to_thread.run_sync(panels.build_overview_analytics)
    return JSONResponse(data)


@app.get("/api/llm/cost")
async def llm_cost(
    range: str = Query(default="7d"),
    _gate: None = Depends(require_passcode),
) -> JSONResponse:
    data = await anyio.to_thread.run_sync(
        functools.partial(panels.build_llm_cost, _clamp_range(range))
    )
    return JSONResponse(data)


@app.get("/api/llm/tools")
async def llm_tools(
    range: str = Query(default="7d"),
    tool: str = Query(default="", max_length=64),
    _gate: None = Depends(require_passcode),
) -> JSONResponse:
    data = await anyio.to_thread.run_sync(
        functools.partial(panels.build_llm_tools, _clamp_range(range), tool)
    )
    return JSONResponse(data)


@app.get("/api/tab/mobile")
async def tab_mobile(_gate: None = Depends(require_passcode)) -> JSONResponse:
    data = await anyio.to_thread.run_sync(panels.build_mobile_tab)
    return JSONResponse(data)


@app.get("/api/tab/desktop")
async def tab_desktop(_gate: None = Depends(require_passcode)) -> JSONResponse:
    data = await anyio.to_thread.run_sync(panels.build_desktop_tab)
    return JSONResponse(data)


@app.get("/api/tab/web")
async def tab_web(_gate: None = Depends(require_passcode)) -> JSONResponse:
    data = await anyio.to_thread.run_sync(panels.build_web_tab)
    return JSONResponse(data)


@app.get("/api/logs")
async def logs(
    services: str = Query(default="", max_length=200),
    severity: str = Query(default="DEFAULT", max_length=16),
    q: str = Query(default="", max_length=200),
    hours: int = Query(default=24, ge=1, le=336),
    limit: int = Query(default=100, ge=1, le=300),
    _gate: None = Depends(require_passcode),
) -> JSONResponse:
    data = await anyio.to_thread.run_sync(
        functools.partial(panels.search_logs, services, severity, q, hours, limit)
    )
    return JSONResponse(data)


# Mounted LAST so the /api/* routes take precedence. html=True serves static/index.html at "/".
# The page is public; data only flows through the gated routes above.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
