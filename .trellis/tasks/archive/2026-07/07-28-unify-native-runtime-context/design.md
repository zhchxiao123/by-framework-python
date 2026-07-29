# Design: Unified Native Runtime Context

## Architecture

Keep the two existing modules because they own different state lifetimes:

```text
core.runtime                 agent.runtime
session scope                run scope
------------------------     --------------------------------
identity                     compiled plan/hash
private/shared files         event log
conversation history         checkpoint/materialized state
agent configuration          version/fencing/approval/usage
```

Connect them through a native `RunContext` seam rather than importing
`AgentContext` into the harness:

```text
Runner / GraphRunner / Team
             |
             v
      RunContext interface
         /          \
 LocalRunContext   WorkerRunContext adapter
                        |
                        v
                  AgentContext
                        |
                        v
                 AgentRuntimeState
```

## Contracts

### Run identity

`RunIdentity` is immutable and JSON-safe:

- `session_id`
- `run_id`
- `agent_id`
- `user_code`
- `user_name`
- trace-context mapping

The Runner resolves omitted identifiers once at run creation. A supplied
`run_id` and `RunContext.identity.run_id` must agree or fail before execution.

### Session capabilities

`RunContext` exposes optional, protocol-typed capabilities:

- private/shared file access
- conversation access/projection policy
- agent configuration lookup

The interface uses structural protocols so local fakes and existing core
runtime managers can act as adapters without inheritance.

### Tool execution context

`ToolExecutionContext` wraps the current run context plus tool-call identity.
A `FunctionTool` may declare one specially annotated context parameter. Tool
schema generation omits it, input validation ignores it, and `ToolExecutor`
injects it explicitly. Multiple context parameters or caller-supplied values
are definition errors.

This preserves a clean model-visible interface while making session resources
available without globals or closures.

### Durable representation

Each committed native state carries a small `runtime_context` projection
containing only identity and trace references. File managers, history backends,
Redis clients, configuration objects, and callables stay process-local.

Recovery validates the persisted projection against the reconstructed context.
Reconstruction itself remains the deployment adapter's responsibility.

### History ownership

`RunStore` is authoritative for the full execution transcript and state.
`HistoryManager` remains the conversational projection consumed by later tasks.

- In a Worker, existing `GatewayWorker`/`AgentContext` user and assistant
  persistence remains the only projector.
- Runner does not unconditionally write history.
- A future standalone projection policy can be supplied explicitly without
  changing the run engine.

## Integration Flow

1. `GatewayWorker` creates `AgentContext` and its `AgentRuntimeState`.
2. `NativeAgentWorker` derives deterministic `run_id`.
3. Worker adapter creates `RunContext` using the existing session managers and
   command trace identifiers.
4. `Runner` validates identity and commits the JSON-safe context projection.
5. Model and tools execute; context-aware tools receive
   `ToolExecutionContext`.
6. Native stream text is emitted through `AgentContext`.
7. Existing Worker completion logic performs the conversation projection once.

## Compatibility

- Context parameters are optional on all Runner entry points.
- Existing tools without a context parameter execute unchanged.
- Existing Worker output protocol and metadata remain unchanged.
- Existing `AgentContextRemoteDispatcher` remains the remote placement adapter.
- No Redis key or wire payload changes are required.

## Error Handling

- Identity mismatch: native definition/runtime error before first commit.
- Invalid context-aware tool signature: tool definition error at construction.
- Missing optional capability: explicit capability-unavailable error when
  accessed, not an `AttributeError`.
- Non-JSON trace metadata: validation error before durable commit.

## Rollback

The feature is additive. Reverting the new context arguments and Worker adapter
returns execution to the existing behavior; no persistent data migration or
Redis namespace rollback is needed.
