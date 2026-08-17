"""Canonical account onboarding read and completion contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from google.cloud import firestore as gcloud_firestore

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request
from ..services.signal_engine.content_category_map import ONBOARDABLE_CATEGORIES
from ..services.user_aura_schema import CATEGORY_LABELS
from .onboarding_profile import _seed_if_consented

_ACCOUNT_ONBOARDING_VERSION = 1
_MINIMUM_AGE = 13
_MINIMUM_INTERESTS = 3
_MAXIMUM_NAME_LENGTH = 40
_MAXIMUM_TEXT_LENGTH = 80
_GENDERS = frozenset({"male", "female", "non-binary", ""})
_ALLOWED_INTERESTS = frozenset(ONBOARDABLE_CATEGORIES)


def _profile(data: dict[str, Any]) -> dict[str, Any]:
    raw_interests = data.get("onboarding_interests")
    return {
        "display_name": (
            data.get("display_name") if isinstance(data.get("display_name"), str) else ""
        ),
        "date_of_birth": (
            data.get("date_of_birth")
            if isinstance(data.get("date_of_birth"), str)
            else None
        ),
        "aura_consent_granted": (
            data.get("aura_consent_granted")
            if isinstance(data.get("aura_consent_granted"), bool)
            else None
        ),
        "gender": data.get("gender") if isinstance(data.get("gender"), str) else None,
        "onboarding_interests": (
            [value for value in raw_interests if isinstance(value, str)]
            if isinstance(raw_interests, list)
            else []
        ),
        "locale": data.get("locale") if isinstance(data.get("locale"), str) else None,
        "language": data.get("language") if isinstance(data.get("language"), str) else None,
    }


def _response(data: dict[str, Any]) -> dict[str, Any]:
    raw_version = data.get("account_onboarding_version")
    return {
        "complete": data.get("onboarding_complete") is True,
        "version": raw_version if isinstance(raw_version, int) else 0,
        "profile": _profile(data),
        "minimum_age": _MINIMUM_AGE,
        "minimum_interests": _MINIMUM_INTERESTS,
        "interest_options": [
            {"slug": slug, "label": CATEGORY_LABELS[slug].title()}
            for slug in ONBOARDABLE_CATEGORIES
        ],
    }


def _read_account_onboarding(uid: str) -> dict[str, Any] | None:
    snap = admin_firestore().collection("users").document(uid).get()
    return (snap.to_dict() or {}) if snap.exists else None


async def handle_get_account_onboarding(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    data = await asyncio.to_thread(_read_account_onboarding, uid)
    if data is None:
        return JSONResponse({"error": "Account profile is not initialized."}, status_code=409)
    return JSONResponse(_response(data), status_code=200)


def _clean_text(body: dict[str, Any], field: str, maximum: int) -> tuple[str | None, str | None]:
    value = body.get(field)
    if not isinstance(value, str):
        return None, f"Field '{field}' must be a string."
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        return None, f"Field '{field}' must contain 1 to {maximum} characters."
    return cleaned, None


def _age_on(born: date, today: date) -> int:
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _validated_update(body: object, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(body, dict):
        return None, "Invalid JSON body."

    display_name, error = _clean_text(body, "display_name", _MAXIMUM_NAME_LENGTH)
    if error:
        return None, error
    locale, error = _clean_text(body, "locale", _MAXIMUM_TEXT_LENGTH)
    if error:
        return None, error
    language, error = _clean_text(body, "language", _MAXIMUM_TEXT_LENGTH)
    if error:
        return None, error

    raw_dob = body.get("date_of_birth")
    if not isinstance(raw_dob, str) or len(raw_dob) != 10:
        return None, "Field 'date_of_birth' must use YYYY-MM-DD."
    try:
        born = date.fromisoformat(raw_dob)
    except ValueError:
        return None, "Field 'date_of_birth' must be a valid date."
    age = _age_on(born, now.date())
    if born < date(1900, 1, 1) or age < _MINIMUM_AGE:
        return None, f"Aura is available only to users age {_MINIMUM_AGE} or older."

    consent = body.get("aura_consent_granted")
    if not isinstance(consent, bool):
        return None, "Field 'aura_consent_granted' must be a boolean."
    effective_consent = consent if age >= 18 else False

    gender = body.get("gender")
    if not isinstance(gender, str) or gender not in _GENDERS:
        return None, "Field 'gender' is invalid."

    raw_interests = body.get("onboarding_interests")
    if not isinstance(raw_interests, list):
        return None, "Field 'onboarding_interests' must be a list."
    interests: list[str] = []
    for value in raw_interests:
        if not isinstance(value, str) or value not in _ALLOWED_INTERESTS:
            return None, "Field 'onboarding_interests' contains an invalid value."
        if value not in interests:
            interests.append(value)
    if len(interests) < _MINIMUM_INTERESTS or len(interests) > len(ONBOARDABLE_CATEGORIES):
        return None, (
            f"Field 'onboarding_interests' must contain {_MINIMUM_INTERESTS} to "
            f"{len(ONBOARDABLE_CATEGORIES)} unique values."
        )

    now_iso = now.isoformat()
    return {
        "onboarding_complete": True,
        "account_onboarding_version": _ACCOUNT_ONBOARDING_VERSION,
        "account_onboarding_surface": "desktop",
        "account_onboarding_completed_at": now_iso,
        "display_name": display_name,
        "date_of_birth": raw_dob,
        "aura_consent_granted": effective_consent,
        "aura_consent_timestamp": now_iso,
        "gender": gender,
        "onboarding_interests": interests,
        "locale": locale,
        "language": language,
    }, None


def _complete_account_onboarding(
    uid: str,
    update: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    db = admin_firestore()
    user_ref = db.collection("users").document(uid)
    transaction = db.transaction()

    @gcloud_firestore.transactional
    def _commit(transaction: Any) -> tuple[str, dict[str, Any]]:
        snap = user_ref.get(transaction=transaction)
        if not snap.exists:
            return "missing", {}
        current = snap.to_dict() or {}
        if current.get("onboarding_complete") is True:
            return "existing", current
        transaction.set(user_ref, update, merge=True)
        return "created", {**current, **update}

    return _commit(transaction)


async def handle_complete_account_onboarding(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    update, error = _validated_update(body, datetime.now(UTC))
    if error or update is None:
        return JSONResponse({"error": error or "Invalid JSON body."}, status_code=400)

    outcome, data = await asyncio.to_thread(_complete_account_onboarding, uid, update)
    if outcome == "missing":
        return JSONResponse({"error": "Account profile is not initialized."}, status_code=409)
    if outcome == "created":
        await asyncio.to_thread(
            _seed_if_consented,
            uid,
            list(update["onboarding_interests"]),
        )
    logger.info("account_onboarding: completion", {
        "user_id": uid,
        "outcome": outcome,
        "consent_granted": data.get("aura_consent_granted") is True,
    })
    return JSONResponse({**_response(data), "created": outcome == "created"}, status_code=200)
