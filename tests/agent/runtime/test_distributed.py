"""Redis durable-runtime contracts using a deterministic script double."""

import asyncio
import json

import pytest
from redis.cluster import key_slot

from by_framework.agent.runtime import (
    CommitConflictError,
    CommitRequest,
    DefinitionConflictError,
    LeaseConflictError,
    RedisCheckpointStore,
    RedisControlStepTransport,
    RedisCoordinatorStore,
    RedisDefinitionStore,
    RedisRemoteResultStore,
    RedisRunStore,
    RunEvent,
    StaleStepResultError,
    StepDispatch,
    StepResult,
    StoredCheckpoint,
)
from by_framework.common.constants import RedisKeys


class FakeRedis:
    """Small Redis behavioral double for the native Lua contracts."""

    def __init__(self):
        self.hashes = {}
        self.strings = {}
        self.zsets = {}
        self.streams = {}
        self.fail_zadd_once = False
        self.now_ms = 1_000

    async def eval(self, script, key_count, key, *argv):
        del key_count
        values = self.hashes.setdefault(key, {})
        if "native:run-load" in script:
            (events_key,) = argv
            return [
                values.get("version", 0),
                values.get("fencing_token", 0),
                values.get("state", ""),
                *[fields["data"] for _, fields in self.streams.get(events_key, [])],
            ]
        if "native:coordinator-acquire" in script:
            owner, now, ttl, retention = argv
            del retention
            now = self.now_ms if now == "" else now
            now, ttl = int(now), int(ttl)
            current_owner = values.get("coordinator:owner")
            expires = int(values.get("coordinator:expires", 0))
            if current_owner and current_owner != owner and expires > now:
                return [0, int(values["coordinator:fence"]), expires]
            if current_owner == owner and expires > now:
                fence = int(values["coordinator:fence"])
            else:
                fence = int(values.get("coordinator:fence_counter", 0)) + 1
                values["coordinator:fence_counter"] = fence
            values.update(
                {
                    "coordinator:owner": owner,
                    "coordinator:fence": fence,
                    "coordinator:expires": now + ttl,
                }
            )
            return [1, fence, now + ttl]
        if "native:coordinator-renew" in script:
            owner, fence, now, ttl, retention = argv
            del retention
            now = self.now_ms if now == "" else now
            if (
                values.get("coordinator:owner") != owner
                or int(values.get("coordinator:fence", 0)) != int(fence)
                or int(values.get("coordinator:expires", 0)) <= int(now)
            ):
                return [0, int(values.get("coordinator:fence", 0)), 0]
            values["coordinator:expires"] = int(now) + int(ttl)
            return [1, int(fence), values["coordinator:expires"]]
        if "native:step-lease" in script:
            owner, fence, now, step_id, attempt, version, ttl = argv
            now = self.now_ms if now == "" else now
            if (
                values.get("coordinator:owner") != owner
                or int(values.get("coordinator:fence", 0)) != int(fence)
                or int(values.get("coordinator:expires", 0)) <= int(now)
            ):
                return [0, int(values.get("coordinator:fence", 0))]
            prefix = f"step:{step_id}:"
            current_attempt = int(values.get(f"{prefix}attempt", 0))
            if int(values.get(f"{prefix}version", -1)) == int(version) and int(
                values.get(f"{prefix}coordinator_fence", 0)
            ) == int(fence):
                if current_attempt == int(attempt):
                    return [1, int(values[f"{prefix}token"])]
                if current_attempt > int(attempt):
                    return [0, int(fence)]
            token = int(values.get(f"{prefix}token", 0)) + 1
            values.update(
                {
                    f"{prefix}token": token,
                    f"{prefix}attempt": int(attempt),
                    f"{prefix}version": int(version),
                    f"{prefix}coordinator_fence": int(fence),
                    f"{prefix}expires": int(now) + int(ttl),
                }
            )
            return [1, token]
        if "native:step-result" in script:
            step_id, token, attempt, version, fence, now, payload = argv
            now = self.now_ms if now == "" else now
            prefix = f"step:{step_id}:"
            identity_is_stale = (
                int(values.get(f"{prefix}token", 0)) != int(token)
                or int(values.get(f"{prefix}attempt", 0)) != int(attempt)
                or int(values.get(f"{prefix}version", -1)) != int(version)
                or int(values.get(f"{prefix}coordinator_fence", 0)) != int(fence)
            )
            if identity_is_stale:
                return [0]
            field = f"{prefix}result:{token}"
            if field in values:
                return [2] if values[field] == payload else [3]
            if int(values.get("coordinator:fence", 0)) != int(fence) or int(
                values.get(f"{prefix}expires", 0)
            ) <= int(now):
                return [0]
            values[field] = payload
            return [1]
        if "native:step-validate" in script:
            step_id, token, attempt, version, fence, now = argv
            now = self.now_ms if now == "" else now
            prefix = f"step:{step_id}:"
            valid = (
                int(values.get(f"{prefix}token", 0)) == int(token)
                and int(values.get(f"{prefix}attempt", 0)) == int(attempt)
                and int(values.get(f"{prefix}version", -1)) == int(version)
                and int(values.get(f"{prefix}coordinator_fence", 0)) == int(fence)
                and int(values.get("coordinator:fence", 0)) == int(fence)
                and int(values.get(f"{prefix}expires", 0)) > int(now)
            )
            return [1 if valid else 0]
        # Milestone 0 atomic state/event commit script.
        events_key = argv[0]
        coordinator_key = argv[1]
        expected, fence, state, *events = argv[2:]
        current_version = int(values.get("version", 0))
        current_fence = int(values.get("fencing_token", 0))
        coordinator_fence = int(
            self.hashes.get(coordinator_key, {}).get("coordinator:fence", 0)
        )
        if (
            current_version != int(expected)
            or int(fence) < current_fence
            or int(fence) < coordinator_fence
        ):
            return [0, current_version, max(current_fence, coordinator_fence)]
        event_ids = []
        for payload in events:
            event_id = f"{len(self.streams.setdefault(events_key, [])) + 1}-0"
            self.streams[events_key].append((event_id, {"data": payload}))
            event_ids.append(event_id)
        values.update(
            {
                "version": current_version + 1,
                "fencing_token": int(fence),
                "state": state,
            }
        )
        return [1, current_version + 1, *event_ids]

    async def set(self, key, value, nx=False):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def get(self, key):
        return self.strings.get(key)

    async def zadd(self, key, mapping):
        if self.fail_zadd_once:
            self.fail_zadd_once = False
            raise ConnectionError("crash after checkpoint SET")
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key, start, end):
        del start, end
        values = self.zsets.get(key, {})
        return [
            member
            for member, _ in sorted(
                values.items(), key=lambda item: item[1], reverse=True
            )[:1]
        ]

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hsetnx(self, key, field, value):
        values = self.hashes.setdefault(key, {})
        if field in values:
            return False
        values[field] = value
        return True

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def xrange(self, key, min="-", max="+"):  # pylint: disable=redefined-builtin
        return list(self.streams.get(key, []))

    async def xadd(self, key, payload):
        event_id = f"{len(self.streams.setdefault(key, [])) + 1}-0"
        self.streams[key].append((event_id, payload))
        return event_id


