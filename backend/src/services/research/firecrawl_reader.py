"""Firecrawl PageReader adapter: markdown and metadata only, no SDK.

Raw httpx on purpose. The backend has no shared HTTP client and no provider SDK for
any integration; every fetcher opens its own bounded AsyncClient per call. Adding the
Firecrawl SDK would buy nothing for a single POST and would pull a dependency whose
surface is far wider than the one endpoint used here.

Scope: `formats=["markdown"]` and `onlyMainContent`. No JSON extraction, highlights,
questions, enhanced proxy, browser actions, search, crawl, or agent endpoints. Aura
keeps claim extraction inside its own bounded, tool-free model stages rather than
paying provider credits for provider-side LLM work it would have to trust.

Two conventions copied from `agents/data_fetchers/brave_search.py`:
  - network, timeout, and non-200 failures degrade to a stateful result, never raise;
  - a missing API key RAISES, because that is a deploy misconfiguration.

NO TEXTUAL PROMPT-INJECTION FILTER IS APPLIED TO THE MARKDOWN HERE, deliberately.
`brave_search._strip_prompt_injection` matches three line-anchored shapes in search
snippets; extending that idea to page bodies would create confidence in a control that
cannot be made complete (normal-language instructions, non-English phrasing, base64,
homoglyphs, zero-width characters, instructions inside code blocks). The real defense
is architectural and belongs to the stage that consumes this markdown: no tools in the
loop, untrusted-document framing, and structured output as the only channel out.
Treat every byte of `PageReadResult.markdown` as hostile third-party input.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...config.settings import settings
from ...lib.logger import logger
from ..observability import log_provider_request
from .models import PageReadRequest, PageReadResult, PageReadState

# The HTTP timeout is per-request (`PageReadRequest.timeout_s`, defaulted by the caller
# from settings.FIRECRAWL_TIMEOUT_S) rather than a module constant, because a research
# stage's remaining wall clock, not this module, decides how long a read may take.
# Firecrawl's own `timeout` is the page-load budget. Keep it under our HTTP timeout so
# the provider answers with a 408 we can classify, rather than us timing out blind.
_PROVIDER_TIMEOUT_MARGIN_S = 5.0

# Checked in order; the first present, non-empty value wins. Firecrawl surfaces
# publication time under whichever key the page's own metadata used, and v2 documents
# none of them, so this stays tolerant rather than assuming one.
_PUBLISHED_KEYS = (
    "publishedTime",
    "article:published_time",
    "ogPublishedTime",
    "datePublished",
    "modifiedTime",
)
_CREDIT_KEYS = ("creditsUsed", "credits_used", "numCredits")

# Content types a markdown-only reader cannot do anything useful with.
_UNSUPPORTED_PREFIXES = ("image/", "audio/", "video/", "font/")


class _ProviderStatusError(Exception):
    """A non-200 from Firecrawl, raised out of the streaming reader for one handler."""

    def __init__(self, status_code: int, received_bytes: int) -> None:
        super().__init__(f"firecrawl status {status_code}")
        self.status_code = status_code
        self.received_bytes = received_bytes


def _looks_like_pdf(url: str) -> bool:
    """A URL that will bill per PDF page rather than per document.

    Deliberately a suffix check on the path and nothing cleverer. The honest alternative
    is a content-type probe, which is a second request against a host we are about to pay
    to read anyway, and the failure it would fix (a PDF served from an extensionless URL)
    is source-inspected as bounded by ``parsers: []`` in the request payload. Real
    Firecrawl billing and response behavior has not yet been observed for this case.
    """
    try:
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        return False
    return path.endswith(".pdf")


def _first_str(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _state_for_site_status(status: int | None) -> PageReadState | None:
    """Map the TARGET SITE's status (metadata.statusCode) to a read state.

    This is a different axis from the Firecrawl API's own HTTP status: a site that
    blocks us arrives as a Firecrawl 200 carrying metadata.statusCode 403.
    """
    # 3xx is not an override: Firecrawl follows redirects, so a redirect status here
    # describes a hop, not the outcome. Where it actually landed is checked separately
    # against the URL policy, and a redirect stub with no body falls out as empty
    # markdown rather than being misreported as a provider failure.
    if status is None or status < 400:
        return None
    if status in (401, 403):
        return PageReadState.BLOCKED
    if status == 402:
        return PageReadState.PAYWALLED
    if status in (404, 410):
        return PageReadState.NOT_FOUND
    if status == 408 or status == 504:
        return PageReadState.TIMEOUT
    if status == 429:
        return PageReadState.BLOCKED
    return PageReadState.PROVIDER_ERROR


class FirecrawlPageReader:
    """PageReader backed by Firecrawl's v2 scrape endpoint."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
        # Both injectable so a caller can point at a different API version without a
        # code change, and so the seam is testable without touching module state.
        self._base_url = base_url or settings.FIRECRAWL_BASE_URL
        self._api_key = api_key if api_key is not None else settings.FIRECRAWL_API_KEY

    def _fail(
        self,
        request: PageReadRequest,
        state: PageReadState,
        reason: str,
        *,
        status_code: int | None = None,
        received_bytes: int = 0,
        request_sent: bool = False,
    ) -> PageReadResult:
        return PageReadResult(
            requested_url=request.url,
            canonical_url=request.url,
            final_url="",
            state=state,
            failure_reason=reason,
            status_code=status_code,
            received_bytes=int(received_bytes),
            request_sent=request_sent,
        )

    async def read(self, request: PageReadRequest) -> PageReadResult:
        """Read one URL. Never raises except on a missing API key.

        The caller is responsible for having run `url_policy.evaluate_url` first; this
        adapter does not re-validate, so that the policy lives in exactly one place.
        """
        if not self._api_key:
            raise ValueError("FIRECRAWL_API_KEY not configured, page reading unavailable")

        provider_timeout_ms = int(
            max(request.timeout_s - _PROVIDER_TIMEOUT_MARGIN_S, 1.0) * 1000
        )
        payload: dict[str, Any] = {
            "url": request.url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "blockAds": True,
            "timeout": provider_timeout_ms,
            # Source inspection shows this disables provider-side document parsing. Real
            # Firecrawl billing and response behavior for parsers=[] has not been observed,
            # so URL, final-URL, and content-type PDF refusals remain mandatory.
            "parsers": [],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        # A PDF that the provider will not parse yields no markdown. Refusing it here sends
        # no request, consumes zero page credits, and produces the typed gap. The check is
        # on the URL only: a content-type probe would itself be a request, and a document
        # whose page count cannot be bounded must not be sent at all.
        if _looks_like_pdf(request.url):
            self._log(request, "unsupported", started)
            logger.info(
                "firecrawl read refused: PDF page credits cannot be bounded before spend",
                {"max_page_credits": request.max_page_credits},
            )
            return self._fail(
                request, PageReadState.UNSUPPORTED, "pdf_page_credits_unbounded"
            )

        try:
            raw, received, over_limit = await self._read_body(request, payload, headers)
        except httpx.TimeoutException as exc:
            self._log(request, "timeout", started)
            logger.warn("firecrawl read timeout", {"error": str(exc)})
            return self._fail(request, PageReadState.TIMEOUT, "provider_timeout", request_sent=True)
        except httpx.HTTPError as exc:
            self._log(request, "network_error", started)
            logger.warn("firecrawl read failed", {"error": str(exc)})
            return self._fail(
                request,
                PageReadState.PROVIDER_ERROR,
                "network_error",
                request_sent=True,
            )
        except _ProviderStatusError as exc:
            return self._handle_provider_status(
                request, exc.status_code, started, exc.received_bytes
            )

        if over_limit:
            # The transport stopped at the ceiling, so this is a real pre-parse bound and
            # not a post-hoc trim. The partial body is discarded rather than salvaged: a
            # truncated JSON document is not evidence, and parsing one would be guessing.
            self._log(request, "too_large", started, status_code=200)
            logger.warn(
                "firecrawl response exceeded the granted byte ceiling, aborted",
                {"max_bytes": request.max_bytes, "received_bytes": received},
            )
            return PageReadResult(
                requested_url=request.url,
                canonical_url=request.url,
                state=PageReadState.TOO_LARGE,
                failure_reason="max_bytes_exceeded",
                received_bytes=received,
                request_sent=True,
            )

        try:
            body = json.loads(raw)
        except ValueError:
            self._log(request, "provider_error", started, status_code=200)
            logger.error("firecrawl unparseable response", {"status": 200})
            return self._fail(
                request, PageReadState.PROVIDER_ERROR, "unparseable_response",
                received_bytes=received, request_sent=True,
            )

        return self._parse_success(request, body, started, received_bytes=received)

    async def _read_body(
        self, request: PageReadRequest, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[bytes, int, bool]:
        """Stream the response, stopping at ``max_bytes``. Returns (body, received, over).

        Streaming rather than ``response.json()`` is the whole point: ``json()`` reads the
        entire body into memory before anything can look at its size, so a byte ceiling
        applied afterwards bounds what is KEPT and not what is TRANSFERRED. Aborting the
        iteration closes the connection at the limit, which is the only version of this
        check that can honestly be called pre-fetch bounded.
        """
        chunks: list[bytes] = []
        received = 0
        limit = int(request.max_bytes)
        async with httpx.AsyncClient(timeout=request.timeout_s) as client:
            async with client.stream(
                "POST", self._base_url, json=payload, headers=headers
            ) as response:
                if response.status_code != 200:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > limit:
                            break
                    raise _ProviderStatusError(response.status_code, received)
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > limit:
                        return b"", received, True
                    chunks.append(chunk)
        return b"".join(chunks), received, False

    def _handle_provider_status(
        self, request: PageReadRequest, status: int, started: float, received_bytes: int
    ) -> PageReadResult:
        """Non-200 from Firecrawl is about OUR account, not the page.

        401/403 = our key. 402 = our credits. 429 = our rate limit. None of these means
        the target site refused us, so none of them maps to BLOCKED or PAYWALLED.
        """
        if status == 402:
            # Loud on purpose: silent credit exhaustion looks identical to "no sources
            # found", which is exactly the zero-rows-versus-healthy confusion to avoid.
            logger.error("firecrawl credits exhausted", {"status": status})
            outcome = "provider_error"
            reason = "credits_exhausted"
        elif status == 429:
            outcome = "rate_limited"
            reason = "rate_limited"
        elif status in (401, 403):
            logger.error("firecrawl auth rejected", {"status": status})
            outcome = "provider_error"
            reason = "provider_auth_failed"
        elif status == 408:
            self._log(request, "timeout", started, status_code=status)
            return self._fail(
                request, PageReadState.TIMEOUT, "provider_page_timeout",
                status_code=status, received_bytes=received_bytes, request_sent=True,
            )
        else:
            outcome = "provider_error"
            reason = "provider_error"
            logger.warn("firecrawl non-200", {"status": status})

        self._log(request, outcome, started, status_code=status)
        return self._fail(
            request, PageReadState.PROVIDER_ERROR, reason, status_code=status,
            received_bytes=received_bytes, request_sent=True,
        )

    def _parse_success(
        self,
        request: PageReadRequest,
        body: dict[str, Any],
        started: float,
        *,
        received_bytes: int = 0,
    ) -> PageReadResult:
        raw_data = body.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        raw_metadata = data.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}

        site_status = _first_int(metadata, ("statusCode", "status_code"))
        final_url = _first_str(metadata, ("sourceURL", "url")) or request.url
        content_type = _first_str(metadata, ("contentType", "content_type"))
        credits = _first_int(body, _CREDIT_KEYS) or _first_int(metadata, _CREDIT_KEYS)

        base_fields: dict[str, Any] = {
            "requested_url": request.url,
            "canonical_url": request.url,
            "final_url": final_url[:2048],
            "title": _first_str(metadata, ("title", "ogTitle"))[:512],
            "published_at": _first_str(metadata, _PUBLISHED_KEYS)[:64],
            "content_type": content_type[:128],
            "language": _first_str(metadata, ("language", "ogLocale"))[:32],
            "credits_used": credits,
            "status_code": site_status,
            # Carried onto EVERY outcome, not just OK: a blocked or paywalled response
            # still crossed the wire and still has to be metered, or the byte ledger
            # under-counts exactly the reads that produced no evidence.
            "received_bytes": int(received_bytes),
            "request_sent": True,
        }

        if body.get("success") is False:
            self._log(request, "provider_error", started, status_code=200)
            error = str(body.get("error") or "")[:64]
            logger.warn("firecrawl success=false", {"detail": error})
            return PageReadResult(
                **base_fields, state=PageReadState.PROVIDER_ERROR,
                failure_reason=error or "provider_reported_failure",
            )

        site_state = _state_for_site_status(site_status)
        if site_state is not None:
            self._log(request, "success", started, status_code=200)
            return PageReadResult(
                **base_fields, state=site_state, failure_reason=f"site_status_{site_status}"
            )

        normalized_type = content_type.lower().split(";", 1)[0].strip()
        if _looks_like_pdf(final_url) or normalized_type == "application/pdf":
            self._log(request, "success", started, status_code=200)
            return PageReadResult(
                **base_fields, state=PageReadState.UNSUPPORTED, failure_reason="pdf_content"
            )

        if normalized_type.startswith(_UNSUPPORTED_PREFIXES):
            self._log(request, "success", started, status_code=200)
            return PageReadResult(
                **base_fields, state=PageReadState.UNSUPPORTED, failure_reason="content_type"
            )

        markdown = data.get("markdown")
        markdown = markdown if isinstance(markdown, str) else ""
        if not markdown.strip():
            # A 200 with no body is not an error, but it is not evidence either.
            self._log(request, "success", started, status_code=200)
            return PageReadResult(
                **base_fields, state=PageReadState.UNSUPPORTED, failure_reason="empty_markdown"
            )

        truncated = len(markdown) > request.max_chars
        if truncated:
            markdown = markdown[: request.max_chars]

        self._log(request, "success", started, status_code=200, billable=True)
        return PageReadResult(
            **base_fields,
            state=PageReadState.OK,
            markdown=markdown,
            char_count=len(markdown),
            truncated=truncated,
            # Hashes the CLAMPED text, so the hash always describes what was kept.
            content_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )

    def _log(
        self,
        request: PageReadRequest,
        outcome: str,
        started: float,
        *,
        status_code: int | None = None,
        billable: bool = False,
    ) -> None:
        log_provider_request(
            provider="firecrawl",
            operation="scrape",
            feature=request.feature,
            outcome=outcome,
            billable=billable,
            latency_ms=int((time.monotonic() - started) * 1000),
            status_code=status_code,
        )
