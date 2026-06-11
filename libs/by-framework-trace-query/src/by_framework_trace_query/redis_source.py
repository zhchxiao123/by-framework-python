"""Redis trace source for by-framework trace query SDK."""

from __future__ import annotations

import json
from typing import Any

from by_framework.common.constants import RedisKeys
from by_framework.common.redis_client import Redis, get_redis
from by_framework.core.registry import WorkerRegistry
from by_framework.metrics.snapshot import build_trace_observability_snapshot
from by_framework.trace import SpanRecord, TraceDiagnostic, TraceRecord


class RedisTraceSource:
    """Read trace records from by-framework Redis observability keys."""

    name = "redis"

    def __init__(self, redis_client: Redis | None = None) -> None:
        self.redis = redis_client or get_redis()

    async def get_trace(
        self, trace_id: str, *, session_id: str = ""
    ) -> tuple[TraceRecord, list[SpanRecord], list[TraceDiagnostic]]:
        meta = await self._read_trace_meta(trace_id)
        trace = TraceRecord.from_mapping({"trace_id": trace_id, **meta})
        if session_id and not trace.session_id:
            trace = TraceRecord(
                trace_id=trace.trace_id,
                name=trace.name,
                session_id=session_id,
                root_message_id=trace.root_message_id,
                root_agent_type=trace.root_agent_type,
                input=trace.input,
                output=trace.output,
                status=trace.status,
                start_ts=trace.start_ts,
                end_ts=trace.end_ts,
                metadata=trace.metadata,
            )

        spans = await self._read_stored_spans(trace_id)
        diagnostics: list[TraceDiagnostic] = []
        if spans:
            return trace, spans, diagnostics

        if trace.session_id:
            fallback = await build_trace_observability_snapshot(
                self.redis,
                trace_id,
                session_id=trace.session_id,
            )
            spans = [
                SpanRecord.from_mapping(span, source="session_fallback")
                for span in fallback.get("spans", [])
            ]
            if spans:
                diagnostics.append(
                    TraceDiagnostic(
                        code="source_partial",
                        message="Trace was reconstructed from session fallback data.",
                        severity="info",
                        source="session_fallback",
                    )
                )
        return trace, spans, diagnostics

    async def list_trace_ids(
        self,
        *,
        session_id: str = "",
        worker_id: str = "",
        agent_type: str = "",
        limit: int = 50,
    ) -> list[str]:
        key = ""
        if session_id:
            key = RedisKeys.trace_index_session(session_id)
        elif worker_id:
            key = RedisKeys.trace_index_worker(worker_id)
        elif agent_type:
            key = RedisKeys.trace_index_agent(agent_type)
        if not key:
            return []
        trace_ids = [
            str(value) for value in await self.redis.zrevrange(key, 0, limit - 1)
        ]
        if trace_ids or not session_id:
            return trace_ids
        registry = WorkerRegistry(self.redis)
        executions = await registry.get_all_session_executions(session_id)
        seen = set()
        fallback_ids = []
        for execution in sorted(
            executions,
            key=lambda item: int(item.get("created_at", 0) or 0),
            reverse=True,
        ):
            trace_id = str(execution.get("trace_id", ""))
            if not trace_id or trace_id in seen:
                continue
            fallback_ids.append(trace_id)
            seen.add(trace_id)
            if len(fallback_ids) >= limit:
                break
        return fallback_ids

    async def _read_trace_meta(self, trace_id: str) -> dict[str, Any]:
        meta = await self.redis.hgetall(RedisKeys.trace_meta(trace_id))
        return {self._decode(key): self._decode(value) for key, value in meta.items()}

    async def _read_stored_spans(self, trace_id: str) -> list[SpanRecord]:
        values = await self.redis.lrange(RedisKeys.trace_spans(trace_id), 0, -1)
        spans: list[SpanRecord] = []
        for value in values:
            decoded = self._decode(value)
            try:
                payload = json.loads(decoded) if isinstance(decoded, str) else decoded
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                spans.append(SpanRecord.from_mapping(payload, source=self.name))
        return spans

    @staticmethod
    def _decode(value: Any) -> Any:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value
