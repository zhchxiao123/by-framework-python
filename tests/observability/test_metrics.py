"""Unit tests for observability metrics integration."""

from __future__ import annotations

from by_framework.observability.metrics import (
    PROMETHEUS_AVAILABLE,
    generate_latest_metrics,
    get_registry,
    record_execution_metrics,
)


def test_metrics_recording_does_not_raise_exception():
    """Metrics record helper functions under standard / dummy modes do not crash."""
    record_execution_metrics(
        status="COMPLETED",
        agent_type="dummy_agent",
        worker_id="test_worker_1",
        execution_ms=125.0,
        queue_wait_ms_val=45.0,
    )


def test_generate_latest_metrics_output():
    """Generating latest metrics produces expected output formats."""
    text = generate_latest_metrics()
    if PROMETHEUS_AVAILABLE:
        assert isinstance(text, str)
        # Verify the key metrics appear in registry outputs
        assert "by_framework_execution_status_total" in text
        assert "by_framework_execution_latency_ms" in text
        assert "by_framework_queue_wait_ms" in text
        assert 'status="COMPLETED"' in text
        assert 'agent_type="dummy_agent"' in text
    else:
        assert text == ""


def test_get_registry():
    """Active Prometheus collector registry mapping."""
    registry = get_registry()
    if PROMETHEUS_AVAILABLE:
        assert registry is not None
    else:
        assert registry is None
