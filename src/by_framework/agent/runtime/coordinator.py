"""Lease and fencing primitives for a logical single-writer coordinator."""

from dataclasses import dataclass
from threading import Lock
from typing import Callable


class StaleStepResultError(RuntimeError):
    """A result belongs to an expired or superseded lease."""


@dataclass(frozen=True)
class CoordinatorLease:
    owner_id: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True)
class StepLease:
    step_id: str
    attempt: int
    run_version: int
    coordinator_token: int
    lease_token: int


@dataclass(frozen=True)
class StepResult:
    lease: StepLease
    writes: dict


class CoordinatorLeaseManager:
    """Thread-safe in-memory model of coordinator takeover semantics."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._lock = Lock()
        self._lease: CoordinatorLease | None = None
        self._next_fencing_token = 1
        self._step_tokens: dict[str, int] = {}

    def acquire(self, owner_id: str, ttl_seconds: float) -> CoordinatorLease | None:
        """Acquire an unowned/expired lease, or renew ownership without refencing."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self._clock()
        with self._lock:
            current = self._lease
            if (
                current is not None
                and current.owner_id != owner_id
                and current.expires_at > now
            ):
                return None
            if (
                current is not None
                and current.owner_id == owner_id
                and current.expires_at > now
            ):
                token = current.fencing_token
            else:
                token = self._next_fencing_token
                self._next_fencing_token += 1
            self._lease = CoordinatorLease(owner_id, token, now + ttl_seconds)
            return self._lease

    def lease_step(
        self,
        coordinator: CoordinatorLease,
        step_id: str,
        attempt: int,
        run_version: int,
    ) -> StepLease:
        """Create a step lease only for the current, live coordinator fence."""
        with self._lock:
            self._assert_current_locked(coordinator, self._clock())
            lease_token = self._step_tokens.get(step_id, 0) + 1
            self._step_tokens[step_id] = lease_token
            return StepLease(
                step_id=step_id,
                attempt=attempt,
                run_version=run_version,
                coordinator_token=coordinator.fencing_token,
                lease_token=lease_token,
            )

    def validate_result(
        self,
        coordinator: CoordinatorLease,
        result: StepResult,
        expected_run_version: int,
    ) -> None:
        """Reject results from an old coordinator, attempt, or state version."""
        with self._lock:
            self._assert_current_locked(coordinator, self._clock())
            lease = result.lease
            current_step_token = self._step_tokens.get(lease.step_id)
            if (
                lease.coordinator_token != coordinator.fencing_token
                or lease.lease_token != current_step_token
                or lease.run_version != expected_run_version
            ):
                raise StaleStepResultError(
                    f"stale result for step {lease.step_id!r} at run version "
                    f"{lease.run_version}"
                )

    def _assert_current(self, lease: CoordinatorLease) -> None:
        with self._lock:
            self._assert_current_locked(lease, self._clock())

    def _assert_current_locked(self, lease: CoordinatorLease, now: float) -> None:
        if self._lease != lease or lease.expires_at <= now:
            raise StaleStepResultError(
                f"coordinator fence {lease.fencing_token} is no longer current"
            )
