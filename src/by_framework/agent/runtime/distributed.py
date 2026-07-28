"""Redis-backed durable coordination and existing-transport adapters."""

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable

from by_framework.common.constants import RedisKeys
from by_framework.core.protocol.commands import AskAgentCommand
from by_framework.core.protocol.message_header import MessageHeader

from .coordinator import CoordinatorLease, StaleStepResultError, StepLease, StepResult
from .serialization import canonical_json, stable_plan_hash


class LeaseConflictError(RuntimeError):
    """A lease is currently owned by another coordinator or attempt."""


class DefinitionConflictError(RuntimeError):
    """A definition hash was already bound to different bytes."""


@dataclass(frozen=True)
class StoredCheckpoint:
    run_id: str
    version: int
    plan_hash: str
    state: dict[str, Any]


class RedisCheckpointStore:
    """Immutable versioned checkpoints with a per-run sorted index."""

    def __init__(self, redis):
        self._redis = redis

    async def put(self, checkpoint: StoredCheckpoint) -> None:
        payload = canonical_json(checkpoint).decode("utf-8")
        key = RedisKeys.native_run_checkpoint(checkpoint.run_id, checkpoint.version)
        created = await self._redis.set(key, payload, nx=True)
        if not created:
            existing = await self._redis.get(key)
            if _decode(existing) != payload:
                raise DefinitionConflictError(
                    f"checkpoint {checkpoint.run_id}:{checkpoint.version} conflicts"
                )
        await self._redis.zadd(
            RedisKeys.native_run_checkpoint_index(checkpoint.run_id),
            {str(checkpoint.version): checkpoint.version},
        )

    async def get(
        self, run_id: str, version: int | None = None
    ) -> StoredCheckpoint | None:
        if version is None:
            versions = await self._redis.zrevrange(
                RedisKeys.native_run_checkpoint_index(run_id), 0, 0
            )
            if not versions:
                return None
            version = int(_decode(versions[0]))
        payload = await self._redis.get(
            RedisKeys.native_run_checkpoint(run_id, version)
        )
        if payload is None:
            return None
        data = json.loads(_decode(payload))
        return StoredCheckpoint(
            data["run_id"], int(data["version"]), data["plan_hash"], data["state"]
        )


class RedisDefinitionStore:
    """Content-addressed immutable compiled definitions."""

    def __init__(self, redis):
        self._redis = redis

    async def put(self, definition: Any) -> str:
        payload = canonical_json(definition).decode("utf-8")
        definition_hash = stable_plan_hash(definition)
        key = RedisKeys.native_definition(definition_hash)
        created = await self._redis.set(key, payload, nx=True)
        if not created and _decode(await self._redis.get(key)) != payload:
            raise DefinitionConflictError(
                f"definition {definition_hash} conflicts with persisted bytes"
            )
        return definition_hash

    async def get(self, definition_hash: str) -> dict[str, Any] | None:
        payload = await self._redis.get(RedisKeys.native_definition(definition_hash))
        return None if payload is None else json.loads(_decode(payload))


_ACQUIRE_COORDINATOR_SCRIPT = """
-- native:coordinator-acquire
local owner = redis.call('HGET', KEYS[1], 'coordinator:owner')
local expires = tonumber(redis.call('HGET', KEYS[1], 'coordinator:expires') or '0')
local now
if ARGV[2] ~= '' then
  now = tonumber(ARGV[2])
else
  local server_time = redis.call('TIME')
  now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
end
if owner and owner ~= ARGV[1] and expires > now then
  return {0, redis.call('HGET', KEYS[1], 'coordinator:fence') or '0', expires}
end
local fence
if owner == ARGV[1] and expires > now then
  fence = tonumber(redis.call('HGET', KEYS[1], 'coordinator:fence'))
else
  fence = redis.call('HINCRBY', KEYS[1], 'coordinator:fence_counter', 1)
end
local next_expires = now + tonumber(ARGV[3])
redis.call('HSET', KEYS[1], 'coordinator:owner', ARGV[1],
  'coordinator:fence', fence, 'coordinator:expires', next_expires)
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[4]))
return {1, fence, next_expires}
"""