def test_all_run_transaction_keys_share_the_cluster_hash_slot():
    run_id = "cluster-run"
    keys = [
        *RedisKeys.native_run_commit_keys(run_id),
        RedisKeys.native_run_coordinator(run_id),
        RedisKeys.native_run_checkpoint_index(run_id),
        RedisKeys.native_run_checkpoint(run_id, 3),
        RedisKeys.native_run_remote_results(run_id),
    ]
    assert len({key_slot(key.encode()) for key in keys}) == 1


def test_coordinator_takeover_step_retry_and_late_result_rejection():
    redis = FakeRedis()
    now = [1_000]
    store = RedisCoordinatorStore(redis, clock_ms=lambda: now[0])

    first = asyncio.run(store.acquire("run-1", "owner-a", 100))
    assert first is not None
    assert asyncio.run(store.acquire("run-1", "owner-b", 100)) is None
    step = asyncio.run(store.lease_step("run-1", first, "step-1", 1, 3, 100))
    duplicate_lease = asyncio.run(store.lease_step("run-1", first, "step-1", 1, 3, 100))
    assert duplicate_lease == step

    now[0] = 1_101
    second = asyncio.run(store.acquire("run-1", "owner-b", 100))
    assert second is not None
    assert second.fencing_token > first.fencing_token
    with pytest.raises(LeaseConflictError):
        asyncio.run(store.renew("run-1", first, 100))

    retry = asyncio.run(store.lease_step("run-1", second, "step-1", 2, 3, 100))
    with pytest.raises(StaleStepResultError):
        asyncio.run(store.submit_result("run-1", StepResult(step, {"old": True})))
    assert asyncio.run(store.submit_result("run-1", StepResult(retry, {"new": True})))
    assert not asyncio.run(
        store.submit_result("run-1", StepResult(retry, {"new": True}))
    )
    with pytest.raises(DefinitionConflictError):
        asyncio.run(
            store.submit_result("run-1", StepResult(retry, {"different": True}))
        )


def test_server_clock_is_default_and_expired_step_result_is_rejected():
    redis = FakeRedis()
    store = RedisCoordinatorStore(redis)
    coordinator = asyncio.run(store.acquire("server-time", "owner", 100))
    assert coordinator is not None
    step = asyncio.run(store.lease_step("server-time", coordinator, "step", 1, 0, 50))

    redis.now_ms = 1_051
    with pytest.raises(StaleStepResultError):
        asyncio.run(
            store.submit_result("server-time", StepResult(step, {"late": True}))
        )


