"""
Best-effort Telegram alert for captured product feedback.

Mirrors analytics/posthog_client.py: a no-op when the bot token / chat id are unset (local dev, or
before the founder wires the secret), and it NEVER raises into the caller — a Telegram outage or a
bad payload degrades to a WARNING log. The durable record is the Firestore observed_feedback doc;
this is only the convenience ping.
"""

from __future__ import annotations

import httpx

from ...config.settings import settings
from ...lib.logger import logger

_TELEGRAM_TIMEOUT_S = 5.0


async def send_feedback_alert(text: str) -> None:
    """Send one Telegram message. No-op when unconfigured; never raises."""
    if not settings.telegram_feedback_configured:
        return

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_FEEDBACK_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        # follow_redirects per the CLAUDE.md httpx rule; api.telegram.org doesn't redirect today,
        # but the default httpx.AsyncClient would silently fail a 3xx if it ever did.
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warn("telegram_client: send failed", {
                    "status": resp.status_code,
                    "body": resp.text[:200],
                })
    except Exception as exc:
        logger.warn("telegram_client: send error", {"error": str(exc)})
