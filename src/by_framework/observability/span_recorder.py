"""Redis-backed trace span recording helpers and generic span exporters."""

from __future__ import annotations

import contextvars
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from inspect import isawaitable
from typing import (Any, AsyncIterator, Optional, Protocol, Sequence, runtime_checkable)

from by_framework.common.constants import RedisKeys
from by_framework.common.logger import logger
from by_framework.common.redis_client import Redis, get_redis

# Context variables to temporarily override generated trace/span IDs
current_trace_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_trace_id_var", default=None
)
current_span_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_span_id_var", default=None
)


def str_to_uint128(s: str) -> int:
    """Convert a string to a deterministic 128-bit integer for OTEL TraceId."""
    if len(s) == 32:
        try:
            return int(s, 16)
        except ValueError:
            pass
    val = int(hashlib.md5(s.encode()).hexdigest(), 16)
    return val if val != 0 else 1


def str_to_uint64(s: str) -> int:
    """Convert a string to a deterministic 64-bit integer for OTEL SpanId."""
    if len(s) == 16:
        try:
            return int(s, 16)
        except ValueError:
            pass
    val = int(hashlib.md5(s.encode()).hexdigest()[:16], 16)
    return val if val != 0 else 1


try:
    from opentelemetry.sdk.trace.id_generator import IdGenerator

    class ContextIdGenerator(IdGenerator):
        """Custom OpenTelemetry ID Generator.

        Checks context variables before falling back.
        """

        def generate_trace_id(self) -> int:
            val = current_trace_id_var.get()
            if val is not None:
                return val
            import secrets

            val_rand = secrets.randbits(128)
            return val_rand if val_rand != 0 else 1

        def generate_span_id(self) -> int:
            val = current_span_id_var.get()
            if val is not None:
                return val
            import secrets

            val_rand = secrets.randbits(64)
            return val_rand if val_rand != 0 else 1

except ImportError:

    class ContextIdGenerator:  # type: ignore
        pass


def configure_otel_id_generator() -> None:
    """Hot-patch the global OpenTelemetry TracerProvider to use ContextIdGenerator."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        provider = trace.get_tracer_provider()
        if hasattr(provider, "_delegate") and provider._delegate is not None:
            provider = provider._delegate

        if isinstance(provider, TracerProvider):
            if not isinstance(provider.id_generator, ContextIdGenerator):
                provider.id_generator = ContextIdGenerator()
            # Patch already instantiated tracers
            if hasattr(provider, "_tracers") and isinstance(provider._tracers, dict):
                for t in provider._tracers.values():
                    if hasattr(t, "id_generator") and not isinstance(
                        t.id_generator, ContextIdGenerator
                    ):
                        t.id_generator = provider.id_generator
    except Exception:  # pylint: disable=broad-exception-caught
        pass


TRACE_TTL_SECONDS = 15 * 60  # Default to 15 minutes to avoid unbounded Redis growth.


@dataclass(frozen=True)
class TraceSpan:
    """A single distributed trace span."""

    trace_id: str
    span_id: str
    parent_span_id: str
    operation: str
    component: str
    start_ts: int
    end_ts: int
    status: str
    session_id: str = ""
    execution_id: str = ""
    message_id: str = ""
    parent_message_id: str = ""
    worker_id: str = ""
    source_agent_type: str = ""
    target_agent_type: str = ""
    error_type: str = ""
    error_message: str = ""
    error_code: str = ""
    failed_stage: str = ""
    retryable: bool = False
    route_policy: str = ""
    route_status: str = ""
    queue_wait_ms: int = 0
    chunk_count: int = 0
    event_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable span payload."""
        payload = asdict(self)
        payload["start_ts"] = int(self.start_ts or 0)
        payload["end_ts"] = max(payload["start_ts"], int(self.end_ts or 0))
        payload["duration_ms"] = max(0, payload["end_ts"] - payload["start_ts"])
        return {
            key: value
            for key, value in payload.items()
            if value not in ("", None) and value is not False
        }


@runtime_checkable
class SpanExporter(Protocol):
    """Protocol for tracing span exporters."""

    async def export_span(self, span: TraceSpan) -> None:
        """Export a single trace span to the backend."""
        pass


