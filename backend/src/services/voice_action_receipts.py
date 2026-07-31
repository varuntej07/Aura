"""Immediate durable receipts for externally visible voice actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..lib.logger import logger
from .firebase import admin_firestore


async def persist(
    user_id: str,
    session_id: str,
    receipt: dict[str, Any],
) -> None:
    call_id = str(receipt.get("call_id") or "").strip()
    if not call_id:
        canonical = json.dumps(receipt, sort_keys=True, default=str)
        call_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ref = (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection("voice_sessions")
        .document(session_id)
        .collection("action_receipts")
        .document(call_id)
    )
    try:
        await asyncio.to_thread(ref.set, receipt, merge=True)
    except Exception as exc:
        logger.error(
            "voice_action_receipt: persistence failed",
            {
                "user_id": user_id,
                "session_id": session_id,
                "tool": receipt.get("tool_name"),
                "error_type": type(exc).__name__,
            },
        )
        raise