_RENEW_COORDINATOR_SCRIPT = """
-- native:coordinator-renew
local owner = redis.call('HGET', KEYS[1], 'coordinator:owner')
local fence = tonumber(redis.call('HGET', KEYS[1], 'coordinator:fence') or '0')
local expires = tonumber(redis.call('HGET', KEYS[1], 'coordinator:expires') or '0')
local now
if ARGV[3] ~= '' then
  now = tonumber(ARGV[3])
else
  local server_time = redis.call('TIME')
  now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
end
if owner ~= ARGV[1] or fence ~= tonumber(ARGV[2]) or expires <= now then
  return {0, fence, expires}
end
local next_expires = now + tonumber(ARGV[4])
redis.call('HSET', KEYS[1], 'coordinator:expires', next_expires)
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[5]))
return {1, fence, next_expires}
"""

_LEASE_STEP_SCRIPT = """
-- native:step-lease
local owner = redis.call('HGET', KEYS[1], 'coordinator:owner')
local fence = tonumber(redis.call('HGET', KEYS[1], 'coordinator:fence') or '0')
local expires = tonumber(redis.call('HGET', KEYS[1], 'coordinator:expires') or '0')
local now
if ARGV[3] ~= '' then
  now = tonumber(ARGV[3])
else
  local server_time = redis.call('TIME')
  now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
end
if owner ~= ARGV[1] or fence ~= tonumber(ARGV[2]) or expires <= now then
  return {0, fence}
end
local prefix = 'step:' .. ARGV[4] .. ':'
local current_attempt = tonumber(redis.call('HGET', KEYS[1], prefix .. 'attempt') or '0')
local current_version = tonumber(redis.call('HGET', KEYS[1], prefix .. 'version') or '-1')
local current_step_fence = tonumber(
  redis.call('HGET', KEYS[1], prefix .. 'coordinator_fence') or '0'
)
if current_version == tonumber(ARGV[6]) and current_step_fence == fence then
  if current_attempt == tonumber(ARGV[5]) then
    return {1, tonumber(redis.call('HGET', KEYS[1], prefix .. 'token'))}
  end
  if current_attempt > tonumber(ARGV[5]) then return {0, fence} end
end
local token = redis.call('HINCRBY', KEYS[1], prefix .. 'token', 1)
redis.call('HSET', KEYS[1], prefix .. 'attempt', ARGV[5],
  prefix .. 'version', ARGV[6], prefix .. 'coordinator_fence', fence,
  prefix .. 'expires', now + tonumber(ARGV[7]))
return {1, token}
"""

_SUBMIT_STEP_RESULT_SCRIPT = """
-- native:step-result
local prefix = 'step:' .. ARGV[1] .. ':'
local token = tonumber(redis.call('HGET', KEYS[1], prefix .. 'token') or '0')
local attempt = tonumber(redis.call('HGET', KEYS[1], prefix .. 'attempt') or '0')
local version = tonumber(redis.call('HGET', KEYS[1], prefix .. 'version') or '-1')
local fence = tonumber(redis.call('HGET', KEYS[1], prefix .. 'coordinator_fence') or '0')
local current_fence = tonumber(
  redis.call('HGET', KEYS[1], 'coordinator:fence') or '0'
)
local step_expires = tonumber(redis.call('HGET', KEYS[1], prefix .. 'expires') or '0')
local now
if ARGV[6] ~= '' then
  now = tonumber(ARGV[6])
else
  local server_time = redis.call('TIME')
  now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
end
if token ~= tonumber(ARGV[2]) or attempt ~= tonumber(ARGV[3])
  or version ~= tonumber(ARGV[4]) or fence ~= tonumber(ARGV[5]) then
  return {0}
end
local result_field = prefix .. 'result:' .. ARGV[2]
if redis.call('HEXISTS', KEYS[1], result_field) == 1 then
  if redis.call('HGET', KEYS[1], result_field) == ARGV[7] then return {2} end
  return {3}
end
if current_fence ~= fence or step_expires <= now then return {0} end
redis.call('HSET', KEYS[1], result_field, ARGV[7])
return {1}
"""