class RedisSpanExporter:
    """Export trace spans to Redis with short-lived cache TTL."""

    def __init__(
        self,
        redis_client: Optional[Redis] = None,
        *,
        ttl_seconds: int = TRACE_TTL_SECONDS,
    ) -> None:
        self.redis = redis_client or get_redis()
        self.ttl_seconds = max(1, int(ttl_seconds or TRACE_TTL_SECONDS))

    async def export_span(self, span: TraceSpan) -> None:
        """Persist a span and update trace lookup indexes in Redis."""
        payload = span.to_payload()
        trace_id = str(payload["trace_id"])
        start_ts = int(payload.get("start_ts", 0) or 0)
        meta_key = RedisKeys.trace_meta(trace_id)
        spans_key = RedisKeys.trace_spans(trace_id)
        pipe = self.redis.pipeline()
        if isawaitable(pipe):
            pipe = await pipe
        await self._call_pipeline(pipe, "hset", meta_key, "trace_id", trace_id)
        await self._call_pipeline(
            pipe, "hset", meta_key, "session_id", str(payload.get("session_id", ""))
        )
        await self._call_pipeline(
            pipe, "hset", meta_key, "status", str(payload.get("status", ""))
        )
        await self._call_pipeline(pipe, "hset", meta_key, "start_ts", start_ts)
        await self._call_pipeline(
            pipe,
            "hset",
            meta_key,
            "updated_at",
            int(payload.get("end_ts", start_ts) or start_ts),
        )
        await self._call_pipeline(
            pipe, "rpush", spans_key, json.dumps(payload, ensure_ascii=False)
        )
        if payload.get("session_id"):
            await self._call_pipeline(
                pipe,
                "zadd",
                RedisKeys.trace_index_session(str(payload["session_id"])),
                {trace_id: start_ts},
            )
        if payload.get("worker_id"):
            await self._call_pipeline(
                pipe,
                "zadd",
                RedisKeys.trace_index_worker(str(payload["worker_id"])),
                {trace_id: start_ts},
            )
        if payload.get("target_agent_type"):
            await self._call_pipeline(
                pipe,
                "zadd",
                RedisKeys.trace_index_agent(str(payload["target_agent_type"])),
                {trace_id: start_ts},
            )
        await self._call_pipeline(pipe, "expire", meta_key, self.ttl_seconds)
        await self._call_pipeline(pipe, "expire", spans_key, self.ttl_seconds)
        result = pipe.execute()
        if isawaitable(result):
            await result

    @staticmethod
    async def _call_pipeline(pipe: Any, method_name: str, *args: Any) -> None:
        result = getattr(pipe, method_name)(*args)
        if isawaitable(result):
            await result


