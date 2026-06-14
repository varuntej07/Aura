"""
ModelProvider — unified LLM interface with tier-based routing.

Usage:
    provider = ModelProvider()

    # Cheap + fast (Gemini Flash) — notification copy, classification, summaries
    text = await provider.cheap("Write a punchy notification about sardines")

    # Mid-tier (Claude Haiku) — tool-calling tasks, structured output with reasoning
    result = await provider.balanced("Classify this query", response_model=MyModel)

    # Full reasoning (Claude Sonnet) — main chat, complex multi-turn
    result = await provider.expert("...", tools=[...], history=[...])

Model IDs come from settings.TIER_CHEAP / TIER_BALANCED / TIER_EXPERT.
To upgrade a tier: change ONE line in settings.py — zero call-site changes.

Provider routing is inferred from the model ID prefix:
    "gemini-*"  → Google Gen AI SDK
    "claude-*"  → Anthropic SDK
    (future) "gpt-*" → OpenAI SDK, "sonar-*" → Perplexity SDK
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
from langfuse import observe

from ..config.settings import settings
from ..lib.logger import logger

T = TypeVar("T")

_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0           # Anthropic backoff: 1s, 2s, 4s
_GEMINI_BASE_DELAY_S = 5.0    # Gemini backoff: 5s, 10s, 20s — background tasks, 503s need time to clear
_TIMEOUT_S = 30.0             # per-call budget for background LLM work
_GROUNDED_TIMEOUT_S = 45.0    # grounded search+synthesis runs longer (server-side search) than a plain call
_REASON_TIMEOUT_S = 90.0      # deep reasoning (Opus + adaptive thinking) runs long; streamed

# Anthropic exceptions that are worth retrying (transient / server-side)
_ANTHROPIC_RETRYABLE = (
    anthropic.RateLimitError,        # 429
    anthropic.APIConnectionError,    # network blip (includes APITimeoutError)
    anthropic.InternalServerError,   # 500 / 529
)

# Model ID prefix -> provider name
_PROVIDER_PREFIXES: dict[str, str] = {
    "gemini": "gemini",
    "claude": "anthropic",
    "gpt": "openai",       # future
    "sonar": "perplexity",   # future
    "o1": "openai",       # future
    "o3": "openai",       # future
}


def _infer_provider(model_id: str) -> str:
    for prefix, provider in _PROVIDER_PREFIXES.items():
        if model_id.startswith(prefix):
            return provider
    raise ValueError(
        f"ModelProvider: cannot infer provider for model '{model_id}'. "
        f"Add its prefix to _PROVIDER_PREFIXES in model_provider.py."
    )


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    return text.strip()


def is_quota_exhausted(exc: BaseException) -> bool:
    """True when an LLM or embedding call failed because the provider's quota or
    prepaid credits are exhausted (HTTP 429 / RESOURCE_EXHAUSTED), as opposed to a
    generic transient error. The signal-engine fail-loud guards use this to log a
    depleted-billing outage in plain terms instead of letting it look healthy.

    Mirrors the inline 429 / RESOURCE_EXHAUSTED checks in _call_gemini and the
    embedder; centralised here so the notification pipeline has one definition."""
    if getattr(exc, "code", None) == 429:
        return True
    if isinstance(exc, anthropic.RateLimitError):
        return True
    return "RESOURCE_EXHAUSTED" in str(exc).upper()


@dataclass(frozen=True)
class GroundedResult:
    """Return value of ``ModelProvider.grounded``. ``sources`` are the grounded web
    references ({title, url}); ``supports`` map text spans the model wrote to the
    source indices that ground them ({text, source_indices}), which lets a caller
    attach a per-sentence / per-item citation without any byte-offset math."""

    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    supports: list[dict[str, Any]] = field(default_factory=list)


def _extract_grounding_supports(resp: Any) -> list[dict[str, Any]]:
    """Pull grounding supports off a Gemini grounding response: for each supported
    span, its text and the indices (into ``grounding_chunks``) that ground it. Fully
    defensive — any missing field or shape change yields an empty list, never raises,
    so a metadata change degrades citations to "none" rather than breaking a briefing."""
    supports: list[dict[str, Any]] = []
    try:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            return supports
        meta = getattr(candidates[0], "grounding_metadata", None)
        raw = (getattr(meta, "grounding_supports", None) or []) if meta else []
        for item in raw:
            seg = getattr(item, "segment", None)
            seg_text = (getattr(seg, "text", "") or "").strip() if seg else ""
            idxs = getattr(item, "grounding_chunk_indices", None) or []
            clean = [i for i in idxs if isinstance(i, int) and i >= 0]
            if seg_text and clean:
                supports.append({"text": seg_text, "source_indices": clean})
    except Exception:
        return supports
    return supports


def _extract_grounding_sources(resp: Any) -> list[dict[str, str]]:
    """Pull the grounded web references off a Gemini grounding response: ONE entry per
    grounding chunk, IN ORDER, no de-dup and no skipping. Keeping it 1:1 with
    ``grounding_chunks`` is deliberate, a grounding support's ``grounding_chunk_indices``
    are indices into this same list, so preserving order/length is what keeps per-item
    citations pointing at the right source. Each entry is ``{title, url}``; ``url`` may
    be empty for a chunk with no web uri (kept as a placeholder so the indexing holds).
    The uri is usually a Google redirect that resolves to the publisher on tap. Fully
    defensive: any missing field or shape change yields an empty list rather than raising.
    """
    sources: list[dict[str, str]] = []
    try:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            return sources
        meta = getattr(candidates[0], "grounding_metadata", None)
        chunks = (getattr(meta, "grounding_chunks", None) or []) if meta else []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            uri = (getattr(web, "uri", "") or "").strip() if web else ""
            title = (getattr(web, "title", "") or "").strip() if web else ""
            sources.append({"title": title or uri, "url": uri})
    except Exception:
        return sources
    return sources


class ModelProvider:
    """
    Tier-based LLM interface. Three tiers, any number of underlying models.

    cheap() -> settings.TIER_CHEAP (currently gemini-2.5-flash)
    balanced() -> settings.TIER_BALANCED (currently claude-haiku-4-5)
    expert() -> settings.TIER_EXPERT (currently claude-sonnet-4-6)

    When response_model (a Pydantic BaseModel subclass) is given, the raw LLM
    text is parsed as JSON into that model and returned as the typed instance.
    Otherwise, the raw string is returned.
    """

    def __init__(self) -> None:
        self._anthropic: anthropic.AsyncAnthropic | None = None
        self._gemini_client: Any = None   # google.genai.Client, lazy

    async def cheap(
        self,
        prompt: str,
        *,
        system: str | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.7,
    ) -> str | T:
        """Cheap and fast. Use for: notification copy, summaries, classification.
        Currently routes to Gemini Flash via TIER_CHEAP setting."""
        model_id = settings.TIER_CHEAP
        logger.debug("ModelProvider.cheap", {"model": model_id, "prompt_len": len(prompt)})
        return await self._call(
            model_id=model_id,
            fallback_chain=[settings.TIER_CHEAP_FALLBACK, settings.TIER_CHEAP_LAST_RESORT],
            prompt=prompt,
            system=system,
            response_model=response_model,
            temperature=temperature,
        )

    async def balanced(
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.5,
    ) -> str | T:
        """Mid-tier reasoning. Use for: tool-calling background tasks, structured
        output that needs mild reasoning. Currently routes to Claude Haiku."""
        model_id = settings.TIER_BALANCED
        logger.debug("ModelProvider.balanced", {"model": model_id, "prompt_len": len(prompt)})
        return await self._call(
            model_id=model_id,
            prompt=prompt,
            system=system,
            tools=tools,
            response_model=response_model,
            temperature=temperature,
        )

    async def expert(
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        history: list[dict] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.7,
    ) -> str | T:
        """Full reasoning. Use for: main chat, complex multi-turn, high-stakes output.
        Most expensive — only use where quality matters. Currently Claude Sonnet."""
        model_id = settings.TIER_EXPERT
        logger.debug("ModelProvider.expert", {"model": model_id, "prompt_len": len(prompt)})
        return await self._call(
            model_id=model_id,
            prompt=prompt,
            system=system,
            tools=tools,
            history=history,
            response_model=response_model,
            temperature=temperature,
        )

    async def grounded(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
    ) -> GroundedResult:
        """Gemini with Google Search grounding: ONE call does live web search +
        synthesis. Returns a :class:`GroundedResult` (text + grounded ``sources``,
        each ``{"title", "url"}``, + ``supports`` mapping spans to source indices).

        This is the DELIBERATE, non-real-time path. Grounding searches and
        synthesizes server-side (seconds of latency) and carries a premium
        per-call grounding fee, so it is intentionally kept off the latency-critical
        chat/voice path — that uses Brave (``tool_executor.web_surf``, ~1s). Use this
        only where fresh web facts matter and a few seconds is acceptable (the
        on-demand world briefing). Grounding and a forced JSON ``response_model`` are
        mutually exclusive in the google-genai SDK, so this returns free text plus
        metadata, never a parsed model — the caller parses the text itself.
        """
        model_id = settings.TIER_GROUNDED
        logger.debug("ModelProvider.grounded", {"model": model_id, "prompt_len": len(prompt)})
        return await self._call_gemini_grounded(
            model_id=model_id,
            prompt=prompt,
            system=system,
            temperature=temperature,
        )

    @observe(name="anthropic_reason_turn")
    async def reason_turn(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> Any:
        """One streamed turn on the reasoning model (Sonnet, REASON_STEP_MODEL),
        returning the RAW final message with tool_use blocks intact so the caller can
        drive its own tool loop (the reason_step funnel). Unlike balanced()/expert(),
        this does not flatten to text. Plain tool-use turn: no thinking, no temperature
        — the step discipline lives in the system prompt, not in extended reasoning.
        Streams so a longer turn never trips the SDK HTTP timeout."""
        model_id = settings.REASON_STEP_MODEL
        client = self._get_anthropic_client().with_options(timeout=_REASON_TIMEOUT_S)
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": 4000,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        logger.debug("ModelProvider.reason_turn", {"model": model_id, "turns": len(messages)})

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with client.messages.stream(**kwargs) as stream:
                    return await stream.get_final_message()
            except _ANTHROPIC_RETRYABLE as exc:
                if attempt == _MAX_RETRIES:
                    logger.exception("ModelProvider: reason_turn() failed after retries", {
                        "model": model_id,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    raise
                delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warn("ModelProvider: reason_turn() retryable error, backing off", {
                    "model": model_id,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                    "error_type": type(exc).__name__,
                })
                await asyncio.sleep(delay)
        # retry loop always returns or raises; this line is unreachable
        raise RuntimeError("ModelProvider: reason_turn() retry loop exited unexpectedly")

    async def _call(
        self,
        *,
        model_id: str,
        fallback_chain: list[str] | None = None,
        prompt: str,
        system: str | None,
        tools: list[dict] | None = None,
        history: list[dict] | None = None,
        response_model: type[T] | None,
        temperature: float,
    ) -> str | T:
        provider = _infer_provider(model_id)
        chain = list(fallback_chain or [])

        if provider == "gemini":
            raw = await self._call_gemini(
                model_id=model_id,
                fallback_chain=chain,
                prompt=prompt,
                system=system,
                temperature=temperature,
            )
        elif provider == "anthropic":
            raw = await self._call_anthropic(
                model_id=model_id,
                prompt=prompt,
                system=system,
                tools=tools,
                history=history,
                temperature=temperature,
            )
        else:
            raise NotImplementedError(
                f"ModelProvider: provider '{provider}' is not yet implemented. "
                f"Add a _call_{provider}() method to model_provider.py."
            )

        if response_model is not None:
            return self._parse_response(raw, response_model)
        return raw

    @observe(name="gemini_call")
    async def _call_gemini(
        self,
        *,
        model_id: str,
        fallback_chain: list[str],
        prompt: str,
        system: str | None,
        temperature: float,
    ) -> str:
        client = self._get_gemini_client()
        from google.genai import types  # type: ignore

        contents: list = []
        if system:
            # Gemini: system instruction goes in GenerateContentConfig, not contents
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                max_output_tokens=4096,
            )
        else:
            config = types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=4096,
            )

        contents.append(prompt)

        def _sync() -> str:
            resp = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=config,
            )
            return resp.text or ""

        async def _use_next_in_chain(reason: str, log_extra: dict) -> str:
            next_model = fallback_chain[0]
            logger.warn(f"ModelProvider: {reason} — falling back", {
                "from_model": model_id,
                "to_model": next_model,
                **log_extra,
            })
            return await self._call(
                model_id=next_model,
                fallback_chain=fallback_chain[1:],
                prompt=prompt,
                system=system,
                response_model=None,
                temperature=temperature,
            )

        last_exc: BaseException | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                result = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=_TIMEOUT_S)
                if attempt > 1:
                    # One visible line per call when retries eventually succeeded, so a
                    # recovered-after-transient-error call isn't completely silent.
                    logger.warn("ModelProvider: Gemini recovered after retries", {
                        "model": model_id,
                        "attempts": attempt,
                        "last_error": str(last_exc)[:300] if last_exc else None,
                    })
                return result
            except TimeoutError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    if fallback_chain:
                        return await _use_next_in_chain(
                            "timeout after retries",
                            {"attempt": attempt, "timeout_s": _TIMEOUT_S},
                        )
                    logger.exception("ModelProvider: Gemini timeout after retries, no fallback left", {
                        "model": model_id,
                        "attempt": attempt,
                        "timeout_s": _TIMEOUT_S,
                    })
                    raise
                delay = _GEMINI_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
                # Per-attempt backoff is DEBUG to keep prod logs to one line per call;
                # the resolution path (recovery / fallback / terminal) carries the visible line.
                logger.debug("ModelProvider: Gemini timeout, backing off", {
                    "model": model_id,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                    "timeout_s": _TIMEOUT_S,
                })
                await asyncio.sleep(delay)
            except Exception as exc:
                last_exc = exc
                # google-genai raises APIError subclasses with an HTTP `.code` attribute.
                # When the SDK wraps a gRPC error, `.code` may be a gRPC StatusCode enum (e.g. UNAVAILABLE=14) rather than the integer 503,
                # so also check the error string for known transient gRPC status names.
                code = getattr(exc, "code", None)
                error_str = str(exc).upper()
                is_model_unavailable = code == 404 or "NOT_FOUND" in error_str
                retryable = (
                    code == 429
                    or (isinstance(code, int) and 500 <= code < 600)
                    or "UNAVAILABLE" in error_str
                    or "RESOURCE_EXHAUSTED" in error_str
                )
                if is_model_unavailable:
                    # Model doesn't exist for this account — retrying the same model is pointless
                    if fallback_chain:
                        return await _use_next_in_chain(
                            "model unavailable (404)",
                            {"code": code, "error": str(exc)[:120]},
                        )
                    logger.exception("ModelProvider: Gemini model unavailable, no fallback left", {
                        "model": model_id,
                        "code": code,
                        "error": str(exc),
                    })
                    raise
                if not retryable or attempt == _MAX_RETRIES:
                    # Resolution point for this model — make a depleted-credits outage scream
                    # in plain terms instead of hiding in a wall of identical backoff lines.
                    if is_quota_exhausted(exc):
                        logger.error(
                            "ModelProvider: Gemini quota/credits EXHAUSTED (429 RESOURCE_EXHAUSTED) — "
                            "background LLM work is failing. Check GEMINI_API_KEY billing at "
                            "https://ai.studio/projects.",
                            {
                                "model": model_id,
                                "attempt": attempt,
                                "code": code,
                                "error": str(exc)[:300],
                            },
                        )
                    if fallback_chain:
                        reason = "retries exhausted" if attempt == _MAX_RETRIES else "non-retryable error"
                        return await _use_next_in_chain(
                            reason,
                            {
                                "attempt": attempt,
                                "code": code,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:300],
                            },
                        )
                    logger.exception("ModelProvider: Gemini call failed, no fallback left", {
                        "model": model_id,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "code": code,
                        "error": str(exc),
                    })
                    raise
                delay = _GEMINI_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
                # Per-attempt backoff is DEBUG (full error preserved for LOG_LEVEL=DEBUG);
                # the resolution path emits the single visible line for this call.
                logger.debug("ModelProvider: Gemini retryable error, backing off", {
                    "model": model_id,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                    "error_type": type(exc).__name__,
                    "code": code,
                    "error": str(exc)[:300],
                })
                await asyncio.sleep(delay)
        # retry loop always returns or raises; this line is unreachable
        raise RuntimeError("ModelProvider: Gemini retry loop exited unexpectedly")

    @observe(name="gemini_grounded_call")
    async def _call_gemini_grounded(
        self,
        *,
        model_id: str,
        prompt: str,
        system: str | None,
        temperature: float,
    ) -> GroundedResult:
        client = self._get_gemini_client()
        from google.genai import types  # type: ignore

        # The Google Search grounding tool is what makes this call live: Gemini issues
        # its own web searches and synthesizes from the results. No response_schema —
        # grounding and forced JSON output cannot be combined in the SDK.
        search_tool = types.Tool(google_search=types.GoogleSearch())
        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": 4096,
            "tools": [search_tool],
        }
        if system:
            config_kwargs["system_instruction"] = system
        config = types.GenerateContentConfig(**config_kwargs)

        def _sync() -> GroundedResult:
            resp = client.models.generate_content(
                model=model_id,
                contents=[prompt],
                config=config,
            )
            return GroundedResult(
                text=(resp.text or ""),
                sources=_extract_grounding_sources(resp),
                supports=_extract_grounding_supports(resp),
            )

        last_exc: BaseException | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_sync), timeout=_GROUNDED_TIMEOUT_S
                )
            except TimeoutError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    logger.exception("ModelProvider: grounded() timeout after retries", {
                        "model": model_id,
                        "timeout_s": _GROUNDED_TIMEOUT_S,
                    })
                    raise
            except Exception as exc:
                last_exc = exc
                # Same transient classification as _call_gemini. No fallback chain: a
                # grounded call must run on a grounding-capable model, and silently
                # falling back to one without grounding would drop the live search.
                code = getattr(exc, "code", None)
                error_str = str(exc).upper()
                retryable = (
                    code == 429
                    or (isinstance(code, int) and 500 <= code < 600)
                    or "UNAVAILABLE" in error_str
                    or "RESOURCE_EXHAUSTED" in error_str
                )
                if not retryable or attempt == _MAX_RETRIES:
                    if is_quota_exhausted(exc):
                        logger.error(
                            "ModelProvider: Gemini grounding quota/credits EXHAUSTED "
                            "(429 RESOURCE_EXHAUSTED) — the world briefing is failing. "
                            "Check GEMINI_API_KEY billing at https://ai.studio/projects.",
                            {"model": model_id, "code": code, "error": str(exc)[:300]},
                        )
                    logger.exception("ModelProvider: grounded() call failed", {
                        "model": model_id,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "code": code,
                        "error": str(exc)[:300],
                    })
                    raise
            delay = _GEMINI_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
            logger.debug("ModelProvider: grounded() retryable error, backing off", {
                "model": model_id,
                "attempt": attempt,
                "delay_s": round(delay, 2),
                "error": str(last_exc)[:300],
            })
            await asyncio.sleep(delay)
        # retry loop always returns or raises; this line is unreachable
        raise RuntimeError("ModelProvider: grounded() retry loop exited unexpectedly")

    @observe(name="anthropic_call")
    async def _call_anthropic(
        self,
        *,
        model_id: str,
        prompt: str,
        system: str | None,
        tools: list[dict] | None,
        history: list[dict] | None,
        temperature: float,
    ) -> str:
        client = self._get_anthropic_client()

        messages: list[dict] = []
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": 2048,
            "messages": messages,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    client.messages.create(**kwargs),
                    timeout=_TIMEOUT_S,
                )
                text_blocks = [b.text for b in response.content if b.type == "text"]
                return " ".join(text_blocks).strip()
            except TimeoutError:
                if attempt == _MAX_RETRIES:
                    logger.exception("ModelProvider: Anthropic timeout after retries", {
                        "model": model_id,
                        "prompt_len": len(prompt),
                        "attempt": attempt,
                        "timeout_s": _TIMEOUT_S,
                    })
                    raise
                delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warn("ModelProvider: Anthropic timeout, backing off", {
                    "model": model_id,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                })
                await asyncio.sleep(delay)
            except _ANTHROPIC_RETRYABLE as exc:
                if attempt == _MAX_RETRIES:
                    logger.exception("ModelProvider: Anthropic call failed after retries", {
                        "model": model_id,
                        "prompt_len": len(prompt),
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    raise
                delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warn("ModelProvider: Anthropic retryable error, backing off", {
                    "model": model_id,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                await asyncio.sleep(delay)
        # retry loop always returns or raises; this line is unreachable
        raise RuntimeError("ModelProvider: Anthropic retry loop exited unexpectedly")

    def _parse_response(self, raw: str, response_model: type[T]) -> T:
        """Parse raw LLM text into a Pydantic model. Strips markdown fences."""
        cleaned = _strip_fences(raw)
        try:
            return response_model.model_validate_json(cleaned)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("ModelProvider: failed to parse LLM response", {
                "model": response_model.__name__,
                "error": str(exc),
                "raw_preview": cleaned[:200],
            })
            raise ValueError(
                f"ModelProvider: could not parse response into {response_model.__name__}: {exc}"
            ) from exc

    def _get_anthropic_client(self) -> anthropic.AsyncAnthropic:
        if self._anthropic is None:
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=_TIMEOUT_S,
            )
        return self._anthropic

    def _get_gemini_client(self) -> Any:
        if self._gemini_client is None:
            if not settings.GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY is not set — cheap() tier unavailable")
            from google import genai  # type: ignore
            self._gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._gemini_client


#  Module-level singleton
_provider: ModelProvider | None = None


def get_model_provider() -> ModelProvider:
    """Return the shared ModelProvider singleton. Thread-safe for read access."""
    global _provider
    if _provider is None:
        _provider = ModelProvider()
    return _provider
