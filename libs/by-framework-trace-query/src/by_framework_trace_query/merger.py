"""Trace source merge and tree diagnostics."""

from __future__ import annotations

from by_framework.trace import (
    SpanNode,
    SpanRecord,
    TraceDiagnostic,
    TraceReadResult,
    TraceRecord,
)


class TraceMerger:
    """Merge spans from one or more sources into a stable trace tree."""

    def __init__(self, *, max_spans: int = 1000) -> None:
        self.max_spans = max(1, int(max_spans or 1000))

    def merge(
        self,
        trace: TraceRecord,
        spans: list[SpanRecord],
        *,
        sources: list[str] | None = None,
        diagnostics: list[TraceDiagnostic] | None = None,
    ) -> TraceReadResult:
        merged_diagnostics = list(diagnostics or [])
        deduped = self._dedupe_spans(spans)
        if len(deduped) > self.max_spans:
            merged_diagnostics.append(
                TraceDiagnostic(
                    code="span_count_exceeded",
                    message=f"Trace has {len(deduped)} spans, over limit {self.max_spans}.",
                    severity="warning",
                )
            )
            deduped = deduped[: self.max_spans]
        merged_diagnostics.extend(self._structural_diagnostics(trace, deduped))
        tree = self._build_tree(deduped, merged_diagnostics)
        status = (
            "partial"
            if any(d.severity == "error" for d in merged_diagnostics)
            else "ok"
        )
        if any(d.code.startswith("source_") for d in merged_diagnostics):
            status = "partial"
        return TraceReadResult(
            trace=trace,
            spans=deduped,
            tree=tree,
            sources=sources or [],
            diagnostics=merged_diagnostics,
            status=status,
        )

    def _dedupe_spans(self, spans: list[SpanRecord]) -> list[SpanRecord]:
        by_id: dict[str, SpanRecord] = {}
        for span in spans:
            if not span.span_id:
                continue
            current = by_id.get(span.span_id)
            if current is None or self._completeness_score(
                span
            ) >= self._completeness_score(current):
                by_id[span.span_id] = span
        return sorted(by_id.values(), key=lambda span: (span.start_ts, span.span_id))

    @staticmethod
    def _completeness_score(span: SpanRecord) -> int:
        payload = span.to_dict()
        score = len(payload)
        if span.input is not None:
            score += 5
        if span.output is not None:
            score += 5
        if span.tokens:
            score += 3
        if span.cost:
            score += 3
        return score

    def _structural_diagnostics(
        self, trace: TraceRecord, spans: list[SpanRecord]
    ) -> list[TraceDiagnostic]:
        diagnostics: list[TraceDiagnostic] = []
        operations = {span.operation or span.name for span in spans}
        if not any(operation.startswith("client.dispatch") for operation in operations):
            diagnostics.append(
                TraceDiagnostic(
                    code="missing_client_dispatch",
                    message="Trace has no client.dispatch span.",
                    severity="warning",
                )
            )
        if not any(operation == "worker.execute" for operation in operations):
            diagnostics.append(
                TraceDiagnostic(
                    code="missing_worker_execute",
                    message="Trace has no worker.execute span.",
                    severity="warning",
                )
            )
        by_id = {span.span_id: span for span in spans}
        for span in spans:
            if span.parent_span_id and span.parent_span_id not in by_id:
                diagnostics.append(
                    TraceDiagnostic(
                        code="missing_parent",
                        message=f"Span parent {span.parent_span_id} is missing.",
                        severity="warning",
                        span_id=span.span_id,
                        source=span.source,
                    )
                )
        if trace.output is None:
            diagnostics.append(
                TraceDiagnostic(
                    code="trace_output_missing",
                    message="Trace-level output is missing.",
                    severity="info",
                )
            )
        diagnostics.extend(self._cycle_diagnostics(spans))
        return diagnostics

    @staticmethod
    def _cycle_diagnostics(spans: list[SpanRecord]) -> list[TraceDiagnostic]:
        by_id = {span.span_id: span for span in spans}
        diagnostics: list[TraceDiagnostic] = []
        reported: set[str] = set()
        for span in spans:
            seen: set[str] = set()
            cursor = span
            while cursor.parent_span_id:
                parent_id = cursor.parent_span_id
                if parent_id in seen:
                    cycle_id = span.span_id
                    if cycle_id not in reported:
                        diagnostics.append(
                            TraceDiagnostic(
                                code="parent_cycle",
                                message="Span parent chain contains a cycle.",
                                severity="error",
                                span_id=cycle_id,
                                source=span.source,
                            )
                        )
                        reported.add(cycle_id)
                    break
                seen.add(cursor.span_id)
                parent = by_id.get(parent_id)
                if parent is None:
                    break
                cursor = parent
        return diagnostics

    def _build_tree(
        self,
        spans: list[SpanRecord],
        diagnostics: list[TraceDiagnostic],
    ) -> list[SpanNode]:
        by_id = {span.span_id: SpanNode(span=span, children=[]) for span in spans}
        cycle_ids = {
            diagnostic.span_id
            for diagnostic in diagnostics
            if diagnostic.code == "parent_cycle"
        }
        roots: list[SpanNode] = []
        for span in spans:
            node = by_id[span.span_id]
            parent = by_id.get(span.parent_span_id)
            if parent is None or span.span_id in cycle_ids:
                roots.append(node)
                continue
            parent.children.append(node)
        attached = {node.span.span_id for node in roots}
        attached.update(
            child.span.span_id for node in by_id.values() for child in node.children
        )
        for span_id, node in by_id.items():
            if span_id not in attached:
                roots.append(node)
        return roots
