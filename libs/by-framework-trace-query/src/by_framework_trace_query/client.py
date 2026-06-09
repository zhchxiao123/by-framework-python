"""Trace read client facade."""

from __future__ import annotations

from by_framework.trace import TraceDiagnostic, TraceReadResult

from .merger import TraceMerger
from .redis_source import RedisTraceSource


class TraceReadClient:
    """Read by-framework traces from one or more sources."""

    def __init__(
        self,
        *,
        redis_client=None,
        sources: list[RedisTraceSource] | None = None,
        max_spans: int = 1000,
    ) -> None:
        self.sources = sources or [RedisTraceSource(redis_client)]
        self.merger = TraceMerger(max_spans=max_spans)

    async def get_trace(
        self, trace_id: str, *, session_id: str = ""
    ) -> TraceReadResult:
        diagnostics: list[TraceDiagnostic] = []
        all_spans = []
        trace = None
        source_names: list[str] = []
        for source in self.sources:
            try:
                source_trace, spans, source_diagnostics = await source.get_trace(
                    trace_id, session_id=session_id
                )
                trace = (
                    source_trace
                    if trace is None
                    else self._prefer_trace(trace, source_trace)
                )
                all_spans.extend(spans)
                diagnostics.extend(source_diagnostics)
                source_names.append(source.name)
            except Exception as err:  # pylint: disable=broad-exception-caught
                diagnostics.append(
                    TraceDiagnostic(
                        code="source_timeout",
                        message=f"Trace source {source.name} failed: {err}",
                        severity="error",
                        source=source.name,
                    )
                )
        if trace is None:
            from by_framework.trace import TraceRecord

            trace = TraceRecord(trace_id=trace_id, session_id=session_id)
        return self.merger.merge(
            trace,
            all_spans,
            sources=source_names,
            diagnostics=diagnostics,
        )

    async def list_traces(
        self,
        *,
        session_id: str = "",
        worker_id: str = "",
        agent_type: str = "",
        limit: int = 50,
    ) -> list[TraceReadResult]:
        trace_ids: list[str] = []
        seen = set()
        for source in self.sources:
            try:
                ids = await source.list_trace_ids(
                    session_id=session_id,
                    worker_id=worker_id,
                    agent_type=agent_type,
                    limit=limit,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                continue
            for trace_id in ids:
                if trace_id in seen:
                    continue
                trace_ids.append(trace_id)
                seen.add(trace_id)
                if len(trace_ids) >= limit:
                    break
        return [
            await self.get_trace(trace_id, session_id=session_id)
            for trace_id in trace_ids[:limit]
        ]

    async def explain_trace(
        self, trace_id: str, *, session_id: str = ""
    ) -> dict[str, object]:
        result = await self.get_trace(trace_id, session_id=session_id)
        return {
            "trace_id": trace_id,
            "status": result.status,
            "sources": result.sources,
            "diagnostics": [diagnostic.to_dict() for diagnostic in result.diagnostics],
            "span_count": len(result.spans),
        }

    @staticmethod
    def _prefer_trace(current, candidate):
        current_payload = current.to_dict()
        candidate_payload = candidate.to_dict()
        return candidate if len(candidate_payload) > len(current_payload) else current