_VALIDATE_STEP_LEASE_SCRIPT = """
-- native:step-validate
local prefix = 'step:' .. ARGV[1] .. ':'
local token = tonumber(redis.call('HGET', KEYS[1], prefix .. 'token') or '0')
local attempt = tonumber(redis.call('HGET', KEYS[1], prefix .. 'attempt') or '0')
local version = tonumber(redis.call('HGET', KEYS[1], prefix .. 'version') or '-1')
local fence = tonumber(redis.call('HGET', KEYS[1], prefix .. 'coordinator_fence') or '0')
local current_fence = tonumber(
  redis.call('HGET', KEYS[1], 'coordinator:fence') or '0'
)
local step_expires = tonumber(redis.call('HGET', KEYS[1], prefix .. 'expires') or '0')
local now
if ARGV[6] ~= '' then
  now = tonumber(ARGV[6])
else
  local server_time = redis.call('TIME')
  now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
end
if token ~= tonumber(ARGV[2]) or attempt ~= tonumber(ARGV[3])
  or version ~= tonumber(ARGV[4]) or fence ~= tonumber(ARGV[5])
  or current_fence ~= fence or step_expires <= now then
  return {0}
end
return {1}
"""


class RedisCoordinatorStore:
    """Coordinator/step leases using one run-local Redis hash."""

    def __init__(
        self,
        redis,
        *,
        clock_ms: Callable[[], int] | None = None,
        retention_ms: int = 86_400_000,
    ):
        self._redis = redis
        self._clock_ms = clock_ms
        _positive(retention_ms, "retention_ms")
        self._retention_ms = retention_ms

    async def acquire(
        self, run_id: str, owner_id: str, ttl_ms: int
    ) -> CoordinatorLease | None:
        _positive(ttl_ms, "ttl_ms")
        self._validate_retention(ttl_ms)
        result = await self._redis.eval(
            _ACQUIRE_COORDINATOR_SCRIPT,
            1,
            RedisKeys.native_run_coordinator(run_id),
            owner_id,
            "" if self._clock_ms is None else self._clock_ms(),
            ttl_ms,
            self._retention_ms,
        )
        if int(result[0]) != 1:
            return None
        return CoordinatorLease(owner_id, int(result[1]), int(result[2]) / 1000)

    async def renew(
        self, run_id: str, lease: CoordinatorLease, ttl_ms: int
    ) -> CoordinatorLease:
        _positive(ttl_ms, "ttl_ms")
        self._validate_retention(ttl_ms)
        result = await self._redis.eval(
            _RENEW_COORDINATOR_SCRIPT,
            1,
            RedisKeys.native_run_coordinator(run_id),
            lease.owner_id,
            lease.fencing_token,
            "" if self._clock_ms is None else self._clock_ms(),
            ttl_ms,
            self._retention_ms,
        )
        if int(result[0]) != 1:
            raise LeaseConflictError("coordinator lease is stale or expired")
        return CoordinatorLease(lease.owner_id, int(result[1]), int(result[2]) / 1000)

    async def lease_step(
        self,
        run_id: str,
        coordinator: CoordinatorLease,
        step_id: str,
        attempt: int,
        run_version: int,
        ttl_ms: int,
    ) -> StepLease:
        _positive(attempt, "attempt")
        _positive(ttl_ms, "ttl_ms")
        self._validate_retention(ttl_ms)
        if run_version < 0:
            raise ValueError("run_version must not be negative")
        result = await self._redis.eval(
            _LEASE_STEP_SCRIPT,
            1,
            RedisKeys.native_run_coordinator(run_id),
            coordinator.owner_id,
            coordinator.fencing_token,
            "" if self._clock_ms is None else self._clock_ms(),
            step_id,
            attempt,
            run_version,
            ttl_ms,
        )
        if int(result[0]) != 1:
            raise LeaseConflictError("coordinator lease is stale or expired")
        return StepLease(
            step_id,
            attempt,
            run_version,
            coordinator.fencing_token,
            int(result[1]),
        )

    async def submit_result(self, run_id: str, result: StepResult) -> bool:
        lease = result.lease
        response = await self._redis.eval(
            _SUBMIT_STEP_RESULT_SCRIPT,
            1,
            RedisKeys.native_run_coordinator(run_id),
            lease.step_id,
            lease.lease_token,
            lease.attempt,
            lease.run_version,
            lease.coordinator_token,
            "" if self._clock_ms is None else self._clock_ms(),
            canonical_json(result.writes).decode("utf-8"),
        )
        status = int(response[0])
        if status == 0:
            raise StaleStepResultError(f"stale result for step {lease.step_id!r}")
        if status == 3:
            raise DefinitionConflictError(
                f"step {lease.step_id!r} submitted conflicting results"
            )
        return status == 1

    async def validate_step_lease(self, run_id: str, lease: StepLease) -> None:
        """Reject stale work before invoking a node handler."""
        response = await self._redis.eval(
            _VALIDATE_STEP_LEASE_SCRIPT,
            1,
            RedisKeys.native_run_coordinator(run_id),
            lease.step_id,
            lease.lease_token,
            lease.attempt,
            lease.run_version,
            lease.coordinator_token,
            "" if self._clock_ms is None else self._clock_ms(),
        )
        if int(response[0]) != 1:
            raise StaleStepResultError(f"stale lease for step {lease.step_id!r}")

    async def get_result(self, run_id: str, lease: StepLease) -> StepResult | None:
        """Recover a submitted result after a coordinator crash."""
        payload = await self._redis.hget(
            RedisKeys.native_run_coordinator(run_id),
            f"step:{lease.step_id}:result:{lease.lease_token}",
        )
        if payload is None:
            return None
        return StepResult(lease, json.loads(_decode(payload)))

    def _validate_retention(self, ttl_ms: int) -> None:
        if ttl_ms > self._retention_ms:
            raise ValueError("lease ttl_ms cannot exceed retention_ms")


