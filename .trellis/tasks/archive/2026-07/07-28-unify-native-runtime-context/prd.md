# Unify Native Agent Runtime Context

## Goal

Unify the native agent harness with `src/by_framework/core/runtime` so that a
native run executed inside a `GatewayWorker` uses the existing session identity,
file managers, history policy, agent configuration, and trace metadata without
coupling the standalone harness to `AgentContext`.

The resulting model must distinguish session-scoped capabilities from durable
run-scoped execution state and keep the native `Runner` usable without Redis or
a Worker.

## Confirmed Facts

- `AgentContext` creates one `AgentRuntimeState` per task and owns Worker-facing
  streaming, history flush, remote-agent dispatch, and trace metadata.
- `AgentRuntimeState` currently aggregates `SessionManager` and
  `AgentConfigManager`; `SessionManager` owns private/shared file managers and
  `HistoryManager`.
- Native `Runner` owns model/tool execution and commits versioned run events,
  state, checkpoints, approvals, and usage through `RunStore`.
- `NativeAgentWorker` already reuses `GatewayWorker`, but currently passes only
  agent input and `run_id` into `Runner`.
- `AgentContextRemoteDispatcher` already adapts native remote-agent dispatch to
  `AgentContext.call_agent()`.
- Worker history currently persists user input at task start and buffered
  assistant text at completion; adding a second unconditional writer would
  duplicate conversation messages.

## Requirements

1. Introduce a small native runtime-context interface that represents immutable
   execution identity and optional session-scoped capabilities.
2. Model `session_id`, `run_id`, and `agent_id` separately:
   - a session may contain multiple runs;
   - every run has one stable `run_id`;
   - child-agent executions may share a session but have their own run IDs.
3. Extend `Runner` and graph/team execution paths to accept an optional runtime
   context while preserving existing calls that provide no context.
4. Provide a local context implementation for standalone/offline execution.
5. Provide a Worker adapter that derives the native context from `AgentContext`
   without making `agent.runtime` import the Worker package.
6. Make private/shared file access and conversation-history access available to
   native tool execution through an explicit tool execution context; injected
   context must not appear in the model-visible JSON schema.
7. Persist only JSON-safe identity and trace references in native run state.
   Live managers/backends must never be serialized into checkpoints.
8. Preserve a single history writer:
   - Worker-hosted runs continue to use the existing Worker history lifecycle;
   - standalone runs do not persist conversation history unless a context
     explicitly supplies a projection policy;
   - complete model/tool transcripts remain authoritative in `RunStore`.
9. `NativeAgentWorker` must construct and pass the Worker runtime adapter and
   retain current stream/result behavior.
10. Existing `Agent`, `Runner`, `FunctionTool`, graph, Team, external-agent, and
    Worker usage must remain backward compatible.
11. Document the session/run distinction and both standalone and Worker-hosted
    composition paths.

## Out of Scope

- Replacing `HistoryManager` backends or migrating existing history data.
- Merging `RunStore` and `BaseHistoryBackend` into one storage interface.
- Changing Redis control/data-stream protocols or key schema.
- Making `AgentConfigManager` the source of truth for compiled native plans.
- Cross-process reconstruction of arbitrary Python tool implementations.
- Adding a new memory/RAG product abstraction.

## Acceptance Criteria

- [x] Existing `Runner.run()` and `run_streamed()` calls work unchanged.
- [x] A standalone run receives a local runtime context with consistent
      `session_id`, `run_id`, and `agent_id`.
- [x] `NativeAgentWorker` passes session identity, user identity, trace metadata,
      file managers, history access, and agent configuration access through the
      Worker adapter.
- [x] A tool can opt into the execution context without exposing that parameter
      in its tool schema.
- [x] Native checkpoints and `RunStore` state contain context identity/references
      but no live manager/backend objects.
- [x] Worker-hosted native runs do not duplicate user or assistant history.
- [x] Local Agent, graph/Team, Worker integration, durability, approval, and
      external-agent tests pass.
- [x] New public contracts are exported and documented.

## Risks and Deferred Items

- Tool-context injection changes signature inspection and requires focused
  schema-validation regressions.
- History projection is deliberately policy-driven; automatic persistence in
  every Runner would conflict with the existing Worker lifecycle.
- A future distributed executor will need to reconstruct session capability
  adapters from persisted identity rather than serialize live objects.
