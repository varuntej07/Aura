"""Thin wrapper over the Gemini Batch API.

Everything here is blocking SDK work dispatched via ``asyncio.to_thread``.

**JSONL file mode only.** The API also accepts inline requests, but inline
results come back in ``dest.inlined_responses`` ordered by submission, whereas
file results carry the caller-defined ``key`` on every line. Key-based mapping is
the documented way to know which output belongs to which request; relying on list
position instead would make a silent mis-attribution — one user's story delivered
into another user's issue — a one-off-error away. One code path, and it sidesteps
the 20MB inline ceiling for free.

Nothing in this module knows what a story is. It moves opaque request dicts out
and opaque response text back, keyed by string.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ...lib.logger import logger
from ...config.settings import settings
from . import models as m

_client: Any = None


def _genai_client() -> Any:
    """Lazily build the google-genai client.

    The key check is not decoration: without it a missing ``GEMINI_API_KEY`` is
    passed to the SDK as ``None`` and surfaces as an opaque provider error inside
    :func:`submit`'s broad except, logged as a submit failure and retried every
    tick forever. Same guard and message shape as
    ``model_provider._get_gemini_client`` (`:1752-1758`).
    """
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not set — llm_batch unavailable")
        from google import genai  # type: ignore

        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def build_request(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    max_output_tokens: int,
    temperature: float = 0.8,
    response_mime_type: str | None = None,
    response_schema: Any = None,
) -> dict[str, Any]:
    """One GenerateContentRequest as a plain dict, ready for a JSONL line.

    ``max_output_tokens`` is REQUIRED rather than defaulted. The interactive legs
    of model_provider fall back to 2048/4096 when it is omitted, and a long-form
    generation silently truncated at that ceiling is the expensive failure this
    whole pipeline exists to avoid — the truncation surfaces as a parse error
    after the tokens are already paid for.

    **The batch FILE shape is not the inline shape, and not quite REST either.**
    Established by bisecting four variants in one live job:

    ===========================================  ======
    variant                                      result
    ===========================================  ======
    ``contents`` + ``generationConfig``          OK
    ``+ systemInstruction`` (top level, parts)   OK
    ``+ generationConfig.thinkingConfig``        **REJECTED**
    ``config`` instead of ``generationConfig``   **REJECTED at upload**
    ===========================================  ======

    So: ``generationConfig`` (not ``config`` — that is what ``types.InlinedRequest``
    uses, and it is the INLINE shape), ``systemInstruction`` at top level as
    ``{"parts": [...]}``, and **no thinking config at all** — the batch path
    rejects it, which means ``thinkingLevel`` cannot be lowered here the way the
    interactive leg lowers it.

    **Consequence: reasoning runs at the model's default and is NOT controllable.**
    Gemini 3 always thinks, and thinking tokens are billed as output AND count
    against ``maxOutputTokens``. Measured on real batch responses: 423 thinking
    tokens for 47 visible, 541 for 50. So ``max_output_tokens`` must carry
    generous headroom beyond the prose you actually want, or the response returns
    truncated (which :func:`parse_result_line` now reports rather than passing off
    as finished).

    A rejected request produces a job that reports SUCCEEDED with an error object
    on every line — which is why per-line type checking is not optional.
    """
    from ..model_provider import (
        _GEMINI_3_TEMPERATURE,
        _gemini_json_schema,
        _is_gemini_3,
    )

    config: dict[str, Any] = {"maxOutputTokens": int(max_output_tokens)}
    # Gemini 3 is documented to run at temperature 1.0 and to degrade below it,
    # so a caller's lower value is overridden for 3.x only — same rule, and the
    # same constant, the interactive leg uses (`model_provider.py:1175-1181`).
    config["temperature"] = (
        _GEMINI_3_TEMPERATURE if _is_gemini_3(model) else float(temperature)
    )

    if response_mime_type:
        config["responseMimeType"] = response_mime_type
    if response_schema is not None:
        # `responseJsonSchema`, NOT `responseSchema`: the latter accepts only
        # Google's narrower OpenAPI subset and rejects Pydantic's
        # `additionalProperties`, so handing it a `model_json_schema()` fails —
        # and in batch that failure arrives as a job reporting SUCCEEDED with an
        # error object on every line. `_gemini_json_schema` strips the keywords
        # Gemini rejects. Both borrowed from `model_provider.py:1188-1200` rather
        # than re-derived; that module is the single entry point for LLM tiers and
        # this is the one place batch has to duplicate a call path.
        config["responseJsonSchema"] = _gemini_json_schema(response_schema)

    request: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config,
    }
    if system:
        request["systemInstruction"] = {"parts": [{"text": system}]}
    return request


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ResultLine:
    """One parsed line of a batch result file."""

    key: str
    text: str = ""
    error: str = ""
    finish_reason: str = ""
    # Captured per line because a batch job reports no usage of its own. Without
    # these the audit block on a generated artifact can only ever record zero
    # spend — on the one pipeline whose entire justification is the 50% batch
    # discount.
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "MAX_TOKENS"

    @property
    def billed_output_tokens(self) -> int:
        """Output tokens as the provider bills them.

        Gemini bills thinking as output, and it is NOT included in
        ``candidatesTokenCount`` — measured at ~0.55x visible output on real story
        generations, so omitting it understates spend by more than a third.
        """
        return self.output_tokens + self.thinking_tokens


@dataclass
class PollResult:
    state: str = m.JOB_RUNNING
    failed_count: int = 0
    succeeded_count: int = 0
    error: str = ""
    result_file_name: str = ""
    reachable: bool = True
    """False when the poll itself failed (network, auth, quota).

    Distinguishing "the job failed" from "we could not ask" is what makes a poll
    failure retryable instead of terminal — the same distinction
    threads/sensitivity.py draws between a verdict and an outage.
    """


def _usage(payload: Any) -> tuple[int, int, int]:
    """``(input, visible_output, thinking)`` token counts from ``usageMetadata``.

    ``thoughtsTokenCount`` is reported separately from ``candidatesTokenCount``
    and is billed as output, so a cost record that reads only the latter
    understates real spend by roughly a third at story length.
    """
    if not isinstance(payload, dict):
        return 0, 0, 0
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        nested = payload.get("response")
        meta = nested.get("usageMetadata") if isinstance(nested, dict) else None
    if not isinstance(meta, dict):
        return 0, 0, 0

    def _count(*names: str) -> int:
        for name in names:
            value = meta.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    return (
        _count("promptTokenCount", "prompt_token_count"),
        _count("candidatesTokenCount", "candidates_token_count"),
        _count("thoughtsTokenCount", "thoughts_token_count"),
    )


def _finish_reason(payload: Any) -> str:
    """First candidate's finishReason, or "" when absent.

    Matters because a response cut off at ``maxOutputTokens`` still arrives as a
    perfectly well-formed success with partial text. Without this check a story
    truncated mid-sentence is indistinguishable from a finished one — the live
    smoke returned "Vast and untamed," as a clean result.
    """
    if not isinstance(payload, dict):
        return ""
    nested = payload.get("response")
    if isinstance(nested, dict) and "candidates" not in payload:
        return _finish_reason(nested)
    for candidate in payload.get("candidates") or []:
        if isinstance(candidate, dict):
            reason = candidate.get("finishReason") or candidate.get("finish_reason")
            if reason:
                return str(reason).strip().upper()
    return ""


def _extract_text(payload: Any) -> str:
    """Pull the response text out of a GenerateContentResponse-shaped dict.

    Written defensively: the exact envelope differs between inline and file
    results and across SDK versions, so this walks the shapes it may see rather
    than indexing blindly into one of them.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    candidates = payload.get("candidates") or []
    chunks: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    # Some shapes nest the whole response one level down.
    nested = payload.get("response")
    if isinstance(nested, dict):
        return _extract_text(nested)
    return ""


