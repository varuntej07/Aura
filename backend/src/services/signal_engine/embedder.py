"""
Thin async wrapper around Gemini gemini-embedding-001.

One responsibility: turn a string into a 768-dim list[float]. Used by:
  - content_pool when a fetcher inserts a new candidate
  - feature_store bootstrap when seeding a user_vector from UserAura interests
  - event_ingester when a search_query event arrives

The underlying SDK is the same google-genai Client used by gemini_client.py
and ModelProvider. We reuse the singleton.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger

EMBEDDING_MODEL_ID = "models/gemini-embedding-001"
EMBEDDING_DIMENSION = 768

_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0
_TIMEOUT_S = 15.0

# Gemini's BatchEmbedContents hard cap: at most 100 texts per request
_MAX_EMBED_BATCH_SIZE = 100

_client_singleton: Any = None


def _get_client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("embedder: GEMINI_API_KEY not configured")
        from google import genai  # type: ignore
        _client_singleton = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options={"api_version": "v1"},
        )
    return _client_singleton


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Returns a 768-dim list of floats."""
    if not text or not text.strip():
        raise ValueError("embed_text: text is empty")
    results = await embed_texts([text])
    return results[0]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed. Empty list returns empty list.

    Splits into sequential sub-batches of at most _MAX_EMBED_BATCH_SIZE so a list
    larger than Gemini's 100-per-request BatchEmbedContents cap never 400s (the
    full-pool content-ingest can hand this >100 fresh items at once). Results are
    concatenated in input order. Sequential, not concurrent: this runs in the
    hourly ingest, not a latency path, so serial calls avoid adding parallel load
    (and extra 429 risk) on the embed API."""
    if not texts:
        return []

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _MAX_EMBED_BATCH_SIZE):
        chunk = texts[start:start + _MAX_EMBED_BATCH_SIZE]
        vectors.extend(await _embed_batch(chunk))
    return vectors


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a single batch of at most _MAX_EMBED_BATCH_SIZE texts. Callers go
    through embed_texts, which enforces the batch ceiling."""
    client = _get_client()

    def _sync() -> list[list[float]]:
        resp = client.models.embed_content(
            model=EMBEDDING_MODEL_ID,
            contents=texts,
            config={"output_dimensionality": EMBEDDING_DIMENSION},
        )

        # google-genai returns either resp.embeddings (list with .values) or resp.embedding (single). handling both
        vectors: list[list[float]] = []
        embeddings = getattr(resp, "embeddings", None)

        if embeddings is None:
            single = getattr(resp, "embedding", None)
            if single is None:
                raise RuntimeError("embedder: response had no embeddings field")
            vectors.append([float(v) for v in single.values])
        else:
            for emb in embeddings:
                vectors.append([float(v) for v in emb.values])
        return vectors

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await asyncio.wait_for(asyncio.to_thread(_sync), timeout=_TIMEOUT_S)
        except TimeoutError:
            if attempt == _MAX_RETRIES:
                logger.exception("embedder: timed out after retries", {
                    "batch_size": len(texts),
                    "timeout_s": _TIMEOUT_S,
                })
                raise
            await asyncio.sleep(_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
        except Exception as exc:
            code = getattr(exc, "code", None)
            error_str = str(exc).upper()
            retryable = (
                code == 429
                or (isinstance(code, int) and 500 <= code < 600)
                or "UNAVAILABLE" in error_str
                or "RESOURCE_EXHAUSTED" in error_str
            )
            if not retryable or attempt == _MAX_RETRIES:
                logger.error("embedder: call failed", {
                    "batch_size": len(texts),
                    "attempt": attempt,
                    "error": str(exc),
                })
                raise
            await asyncio.sleep(_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
    raise RuntimeError("embedder: retry loop exited unexpectedly")
