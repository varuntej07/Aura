"""Per-user daily LLM spend ledger.

One funnel: every backend LLM call already ends in
``llm_telemetry._Recording.finish(tokens=...)``, so this is the single place
per-user usage becomes a queryable Firestore record — chat, voice, fallbacks,
and background agents alike, whether or not Langfuse is configured. Writes are
fire-and-forget merge-increments on the same ``users/{uid}/cost/{YYYY-MM-DD}``
doc the reactive cost cap uses; a lost increment only under-reports spend, so
every failure path degrades to a warn and never reaches the caller.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..cost_doc import (
    COST_DOC_TTL,
    COST_SUBCOLLECTION,
    FIELD_EST_LLM_MICROUSD,
    FIELD_EXPIRES_AT,
    FIELD_LLM_CACHED_INPUT_TOKENS,
    FIELD_LLM_GENERATIONS,
    FIELD_LLM_INPUT_TOKENS,
    FIELD_LLM_OUTPUT_TOKENS,
    USERS_COLLECTION,
)
from ..firebase import admin_firestore
from .llm_pricing import estimate_microusd


def _write(uid: str, model: str, tokens: dict[str, int]) -> None:
    when = datetime.now(UTC)
    (
        admin_firestore()
        .collection(USERS_COLLECTION)
        .document(uid)
        .collection(COST_SUBCOLLECTION)
        .document(when.strftime("%Y-%m-%d"))
        .set(
            {
                FIELD_LLM_GENERATIONS: fs.Increment(1),
                FIELD_LLM_INPUT_TOKENS: fs.Increment(int(tokens.get("input", 0) or 0)),
                FIELD_LLM_CACHED_INPUT_TOKENS: fs.Increment(
                    int(tokens.get("cache_read_input_tokens", 0) or 0)
                ),
                FIELD_LLM_OUTPUT_TOKENS: fs.Increment(int(tokens.get("output", 0) or 0)),
                FIELD_EST_LLM_MICROUSD: fs.Increment(estimate_microusd(model, tokens)),
                "updated_at": when,
                FIELD_EXPIRES_AT: when + COST_DOC_TTL,
            },
            merge=True,
        )
    )


def record_llm_usage(uid: str, model: str, tokens: dict[str, int]) -> None:
    """Schedule the ledger increment off the caller's path. Never raises."""
    if not uid or not tokens:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (interpreter teardown or a sync utility script):
        # a rare path, so the blocking write is acceptable rather than lost.
        try:
            _write(uid, model, tokens)
        except Exception as exc:
            logger.warn(
                "llm_cost_ledger: blocking write failed",
                {"uid": uid, "model": model, "error": str(exc)},
            )
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(_write, uid, model, tokens)
        except Exception as exc:
            logger.warn(
                "llm_cost_ledger: write failed",
                {"uid": uid, "model": model, "error": str(exc)},
            )

    loop.create_task(_run(), name=f"llm-cost-ledger-{uid[:8]}")
