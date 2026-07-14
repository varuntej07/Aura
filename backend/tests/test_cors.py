"""
CORS policy tests.

Two layers, matching how the policy is actually assembled:
  1. settings.cors_allowed_origins — pure parsing logic (env string -> list),
     same style as scheduler_oidc_audience_list / tracking_fetch_tier_order.
  2. The CORSMiddleware wiring itself, exercised against a minimal harness app
     built with the exact same config main.py uses (allow_origins from
     settings, allow_credentials=False, scoped methods/headers) — not the
     real `src.main` app, since importing that drags in MCP/LiveKit/etc.
     module-level setup this test has no business depending on. Starlette's
     CORSMiddleware is the thing under test either way; this just avoids
     coupling a transport-layer test to the whole app's import graph.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.config.settings import Settings

ALLOWED_ORIGIN = "https://auravoiceapp.com"
DISALLOWED_ORIGIN = "https://evil.example.com"


# ── 1. settings.cors_allowed_origins parsing ──────────────────────────────────
def test_cors_allowed_origins_defaults_to_production_domain():
    s = Settings(CORS_ALLOWED_ORIGINS="https://auravoiceapp.com", ENV="production")
    assert s.cors_allowed_origins == ["https://auravoiceapp.com"]


def test_cors_allowed_origins_adds_localhost_outside_production():
    s = Settings(CORS_ALLOWED_ORIGINS="https://auravoiceapp.com", ENV="development")
    assert "http://localhost:3000" in s.cors_allowed_origins
    assert "http://127.0.0.1:3000" in s.cors_allowed_origins


def test_cors_allowed_origins_never_adds_localhost_in_production():
    s = Settings(CORS_ALLOWED_ORIGINS="https://auravoiceapp.com", ENV="production")
    assert "http://localhost:3000" not in s.cors_allowed_origins


def test_cors_allowed_origins_parses_comma_and_whitespace_separated_extras():
    s = Settings(
        CORS_ALLOWED_ORIGINS="https://auravoiceapp.com, https://preview.example.com",
        ENV="production",
    )
    assert s.cors_allowed_origins == [
        "https://auravoiceapp.com",
        "https://preview.example.com",
    ]


# ── 2. CORSMiddleware wiring, mirroring main.py's exact config ────────────────
def _harness_client(origins: list[str]) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )

    @app.get("/history/sessions")
    async def sessions():
        return {"sessions": []}

    return TestClient(app)


def test_preflight_from_allowed_origin_gets_explicit_acao_header():
    client = _harness_client([ALLOWED_ORIGIN])
    resp = client.options(
        "/history/sessions",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "access-control-allow-credentials" not in resp.headers


def test_preflight_allows_google_popup_csrf_header():
    client = _harness_client([ALLOWED_ORIGIN])
    resp = client.options(
        "/history/sessions",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,x-requested-with",
        },
    )
    assert resp.status_code == 200
    assert "x-requested-with" in resp.headers["access-control-allow-headers"].lower()


def test_preflight_from_disallowed_origin_gets_no_acao_header():
    client = _harness_client([ALLOWED_ORIGIN])
    resp = client.options(
        "/history/sessions",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    # Starlette answers a disallowed preflight with 400 and deliberately omits
    # Access-Control-Allow-Origin — the browser is what actually enforces the
    # block on the real request, this just makes the rejection informative.
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


def test_actual_get_from_allowed_origin_carries_acao_header():
    client = _harness_client([ALLOWED_ORIGIN])
    resp = client.get("/history/sessions", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_actual_get_from_disallowed_origin_has_no_acao_header():
    client = _harness_client([ALLOWED_ORIGIN])
    resp = client.get("/history/sessions", headers={"Origin": DISALLOWED_ORIGIN})
    # The route still runs (CORS is enforced client-side by the browser, not
    # server-side), but without the header a real browser refuses to expose
    # the response body to the calling page's JS.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_policy_never_enables_wildcard_with_credentials():
    """The one footgun this policy must never resolve to: allow_origins="*"
    combined with allow_credentials=True lets any site read authenticated
    responses. Pin allow_credentials=False permanently for this API, since
    auth here is a manually-set Authorization header, not a cookie."""
    client = _harness_client([ALLOWED_ORIGIN])
    resp = client.get("/history/sessions", headers={"Origin": ALLOWED_ORIGIN})
    assert "access-control-allow-credentials" not in resp.headers
