"""First-run desktop attribution and install analytics saved on the user profile."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from google.cloud import firestore as gcloud_firestore

from ..services.firebase import admin_firestore
from ..services.linked_devices import upsert_linked_device
from ..services.request_auth import resolve_user_id_from_request

_FIELDS = ("where_heard", "where_heard_other", "role", "role_other")
_DESKTOP_EVENTS_COLLECTION = "desktop_events"
_MAX_STRING_LENGTH = 500
_MAX_EVENTS_PER_REQUEST = 25
_SAFE_EVENT_ID = re.compile(r"[^A-Za-z0-9_.:-]+")
_PLATFORM_WINDOWS = "windows"


def _clean_string(value: object, field: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"{field} must be a string or null."
    return value.strip()[:_MAX_STRING_LENGTH], None


def _clean_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _clean_map(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        safe_key = key.strip()[:100]
        if not safe_key:
            continue
        if isinstance(raw, dict):
            cleaned[safe_key] = _clean_map(raw)
        elif isinstance(raw, list):
            cleaned[safe_key] = [_clean_scalar(item) for item in raw[:25]]
        else:
            cleaned[safe_key] = _clean_scalar(raw)
    return cleaned


def _event_id(raw: object, fallback: str) -> str:
    candidate = raw if isinstance(raw, str) else fallback
    cleaned = _SAFE_EVENT_ID.sub("_", candidate.strip())[:120]
    return cleaned or fallback


def _events(raw: object, now_iso: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    events: list[dict[str, Any]] = []
    for index, item in enumerate(raw[:_MAX_EVENTS_PER_REQUEST]):
        if not isinstance(item, dict):
            continue
        event_name, error = _clean_string(item.get("event"), "event")
        if error or not event_name:
            continue
        occurred_at, _ = _clean_string(item.get("occurred_at"), "occurred_at")
        event_id = _event_id(item.get("event_id"), f"{event_name}_{index}")
        events.append({
            "event_id": event_id,
            "event": event_name,
            "occurred_at": occurred_at or now_iso,
            "received_at": now_iso,
            "source": "aura_desktop",
            "properties": _clean_map(item.get("properties")),
        })
    return events


async def handle_desktop_profile(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    profile: dict[str, str | None] = {}
    for field in _FIELDS:
        if field not in body:
            continue
        cleaned, error = _clean_string(body.get(field), field)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        profile[field] = cleaned

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    desktop = _clean_map(body.get("desktop"))
    install = desktop.get("install") if isinstance(desktop.get("install"), dict) else {}
    device = desktop.get("device") if isinstance(desktop.get("device"), dict) else {}
    auth = desktop.get("auth") if isinstance(desktop.get("auth"), dict) else {}
    onboarding = desktop.get("onboarding") if isinstance(desktop.get("onboarding"), dict) else {}
    events = _events(body.get("events"), now_iso)

    update: dict[str, Any] = {"desktop_last_seen_at": now_iso}
    if profile:
        update.update(profile)
        update["desktop_profile"] = profile
    if install:
        update["desktop_install"] = install
    if device:
        update["desktop_device"] = device
    if onboarding:
        update["desktop_onboarding"] = onboarding
    if auth:
        update["desktop_auth"] = auth
        last_login_at = auth.get("last_login_at")
        if isinstance(last_login_at, str) and last_login_at:
            update["last_login_at"] = last_login_at
        created_at = auth.get("created_at")
        if isinstance(created_at, str) and created_at:
            update["account_created_at"] = created_at
    if install:
        first_seen_at = install.get("first_started_at")
        if isinstance(first_seen_at, str) and first_seen_at:
            update["desktop_first_seen_at"] = first_seen_at
        app_version = install.get("last_started_version")
        if isinstance(app_version, str) and app_version:
            update["desktop_app_version"] = app_version
        update["linked_platforms"] = gcloud_firestore.ArrayUnion([_PLATFORM_WINDOWS])
        update["last_desktop_active_at"] = now_iso
    if device:
        region = device.get("region")
        if isinstance(region, str) and region:
            update["region"] = region
        timezone = device.get("timezone")
        if isinstance(timezone, str) and timezone:
            update["timezone"] = timezone

    def _write() -> None:
        db = admin_firestore()
        root_ref = db.collection("users").document(uid)
        batch = db.batch()
        batch.set(root_ref, update, merge=True)
        install_id = install.get("install_id") if isinstance(install, dict) else None
        if install_id:
            upsert_linked_device(
                db,
                uid,
                install_id,
                device.get("device_name") or "Windows PC",
                now=now,
                metadata={
                    "app_version": install.get("last_started_version"),
                    "os_platform": device.get("os_platform"),
                    "os_family": device.get("os_family"),
                    "os_type": device.get("os_type"),
                    "os_version": device.get("os_version"),
                    "os_arch": device.get("os_arch"),
                    "locale": device.get("locale"),
                    "region": device.get("region"),
                    "timezone": device.get("timezone"),
                    "sign_in_method": auth.get("sign_in_method"),
                },
            )
        for event in events:
            event_id = str(event["event_id"])
            event_doc = {key: value for key, value in event.items() if key != "event_id"}
            batch.set(
                root_ref.collection(_DESKTOP_EVENTS_COLLECTION).document(event_id),
                event_doc,
                merge=True,
            )
        batch.commit()

    await asyncio.to_thread(_write)
    return JSONResponse({"ok": True, "events_saved": len(events)})
