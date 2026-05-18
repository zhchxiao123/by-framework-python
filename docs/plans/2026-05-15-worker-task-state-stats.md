# Worker Task State Stats Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add worker-level task state tracking so callers can inspect a worker's current active executions and historical processing state, complementing the existing session-level execution registry.

**Architecture:** Keep the existing session registry as the source of truth for per-execution detail, and add Redis worker indexes that point back to execution IDs by worker. Update these indexes whenever execution status changes, then expose query APIs that return worker summaries and optional execution detail. This keeps cancellation/session workflows unchanged while making worker observability efficient.

**Tech Stack:** Python 3, asyncio, Redis hashes/sets/sorted sets, existing `WorkerRegistry`, `WorkerRunner`, pytest, pytest-asyncio.

---

## Current System Notes

- Session execution state is stored in `RedisKeys.session_registry(session_id)` as a hash.
- Execution records are JSON fields named `exec:{execution_id}`.
- Message lookup is stored as `msg_map:{message_id} -> execution_id`.
- Sender-side requests call `WorkerRegistry.initialize_execution(...)` with `QUEUED`.
- Worker-side execution start calls `WorkerRegistry.update_execution_status(..., "RUNNING", worker_id=...)`.
- Completion and cancellation use `mark_execution_finished(...)` and `mark_execution_cancelling(...)`.
- Worker liveness currently uses `RedisKeys.worker_online_lease(worker_id)` and `RedisKeys.KNOWN_WORKERS`.

## Proposed Redis Model

Note: the final implementation moved from `execution_id -> session_id` references
to aggregate counters plus lightweight snapshots. The earlier
`worker_execution_refs(worker_id)` idea is obsolete and should not be implemented.

Add worker execution indexes in `src/by_framework/common/constants.py`:

```python
@staticmethod
def worker_executions(worker_id: str) -> str:
    """ZSET of execution IDs handled by a worker, scored by last update time."""
    return f"byai_gateway:registry:worker:executions:{worker_id}"

@staticmethod
def worker_active_executions(worker_id: str) -> str:
    """SET of currently non-terminal execution IDs for a worker."""
    return f"byai_gateway:registry:worker:active_executions:{worker_id}"

@staticmethod
def worker_execution_refs(worker_id: str) -> str:
    """HASH execution_id -> session_id for worker-level lookup."""
    return f"byai_gateway:registry:worker:execution_refs:{worker_id}"
```

The full execution payload stays in the session registry. Worker indexes are only lookup/aggregation support.

## Public API Shape

Add typed result helpers in `src/by_framework/core/registry.py`:

```python
async def get_worker_executions(
    self,
    worker_id: str,
    *,
    include_terminal: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ...

async def get_worker_execution_summary(self, worker_id: str) -> dict[str, Any]:
    ...
```

Summary shape:

```python
{
    "worker_id": "worker-1",
    "online": True,
    "agent_types": ["dummy_agent"],
    "last_seen": 1710000000000,
    "active_count": 2,
    "total_tracked": 37,
    "status_counts": {
        "QUEUED": 0,
        "RUNNING": 2,
        "COMPLETED": 30,
        "FAILED": 3,
        "CANCELLED": 2,
    },
    "active_executions": [...],
    "recent_executions": [...],
}
```

## Task 1: Add Worker Index Redis Keys

**Files:**
- Modify: `src/by_framework/common/constants.py`
- Test: `tests/core/test_registry.py`

**Step 1: Write the failing test**

Add assertions for the new key builders:

```python
def test_worker_execution_key_builders():
    assert RedisKeys.worker_executions("worker-1") == (
        "byai_gateway:registry:worker:executions:worker-1"
    )
    assert RedisKeys.worker_active_executions("worker-1") == (
        "byai_gateway:registry:worker:active_executions:worker-1"
    )
    assert RedisKeys.worker_execution_refs("worker-1") == (
        "byai_gateway:registry:worker:execution_refs:worker-1"
    )
```

**Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/core/test_registry.py::test_worker_execution_key_builders -q
```

Expected: FAIL because the key builders do not exist.

**Step 3: Implement the key builders**

Add the three static methods under the registry section of `RedisKeys`.

**Step 4: Run the test to verify it passes**

Run:

```bash
uv run pytest tests/core/test_registry.py::test_worker_execution_key_builders -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/by_framework/common/constants.py tests/core/test_registry.py
git commit -m "feat: add worker execution registry keys"
```

## Task 2: Maintain Worker Execution Indexes

**Files:**
- Modify: `src/by_framework/core/registry.py`
- Test: `tests/core/test_registry.py`

**Step 1: Extend `MockRedis` in tests**

Add support for the Redis commands needed by the new indexes:

```python
async def zadd(self, name, mapping): ...
async def zrevrange(self, name, start, end): ...
async def zcard(self, name): ...
async def hdel(self, name, *keys): ...
```

The file already has partial zset/hash support; extend it without changing existing behavior.

**Step 2: Write failing tests for RUNNING and terminal transitions**

Add a test that saves a queued execution, moves it to `RUNNING` with `worker_id`, then finishes it:

```python
@pytest.mark.asyncio
async def test_worker_execution_indexes_track_active_and_history():
    redis_mock = MockRedis()
    registry = WorkerRegistry(redis_mock)

    await registry.initialize_execution({
        "execution_id": "exec-1",
        "message_id": "msg-1",
        "session_id": "sess-1",
        "target_agent_type": "dummy_agent",
        "status": "QUEUED",
    })

    await registry.update_execution_status(
        "exec-1", "sess-1", "RUNNING", worker_id="worker-1"
    )

    assert "exec-1" in redis_mock.data[RedisKeys.worker_active_executions("worker-1")]
    assert redis_mock.data[RedisKeys.worker_execution_refs("worker-1")]["exec-1"] == "sess-1"

    await registry.mark_execution_finished("exec-1", "sess-1", "COMPLETED")

    assert "exec-1" not in redis_mock.data[RedisKeys.worker_active_executions("worker-1")]
    assert "exec-1" in redis_mock.data[RedisKeys.worker_executions("worker-1")]
```

**Step 3: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/core/test_registry.py::test_worker_execution_indexes_track_active_and_history -q
```

Expected: FAIL because worker indexes are not updated.

**Step 4: Add private registry helpers**

Implement:

```python
async def _index_worker_execution(self, execution: dict[str, Any], now: int) -> None:
    worker_id = str(execution.get("worker_id") or "")
    if not worker_id:
        return
    execution_id = str(execution["execution_id"])
    session_id = str(execution["session_id"])
    status = str(execution.get("status") or "")

    pipe = self.redis.pipeline()
    pipe.zadd(RedisKeys.worker_executions(worker_id), {execution_id: now})
    pipe.hset(RedisKeys.worker_execution_refs(worker_id), execution_id, session_id)
    if is_terminal_state(status):
        pipe.srem(RedisKeys.worker_active_executions(worker_id), execution_id)
    else:
        pipe.sadd(RedisKeys.worker_active_executions(worker_id), execution_id)
    pipe.expire(RedisKeys.worker_executions(worker_id), RedisKeys.DEFAULT_SESSION_TTL)
    pipe.expire(RedisKeys.worker_execution_refs(worker_id), RedisKeys.DEFAULT_SESSION_TTL)
    pipe.expire(RedisKeys.worker_active_executions(worker_id), RedisKeys.DEFAULT_SESSION_TTL)
    await pipe.execute()
```

Call the helper from:

- `save_execution(...)`
- `update_execution_status(...)`
- `mark_execution_cancelling(...)`
- `mark_cancel_requested(...)` only if `worker_id` exists and status remains non-terminal
- `mark_execution_finished(...)`

Do not index sender-side `QUEUED` records that do not have a worker assignment yet.

**Step 5: Run targeted tests**

Run:

```bash
uv run pytest tests/core/test_registry.py::test_worker_execution_indexes_track_active_and_history -q
uv run pytest tests/core/test_registry.py::test_registry_tracks_execution_lifecycle -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/by_framework/core/registry.py tests/core/test_registry.py
git commit -m "feat: index executions by worker"
```

## Task 3: Expose Worker Execution Queries

**Files:**
- Modify: `src/by_framework/core/registry.py`
- Test: `tests/core/test_registry.py`

**Step 1: Write failing tests for worker detail and summary**

Cover:

- `get_worker_executions("worker-1")` returns recent execution records.
- `include_terminal=False` returns only active executions.
- `get_worker_execution_summary("worker-1")` returns counts by status and active count.
- Missing/deleted session execution references are skipped gracefully.

**Step 2: Run the tests to verify failure**

Run:

```bash
uv run pytest tests/core/test_registry.py -k "worker_execution" -q
```

Expected: FAIL because query methods do not exist.

**Step 3: Implement query methods**

Implementation outline:

