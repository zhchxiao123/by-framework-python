"""Atomic event/state commit contract and spike implementations."""

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock
from typing import Any

from by_framework.common.constants import RedisKeys

from .serialization import canonical_json


class CommitConflictError(RuntimeError):
    """The expected version or coordinator fence no longer matches."""


@dataclass(frozen=True)
class RunEvent:
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CommitRequest:
    run_id: str
    expected_version: int
    fencing_token: int
    events: tuple[RunEvent, ...]
    state: dict[str, Any]


@dataclass(frozen=True)
class CommitResult:
    version: int
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class RunSnapshot:
    """Recovered materialized state plus its authoritative event history."""

    version: int
    fencing_token: int
    state: dict[str, Any]
    events: tuple[RunEvent, ...]


class RunStore(ABC):
    """Store boundary for exactly-once committed run transitions."""

    @abstractmethod
    async def commit(self, request: CommitRequest) -> CommitResult:
        """Append events and advance state iff version/fence still match."""


class InMemoryRunStore(RunStore):
    """Deterministic contract implementation for focused runtime tests."""

    def __init__(self):
        self._lock = Lock()
        self._runs: dict[str, tuple[int, int, dict[str, Any], list[RunEvent]]] = {}

    async def commit(self, request: CommitRequest) -> CommitResult:
        canonical_json(request.state)
        for event in request.events:
            canonical_json(event)
        with self._lock:
            version, fence, _, events = self._runs.get(
                request.run_id, (0, request.fencing_token, {}, [])
            )
            if version != request.expected_version or request.fencing_token < fence:
                raise CommitConflictError(
                    f"run {request.run_id!r} expected version "
                    f"{request.expected_version}/fence {request.fencing_token}, "
                    f"found {version}/fence {fence}"
                )
            next_version = version + 1
            first_sequence = len(events) + 1
            event_ids = tuple(
                f"{request.run_id}:{sequence}"
                for sequence in range(
                    first_sequence, first_sequence + len(request.events)
                )
            )
            self._runs[request.run_id] = (
                next_version,
                request.fencing_token,
                copy.deepcopy(request.state),
                [*events, *copy.deepcopy(request.events)],
            )
            return CommitResult(next_version, event_ids)

    def snapshot(
        self, run_id: str
    ) -> tuple[int, int, dict[str, Any], tuple[RunEvent, ...]]:
        """Inspect committed state in tests and local spike tooling."""
        version, fence, state, events = self._runs.get(run_id, (0, 0, {}, []))
        return version, fence, copy.deepcopy(state), copy.deepcopy(tuple(events))


_COMMIT_SCRIPT = """
local current_version = tonumber(redis.call('HGET', KEYS[1], 'version') or '0')
local current_fence = tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0')
local coordinator_fence = tonumber(
  redis.call('HGET', KEYS[3], 'coordinator:fence') or '0'
)
local expected_version = tonumber(ARGV[1])
local proposed_fence = tonumber(ARGV[2])
if current_version ~= expected_version or proposed_fence < current_fence
  or proposed_fence < coordinator_fence then
  return {0, current_version, math.max(current_fence, coordinator_fence)}
end
local event_ids = {}
for index = 4, #ARGV do
  event_ids[#event_ids + 1] = redis.call('XADD', KEYS[2], '*', 'data', ARGV[index])
end
local next_version = current_version + 1
redis.call(
  'HSET', KEYS[1],
  'version', next_version,
  'fencing_token', proposed_fence,
  'state', ARGV[3]
)
local result = {1, next_version}
for _, event_id in ipairs(event_ids) do result[#result + 1] = event_id end
return result
"""

_LOAD_SCRIPT = """
-- native:run-load
local version = redis.call('HGET', KEYS[1], 'version') or '0'
local fence = redis.call('HGET', KEYS[1], 'fencing_token') or '0'
local state = redis.call('HGET', KEYS[1], 'state') or ''
local entries = redis.call('XRANGE', KEYS[2], '-', '+')
local result = {version, fence, state}
for _, entry in ipairs(entries) do
  local fields = entry[2]
  for index = 1, #fields, 2 do
    if fields[index] == 'data' then
      result[#result + 1] = fields[index + 1]
      break
    end
  end
end
return result
"""


class RedisRunStore(RunStore):
    """Redis Cluster-safe commit using one atomic Lua invocation."""

    def __init__(self, redis):
        self._redis = redis

    async def commit(self, request: CommitRequest) -> CommitResult:
        state_json = canonical_json(request.state).decode("utf-8")
        event_json = [canonical_json(event).decode("utf-8") for event in request.events]
        keys = (
            *RedisKeys.native_run_commit_keys(request.run_id),
            RedisKeys.native_run_coordinator(request.run_id),
        )
        result = await self._redis.eval(
            _COMMIT_SCRIPT,
            len(keys),
            *keys,
            request.expected_version,
            request.fencing_token,
            state_json,
            *event_json,
        )
        if int(result[0]) != 1:
            raise CommitConflictError(
                f"run {request.run_id!r} expected version "
                f"{request.expected_version}/fence {request.fencing_token}, "
                f"found {int(result[1])}/fence {int(result[2])}"
            )
        return CommitResult(
            version=int(result[1]),
            event_ids=tuple(_decode(item) for item in result[2:]),
        )

    async def load(self, run_id: str) -> RunSnapshot:
        """Recover state and ordered events after coordinator takeover."""
        keys = RedisKeys.native_run_commit_keys(run_id)
        loaded = await self._redis.eval(_LOAD_SCRIPT, len(keys), *keys)
        events = []
        for raw in loaded[3:]:
            data = json.loads(_decode(raw))
            events.append(RunEvent(data["kind"], data["payload"]))
        raw_state = _decode(loaded[2])
        return RunSnapshot(
            version=int(loaded[0]),
            fencing_token=int(loaded[1]),
            state={} if not raw_state else json.loads(raw_state),
            events=tuple(events),
        )


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
