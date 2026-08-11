"""Privacy-preserving Arize AX tracing for the API and LiveKit worker.

The exporter is deliberately metadata-only. Provider instrumentors and LiveKit
both create useful trace topology, but their raw spans can contain prompts,
transcripts, screen-derived context, tool arguments, and tool results. Every
span is cloned through an allowlist before it leaves the process.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter

from ...config.settings import settings
from ...lib.logger import logger

_REDACTED = "__REDACTED__"

_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "aura.client_message_id",
        "aura.prompt_version",
        "aura.runtime",
        "aura.surface",
        "aura.trace_id",
        "exception.type",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "lk.agent_label",
        "lk.agent_name",
        "lk.e2e_latency",
        "lk.end_of_turn_delay",
        "lk.end_time",
        "lk.eou.detection_delay",
        "lk.eou.endpointing_delay",
        "lk.eou.from_cache",
        "lk.eou.language",
        "lk.eou.probability",
        "lk.eou.source",
        "lk.eou.unlikely_threshold",
        "lk.function_tool.id",
        "lk.function_tool.is_error",
        "lk.function_tool.name",
        "lk.generation_id",
        "lk.interrupted",
        "lk.is_interruption",
        "lk.job_id",
        "lk.parent_generation_id",
        "lk.response.ttfb",
        "lk.response.ttft",
        "lk.retry_count",
        "lk.speech_id",
        "lk.start_time",
        "lk.transcript_confidence",
        "lk.transcription_delay",
        "lk.tts.label",
        "lk.tts.streaming",
        "openinference.span.kind",
        "session.id",
        "tool.id",
        "tool.name",
    }
)
_SAFE_ATTRIBUTE_PREFIXES = (
    "aura.",
    "gen_ai.usage.",
    "llm.token_count.",
)

_context_attributes: ContextVar[dict[str, str]] = ContextVar(
    "arize_context_attributes",
    default={},
)
_provider: Any | None = None
_tracer: Any | None = None
_runtime: str | None = None
_init_attempted = False
_instrumentors: list[Any] = []


class _ContextAttributeProcessor(SpanProcessor):
    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        del parent_context
        span.set_attributes(_context_attributes.get())

    def on_end(self, span: Any) -> None:
        del span

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        return True


class _SanitizingSpanExporter(SpanExporter):
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def export(self, spans: Any) -> Any:
        return self._delegate.export(tuple(_sanitize_span(span) for span in spans))

    def shutdown(self) -> Any:
        return self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        force_flush = getattr(self._delegate, "force_flush", None)
        if callable(force_flush):
            return bool(force_flush(timeout_millis=timeout_millis))
        return True


@dataclass
class ArizeSpanHandle:
    span: Any
    scope: Any
    finished: bool = False

    def finish(self, *, success: bool = True, error_type: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            from opentelemetry.trace import Status, StatusCode

            self.span.set_attribute("output.value", _REDACTED)
            if error_type:
                self.span.set_attribute("exception.type", error_type)
            status_code = StatusCode.OK if success else StatusCode.ERROR
            self.span.set_status(Status(status_code))
            self.span.end()
        except Exception as exc:
            logger.debug("arize_tracing: span finish failed", {"error": str(exc)})
        finally:
            try:
                self.scope.__exit__(None, None, None)
            except Exception:
                pass


def configure_arize_tracing(runtime: str) -> Any | None:
    """Configure one metadata-only Arize exporter for this process."""
    global _init_attempted, _provider, _runtime, _tracer
    if _init_attempted:
        return _provider
    _init_attempted = True

    if not settings.arize_configured:
        logger.info("arize_tracing: credentials absent, tracing disabled")
        return None

    try:
        from arize.otel import PROJECT_NAME, GRPCSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                PROJECT_NAME: settings.ARIZE_PROJECT_NAME,
                "service.name": f"aura-{runtime}",
                "deployment.environment.name": settings.ENV,
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(_ContextAttributeProcessor())
        provider.add_span_processor(
            BatchSpanProcessor(
                _SanitizingSpanExporter(
                    GRPCSpanExporter(
                        space_id=settings.ARIZE_SPACE_ID,
                        api_key=settings.ARIZE_API_KEY,
                        endpoint=settings.ARIZE_COLLECTOR_ENDPOINT,
                    )
                )
            )
        )

        if runtime == "api":
            _instrument_api_providers(provider)
        elif runtime == "voice":
            from livekit.agents.telemetry import set_tracer_provider

            set_tracer_provider(provider)
        else:
            raise ValueError(f"unsupported Arize runtime: {runtime}")

        _provider = provider
        _tracer = provider.get_tracer("aura.arize")
        _runtime = runtime
        logger.info(
            "arize_tracing: initialised",
            {
                "runtime": runtime,
                "project": settings.ARIZE_PROJECT_NAME,
                "endpoint": settings.ARIZE_COLLECTOR_ENDPOINT,
                "content_capture": "disabled",
            },
        )
        return provider
    except Exception as exc:
        logger.warn(
            "arize_tracing: init failed, tracing disabled",
            {"runtime": runtime, "error": str(exc)},
        )
        _provider = None
        _tracer = None
        _runtime = None
        return None


def _instrument_api_providers(provider: Any) -> None:
    from openinference.instrumentation import TraceConfig
    from openinference.instrumentation.anthropic import AnthropicInstrumentor
    from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
    from openinference.instrumentation.openai import OpenAIInstrumentor

    config = TraceConfig(
        hide_llm_invocation_parameters=True,
        hide_llm_tools=True,
        hide_inputs=True,
        hide_outputs=True,
        hide_input_messages=True,
        hide_output_messages=True,
        hide_input_images=True,
        hide_input_text=True,
        hide_output_text=True,
        hide_embedding_vectors=True,
        hide_embeddings_text=True,
        hide_prompts=True,
        hide_choices=True,
    )
    for name, instrumentor in (
        ("openai", OpenAIInstrumentor()),
        ("anthropic", AnthropicInstrumentor()),
        ("google_genai", GoogleGenAIInstrumentor()),
    ):
        try:
            instrumentor.instrument(tracer_provider=provider, config=config)
            _instrumentors.append(instrumentor)
        except Exception as exc:
            logger.warn(
                "arize_tracing: provider instrumentation failed",
                {"provider": name, "error": str(exc)},
            )


def bind_arize_context(**values: str | None) -> Token[dict[str, str]]:
    current = dict(_context_attributes.get())
    mappings = {
        "client_message_id": "aura.client_message_id",
        "prompt_version": "aura.prompt_version",
        "session_id": "session.id",
        "surface": "aura.surface",
        "trace_id": "aura.trace_id",
    }
    for source, target in mappings.items():
        value = values.get(source)
        if isinstance(value, str) and value:
            current[target] = value
    if _runtime:
        current["aura.runtime"] = _runtime
    return _context_attributes.set(current)


def reset_arize_context(token: Token[dict[str, str]]) -> None:
    _context_attributes.reset(token)


def start_chain_span(name: str) -> ArizeSpanHandle | None:
    if _runtime != "api":
        return None
    return _start_span(name, kind="CHAIN")


def start_tool_span(tool_name: str) -> ArizeSpanHandle | None:
    if _runtime != "api":
        return None
    return _start_span(
        f"tool:{tool_name}",
        kind="TOOL",
        attributes={
            "tool.name": tool_name,
            "tool.description": "Aura tool execution",
            "tool.parameters": "{}",
        },
    )


def _start_span(
    name: str,
    *,
    kind: str,
    attributes: dict[str, str] | None = None,
) -> ArizeSpanHandle | None:
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace as trace_api

        span = _tracer.start_span(name)
        span.set_attribute("openinference.span.kind", kind)
        span.set_attribute("input.value", _REDACTED)
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        scope = trace_api.use_span(span, end_on_exit=False)
        scope.__enter__()
        return ArizeSpanHandle(span=span, scope=scope)
    except Exception as exc:
        logger.warn("arize_tracing: span start failed", {"name": name, "error": str(exc)})
        return None


def flush_arize_tracing() -> None:
    if _provider is None:
        return
    try:
        _provider.force_flush()
    except Exception as exc:
        logger.warn("arize_tracing: flush failed", {"error": str(exc)})


def shutdown_arize_tracing() -> None:
    global _init_attempted, _provider, _runtime, _tracer
    for instrumentor in reversed(_instrumentors):
        try:
            instrumentor.uninstrument()
        except Exception:
            pass
    _instrumentors.clear()
    if _provider is not None:
        try:
            _provider.force_flush()
            _provider.shutdown()
        except Exception as exc:
            logger.warn("arize_tracing: shutdown failed", {"error": str(exc)})
    _provider = None
    _tracer = None
    _runtime = None
    _init_attempted = False


def _sanitize_span(span: Any) -> Any:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Status, StatusCode

    original = dict(span.attributes or {})
    attributes = {
        key: value
        for key, value in original.items()
        if key in _SAFE_ATTRIBUTE_KEYS or key.startswith(_SAFE_ATTRIBUTE_PREFIXES)
    }
    kind = _infer_openinference_kind(span.name, original)
    if kind:
        attributes["openinference.span.kind"] = kind
        attributes["input.value"] = _REDACTED
        attributes["output.value"] = _REDACTED
    if kind == "LLM":
        model = original.get("llm.model_name") or original.get("gen_ai.request.model")
        provider = original.get("llm.provider") or original.get("gen_ai.provider.name")
        if isinstance(model, str) and model:
            attributes["llm.model_name"] = model
        if isinstance(provider, str) and provider:
            attributes["llm.provider"] = provider
    if kind == "TOOL":
        tool_name = original.get("tool.name") or original.get("lk.function_tool.name")
        if isinstance(tool_name, str) and tool_name:
            attributes["tool.name"] = tool_name
        attributes["tool.description"] = "Aura tool execution"
        attributes["tool.parameters"] = "{}"

    status_code = span.status.status_code
    if original.get("lk.function_tool.is_error") is True:
        status_code = StatusCode.ERROR
    elif kind and status_code == StatusCode.UNSET:
        status_code = StatusCode.OK

    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=(),
        links=(),
        kind=span.kind,
        status=Status(status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _infer_openinference_kind(name: str, attributes: dict[str, Any]) -> str | None:
    existing = attributes.get("openinference.span.kind")
    if isinstance(existing, str) and existing:
        return existing
    if "gen_ai.operation.name" in attributes or "gen_ai.request.model" in attributes:
        return "LLM"
    if name == "function_tool" or "lk.function_tool.name" in attributes:
        return "TOOL"
    if name == "agent_session":
        return "AGENT"
    if name in {"agent_turn", "job_entrypoint"}:
        return "CHAIN"
    return None
