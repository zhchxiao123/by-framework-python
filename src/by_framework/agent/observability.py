"""Project durable native events into the existing trace and metric surfaces."""

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from by_framework.metrics import (
    executions_completed_total,
    executions_failed_total,
    executions_started_total,
)
from by_framework.trace import ObservabilityConfig, SpanRecorder, TraceSpan

from .runtime.store import RunEvent
from .safety import TerminalStatus


_COMPONENTS = {
    "RunCreated": ("run", "run"),
    "SuperstepCommitted": ("coordinator.superstep", "coordinator"),
    "NodeCompleted": ("node.execute", "graph"),
    "ModelCompleted": ("model.call", "model"),
    "ToolCompleted": ("tool.call", "tool"),
    "AgentDispatched": ("agent.dispatch", "agent"),
    "AgentReturned": ("agent.return", "agent"),
    "HandoffCommitted": ("handoff.commit", "agent"),
    "CheckpointSaved": ("checkpoint.save", "checkpoint"),
    "RunInterrupted": ("interrupt.wait", "interrupt"),
    "RunResumed": ("interrupt.resume", "interrupt"),
    "ApprovalRequested": ("approval.request", "approval"),
    "ApprovalResolved": ("approval.resolve", "approval"),
    "RunCompleted": ("run.complete", "run"),
    "RunFailed": ("run.fail", "run"),
    "RunCancelled": ("run.cancel", "run"),
}


@dataclass(frozen=True)
class NativeTraceContext:
    trace_id: str
    run_span_id: str
    node_span_id: str = ""
    session_id: str = ""
    execution_id: str = ""
    coordinator_span_id: str = ""
    agent_span_id: str = ""


@dataclass
class NativeRuntimeMetrics:
    """Native counters plus safe projection to existing metrics."""

    counters: dict[str, int] = field(default_factory=dict)
    project_existing_metrics: bool = False
    _seen_event_ids: set[str] = field(default_factory=set, repr=False)

    def record(
        self,
        event: RunEvent,
        *,
        agent_type: str = "native",
        event_id: str | None = None,
    ) -> None:
        if event_id is not None:
            if event_id in self._seen_event_ids:
                return
            self._seen_event_ids.add(event_id)
        self.counters[event.kind] = self.counters.get(event.kind, 0) + 1
        if not self.project_existing_metrics:
            return
        if event.kind == "RunCreated":
            executions_started_total.labels(agent_type=agent_type).inc()
        elif event.kind in ("RunCompleted", "RunCancelled"):
            status = (
                TerminalStatus.COMPLETED.value
                if event.kind == "RunCompleted"
                else TerminalStatus.CANCELLED.value
            )
            executions_completed_total.labels(
                status=status, agent_type=agent_type
            ).inc()
        elif event.kind == "RunFailed":
            executions_failed_total.labels(
                agent_type=agent_type,
                error_type=str(event.payload.get("error_type", "unknown")),
            ).inc()


class NativeTraceProjector:
    """Create a consistent span hierarchy from native durable events."""

    def __init__(
        self,
        recorder: SpanRecorder,
        *,
        config: ObservabilityConfig | None = None,
        capture_sensitive: bool = False,
    ):
        self.recorder = recorder
        self.config = config or recorder.config
        self.capture_sensitive = capture_sensitive and not self.config.redact_inputs

    async def record(
        self,
        event: RunEvent,
        context: NativeTraceContext,
        *,
        event_id: str | None = None,
    ) -> TraceSpan:
        operation, component = _COMPONENTS.get(event.kind, ("runtime.event", "native"))
        parent_span_id = context.run_span_id
        if component in ("coordinator", "graph", "checkpoint") and (
            context.coordinator_span_id
        ):
            parent_span_id = context.coordinator_span_id
        if component in ("model", "tool", "agent", "approval") and (
            context.agent_span_id
        ):
            parent_span_id = context.agent_span_id
        elif component in ("model", "tool", "agent", "approval") and (
            context.node_span_id
        ):
            parent_span_id = context.node_span_id
        now = int(time.time() * 1000)
        payload = _redact(event.payload, self.capture_sensitive)
        span = TraceSpan(
            trace_id=context.trace_id,
            span_id=(
                context.run_span_id
                if event.kind == "RunCreated"
                else _span_id(context.trace_id, event, event_id)
            ),
            parent_span_id="" if event.kind == "RunCreated" else parent_span_id,
            operation=operation,
            component=component,
            start_ts=now,
            end_ts=now,
            status=_span_status(event.kind),
            input=payload if event.kind.endswith(("Requested", "Dispatched")) else None,
            output=payload,
            session_id=context.session_id,
            execution_id=context.execution_id,
            event_type=event.kind,
            metadata={"native_event_kind": event.kind},
        )
        await self.recorder.record_span(span)
        return span


def _span_id(trace_id: str, event: RunEvent, event_id: str | None) -> str:
    normalized_event_id = event_id or ""
    value = f"{trace_id}:{normalized_event_id}:{event.kind}:{event.payload}".encode()
    return hashlib.sha256(value).hexdigest()[:16]


def _span_status(kind: str) -> str:
    if kind == "RunFailed":
        return TerminalStatus.FAILED.value
    if kind == "RunCancelled":
        return TerminalStatus.CANCELLED.value
    if kind == "RunInterrupted":
        return TerminalStatus.INTERRUPTED.value
    return TerminalStatus.COMPLETED.value


def _redact(value: Any, capture_sensitive: bool, key: str = "") -> Any:
    sensitive = (
        "token",
        "secret",
        "password",
        "authorization",
        "api_key",
        "arguments",
        "content",
        "input",
        "output",
        "prompt",
    )
    if not capture_sensitive and any(part in key.lower() for part in sensitive):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, capture_sensitive, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, capture_sensitive, key) for item in value]
    return value
