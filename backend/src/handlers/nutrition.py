"""
POST /nutrition/scan    — image → detected food + optional clarifying questions
POST /nutrition/analyze — scan_id + user answers → full macro verdict
"""

from __future__ import annotations

import asyncio
import base64
import json
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from ..lib.logger import logger
from ..lib.query_logger import log_query
from ..services.engagement.task_scheduler import get_task_scheduler
from ..services.firebase import admin_firestore
from ..services.gemini_client import ScanResult, get_gemini_client
from ..services.request_auth import resolve_user_id_from_event


def _json(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


async def _get_dietary_profile(user_id: str) -> dict[str, Any] | None:
    """Fetch the user's dietary profile from Firestore without blocking the event loop."""
    try:
        ref = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("dietary_profile")
            .document("data")
        )
        doc = await asyncio.to_thread(ref.get)
        return doc.to_dict() if doc.exists else None
    except Exception:
        return None


# In-memory scan cache: scans complete within a single user session (seconds to minutes).
# Cloud Run runs with min-instances=1 so the cache survives between requests for the same user.
_scan_cache: dict[str, ScanResult] = {}


# POST /nutrition/scan
async def handle_nutrition_scan_request(event: dict[str, Any]) -> dict[str, Any]:
    try:
        body: dict[str, Any] = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json(400, {"error": "Invalid JSON body"})

    user_id = resolve_user_id_from_event(event, body=body)
    if not user_id:
        return _json(401, {"error": "Unauthorized: user_id required"})

    image_b64 = str(body.get("image_base64", "")).strip()
    mime_type = str(body.get("mime_type", "image/jpeg"))

    if not image_b64:
        return _json(400, {"error": "image_base64 is required"})

    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception:
        return _json(400, {"error": "Invalid base64 image data"})

    dietary_profile = await _get_dietary_profile(user_id)

    try:
        client = get_gemini_client()
        result = await client.scan_image(image_bytes, mime_type, dietary_profile)

        _scan_cache[result.scan_id] = result

        items_label = ", ".join(result.detected_items) or "unknown item"
        asyncio.create_task(log_query(user_id, "nutrition_scan", f"[scan] {items_label}"))

        return _json(200, {
            "scan_id": result.scan_id,
            "detected_type": result.detected_type,
            "detected_items": result.detected_items,
            "food_category": result.food_category,
            "confidence": result.confidence,
            "needs_clarification": result.needs_clarification,
            "clarifying_questions": [
                {
                    "id": q.id,
                    "text": q.text,
                    "input_type": q.input_type,
                    "options": q.options,
                }
                for q in result.clarifying_questions
            ],
        })

    except Exception as exc:
        logger.error("Nutrition scan failed", {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
            "user_id": user_id,
        })
        return _json(500, {"error": "Failed to analyze image. Please try again."})


# POST /nutrition/analyze
async def handle_nutrition_analyze_request(event: dict[str, Any]) -> dict[str, Any]:
    try:
        body: dict[str, Any] = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json(400, {"error": "Invalid JSON body"})

    user_id = resolve_user_id_from_event(event, body=body)
    if not user_id:
        return _json(401, {"error": "Unauthorized: user_id required"})

    scan_id = str(body.get("scan_id", "")).strip()
    user_answers: dict[str, Any] = body.get("user_answers") or {}

    if not scan_id:
        return _json(400, {"error": "scan_id is required"})

    scan_result = _scan_cache.get(scan_id)
    if not scan_result:
        return _json(404, {"error": "Scan session not found or expired. Please scan again."})

    dietary_profile, _ = await asyncio.gather(
        _get_dietary_profile(user_id),
        log_query(user_id, "nutrition_scan", f"[analyze] {scan_id}"),
    )

    try:
        client = get_gemini_client()
        analysis = await client.analyze_food(scan_result, user_answers, dietary_profile)

        log_id = str(uuid.uuid4())
        now_iso = datetime.now(UTC).isoformat()

        log_doc = {
            "scan_id": scan_id,
            "detected_type": scan_result.detected_type,
            "detected_items": scan_result.detected_items,
            "food_category": scan_result.food_category,
            "confidence": scan_result.confidence,
            "questions_asked": [{"id": q.id, "text": q.text} for q in scan_result.clarifying_questions],
            "user_answers": user_answers,
            "food_name": analysis.food_name,
            "headline": analysis.headline,
            "macros": analysis.macros,
            "key_nutrients": analysis.key_nutrients,
            "recommendation": analysis.recommendation,
            "verdict_reason": analysis.verdict_reason,
            "concerns": analysis.concerns,
            "pros": analysis.pros,
            "cons": analysis.cons,
            "occasion": user_answers.get("occasion"),
            "is_cheat_meal": bool(user_answers.get("is_cheat_meal", False)),
            "created_at": now_iso,
        }

        ref = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("nutrition_logs")
            .document(log_id)
        )
        await asyncio.to_thread(lambda: ref.set(log_doc))

        # Durably schedule engagement orchestration via Cloud Tasks
        # Failures here will never surface to the user, as nutrition result is already saved.
        try:
            await asyncio.to_thread(
                get_task_scheduler().schedule_orchestration,
                user_id,
                "nutrition_scan",
                {
                    "food_name": analysis.food_name,
                    "recommendation": analysis.recommendation,
                    "verdict_reason": analysis.verdict_reason,
                    "concerns": analysis.concerns,
                    "macros": analysis.macros,
                    "log_id": log_id,
                },
            )
        except Exception as sched_exc:
            logger.error("Engagement scheduling failed (non-fatal)", {
                "error": str(sched_exc),
                "user_id": user_id,
                "log_id": log_id,
            })

        _scan_cache.pop(scan_id, None)

        return _json(200, {
            "nutrition_log_id": log_id,
            "food_name": analysis.food_name,
            "headline": analysis.headline,
            "macros": analysis.macros,
            "key_nutrients": analysis.key_nutrients,
            "recommendation": analysis.recommendation,
            "verdict_reason": analysis.verdict_reason,
            "concerns": analysis.concerns,
            "pros": analysis.pros,
            "cons": analysis.cons,
        })

    except Exception as exc:
        logger.error("Nutrition analyze failed", {"error": str(exc), "user_id": user_id})
        return _json(500, {"error": "Internal server error"})
