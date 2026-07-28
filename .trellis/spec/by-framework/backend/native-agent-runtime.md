# Native Agent Runtime Contracts

## Scenario: Extend the native Agent framework safely

### 1. Scope / Trigger

Use this spec when changing `src/by_framework/agent/`, the native deployment
bridge, Redis run keys, or the optional OpenAI-compatible/MCP packages.

The native framework is Agent-first and graph-capable. `Agent`, Team, and
`StateGraph` authoring APIs compile to immutable, versioned definitions and run
through one durable runtime. Existing `GatewayWorker`, LangGraph, ADK, and
plugin paths remain additive compatibility boundaries.

### 2. Signatures

Core boundaries:

```python
Agent.compile() -> AgentSpec
StateGraph.compile() -> GraphPlan
Runner.run(...) -> RunResult
Runner.run_streamed(...) -> AsyncIterator[StreamEvent]
RunStore.commit(CommitRequest) -> CommitResult
GraphRunner.resume(run_id, value, ...) -> GraphRunResult
```

Distributed boundaries:

```python
RedisCoordinatorStore.acquire(...)
RedisCoordinatorStore.lease_step(...)
RedisCoordinatorStore.submit_result(...)
NativeStepWorker.process_command(command, context)
```

Concrete provider and MCP implementations live in optional workspace packages.
Core model/tool protocols must not import their SDK types.

### 3. Contracts

- Persisted definitions accept only controlled JSON-domain values. There is no
  arbitrary object or pickle fallback.
- Plan identity is SHA-256 over canonical UTF-8 JSON and must include nested
  Agent/subgraph identities, model identity, tool behavior, Handoff filters,
  schemas, reducers, and relevant policies.
- Compiled definitions and nested schemas are deeply immutable.
- `RunEventStore` is authoritative; checkpoints are version-bound projections.
- Every run binds plan/catalog/state-schema versions.
- A Coordinator is the logical single writer. Step results bind run version,
  Coordinator fencing token, step lease token, attempt, and expiry.
- Redis keys participating in one Lua transaction share the run hash tag.
- Redis production leases use Redis `TIME`; client clocks are test-only.
- State commits are exactly-once. External side effects still require
  idempotency because a takeover can occur around handler execution.
- High-risk tools persist `ApprovalRequested`, `RunInterrupted`, and a pending
  checkpoint before execution. Approve/edit/reject decisions are correlated and
  idempotent; edited arguments are schema-validated.
- Legacy bridges are explicit opt-ins. Do not reroute old Worker execution
  automatically.
- Embedded and server deployment compile identical Agent definitions to the
  same plan hash.

### 4. Validation & Error Matrix

| Condition | Required result |
|---|---|
| Unsupported/non-finite persisted value | `SerializationError`; no partial write |
| Plan/schema changed incompatibly | Reject resume; never silently rebind |
| Expected run version mismatch | `CommitConflictError` |
| Expired/old Coordinator or Step lease | Reject as stale before state commit |
| Identical duplicate result/decision | Idempotent replay |
| Conflicting duplicate result/decision | Explicit conflict error |
| Tool input/output violates schema | Structured validation failure; no unsafe durable value |
| Risky tool lacks approval | Durable interrupt; no side effect |
| Edited approval input is invalid | Reject decision; do not execute tool |
| Stream fails after user-visible output | Do not transparently retry |
| Unknown Resume correlation | Reject without consuming the pending interrupt |
| Unsupported parallel/subgraph interrupt combination | Fail explicitly; do not claim resumability |

### 5. Good/Base/Bad Cases

- Good: a parallel superstep submits isolated writes, the Coordinator merges
  them deterministically, atomically commits events/state, then checkpoints.
- Base: an in-memory Runner uses the same contracts without Redis placement.
- Bad: a StepWorker writes global graph state directly.
- Bad: a provider type leaks into `by_framework.agent.model`.
- Bad: a queued remote dispatch acknowledgement is treated as a completed
  Agent result.
- Bad: a risky callable runs and only then emits an approval event.

### 6. Tests Required

- Canonical serialization and stable hash equivalence/rejection tests.
- Deep immutability tests for compiled schemas, tools, Agent, Team, and graph
  definitions.
- Store contract tests shared by in-memory and Redis implementations.
- Fencing/takeover, duplicate delivery, late result, and crash-window tests.
- Reducer determinism, parallel fan-in, retry/fallback, replay/fork, and
  interrupt/resume tests.
- FakeModel tests for multi-turn tools, Handoffs, Teams, budgets, and failures.
- Approval tests asserting durable ordering before observing a side effect.
- Embedded/server plan-hash equivalence and GatewayWorker regression tests.
- Provider/MCP fake-transport tests; live network certification is separate.
- Full `make test` and `make lint` before task completion.

### 7. Wrong vs Correct

#### Wrong

```python
# A worker result directly advances durable graph state.
await redis.hset(run_key, "state", json.dumps(step_result))

# A callable is persisted to make resume "easy".
checkpoint["node"] = pickle.dumps(node_callable)
```

#### Correct

```python
# A StepWorker only submits a fenced result. The Coordinator owns reduction
# and the expected-version commit.
await coordinator_store.submit_result(step_lease, state_writes)

# Persist stable references and controlled data; resolve executables from the
# immutable catalog deployed with the matching plan hash.
checkpoint["plan_hash"] = compiled_plan.plan_hash
checkpoint["node_id"] = node_id
```

## Known MVP Boundaries

- Real Redis Cluster/multi-process failover certification is separate from the
  deterministic Redis-double contract tests.
- Process-local executable handlers require the matching deployed code bundle.
- Local synchronous timeout/cancellation is cooperative.
- Reconstructing a new local Runner from a persisted approval suspension is not
  yet supported; server-side durable composition owns restart recovery.
- Live OpenAI-compatible HTTP/SSE and MCP stdio/SSE transports remain concrete
  transport-package responsibilities.