class OTelSpanExporter:
    """Export TraceSpan objects into OpenTelemetry's global tracer."""

    def __init__(self, tracer_name: str = "by-framework") -> None:
        self._tracer = None
        self.trace_mod = None
        try:
            from opentelemetry import trace

            if trace is not None:
                self.trace_mod = trace
                configure_otel_id_generator()
                self._tracer = trace.get_tracer(tracer_name)
        except (ImportError, AttributeError):
            pass

    async def export_span(self, span: TraceSpan) -> None:
        """Export a TraceSpan to OpenTelemetry."""
        if self._tracer is None or self.trace_mod is None:
            return

        configure_otel_id_generator()

        trace_id_int = str_to_uint128(span.trace_id)
        span_id_int = str_to_uint64(span.span_id)

        parent_context = None
        if span.parent_span_id:
            parent_span_id_int = str_to_uint64(span.parent_span_id)
            parent_span_context = self.trace_mod.SpanContext(
                trace_id=trace_id_int,
                span_id=parent_span_id_int,
                is_remote=True,
                trace_flags=self.trace_mod.TraceFlags(
                    self.trace_mod.TraceFlags.SAMPLED
                ),
            )
            parent_context = self.trace_mod.set_span_in_context(
                self.trace_mod.NonRecordingSpan(parent_span_context)
            )

        attributes = {
            "component": span.component,
            "status": span.status,
            "session_id": span.session_id,
            "execution_id": span.execution_id,
            "message_id": span.message_id,
            "parent_message_id": span.parent_message_id,
            "worker_id": span.worker_id,
            "source_agent_type": span.source_agent_type,
            "target_agent_type": span.target_agent_type,
        }
        if span.error_type:
            attributes["error_type"] = span.error_type
        if span.error_message:
            attributes["error_message"] = span.error_message
        if span.error_code:
            attributes["error_code"] = span.error_code
        if span.failed_stage:
            attributes["failed_stage"] = span.failed_stage
        if span.route_policy:
            attributes["route_policy"] = span.route_policy
        if span.route_status:
            attributes["route_status"] = span.route_status
        if span.queue_wait_ms:
            attributes["queue_wait_ms"] = span.queue_wait_ms
        if span.chunk_count:
            attributes["chunk_count"] = span.chunk_count
        if span.event_type:
            attributes["event_type"] = span.event_type

        if span.metadata:
            for k, v in span.metadata.items():
                attributes[f"metadata.{k}"] = str(v)

        trace_id_token = current_trace_id_var.set(trace_id_int)
        span_id_token = current_span_id_var.set(span_id_int)
        try:
            logger.info(
                "OTelSpanExporter triggering OTel span: %s (trace_id: %s, "
                "span_id: %s)",
                span.operation,
                span.trace_id,
                span.span_id,
            )
            otel_span = self._tracer.start_span(
                name=span.operation,
                context=parent_context,
                start_time=int(span.start_ts * 1_000_000),
                attributes=attributes,
            )
            if (
                span.status in ("FAILED", "error")
                or span.error_message
                or span.error_type
            ):
                from opentelemetry.trace import Status, StatusCode

                otel_span.set_status(
                    Status(StatusCode.ERROR, span.error_message or "Execution failed")
                )
            otel_span.end(end_time=int(span.end_ts * 1_000_000))
            logger.info(
                "OTelSpanExporter successfully triggered OTel span: %s",
                span.operation,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning(
                "OTelSpanExporter failed to export span %s: %s",
                span.operation,
                e,
                exc_info=True,
            )
        finally:
            current_trace_id_var.reset(trace_id_token)
            current_span_id_var.reset(span_id_token)


class LiveSpanHandle:
    """Mutable handle to update the terminal status of a live OTel span."""

    def __init__(self) -> None:
        self.status = "COMPLETED"
        self.error_message = ""

    @property
    def is_error(self) -> bool:
        """Whether the span should be marked as an OTel error span."""
        return self.status in ("FAILED", "error") or bool(self.error_message)

    def set_status(self, status: str, *, error_message: str = "") -> None:
        """Record the terminal status / error for the wrapped execution."""
        if status:
            self.status = status
        if error_message:
            self.error_message = error_message


@asynccontextmanager
async def live_execution_otel_span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str,
    operation: str,
    attributes: dict[str, Any],
    start_ts: int,
    tracer_name: str = "by-framework",
) -> AsyncIterator[LiveSpanHandle]:
    """Open a *live* OTel span set as the current context for an execution.

    Unlike :class:`OTelSpanExporter` (which replays a finished span after the
    fact), this keeps the span active for the whole ``async with`` body, so any
    spans produced inside the agent (e.g. LangGraph/Langfuse LLM calls) nest
    under it via normal OTel context propagation.

    The deterministic ``span_id`` is only injected for the brief moment the span
    is created; the context vars are reset immediately afterwards so that nested
    child spans generate their own ids (and merely inherit this span as parent)
    instead of colliding on the same span_id.
    """
    handle = LiveSpanHandle()
    span = None
    ctx_token = None
    context_mod = None
    try:
        from opentelemetry import context as context_mod  # type: ignore
        from opentelemetry import trace as trace_mod

        configure_otel_id_generator()
        tracer = trace_mod.get_tracer(tracer_name)

        trace_id_int = str_to_uint128(trace_id)
        span_id_int = str_to_uint64(span_id)

        parent_context = None
        if parent_span_id:
            parent_span_context = trace_mod.SpanContext(
                trace_id=trace_id_int,
                span_id=str_to_uint64(parent_span_id),
                is_remote=True,
                trace_flags=trace_mod.TraceFlags(trace_mod.TraceFlags.SAMPLED),
            )
            parent_context = trace_mod.set_span_in_context(
                trace_mod.NonRecordingSpan(parent_span_context)
            )

        trace_id_token = current_trace_id_var.set(trace_id_int)
        span_id_token = current_span_id_var.set(span_id_int)
        try:
            span = tracer.start_span(
                name=operation,
                context=parent_context,
                start_time=int(start_ts * 1_000_000),
                attributes=attributes,
            )
        finally:
            current_span_id_var.reset(span_id_token)
            current_trace_id_var.reset(trace_id_token)

        ctx_token = context_mod.attach(trace_mod.set_span_in_context(span))
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.debug("live_execution_otel_span setup skipped: %s", err)
        span = None
        ctx_token = None

    try:
        yield handle
    finally:
        if span is not None:
            try:
                if handle.is_error:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_status(
                        Status(
                            StatusCode.ERROR,
                            handle.error_message or "Execution failed",
                        )
                    )
                span.end(end_time=int(time.time() * 1000) * 1_000_000)
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.debug("live_execution_otel_span end failed: %s", err)
        if ctx_token is not None and context_mod is not None:
            try:
                context_mod.detach(ctx_token)
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.debug("live_execution_otel_span detach failed: %s", err)