def test_submitted_step_result_survives_coordinator_crash_before_commit():
    redis = FakeRedis()
    now = [1_000]
    store = RedisCoordinatorStore(redis, clock_ms=lambda: now[0])
    first = asyncio.run(store.acquire("crash-run", "owner-a", 100))
    assert first is not None
    step = asyncio.run(store.lease_step("crash-run", first, "step-1", 1, 0, 100))
    asyncio.run(store.submit_result("crash-run", StepResult(step, {"value": 7})))

    # Owner A crashes after result submit and before the state transition commit.
    now[0] = 1_101
    second = asyncio.run(store.acquire("crash-run", "owner-b", 100))
    assert second is not None
    assert asyncio.run(store.get_result("crash-run", step)) == StepResult(
        step, {"value": 7}
    )


def test_checkpoint_crash_window_is_repaired_and_definition_is_immutable():
    redis = FakeRedis()
    checkpoints = RedisCheckpointStore(redis)
    checkpoint = StoredCheckpoint("run-2", 4, "sha256:plan", {"value": 4})
    redis.fail_zadd_once = True
    with pytest.raises(ConnectionError):
        asyncio.run(checkpoints.put(checkpoint))

    # Retrying sees the immutable payload and repairs the missing index.
    asyncio.run(checkpoints.put(checkpoint))
    assert asyncio.run(checkpoints.get("run-2")) == checkpoint

    definitions = RedisDefinitionStore(redis)
    definition = {"schema_version": 1, "nodes": [{"id": "node"}]}
    definition_hash = asyncio.run(definitions.put(definition))
    assert asyncio.run(definitions.put(definition)) == definition_hash
    assert asyncio.run(definitions.get(definition_hash)) == definition


def test_atomic_commit_recovery_and_duplicate_delivery_conflict():
    redis = FakeRedis()
    store = RedisRunStore(redis)
    request = CommitRequest(
        "run-3",
        0,
        5,
        (RunEvent("SuperstepCommitted", {"step": "one"}),),
        {"value": 1},
    )
    result = asyncio.run(store.commit(request))
    assert result.version == 1

    # Duplicate transport delivery cannot advance the same expected version.
    with pytest.raises(CommitConflictError):
        asyncio.run(store.commit(request))
    recovered = asyncio.run(store.load("run-3"))
    assert recovered.version == 1
    assert recovered.fencing_token == 5
    assert recovered.state == {"value": 1}
    assert recovered.events == request.events


def test_takeover_fence_rejects_old_commit_before_new_owner_commits():
    redis = FakeRedis()
    now = [1_000]
    coordinators = RedisCoordinatorStore(redis, clock_ms=lambda: now[0])
    runs = RedisRunStore(redis)
    first = asyncio.run(coordinators.acquire("fenced-run", "owner-a", 100))
    assert first is not None
    now[0] = 1_101
    second = asyncio.run(coordinators.acquire("fenced-run", "owner-b", 100))
    assert second is not None

    with pytest.raises(CommitConflictError):
        asyncio.run(
            runs.commit(
                CommitRequest(
                    "fenced-run",
                    0,
                    first.fencing_token,
                    (RunEvent("OldOwnerCommit", {}),),
                    {"value": "stale"},
                )
            )
        )


def test_step_dispatch_uses_existing_ask_agent_control_stream():
    redis = FakeRedis()
    leases = RedisCoordinatorStore(redis, clock_ms=lambda: 1_000)
    coordinator = asyncio.run(leases.acquire("run-4", "owner", 1_000))
    assert coordinator is not None
    lease = asyncio.run(leases.lease_step("run-4", coordinator, "node-1", 1, 0, 1_000))
    dispatch = StepDispatch(
        "run-4",
        "sha256:plan",
        "node",
        lease,
        {"input": "value"},
        "native-step-worker",
        "session-1",
        "trace-1",
    )

    message_id = asyncio.run(RedisControlStepTransport(redis).dispatch(dispatch))
    assert message_id == "msg-native-step-run-4-node-1-1-1"
    _, payload = next(iter(redis.streams.values()))[0]
    command = json.loads(payload["data"])
    assert command["action_type"] == "ASK_AGENT"
    native = command["body"]["extra_payload"]["native_step"]
    assert command["body"]["wait_for_reply"] is False
    assert native["lease"]["coordinator_token"] == coordinator.fencing_token
    assert native["input_state"] == {"input": "value"}


def test_remote_result_is_durable_and_duplicate_resume_is_idempotent():
    redis = FakeRedis()
    store = RedisRemoteResultStore(redis)
    assert asyncio.run(store.create("run-5", "remote-1"))
    assert not asyncio.run(store.create("run-5", "remote-1"))
    assert asyncio.run(store.get("run-5", "remote-1")) is None
    assert asyncio.run(store.resolve("run-5", "remote-1", {"answer": 42}))
    assert not asyncio.run(store.resolve("run-5", "remote-1", {"answer": 42}))
    with pytest.raises(DefinitionConflictError):
        asyncio.run(store.resolve("run-5", "remote-1", {"answer": 99}))
    assert asyncio.run(store.get("run-5", "remote-1")) == {"answer": 42}
