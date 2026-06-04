"""In-memory metrics primitives for by-framework.

The framework currently has no hard dependency on an external metrics
backend (Prometheus / OpenTelemetry). These lightweight primitives
provide a stable, importable surface so that call sites can record
counters and gauges today and the storage layer can be swapped for an
OTel exporter later without touching every ``record_failure(...)``
callsite.

This module deliberately does not import any third-party metrics
library — it only depends on the standard library and the existing
project logger. Counters and gauges are process-local and
thread/coroutine-safe via a single :class:`threading.Lock` (which is
cheaper than the GIL on CPython and correct on free-threaded builds).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict, Optional, Tuple

from .logger import logger

# All metric values are stored as plain floats. Counters are always
# non-decreasing; gauges may go up or down.
_LabelKey = Tuple[Tuple[str, str], ...]


class InMemoryCounter:
    """Thread-safe monotonic counter with optional label dimensions.

    Values accumulate; reading ``value()`` returns the running total.
    ``inc()`` is the only mutation API; resetting is intentionally not
    exposed so that long-running workers cannot accidentally lose
    history.
    """

    def __init__(self, name: str, help_text: str = "") -> None:
        self._name = name
        self._help = help_text
        self._totals: Dict[_LabelKey, float] = defaultdict(float)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def help_text(self) -> str:
        return self._help

    def inc(
        self,
        amount: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        if amount < 0:
            # Counters must be monotonic.
            logger.warning(
                "[metrics] %s.inc called with negative amount=%s; ignoring",
                self._name,
                amount,
            )
            return
        key: _LabelKey = tuple(sorted((labels or {}).items()))
        with self._lock:
            self._totals[key] += amount

    def value(self, labels: Optional[Dict[str, str]] = None) -> float:
        key: _LabelKey = tuple(sorted((labels or {}).items()))
        with self._lock:
            return self._totals.get(key, 0.0)

    def snapshot(self) -> Dict[Dict[str, str], float]:
        """Return a point-in-time copy of all (labels -> value) pairs."""
        with self._lock:
            return [
                ({k: v for k, v in key}, value) for key, value in self._totals.items()
            ]

    def reset(self) -> None:
        """Clear all accumulated values. Tests only."""
        with self._lock:
            self._totals.clear()


class InMemoryGauge:
    """Thread-safe gauge that can move up or down."""

    def __init__(self, name: str, help_text: str = "") -> None:
        self._name = name
        self._help = help_text
        self._value: float = 0.0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def help_text(self) -> str:
        return self._help

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    def value(self) -> float:
        with self._lock:
            return self._value


# ----------------------------------------------------------------------
# Framework-defined metrics
# ----------------------------------------------------------------------
# Counted every time we degrade past a registry / persistence error
# (e.g. call_agent, dispatch_group). The ``operation`` label identifies
# which call site recorded the failure so that operators can correlate
# logs with the spike.
REGISTRY_FAILURES_COUNTER = InMemoryCounter(
    name="by_framework_registry_failures_total",
    help_text=(
        "Number of times the worker downgraded past a registry / "
        "execution-tracking failure (network, schema, or otherwise)."
    ),
)

# Counted when a stream/control payload could not be parsed and the
# message was acked anyway. Useful for detecting schema drift between
# producer and consumer.
MESSAGE_PARSE_FAILURES_COUNTER = InMemoryCounter(
    name="by_framework_message_parse_failures_total",
    help_text="Number of control/data messages skipped because they could not be parsed.",
)

# Counted when a plugin reload chain raised. Distinct from
# REGISTRY_FAILURES_COUNTER so that SREs can alert on plugin churn
# without confusing it with execution-tracking outages.
PLUGIN_RELOAD_FAILURES_COUNTER = InMemoryCounter(
    name="by_framework_plugin_reload_failures_total",
    help_text="Number of plugin reload attempts that raised before completion.",
)


def record_failure(
    counter: InMemoryCounter,
    *,
    operation: str,
    error: BaseException,
) -> None:
    """Increment ``counter`` and log a single-line degradation notice.

    The caller is expected to *also* have either re-raised the
    exception or produced a clear fallback log line; this helper only
    owns the metrics + a compact info log so that operators see both
    a structured counter and a human-readable explanation in the
    same place.
    """

    labels = {"operation": operation, "error_type": type(error).__name__}
    counter.inc(labels=labels)
    logger.info(
        "[metrics] %s operation=%s error_type=%s error=%s",
        counter.name,
        operation,
        type(error).__name__,
        error,
    )


__all__ = [
    "InMemoryCounter",
    "InMemoryGauge",
    "REGISTRY_FAILURES_COUNTER",
    "MESSAGE_PARSE_FAILURES_COUNTER",
    "PLUGIN_RELOAD_FAILURES_COUNTER",
    "record_failure",
]