def register_langfuse_span_processor() -> None:
    """Register LangfuseSpanProcessor on the global OpenTelemetry TracerProvider."""
    try:
        import os
        from importlib import import_module

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        def clean(val):
            return val.strip().strip("'\"“”‘’") if val else ""

        enabled = clean(os.environ.get("BYAI_LANGFUSE_ENABLED", ""))
        if enabled and enabled.lower() in {"0", "false", "no", "off", "disabled"}:
            return

        secret_key = clean(os.environ.get("LANGFUSE_SECRET_KEY", ""))
        public_key = clean(os.environ.get("LANGFUSE_PUBLIC_KEY", ""))
        base_url = clean(os.environ.get("LANGFUSE_BASE_URL", ""))

        if secret_key and public_key and base_url:
            # 1. Ensure the global TracerProvider exists and patch the ID generator.
            provider = trace.get_tracer_provider()
            if hasattr(provider, "_delegate") and provider._delegate is not None:
                provider = provider._delegate

            if not isinstance(provider, TracerProvider):
                provider = TracerProvider()
                trace.set_tracer_provider(provider)
                configure_otel_id_generator()

            # 2. Avoid registering duplicate LangfuseSpanProcessor instances.
            has_processor = False
            active_processor = getattr(provider, "_active_span_processor", None)
            if active_processor is not None:
                processors = []
                if hasattr(active_processor, "_span_processors"):
                    processors = active_processor._span_processors
                else:
                    processors = [active_processor]

                for p in processors:
                    if p.__class__.__name__ == "LangfuseSpanProcessor":
                        has_processor = True
                        break

            if not has_processor:
                # 3. Dynamically import and attach LangfuseSpanProcessor.
                langfuse_processor_mod = import_module(
                    "langfuse._client.span_processor"
                )
                langfuse_processor_cls = getattr(
                    langfuse_processor_mod, "LangfuseSpanProcessor"
                )

                def should_export_span(span) -> bool:
                    # Allow spans emitted by the by-framework tracer.
                    if (
                        span.instrumentation_scope
                        and span.instrumentation_scope.name == "by-framework"
                    ):
                        return True
                    # Allow custom client/worker spans.
                    if span.attributes:
                        if span.attributes.get("component") in ("client", "worker"):
                            return True
                    # Fall back to Langfuse's default filter.
                    try:
                        from langfuse.span_filter import is_default_export_span

                        return is_default_export_span(span)
                    except ImportError:
                        return True

                processor = langfuse_processor_cls(
                    public_key=public_key,
                    secret_key=secret_key,
                    base_url=base_url,
                    should_export_span=should_export_span,
                )
                provider.add_span_processor(processor)
                logger.info(
                    "LangfuseSpanProcessor registered successfully to global OTel "
                    "TracerProvider. Base URL: %s",
                    base_url,
                )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to auto-register LangfuseSpanProcessor: %s", e)


class SpanRecorder:
    """Wrapper that routes trace spans to one or more exporters."""

    def __init__(
        self,
        redis_client: Optional[Redis] = None,
        *,
        exporters: Optional[Sequence[SpanExporter]] = None,
        ttl_seconds: int = TRACE_TTL_SECONDS,
        enable_otel: bool = True,
    ) -> None:
        if exporters is not None:
            self.exporters = list(exporters)
        elif not enable_otel:
            # OTel emission handled elsewhere (e.g. a live wrapping span); keep
            # only the Redis dashboard exporter to avoid double-exporting spans.
            self.exporters = [RedisSpanExporter(redis_client, ttl_seconds=ttl_seconds)]
        else:
            self.exporters = [RedisSpanExporter(redis_client, ttl_seconds=ttl_seconds)]
            try:
                from opentelemetry import trace  # pylint: disable=unused-import

                if trace is not None:
                    self.exporters.append(OTelSpanExporter())
            except (ImportError, AttributeError):
                pass

            try:
                import os

                # Register LangfuseSpanProcessor when credentials are configured.
                if os.environ.get("LANGFUSE_SECRET_KEY") and os.environ.get(
                    "LANGFUSE_PUBLIC_KEY"
                ):
                    register_langfuse_span_processor()
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.warning("Failed to auto-register LangfuseSpanProcessor: %s", e)

        logger.info(
            "SpanRecorder initialized with exporters: %s",
            [type(e).__name__ for e in self.exporters],
        )

    async def record_span(self, span: TraceSpan) -> None:
        """Record a span by forwarding it to all registered exporters."""
        for exporter in self.exporters:
            try:
                await exporter.export_span(span)
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Exporter %s failed to export span: %s",
                    type(exporter).__name__,
                    err,
                )
