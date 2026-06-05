"""Prometheus metrics definition and safe-recording helpers."""

from __future__ import annotations

from typing import Any, Optional

try:
    from prometheus_client import REGISTRY, Counter, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None


class DummyMetric:
    """Fallback no-op metric wrapper when prometheus-client is missing."""

    def __init__(
        self, name: str, documentation: str, labelnames: list[str] = None
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames or []

    def labels(self, *args: Any, **kwargs: Any) -> DummyMetric:
        del args, kwargs
        return self

    def inc(self, amount: float = 1.0) -> None:
        del amount

    def observe(self, amount: float) -> None:
        del amount

    def set(self, value: float) -> None:
        del value


def get_registry() -> Any:
    """Get the active Prometheus collector registry."""
    return REGISTRY


# Global metric singletons
if PROMETHEUS_AVAILABLE:
    execution_status_total = Counter(
        "by_framework_execution_status_total",
        "Total count of executions by final status, agent type, and worker id.",
        ["status", "agent_type", "worker_id"],
    )
    execution_latency_ms = Histogram(
        "by_framework_execution_latency_ms",
        "Execution latency in milliseconds.",
        ["status", "agent_type", "worker_id"],
        buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
    )
    queue_wait_ms = Histogram(
        "by_framework_queue_wait_ms",
        "Queue waiting time in Redis Streams in milliseconds.",
        ["agent_type"],
        buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000),
    )
else:
    execution_status_total = DummyMetric(
        "by_framework_execution_status_total",
        "Total count of executions by final status, agent type, and worker id.",
        ["status", "agent_type", "worker_id"],
    )
    execution_latency_ms = DummyMetric(
        "by_framework_execution_latency_ms",
        "Execution latency in milliseconds.",
        ["status", "agent_type", "worker_id"],
    )
    queue_wait_ms = DummyMetric(
        "by_framework_queue_wait_ms",
        "Queue waiting time in Redis Streams in milliseconds.",
        ["agent_type"],
    )


def record_execution_metrics(
    *,
    status: str,
    agent_type: str,
    worker_id: str,
    execution_ms: float,
    queue_wait_ms_val: Optional[float] = None,
) -> None:
    """Record worker execution metrics safely."""
    try:
        execution_status_total.labels(
            status=status, agent_type=agent_type, worker_id=worker_id
        ).inc()
        execution_latency_ms.labels(
            status=status, agent_type=agent_type, worker_id=worker_id
        ).observe(execution_ms)
        if queue_wait_ms_val is not None and queue_wait_ms_val >= 0:
            queue_wait_ms.labels(agent_type=agent_type).observe(queue_wait_ms_val)
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def generate_latest_metrics() -> str:
    """Generate latest prometheus metrics exposition representation."""
    if not PROMETHEUS_AVAILABLE or REGISTRY is None:
        return ""
    try:
        from prometheus_client import generate_latest

        return generate_latest(REGISTRY).decode("utf-8")
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
