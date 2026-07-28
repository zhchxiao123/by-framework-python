"""Expected-version atomic commit spike tests."""

import asyncio

import pytest
from redis.cluster import key_slot

from by_framework.agent.runtime import (
    CommitConflictError,
    CommitRequest,
    InMemoryRunStore,
    RedisRunStore,
    RunEvent,
)
from by_framework.common.constants import RedisKeys


def request(version: int, fence: int = 1) -> CommitRequest:
    return CommitRequest(
        run_id="run-1",
        expected_version=version,
        fencing_token=fence,
        events=(RunEvent("SuperstepCommitted", {"from": version}),),
        state={"count": version + 1},
    )


def test_only_one_concurrent_expected_version_commit_wins():
    store = InMemoryRunStore()

    async def commit_concurrently():
        return await asyncio.gather(
            store.commit(request(0)),
            store.commit(request(0)),
            return_exceptions=True,
        )

    results = asyncio.run(commit_concurrently())

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CommitConflictError) for result in results) == 1
    version, fence, state, events = store.snapshot("run-1")
    assert (version, fence, state, len(events)) == (1, 1, {"count": 1}, 1)


def test_store_rejects_a_stale_fencing_token():
    store = InMemoryRunStore()
    asyncio.run(store.commit(request(0, fence=4)))

    with pytest.raises(CommitConflictError):
        asyncio.run(store.commit(request(1, fence=3)))


def test_redis_commit_keys_are_in_the_same_cluster_slot():
    state_key, events_key = RedisKeys.native_run_commit_keys("run-42")
    assert key_slot(state_key.encode()) == key_slot(events_key.encode())
    assert "{run-42}" in state_key


class RecordingRedis:
    def __init__(self, result):
        self.result = result
        self.call = None

    async def eval(self, *args):
        self.call = args
        return self.result


def test_redis_store_uses_one_atomic_script_for_events_and_state():
    redis = RecordingRedis([1, 8, b"101-0"])
    store = RedisRunStore(redis)

    result = asyncio.run(
        store.commit(
            CommitRequest(
                run_id="run-atomic",
                expected_version=7,
                fencing_token=12,
                events=(RunEvent("Committed", {"ok": True}),),
                state={"value": "next"},
            )
        )
    )

    assert result.version == 8
    assert result.event_ids == ("101-0",)
    assert redis.call is not None
    assert redis.call[1] == 3
    state_key, events_key, coordinator_key = redis.call[2:5]
    assert (
        len(
            {
                key_slot(state_key.encode()),
                key_slot(events_key.encode()),
                key_slot(coordinator_key.encode()),
            }
        )
        == 1
    )
    assert "XADD" in redis.call[0]
    assert "HSET" in redis.call[0]
