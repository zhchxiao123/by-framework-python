"""Observability helpers and dashboard entry points."""

from .metrics import generate_latest_metrics, record_execution_metrics
from .snapshot import (
    build_demo_observability_snapshot,
    build_demo_session_observability_snapshot,
    build_demo_trace_observability_snapshot,
    build_execution_observability_snapshot,
    build_observability_snapshot,
    build_queue_observability_snapshot,
    build_trace_observability_snapshot,
    build_worker_observability_snapshot,
    load_history_from_redis,
    save_history_point_to_redis,
)
from .span_recorder import (
    LiveSpanHandle,
    SpanRecorder,
    TraceSpan,
    live_execution_otel_span,
)

__all__ = [
    "SpanRecorder",
    "TraceSpan",
    "LiveSpanHandle",
    "live_execution_otel_span",
    "record_execution_metrics",
    "generate_latest_metrics",
    "build_demo_observability_snapshot",
    "build_demo_session_observability_snapshot",
    "build_demo_trace_observability_snapshot",
    "build_execution_observability_snapshot",
    "build_observability_snapshot",
    "build_queue_observability_snapshot",
    "build_trace_observability_snapshot",
    "build_worker_observability_snapshot",
    "load_history_from_redis",
    "save_history_point_to_redis",
]
