"""
Batch-size coverage for the Gemini embedder.

Gemini's BatchEmbedContents request caps at 100 texts; a larger list 400s with
INVALID_ARGUMENT. The hourly content-ingest can hand embed_texts >100 fresh
candidates at once (a full Google News pull after the pool has drained), which
turned /internal/signal-engine/content-ingest into a 500 every hour and left the
pool unable to refill (a self-perpetuating starvation). embed_texts must split
anything over the cap into sequential sub-batches and stitch the results back in
input order.

These pin:
  1. >100 texts are split into <=100 sub-batches (never one oversized call).
  2. results come back in the original input order across chunk boundaries.
  3. a sub-cap call still makes exactly one request (no needless chunking).
  4. the empty-list short-circuit is preserved (client never touched).
"""

from __future__ import annotations

from src.services.signal_engine import embedder


class _FakeEmbedding:
    def __init__(self, value: float) -> None:
        self.values = [value]


class _FakeResponse:
    def __init__(self, embeddings: list[_FakeEmbedding]) -> None:
        self.embeddings = embeddings


class _BatchTooLargeError(Exception):
    """Stands in for the google-genai 400 raised when a batch exceeds 100. Carries
    none of the retryable markers (429 / 5xx / UNAVAILABLE / RESOURCE_EXHAUSTED),
    so the embedder's retry loop re-raises it immediately — exactly as the real
    INVALID_ARGUMENT does."""


class _FakeModels:
    def __init__(self, parent: "_FakeEmbedClient") -> None:
        self._parent = parent

    def embed_content(self, *, model, contents, config):  # noqa: ANN001 - mirrors SDK kwargs
        return self._parent._embed(list(contents))


class _FakeEmbedClient:
    """Records each embed_content batch size and rejects any batch over the cap,
    exactly like the real BatchEmbedContents endpoint. Each input text is the str
    of its global index, so the returned value encodes that index — which lets the
    test assert ordering is preserved across chunk boundaries."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.models = _FakeModels(self)

    def _embed(self, contents: list[str]) -> _FakeResponse:
        self.batch_sizes.append(len(contents))
        if len(contents) > embedder._MAX_EMBED_BATCH_SIZE:
            raise _BatchTooLargeError(
                "400 INVALID_ARGUMENT BatchEmbedContentsRequest.requests: "
                "at most 100 requests can be in one batch"
            )
        return _FakeResponse([_FakeEmbedding(float(t)) for t in contents])


async def test_embed_texts_splits_batches_over_100(monkeypatch):
    """150 texts → two sub-batches (100 + 50), neither over the cap, order kept."""
    fake = _FakeEmbedClient()
    monkeypatch.setattr(embedder, "_get_client", lambda: fake)

    texts = [str(i) for i in range(150)]
    result = await embedder.embed_texts(texts)

    assert fake.batch_sizes == [100, 50]
    assert len(result) == 150
    # Each vector encodes its original index → order preserved across the chunk seam.
    assert [vec[0] for vec in result] == [float(i) for i in range(150)]


async def test_embed_texts_single_batch_under_cap(monkeypatch):
    """50 texts → exactly one request, no chunking, all results returned."""
    fake = _FakeEmbedClient()
    monkeypatch.setattr(embedder, "_get_client", lambda: fake)

    result = await embedder.embed_texts([str(i) for i in range(50)])

    assert fake.batch_sizes == [50]
    assert len(result) == 50


async def test_embed_texts_exactly_at_cap(monkeypatch):
    """Exactly 100 texts is one request — the boundary is inclusive, no second call."""
    fake = _FakeEmbedClient()
    monkeypatch.setattr(embedder, "_get_client", lambda: fake)

    result = await embedder.embed_texts([str(i) for i in range(100)])

    assert fake.batch_sizes == [100]
    assert len(result) == 100


async def test_embed_texts_empty_returns_empty(monkeypatch):
    """Empty input short-circuits without ever touching the client."""
    fake = _FakeEmbedClient()
    monkeypatch.setattr(embedder, "_get_client", lambda: fake)

    result = await embedder.embed_texts([])

    assert result == []
    assert fake.batch_sizes == []