@dataclass(frozen=True)
class StepDispatch:
    run_id: str
    plan_hash: str
    node_id: str
    lease: StepLease
    input_state: dict[str, Any]
    target_agent_type: str
    session_id: str
    trace_id: str


class RedisControlStepTransport:
    """Dispatch native steps through the existing AskAgent control stream."""

    def __init__(self, redis):
        self._redis = redis

    async def dispatch(self, dispatch: StepDispatch) -> str:
        message_id = (
            f"msg-native-step-{dispatch.run_id}-{dispatch.lease.step_id}-"
            f"{dispatch.lease.attempt}-{dispatch.lease.lease_token}"
        )
        command = AskAgentCommand(
            header=MessageHeader(
                message_id=message_id,
                session_id=dispatch.session_id,
                trace_id=dispatch.trace_id,
                target_agent_type=dispatch.target_agent_type,
                metadata={
                    "native_run_id": dispatch.run_id,
                    "native_step_id": dispatch.lease.step_id,
                },
            ),
            content=f"Execute native plan node {dispatch.node_id}",
            # Native Step Workers submit through RedisCoordinatorStore rather
            # than the legacy Agent return/callback path.
            wait_for_reply=False,
            extra_payload={
                "native_step": {
                    "run_id": dispatch.run_id,
                    "plan_hash": dispatch.plan_hash,
                    "node_id": dispatch.node_id,
                    "lease": asdict(dispatch.lease),
                    "input_state": dispatch.input_state,
                }
            },
        )
        await self._redis.xadd(
            RedisKeys.ctrl_stream(dispatch.target_agent_type),
            command.to_redis_payload(),
        )
        return message_id


class RedisRemoteResultStore:
    """Durable correlation for remote Agent interrupts and idempotent results."""

    def __init__(self, redis):
        self._redis = redis

    async def create(self, run_id: str, correlation_id: str) -> bool:
        key = RedisKeys.native_run_remote_results(run_id)
        created = await self._redis.hsetnx(
            key,
            correlation_id,
            canonical_json({"status": "pending"}).decode("utf-8"),
        )
        return bool(created)

    async def resolve(self, run_id: str, correlation_id: str, result: Any) -> bool:
        key = RedisKeys.native_run_remote_results(run_id)
        current = await self._redis.hget(key, correlation_id)
        if current is None:
            raise LeaseConflictError(f"correlation {correlation_id!r} was not created")
        payload = canonical_json(result).decode("utf-8")
        created = await self._redis.hsetnx(
            key,
            f"{correlation_id}:result",
            payload,
        )
        if created:
            return True
        existing = await self._redis.hget(key, f"{correlation_id}:result")
        if _decode(existing) != payload:
            raise DefinitionConflictError(
                f"correlation {correlation_id!r} resolved with conflicting results"
            )
        return False

    async def get(self, run_id: str, correlation_id: str) -> Any | None:
        payload = await self._redis.hget(
            RedisKeys.native_run_remote_results(run_id),
            f"{correlation_id}:result",
        )
        return None if payload is None else json.loads(_decode(payload))


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _positive(value: int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
