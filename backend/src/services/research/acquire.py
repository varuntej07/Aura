"""Brave discovery followed by bounded page reads.

The one orchestration function in phase one, and deliberately unregistered: no route,
no tool, no Cloud Task target, no scheduler hook, no Firestore write. It is reachable
only by an explicit import, so it can be exercised and measured before any of the
research run lifecycle exists.

    brave_search(query)                       existing client, untouched, <= 5 sources
            |
            v
    evaluate_url() per URL                    reject or canonicalize
            |  dedupe by canonical_url, Brave rank order preserved
            v
    Semaphore(<= 3) + gather                  reader.read() per surviving URL
            |
            v
    evaluate_url(final_url)                   discard anything that redirected out
            |
            v
    AcquisitionResult(pages, rejected)

Brave's own behavior is untouched: same 5-result cap, same recency clamp, same cache,
same defaults. Only the `feature` label differs, so research traffic is separable from
chat and voice in provider telemetry.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from .models import (
    AcquisitionResult,
    PageReadRequest,
    PageReadResult,
    PageReadState,
    RejectedUrl,
    UrlRejectReason,
)
from .page_reader import PageReader
from .url_policy import evaluate_url

# Brave's own `_RESULT_COUNT` is 5, so this is a ceiling that matches, not a widening.
_MAX_PAGES = 5
# Hard ceiling on concurrent provider reads regardless of what settings says.
_MAX_CONCURRENCY = 3
_FEATURE = "research_acquire"


async def _read_one(
    reader: PageReader,
    url: str,
    *,
    semaphore: asyncio.Semaphore,
    timeout_s: float,
    max_chars: int,
    correlation_id: str,
    feature: str,
) -> PageReadResult:
    """One bounded read. Never raises: a failure becomes a stateful result.

    Per-item isolation matters more here than anywhere else in the pipeline, because
    one page raising inside `gather` would discard every sibling read already paid for.
    """
    request = PageReadRequest(
        url=url,
        timeout_s=timeout_s,
        max_chars=max_chars,
        correlation_id=correlation_id,
        feature=feature,
    )
    async with semaphore:
        try:
            result = await reader.read(request)
        except Exception as exc:
            logger.exception("research.acquire: reader raised", {"error_type": type(exc).__name__})
            return PageReadResult(
                requested_url=url,
                canonical_url=url,
                state=PageReadState.PROVIDER_ERROR,
                failure_reason="reader_exception",
            )

    if result.state is not PageReadState.OK:
        return result

    # Post-read redirect check. Structural only (resolve_dns=False): the provider has
    # demonstrably already reached this host, the credit is already spent, and a
    # transient DNS failure should not throw away content we hold. The checks that
    # matter for a redirect (scheme downgrade, embedded credentials, private literal,
    # reserved suffix, metadata host) all still run.
    landing = result.final_url or result.canonical_url
    verdict = await evaluate_url(landing, resolve_dns=False)
    if not verdict.allowed:
        logger.warn("research.acquire: redirect left the public web", {
            "reason": str(verdict.reason),
        })
        return PageReadResult(
            requested_url=result.requested_url,
            canonical_url=result.canonical_url,
            final_url=result.final_url,
            state=PageReadState.URL_NOT_ALLOWED,
            failure_reason=UrlRejectReason.REDIRECT_BLOCKED.value,
            status_code=result.status_code,
            credits_used=result.credits_used,
        )
    return result


async def acquire_research_sources(
    query: str,
    *,
    uid: str,
    recency: str = "any",
    max_pages: int = _MAX_PAGES,
    reader: PageReader | None = None,
    feature: str = _FEATURE,
    correlation_id: str = "",
) -> AcquisitionResult:
    """Discover up to `max_pages` public URLs for `query` and read each one.

    Raises ValueError only when BRAVE_API_KEY or FIRECRAWL_API_KEY is unset, matching
    the existing convention that a missing credential is a deploy bug a developer must
    see. Every other failure is recorded in the result and the run continues.
    """
    from ...agents.data_fetchers.brave_search import brave_search

    search: dict[str, Any] = await brave_search(
        query, uid=uid, recency=recency, feature=feature
    )
    sources: list[dict[str, str]] = list(search.get("sources") or [])

    allowed: list[str] = []
    rejected: list[RejectedUrl] = []
    seen: set[str] = set()
    budget = max(0, min(max_pages, _MAX_PAGES))

    for source in sources:
        raw = str(source.get("url") or "").strip()
        if not raw:
            continue
        verdict = await evaluate_url(raw)
        if not verdict.allowed:
            # A rejecting verdict always carries a reason; the fallback keeps the
            # closed enum total rather than letting a None reach the model.
            rejected.append(
                RejectedUrl(url=raw[:2048], reason=verdict.reason or UrlRejectReason.MALFORMED)
            )
            continue
        if verdict.canonical_url in seen:
            continue
        seen.add(verdict.canonical_url)
        if len(allowed) < budget:
            allowed.append(verdict.canonical_url)

    if reader is None:
        from .firecrawl_reader import FirecrawlPageReader

        reader = FirecrawlPageReader()

    semaphore = asyncio.Semaphore(max(1, min(settings.FIRECRAWL_CONCURRENCY, _MAX_CONCURRENCY)))
    pages: list[PageReadResult] = []
    if allowed:
        pages = list(
            await asyncio.gather(*[
                _read_one(
                    reader,
                    url,
                    semaphore=semaphore,
                    timeout_s=settings.FIRECRAWL_TIMEOUT_S,
                    max_chars=settings.FIRECRAWL_MAX_CHARS,
                    correlation_id=correlation_id,
                    feature=feature,
                )
                for url in allowed
            ])
        )

    logger.info("research.acquire: complete", {
        "source_count": len(sources),
        "allowed_count": len(allowed),
        "rejected_count": len(rejected),
        "ok_count": sum(1 for page in pages if page.state is PageReadState.OK),
        "brave_cached": bool(search.get("cached")),
    })

    return AcquisitionResult(
        query=query,
        search_sources=sources,
        pages=pages,
        rejected=rejected,
        brave_cached=bool(search.get("cached")),
    )
