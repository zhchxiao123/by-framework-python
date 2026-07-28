"""Coordinator fencing and late-result rejection spike tests."""

import pytest

from by_framework.agent.runtime import (
    CoordinatorLeaseManager,
    StaleStepResultError,
    StepResult,
)


def test_takeover_refences_and_rejects_old_coordinator_and_step_results():
    now = [100.0]
    manager = CoordinatorLeaseManager(lambda: now[0])
    first = manager.acquire("coordinator-a", ttl_seconds=10)
    assert first is not None
    old_step = manager.lease_step(first, "step-1", attempt=1, run_version=3)

    assert manager.acquire("coordinator-b", ttl_seconds=10) is None
    now[0] = 111.0
    second = manager.acquire("coordinator-b", ttl_seconds=10)
    assert second is not None
    assert second.fencing_token > first.fencing_token

    with pytest.raises(StaleStepResultError):
        manager.validate_result(second, StepResult(old_step, {"x": 1}), 3)
    with pytest.raises(StaleStepResultError):
        manager.lease_step(first, "step-2", attempt=1, run_version=3)


def test_retry_supersedes_prior_step_lease_and_version_is_checked():
    manager = CoordinatorLeaseManager(lambda: 100.0)
    coordinator = manager.acquire("coordinator", ttl_seconds=10)
    assert coordinator is not None
    first = manager.lease_step(coordinator, "step", attempt=1, run_version=4)
    retry = manager.lease_step(coordinator, "step", attempt=2, run_version=4)

    with pytest.raises(StaleStepResultError):
        manager.validate_result(coordinator, StepResult(first, {}), 4)
    manager.validate_result(coordinator, StepResult(retry, {}), 4)
    with pytest.raises(StaleStepResultError):
        manager.validate_result(coordinator, StepResult(retry, {}), 5)


def test_same_owner_reacquisition_after_expiry_refences_old_step_results():
    now = [100.0]
    manager = CoordinatorLeaseManager(lambda: now[0])
    first = manager.acquire("coordinator", ttl_seconds=10)
    assert first is not None
    old_step = manager.lease_step(first, "step", attempt=1, run_version=4)

    now[0] = 111.0
    reacquired = manager.acquire("coordinator", ttl_seconds=10)

    assert reacquired is not None
    assert reacquired.fencing_token > first.fencing_token
    with pytest.raises(StaleStepResultError):
        manager.validate_result(reacquired, StepResult(old_step, {}), 4)
