# Native Agent Framework — Implementation Plan

## Delivery Strategy

Implement as vertical milestones. Each milestone must produce an executable, tested capability and preserve all legacy tests. Do not begin with a broad directory scaffold containing unexercised abstractions.

## Milestone 0 — Technical Spikes

- Prototype Redis Cluster-safe atomic event append/state-version advance with optimistic concurrency.
- Prototype Coordinator lease/fencing takeover and rejection of stale Step results.
- Prototype controlled serialization and stable plan hashing.
- Validate an OpenAI-compatible streaming response can normalize text, tool calls and usage without leaking SDK types.
- Record spike decisions before public API implementation.

Exit: the three highest-risk contracts—atomic commit, takeover, serialization—have passing focused tests.

## Milestone 1 — Core Types and Single-Agent Vertical Slice

- Add typed model messages, model protocol, stream events and structured errors.
- Add `ToolSpec`, `FunctionTool`, tool schema generation and `ToolExecutor`.
- Add authoring `Agent`, compiled `AgentSpec` and plan compiler.
- Add `FakeModel` / `ScriptedModel`.
- Implement an in-memory event/checkpoint store.
- Implement a local Coordinator and single Agent model/tool loop.
- Add `Runner.run`, `Runner.run_streamed` and result types.

Exit: a deterministic Agent uses multiple model turns and tools, streams output and can be replayed in memory.

## Milestone 2 — Graph Engine

- Add typed state schemas and reducers.
- Add graph builder, compiler validation and plan inspection.
- Implement ordinary/conditional edges, loops, fan-out/fan-in and parallel supersteps.
- Add subgraphs, retry/timeout/fallback and run budgets.
- Add checkpoint, replay, fork, cancel and generic interrupt/resume.

Exit: graph tests cover deterministic merge, partial parallel failure, resume and replay without model dependencies.

## Milestone 3 — Multi-Agent APIs

- Compile `AgentTool` and local sub-Agent calls.
- Implement `Handoff` with explicit history/input filtering.
- Add `SupervisorTeam`, `HandoffTeam`, `ParallelTeam`, `WorkflowTeam`.
- Aggregate usage, budgets and trace context across nested Agent runs.
- Verify every Team exposes an inspectable ExecutionPlan.

Exit: all four Team modes pass deterministic FakeModel scenarios including failure and interrupt paths.

## Milestone 4 — Redis Durable Runtime

- Implement Redis event, checkpoint and definition stores.
- Implement atomic expected-version commits and fencing tokens.
- Add Coordinator lease acquisition, renewal, takeover and recovery.
- Add distributed Step leases, attempts and late-result rejection.
- Map Step dispatch onto existing Worker/Redis transport without duplicating WorkerRunner lifecycle.
- Add duplicate-delivery and crash-window integration tests.

Exit: multi-process tests demonstrate Coordinator failover and exactly-once state transition.

## Milestone 5 — Worker and Server Deployment

- Add `NativeAgentWorker` embedded entry point.
- Add `AgentServer` Coordinator and generic StepWorker services.
- Add catalog registration and immutable snapshot binding.
- Add CLI commands for start/register/run/status/cancel/resume.
- Verify identical Agent code and plan hash in both deployment modes.

Exit: one sample Agent/Team runs unchanged in embedded and server modes.

## Milestone 6 — Provider and MCP Packages

- Add OpenAI-compatible provider package with streaming, structured output, tool calls, usage and error mapping.
- Add optional MCP toolset package with discovery, session lifecycle, cancellation and error normalization.
- Add provider contract tests with recorded/fake transports; keep network tests optional.

Exit: end-to-end native Agent runs through the official provider and MCP adapter without provider types entering core.

## Milestone 7 — Compatibility, Approval, and Observability

- Add `AgentConfig` to `Agent`/`AgentSpec` adapter and catalog coexistence.
- Map existing `ask_user`, `call_agent`, ResumeCommand and tracing into native interrupts/events.
- Add approval policy and approve/edit/reject flows.
- Add span hierarchy, redaction controls, metrics and structured terminal states.
- Run LangGraph, ADK, plugin and Worker regression suites.

Exit: legacy paths remain unchanged and native paths are fully observable.

## Validation

Run focused tests during each milestone, then:

```bash
make format
make lint
make test
```

Required additional suites:

- property tests for reducers, plan hashing and replay equivalence;
- concurrency tests for expected-version commits and fencing;
- crash-window tests around lease, side effect, result submit and commit;
- compatibility tests for persisted plan/schema versions;
- contract tests shared by in-memory and Redis stores;
- contract tests shared by FakeModel and provider implementations;
- security tests for schema validation, redaction and unsafe deserialization rejection.

## Risky Areas and Rollback Points

- Redis atomic commit/Cluster slot design: keep behind store interface until spike passes.
- Changes to `AgentContext` and Worker control handling: additive adapters only; preserve direct legacy branch.
- Public API names: mark experimental until Milestone 3 validates compilation boundaries.
- Serialization format: version from first persisted artifact and never silently reinterpret.
- Provider/MCP dependencies: isolate in workspace packages so core can roll back independently.

## Pre-Start Review Gates

- Confirm milestone slicing and whether separate child Trellis tasks will own each milestone.
- Confirm public naming (`Agent`, `Runner`, `StateGraph`, `AgentServer`) before exporting.
- Complete Redis atomicity spike before committing production key schemas.
- Curate implementation/check context for the first selected milestone rather than dispatching the entire roadmap as one task.
