"""Adapters from trace read SDK results to dashboard payloads."""

from __future__ import annotations

from typing import Any

from by_framework.trace import TraceReadResult
from by_framework.metrics.snapshot import _build_trace_snapshot


def trace_result_to_dashboard_trace(result: TraceReadResult) -> dict[str, Any]:
    """Convert a TraceReadResult into the legacy dashboard trace response shape."""
    trace = _build_trace_snapshot(
        result.trace.trace_id,
        result.trace.session_id,
        [span.to_dict() for span in result.spans],
    )
    trace["name"] = result.trace.name
    trace["input"] = result.trace.input
    trace["output"] = result.trace.output
    if result.trace.status:
        trace["status"] = result.trace.status
    trace["sources"] = result.sources
    trace["diagnostics"] = [diagnostic.to_dict() for diagnostic in result.diagnostics]
    return trace


def trace_result_to_dashboard_summary(result: TraceReadResult) -> dict[str, Any]:
    """Convert a TraceReadResult into the legacy dashboard trace summary shape."""
    trace = trace_result_to_dashboard_trace(result)
    return {
        "trace_id": trace.get("trace_id", ""),
        "session_id": trace.get("session_id", ""),
        "name": result.trace.name,
        "status": trace.get("status", ""),
        "start_ts": trace.get("start_ts", 0),
        "end_ts": trace.get("end_ts", 0),
        "duration_ms": trace.get("duration_ms", 0),
        "span_count": trace.get("span_count", 0),
        "sources": result.sources,
        "diagnostic_count": len(result.diagnostics),
    }
