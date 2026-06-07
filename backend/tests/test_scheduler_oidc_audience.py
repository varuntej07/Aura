"""
Contract test for the Cloud Scheduler / Cloud Tasks OIDC audience.

On 2026-06-04 every internal endpoint (reminders, notifications, ingest) started
returning 401: deploy.sh pointed the scheduler jobs' OIDC audience at Cloud Run's
`status.url` (which had switched to the per-service hash hostname) while the
backend still verified against the old project-number hostname. The two drifted
and the scheduler went silently dark.

These tests lock the contract three ways so that drift breaks CI, not production:
  1. The backend accepts the audience that the in-code Cloud Tasks minters sign
     with (BACKEND_INTERNAL_URL) — a pure Python writer→reader round-trip.
  2. The backend's verifier passes the *list* of accepted audiences (not a single
     hardcoded literal) to verify_oauth2_token.
  3. deploy.sh signs scheduler tokens with the STABLE project-number URL and tells
     the backend to accept it — the bash↔python half of the same contract.
"""

from __future__ import annotations

from pathlib import Path

from src.config.settings import settings

# backend/tests/ -> backend/ is one parent up.
_DEPLOY_SH = Path(__file__).resolve().parents[1] / "deploy.sh"
_MAIN_PY = Path(__file__).resolve().parents[1] / "src" / "main.py"


# 1. Python round-trip: the Cloud Tasks audience must always be accepted.
def test_backend_internal_url_is_an_accepted_audience():
    accepted = settings.scheduler_oidc_audience_list
    assert settings.BACKEND_INTERNAL_URL in accepted, (
        "Cloud Tasks (orchestrator + engagement) sign tokens with "
        f"BACKEND_INTERNAL_URL={settings.BACKEND_INTERNAL_URL!r}, but it is not in "
        f"the accepted audience list {accepted!r}. Those tasks would 401."
    )


def test_accepted_audiences_are_nonempty_https_urls():
    accepted = settings.scheduler_oidc_audience_list
    assert accepted, "scheduler_oidc_audience_list is empty — every internal call would 401."
    for audience in accepted:
        assert audience.startswith("https://"), f"Audience is not an https URL: {audience!r}"


# 2. The verifier must check the audience *list*, not a single hardcoded literal.
def test_main_verifies_against_the_audience_list():
    text = _MAIN_PY.read_text(encoding="utf-8")
    assert "audience=settings.scheduler_oidc_audience_list" in text, (
        "_verify_scheduler_token must pass settings.scheduler_oidc_audience_list to "
        "verify_oauth2_token. A single hardcoded audience literal is the drift trap "
        "that caused the 2026-06-04 outage."
    )
    assert "_CLOUD_RUN_AUDIENCE" not in text, (
        "The hardcoded _CLOUD_RUN_AUDIENCE literal is back — use the env-driven "
        "settings.scheduler_oidc_audience_list instead."
    )


# 3. bash↔python: deploy.sh signs with the stable URL and accepts it on the backend.
def test_deploy_signs_jobs_with_stable_url_not_status_url():
    text = _DEPLOY_SH.read_text(encoding="utf-8")
    # Scheduler jobs must be pinned to the stable project-number URL.
    assert '--oidc-token-audience="${STABLE_SERVICE_URL}"' in text, (
        "deploy.sh must pin scheduler jobs' --oidc-token-audience to "
        "${STABLE_SERVICE_URL} (the stable project-number URL), never status.url."
    )
    # And the backend must be told to accept that same audience.
    assert 'SCHEDULER_OIDC_AUDIENCES=${ACCEPTED_AUDIENCES}' in text, (
        "deploy.sh must set SCHEDULER_OIDC_AUDIENCES on the backend so it accepts "
        "the audience the jobs sign with."
    )
    assert 'ACCEPTED_AUDIENCES="${STABLE_SERVICE_URL}"' in text, (
        "ACCEPTED_AUDIENCES must include the stable URL the jobs sign with."
    )