def parse_result_line(raw: str) -> ResultLine | None:
    """Parse one JSONL result line.

    Per the API docs each line is EITHER a GenerateContentResponse OR a status
    object describing an error, so the type is checked before the content is
    used. A line we cannot parse or cannot key is dropped with a log rather than
    aborting the whole job's dispatch — one malformed line must not cost every
    other user in the batch their issue.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warn("llm_batch.client: unparseable result line", {"sample": raw[:200]})
        return None
    if not isinstance(data, dict):
        return None

    key = str(data.get("key") or "")
    if not key:
        logger.warn("llm_batch.client: result line has no key", {"sample": raw[:200]})
        return None

    # Error shapes: a top-level `error`, or a `status` object.
    err = data.get("error") or data.get("status")
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("code") or "unknown provider error")
        return ResultLine(key=key, error=message[:500])

    payload = data.get("response") if "response" in data else data
    text = _extract_text(payload)
    reason = _finish_reason(payload)
    usage = _usage(payload)

    if not text.strip():
        # A blocked or empty candidate carries its reason (SAFETY, RECITATION,
        # PROHIBITED_CONTENT); surface it instead of a generic "empty", and keep
        # it on the record so drop-reason metrics can tell a safety block apart
        # from an empty generation.
        return ResultLine(
            key=key,
            error=f"empty response ({reason})" if reason else "empty response",
            finish_reason=reason,
            input_tokens=usage[0],
            output_tokens=usage[1],
            thinking_tokens=usage[2],
        )

    if reason == "MAX_TOKENS":
        # Partial prose is worse than none: it ships a story that stops
        # mid-sentence. Treat as failure so the item retries with headroom.
        return ResultLine(
            key=key,
            text=text,
            error=f"truncated at max_output_tokens ({len(text)} chars)",
            finish_reason=reason,
            input_tokens=usage[0],
            output_tokens=usage[1],
            thinking_tokens=usage[2],
        )

    if reason and reason not in ("STOP", "FINISH_REASON_STOP", ""):
        return ResultLine(
            key=key,
            text=text,
            error=f"finish_reason={reason}",
            finish_reason=reason,
            input_tokens=usage[0],
            output_tokens=usage[1],
            thinking_tokens=usage[2],
        )

    return ResultLine(
        key=key,
        text=text,
        finish_reason=reason,
        input_tokens=usage[0],
        output_tokens=usage[1],
        thinking_tokens=usage[2],
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def submit(
    *, model: str, items: list[m.BatchItem], display_name: str
) -> tuple[str, str]:
    """Upload a JSONL of requests and create a batch job.

    Returns ``(provider_job_name, error)``; exactly one is non-empty. The caller
    must only record a job document when this returns a name, so a job record
    always corresponds to real remote work.
    """
    if not items:
        return "", "no items"

    def _do() -> tuple[str, str]:
        from google.genai import types  # type: ignore

        path = ""
        try:
            fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="llm_batch_")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for item in items:
                    handle.write(
                        json.dumps(
                            {"key": item.key, "request": item.request},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            uploaded = _genai_client().files.upload(
                file=path,
                config=types.UploadFileConfig(
                    display_name=display_name, mime_type="jsonl"
                ),
            )
            job = _genai_client().batches.create(
                model=model,
                src=uploaded.name,
                config={"display_name": display_name},
            )
            return str(getattr(job, "name", "") or ""), ""
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    try:
        name, err = await asyncio.to_thread(_do)
        if name:
            logger.info("llm_batch.client: submitted job", {
                "provider_job_name": name,
                "model": model,
                "items": len(items),
                "display_name": display_name,
            })
        return name, err
    except Exception as exc:
        logger.error("llm_batch.client: submit failed", {
            "model": model,
            "items": len(items),
            "display_name": display_name,
            "error": str(exc),
        })
        return "", str(exc)[:500]


async def poll(provider_job_name: str) -> PollResult:
    """Ask the provider where a job stands."""

    def _do() -> PollResult:
        job = _genai_client().batches.get(name=provider_job_name)
        state = m.normalize_provider_state(getattr(job, "state", None))
        stats = getattr(job, "completion_stats", None)
        failed = int(getattr(stats, "failed_count", 0) or 0) if stats else 0
        succeeded = int(getattr(stats, "successful_count", 0) or 0) if stats else 0
        job_error = getattr(job, "error", None)
        message = str(getattr(job_error, "message", "") or "") if job_error else ""
        dest = getattr(job, "dest", None)
        file_name = str(getattr(dest, "file_name", "") or "") if dest else ""
        return PollResult(
            state=state,
            failed_count=failed,
            succeeded_count=succeeded,
            error=message[:500],
            result_file_name=file_name,
        )

    try:
        return await asyncio.to_thread(_do)
    except Exception as exc:
        # Could not ask. NOT a job failure — leave the job open and retry.
        logger.warn("llm_batch.client: poll unreachable (will retry)", {
            "provider_job_name": provider_job_name,
            "error": str(exc),
        })
        return PollResult(state=m.JOB_RUNNING, reachable=False, error=str(exc)[:500])


@dataclass
class FetchResult:
    lines: dict[str, ResultLine] = field(default_factory=dict)
    reachable: bool = True
    error: str = ""


async def fetch_results(result_file_name: str) -> FetchResult:
    """Download and parse a finished job's result file, keyed by item key."""
    if not result_file_name:
        return FetchResult(reachable=True, error="no result file")

    def _do() -> FetchResult:
        raw = _genai_client().files.download(file=result_file_name)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        lines: dict[str, ResultLine] = {}
        malformed = 0
        for line in text.splitlines():
            parsed = parse_result_line(line)
            if parsed is None:
                malformed += 1
                continue
            lines[parsed.key] = parsed
        if malformed:
            logger.warn("llm_batch.client: dropped malformed result lines", {
                "result_file_name": result_file_name,
                "malformed": malformed,
                "parsed": len(lines),
            })
        return FetchResult(lines=lines)

    try:
        result = await asyncio.to_thread(_do)
        logger.info("llm_batch.client: fetched results", {
            "result_file_name": result_file_name,
            "keys": len(result.lines),
            "errored_keys": sum(1 for line in result.lines.values() if line.error),
        })
        return result
    except Exception as exc:
        logger.error("llm_batch.client: fetch_results failed", {
            "result_file_name": result_file_name,
            "error": str(exc),
        })
        return FetchResult(reachable=False, error=str(exc)[:500])


async def cancel(provider_job_name: str) -> bool:
    def _do() -> bool:
        _genai_client().batches.cancel(name=provider_job_name)
        return True

    try:
        return await asyncio.to_thread(_do)
    except Exception as exc:
        logger.warn("llm_batch.client: cancel failed", {
            "provider_job_name": provider_job_name,
            "error": str(exc),
        })
        return False