```python
async def get_worker_executions(
    self,
    worker_id: str,
    *,
    include_terminal: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    execution_ids = await self.redis.zrevrange(
        RedisKeys.worker_executions(worker_id), 0, max(limit - 1, 0)
    )
    refs = await self.redis.hgetall(RedisKeys.worker_execution_refs(worker_id))
    executions = []
    for execution_id in execution_ids:
        execution_id = execution_id.decode() if isinstance(execution_id, bytes) else execution_id
        session_id = refs.get(execution_id) or refs.get(execution_id.encode())
        if isinstance(session_id, bytes):
            session_id = session_id.decode("utf-8")
        if not session_id:
            continue
        execution = await self.get_execution(execution_id, session_id)
        if not execution:
            continue
        if not include_terminal and is_terminal_state(str(execution.get("status", ""))):
            continue
        executions.append(execution)
    return executions
```

Summary should reuse `get_all_workers()` for liveness/agent types when possible and compute `status_counts` from recent tracked records.

**Step 4: Run targeted tests**

Run:

```bash
uv run pytest tests/core/test_registry.py -k "worker_execution" -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/by_framework/core/registry.py tests/core/test_registry.py
git commit -m "feat: query worker execution state"
```

## Task 4: Add Runner-Level Current State Coverage

**Files:**
- Modify: `tests/worker/test_runner.py`
- Modify only if needed: `src/by_framework/worker/runner.py`

**Step 1: Write or adjust runner test**

Assert the runner writes enough information for worker-level tracking:

```python
worker.registry.update_execution_status.assert_awaited_once_with(
    "exec-queued",
    "sess-1",
    "RUNNING",
    worker_id="worker-1",
)
worker.registry.mark_execution_finished.assert_awaited_once_with(
    "exec-queued",
    "sess-1",
    "COMPLETED",
)
```

Existing tests already cover most of this; add only the missing assertion if needed.

**Step 2: Run runner tests**

Run:

```bash
uv run pytest tests/worker/test_runner.py -q
```

Expected: PASS.

**Step 3: Implement minimal runner change if the test exposes a gap**

Likely no production code is needed because `WorkerRunner` already passes `worker_id` when moving existing queued execution to `RUNNING`, and saves `worker_id` for worker-created execution records.

**Step 4: Commit if files changed**

```bash
git add tests/worker/test_runner.py src/by_framework/worker/runner.py
git commit -m "test: cover worker execution state transitions"
```

## Task 5: Add Optional Client Convenience API

**Files:**
- Modify: `src/by_framework/client/client.py`
- Test: `tests/client/test_client.py`

**Step 1: Decide API name**

Recommended:

```python
async def get_worker_status(self, worker_id: str) -> dict[str, Any]:
    if self.registry is None:
        raise WorkerRegistryNotSetError("get worker status")
    return await self.registry.get_worker_execution_summary(worker_id)
```

**Step 2: Write failing client test**

Use `AsyncMock` registry and assert delegation.

**Step 3: Run test**

```bash
uv run pytest tests/client/test_client.py -k "worker_status" -q
```

Expected: FAIL before implementation, PASS after.

**Step 4: Commit**

```bash
git add src/by_framework/client/client.py tests/client/test_client.py
git commit -m "feat: expose worker status through client"
```

## Task 6: Verification

**Files:**
- No production edits expected.

**Step 1: Run focused registry/client/runner tests**

```bash
uv run pytest tests/core/test_registry.py tests/worker/test_runner.py tests/client/test_client.py -q
```

Expected: PASS.

**Step 2: Run formatting**

```bash
make format
```

Expected: modifies only touched Python files if formatting is needed.

**Step 3: Run lint**

```bash
make lint
```

Expected: PASS.

**Step 4: Run full test suite if Redis-free test runtime is acceptable**

```bash
make test
```

Expected: PASS.

## Open Design Questions

- Should worker history be capped by count, time, or both? The first implementation can use `DEFAULT_SESSION_TTL` and query `limit`.
- Should `get_worker_execution_summary` count all tracked history or only the last `limit` records? Recommended first version: last 100 by default, configurable later.
- Should worker indexes survive worker shutdown? Recommended: yes, because historical state is the requested feature; active set should drain as executions finish.
- Should stuck active executions be reconciled when a worker goes offline? Recommended follow-up: add a later reconciliation pass that marks active records as `WORKER_LOST` or similar if product semantics need it.

## Acceptance Criteria

- A worker's currently running/cancelling tasks can be queried directly by `worker_id`.
- A worker's recent historical execution records can be queried without scanning every session.
- Summary includes status counts and online metadata.
- Existing session-level cancellation and execution tracking tests still pass.
- No Redis key names are hardcoded outside `RedisKeys`.
