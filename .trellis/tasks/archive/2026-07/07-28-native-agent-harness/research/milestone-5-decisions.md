# Milestone 5 Deployment Decisions

- `NativeAgentWorker` is a normal `GatewayWorker`. It advertises catalog Agent
  names, processes `AskAgentCommand`, streams normalized text through the
  existing `AgentContext`, and returns the normal `AgentTaskResult`.
- Embedded and server modes resolve the same `CompiledAgent` and plan hash.
  Catalog snapshots bind Agent names to immutable plan hashes and reject a
  mismatched definition at execution time. Registration never replaces the
  process-local executable for an already-bound name, even when another runtime
  object compiles to the same persisted plan hash.
- `NativeStepWorker` is also a normal `GatewayWorker`; `WorkerRunner` remains the
  only Redis control-stream consumer. The Worker resolves executable node
  handlers by plan hash/node ID and submits fenced results through the Redis
  coordinator store. It recovers an already-submitted result before executing a
  duplicate delivery and validates a fresh lease before invoking the handler.
- `AgentServer` is a composition root for the definition catalog, Agent Runner,
  GraphRunner, coordinator store, step-handler registry, and durable remote
  result store. It does not introduce a separate scheduling runtime.
- Durable remote result resolution is wired to `GraphRunner.resume`; duplicate
  result delivery returns the already-recorded terminal result or retries a
  process-local interrupted resume after the result was durably stored.
  Correlation IDs must match the active interrupt, and pending correlations are
  created automatically when a server graph interrupts.
- `NativeCommandService` provides the minimal start/register/run/status/cancel/
  resume command surface as callable methods. A Typer CLI can wrap this service
  without duplicating command behavior.

## Validation Boundary

- Deployment tests run in one process with deterministic models and storage
  doubles. They verify lifecycle composition and identical plan identity, not
  operating-system process supervision or Redis Cluster failover.
- Executable Python node handlers remain process-local and are resolved from the
  immutable plan/node registry. Production server bootstrapping must load the
  matching code bundle before accepting a catalog snapshot.
- Server discovery, authentication, access control, long-running daemon
  supervision, and network APIs remain deployment-product work rather than
  hidden inside the runtime.
- Lease validation closes stale deliveries before handler invocation, but a
  takeover can still occur between validation and the handler's external side
  effect. Side-effecting handlers must therefore honor idempotency keys; the
  framework does not claim exactly-once external effects.
- Automatic resume retries rely on the current process-local `GraphRunner`
  suspension. Reconstructing suspended graph execution after a server process
  restart requires the Redis-backed graph recovery/catalog bootstrapping slice.
