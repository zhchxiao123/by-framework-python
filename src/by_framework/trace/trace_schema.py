"""Shared trace write/read records for by-framework observability."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from by_framework.trace.span_recorder import _sanitize_value


def _clean_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None) and value is not False
    }


def _sanitize_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_value("metadata", mapping)


def encode_redis_value(value: Any) -> str:
    """Encode structured trace values for Redis hashes."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode_redis_value(value: Any) -> Any:
    """Decode a Redis value that may contain JSON."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped[0] not in '[{"' and stripped not in ("true", "false", "null"):
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


@dataclass(frozen=True)
class TraceRecord:
    """Trace-level metadata shared by writers and trace readers."""

    trace_id: str
    name: str = ""
    session_id: str = ""
    root_message_id: str = ""
    root_agent_type: str = ""
    input: Any = None
    output: Any = None
    status: str = ""
    start_ts: int = 0
    end_ts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_ts"] = int(self.start_ts or 0)
        payload["end_ts"] = int(self.end_ts or 0)
        if payload.get("metadata"):
            payload["metadata"] = _sanitize_mapping(payload["metadata"])
        return _clean_dict(payload)

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "TraceRecord":
        data = {str(key): decode_redis_value(value) for key, value in mapping.items()}
        return cls(
            trace_id=str(data.get("trace_id", "")),
            name=str(data.get("name", "")),
            session_id=str(data.get("session_id", "")),
            root_message_id=str(data.get("root_message_id", "")),
            root_agent_type=str(data.get("root_agent_type", "")),
            input=data.get("input"),
            output=data.get("output"),
            status=str(data.get("status", "")),
            start_ts=int(data.get("start_ts", 0) or 0),
            end_ts=int(data.get("end_ts", 0) or 0),
            metadata=(
                data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            ),
        )


@dataclass(frozen=True)
class ExecutionRecord:
    """Task execution metadata for registry-backed trace reads."""

    execution_id: str
    message_id: str
    trace_id: str
    session_id: str
    parent_message_id: str = ""
    worker_id: str = ""
    source_agent_type: str = ""
    target_agent_type: str = ""
    status: str = ""
    timing: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    route: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload.get("metadata"):
            payload["metadata"] = _sanitize_mapping(payload["metadata"])
        if payload.get("error"):
            payload["error"] = _sanitize_mapping(payload["error"])
        return _clean_dict(payload)


@dataclass(frozen=True)
class SpanRecord:
    """Trace-tree span model shared by Redis, Langfuse, Phoenix, and UI readers."""

    trace_id: str
    span_id: str
    parent_span_id: str = ""
    name: str = ""
    operation: str = ""
    component: str = ""
    kind: str = ""
    start_ts: int = 0
    end_ts: int = 0
    status: str = "COMPLETED"
    input: Any = None
    output: Any = None
    tokens: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "redis"
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

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["name"] = self.name or self.operation
        payload["operation"] = self.operation or self.name
        payload["start_ts"] = int(self.start_ts or 0)
        payload["end_ts"] = max(payload["start_ts"], int(self.end_ts or 0))
        payload["duration_ms"] = max(0, payload["end_ts"] - payload["start_ts"])
        if payload.get("metadata"):
            payload["metadata"] = _sanitize_mapping(payload["metadata"])
        if payload.get("error_message"):
            payload["error_message"] = _sanitize_value(
                "error_message", payload["error_message"]
            )
        return _clean_dict(payload)

    @classmethod
    def from_mapping(
        cls, mapping: dict[str, Any], *, source: str = "redis"
    ) -> "SpanRecord":
        data = {str(key): decode_redis_value(value) for key, value in mapping.items()}
        return cls(
            trace_id=str(data.get("trace_id", "")),
            span_id=str(data.get("span_id", "")),
            parent_span_id=str(data.get("parent_span_id", "")),
            name=str(data.get("name") or data.get("operation") or ""),
            operation=str(data.get("operation") or data.get("name") or ""),
            component=str(data.get("component", "")),
            kind=str(data.get("kind", "")),
            start_ts=int(data.get("start_ts", 0) or 0),
            end_ts=int(data.get("end_ts", 0) or 0),
            status=str(data.get("status", "COMPLETED")),
            input=data.get("input"),
            output=data.get("output"),
            tokens=data.get("tokens") if isinstance(data.get("tokens"), dict) else {},
            cost=data.get("cost") if isinstance(data.get("cost"), dict) else {},
            metadata=(
                data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            ),
            source=str(data.get("source") or source),
            session_id=str(data.get("session_id", "")),
            execution_id=str(data.get("execution_id", "")),
            message_id=str(data.get("message_id", "")),
            parent_message_id=str(data.get("parent_message_id", "")),
            worker_id=str(data.get("worker_id", "")),
            source_agent_type=str(data.get("source_agent_type", "")),
            target_agent_type=str(data.get("target_agent_type", "")),
            error_type=str(data.get("error_type", "")),
            error_message=str(data.get("error_message", "")),
            error_code=str(data.get("error_code", "")),
            failed_stage=str(data.get("failed_stage", "")),
            retryable=bool(data.get("retryable", False)),
            route_policy=str(data.get("route_policy", "")),
            route_status=str(data.get("route_status", "")),
            queue_wait_ms=int(data.get("queue_wait_ms", 0) or 0),
            chunk_count=int(data.get("chunk_count", 0) or 0),
            event_type=str(data.get("event_type", "")),
        )


@dataclass(frozen=True)
class EventRecord:
    """Session stream event model used by trace fallback readers."""

    event_id: str
    trace_id: str
    session_id: str
    message_id: str
    event_type: str
    content_type: str = ""
    timestamp: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return _clean_dict(payload)


@dataclass(frozen=True)
class TraceDiagnostic:
    """A trace query diagnostic emitted while reading or merging sources."""

    code: str
    message: str
    severity: str = "info"
    span_id: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(asdict(self))


@dataclass(frozen=True)
class SpanNode:
    """A span tree node."""

    span: SpanRecord
    children: list["SpanNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.span.to_dict(),
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class TraceReadResult:
    """Combined trace read result returned by TraceReadClient."""

    trace: TraceRecord
    executions: list[ExecutionRecord] = field(default_factory=list)
    spans: list[SpanRecord] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)
    tree: list[SpanNode] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    diagnostics: list[TraceDiagnostic] = field(default_factory=list)
    status: str = "ok"

    def diagnostic_codes(self) -> list[str]:
        return [diagnostic.code for diagnostic in self.diagnostics]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace": self.trace.to_dict(),
            "executions": [execution.to_dict() for execution in self.executions],
            "spans": [span.to_dict() for span in self.spans],
            "events": [event.to_dict() for event in self.events],
            "tree": [node.to_dict() for node in self.tree],
            "sources": self.sources,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "status": self.status,
        }
