"""
Capture orchestration for the `report_feedback` tool.

`capture_feedback` persists the structured feedback to the `observed_feedback` Firestore collection
(the durable record) and fires a best-effort Telegram alert. Called from
`ToolExecutor._report_feedback` for both the text chat and the voice (MCP) surfaces. Never raises —
a failure here must never break a chat or voice turn.
"""

from __future__ import annotations

import asyncio
import zoneinfo
from datetime import datetime

from ...lib.logger import logger
from ..briefing.world_region import resolve_region
from .feedback_schema import (
    FEEDBACK_COLLECTION,
    FeedbackReport,
    FeedbackUserContext,
    build_feedback_document,
    format_telegram_alert,
)
from .telegram_client import send_feedback_alert


async def _load_user_context(uid: str) -> FeedbackUserContext:
    """Best-effort enrichment from users/{uid}: display name, local time, region, country.

    Reads display_name + timezone (the two identity/region fields the Flutter client writes on
    every sign-in) and derives the user's local wall-clock time and ISO country from the timezone
    (reusing resolve_region, the same timezone→country mapping the world briefing uses, since
    locale is empty for most users). Never raises — a profile-read failure degrades to an empty
    context so the feedback is still persisted and pinged.
    """

    def _fetch() -> tuple[str, str]:
        from ..firebase import admin_firestore

        snap = admin_firestore().collection("users").document(uid).get()
        data = snap.to_dict() if snap.exists else None
        if not data:
            return "", ""
        raw_name = str(data.get("display_name", "") or "").strip()
        # "User" is the client-side placeholder for an unnamed account — treat as no name.
        name = "" if raw_name in ("", "User") else raw_name
        timezone = str(data.get("timezone", "") or "").strip()
        return name, timezone

    try:
        name, timezone = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("FeedbackCapture: profile read failed (enrichment skipped)", {
            "user_id": uid,
            "error": str(exc),
        })
        return FeedbackUserContext()

    local_time = ""
    if timezone:
        try:
            local_time = datetime.now(zoneinfo.ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            local_time = ""

    region = resolve_region(timezone)
    return FeedbackUserContext(
        username=name,
        timezone=timezone,
        local_time=local_time,
        region="" if region.is_global else region.country_code,
        country="" if region.is_global else region.country_name,
    )


async def capture_feedback(
    uid: str,
    report: FeedbackReport,
    *,
    source: str,
    session_id: str | None = None,
) -> None:
    """Persist one feedback report and ping Telegram. Best-effort, never raises."""
    try:
        context = await _load_user_context(uid)
        document = build_feedback_document(
            uid, report, source=source, session_id=session_id, context=context
        )

        from ..firebase import admin_firestore

        def _write() -> None:
            admin_firestore().collection(FEEDBACK_COLLECTION).document().set(document)

        await asyncio.to_thread(_write)

        logger.info("FeedbackCapture: stored", {
            "user_id": uid,
            "category": report.category,
            "about": report.about,
            "severity": report.severity,
            "source": source,
            "region": context.region,
        })

        # Best-effort founder ping. Detached so Telegram latency never blocks the tool response (and
        # so a slow/outage Telegram never eats into the voice tool's 8s budget). The Firestore doc
        # above is the durable record either way.
        asyncio.create_task(
            send_feedback_alert(format_telegram_alert(uid, report, source=source, context=context))
        )

    except Exception as exc:
        logger.warn("FeedbackCapture: failed to capture feedback", {
            "user_id": uid,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
