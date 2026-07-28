# Native Agent Framework — Technical Design

## 1. Architectural Position

`by-framework` evolves from a distributed Worker scheduling engine into a full Agent Framework. The existing Worker runtime remains the transport, isolation, routing, and lifecycle substrate. A new native programming and durable execution layer sits above it.

```text
Developer APIs
  Agent | Tool | Team | StateGraph | Runner
                    |
                    v
Compilers
  AgentCompiler | TeamCompiler | GraphCompiler
                    |
                    v
Versioned ExecutionPlan / AgentSpec / StateSchema
                    |
                    v
Durable Runtime
  RunCoordinator | Scheduler | Reducers | Checkpoint/Replay
                    |
          +---------+---------+
          |                   |
          v                   v
Local Step Executor     Distributed Step Workers
          |                   |
          +---------+---------+
                    v
Worker / AgentContext / Redis Streams / Workspace / Sandbox
```

External LangGraph and ADK integrations remain peers of the native framework. They may participate as remote Agent nodes, but the native engine does not depend on them.

## 2. Bounded Responsibilities

| Component | Owns | Does not own |
|---|---|---|
| Worker runtime | Redis consumption, heartbeat, workspace, sandbox setup, task lifecycle, wire protocol | Model loop, graph semantics |
| Native Agent Harness | Message preparation, model turns, tool loop, output validation, budgets | Cluster membership, transport |
| Graph Engine | plan validation, scheduling, reducers, interrupts, replay, checkpoints | Provider-specific model SDK behavior |
| RunCoordinator | single-writer run transition, leases, supersteps, commits | Arbitrary node business logic |
| StepWorker | leased node execution and result submission | Global run advancement |
| Model provider | provider request translation and stream normalization | Agent orchestration |
| ToolExecutor | invocation policy, timeout, retry, approval, sandbox/remote execution | Model selection |
| Run stores | events, checkpoints, optimistic concurrency | User-facing conversation assembly |
| Conversation projection | model-ready transcript and history views | Authoritative orchestration state |

## 3. Core Domain Model

### 3.1 Authoring objects

- `Agent`: name, instructions, model, tools, handoffs, output schema, guardrails, policies.
- `Team`: members plus a collaboration compiler (`SupervisorTeam`, `HandoffTeam`, `ParallelTeam`, `WorkflowTeam`).
- `StateGraph[StateT, ContextT]`: nodes, edges, reducers, entry/exit and subgraphs.
- `Tool`: typed capability definition independent of execution placement.

Authoring objects may contain Python callables and are not persisted directly.

### 3.2 Compiled objects

- `AgentSpec`: normalized and validated Agent definition using stable references.
- `ExecutionPlan`: nodes, edges, routing rules, policies, subplan references and version hash.
- `StateSchema`: field schemas, defaults, reducers and migration version.
- `CatalogSnapshot`: immutable set of AgentSpec/ToolSpec/Plan definitions bound to a run.

Compiled objects must use controlled, versioned serialization. Callable implementations are resolved through a registry by stable reference, not serialized as arbitrary code.

### 3.3 Runtime objects

- `Run`: one invocation of an ExecutionPlan.
- `Superstep`: a set of nodes runnable against the same committed state version.
- `Step`: one node execution attempt.
- `StateWrite`: a node's proposed updates, merged only by the Coordinator.
- `Interrupt`: durable suspension awaiting user, approval, agent return, timer, or external signal.
- `Checkpoint`: materialized state, frontier, pending steps, interrupts and version bindings.

## 4. Agent and Team Compilation

A normal Agent compiles to a prebuilt loop:

```text
prepare_input -> call_model -> route_output
                   ^              |
                   |              +-> final
                   |              +-> execute_tools --+
                   |              +-> handoff ---------+
                   +-----------------------------------+
```

Team compilers produce ordinary plans:

- `SupervisorTeam`: supervisor loop plus AgentTool nodes and synthesis.
- `HandoffTeam`: active-agent state and control-transfer edges.
- `ParallelTeam`: fan-out member nodes plus deterministic/synthesizer join.
- `WorkflowTeam`: explicit sequence/condition/loop compiled directly to graph edges.

Plan inspection must reveal generated nodes and policies so high-level APIs are not opaque.

## 5. Model Protocol

The core model SPI normalizes:

- ordered input items/messages;
- system/developer instructions;
- tool declarations and tool choice;
- structured output schema;
- streaming deltas;
- tool-call deltas and completed calls;
- reasoning/provider metadata;
- usage and finish reason;
- retryable, rate-limit, authentication, invalid-request and provider errors.

`FakeModel` and `ScriptedModel` emit deterministic scripted turns. The OpenAI-compatible implementation lives in an optional provider package and translates provider-native events into the core event model.

## 6. Tool Protocol

`ToolSpec` contains stable name/version, description, input/output schemas, side-effect classification, risk, idempotency behavior and execution requirements.

Tool forms:

- `FunctionTool`: typed Python callable.
- `AgentTool`: local subplan or remote `AgentContext.call_agent`.
- `Handoff`: control transfer with explicit input/history filtering.
- `MCPToolset`: dynamic discovery adapter that yields ToolSpecs.

`ToolExecutor` selects local, sandbox or remote execution. Every call has a stable call ID and attempt. Approval produces an interrupt before side effects. Tool results and failures are durable events.

## 7. State, Events, and Checkpoints

### 7.1 Event source

Representative events:

- `RunCreated`, `PlanBound`, `RunStarted`;
- `SuperstepScheduled`, `StepLeased`, `StepStarted`;
- `ModelStarted`, `ModelDelta`, `ModelCompleted`;
- `ToolRequested`, `ApprovalRequested`, `ApprovalResolved`, `ToolCompleted`;
- `AgentDispatched`, `AgentReturned`, `HandoffCommitted`;
- `StateWritesSubmitted`, `SuperstepCommitted`, `CheckpointSaved`;
- `RunInterrupted`, `RunResumed`, `RunCancelled`, `RunCompleted`, `RunFailed`.

Events have `run_id`, monotonic sequence, event ID, causation/correlation IDs, schema version and optional trace IDs.

### 7.2 Commit protocol

1. Coordinator reads committed state/frontier at version N.
2. It creates stable Step leases for the next superstep.
3. StepWorkers execute and submit results tagged with run version, lease and fencing token.
4. Coordinator validates all required results.
5. Reducers merge writes deterministically.
6. One atomic commit appends transition events and advances state to N+1.
7. A checkpoint is written according to policy; event log remains authoritative.

Redis implementation must co-locate transaction keys by run hash tag or use a documented equivalent compatible with Redis Cluster.

## 8. Coordinator and Failure Semantics

- Coordinator ownership uses renewable leases and monotonically increasing fencing tokens.
- A recovered Coordinator rebuilds from the latest checkpoint plus subsequent events.
- Expired Step leases may be retried with incremented attempts.
- A late result from an old lease cannot commit.
- Completed results from parallel siblings are retained when another sibling retries.
- Cancellation prevents new leases, propagates to active StepWorkers, and ends only after the configured graceful/forced policy.
- Non-deterministic calls must occur inside durable steps and use idempotency metadata.

The runtime guarantees exactly-once committed state transitions, not exactly-once external side effects.

## 9. Interrupt and Resume

Interrupts share one durable abstraction:

- human question;
- tool approval;
- remote Agent result;
- timer/schedule;
- external signal.

An interrupt records expected resume schema, correlation key, suspended frontier and expiration policy. Resume is idempotent and rejected when it targets the wrong run version or an already-resolved interrupt.

Existing `AskUserEvent` and `ResumeCommand` become transport mappings to this abstraction. Existing external Workers retain current behavior.

## 10. Storage Interfaces

- `RunEventStore`: append with expected sequence, read range, subscribe.
- `CheckpointStore`: put/get/list checkpoints, optional encryption.
- `DefinitionStore`: versioned AgentSpec/ExecutionPlan/catalog snapshots.
- `ConversationStore` or projector: materialized model transcript.
- Existing `FileStorage`: artifacts and large state values.

MVP implementations:

- in-memory implementations for unit tests and local execution;
- Redis implementations for distributed production;
- existing history backends remain user-facing history projections until dedicated projectors are added.

Default serialization is schema-controlled JSON/msgpack-like data. Arbitrary pickle fallback is not enabled.

## 11. Deployment

### Embedded

`NativeAgentWorker` compiles definitions and runs Coordinator plus local Step executor within the Worker process. Durable Redis storage remains available and required for restart recovery.

### Server

`AgentServer` hosts definition catalog, Coordinator instances and generic StepWorkers. Components scale independently while existing external Agent Workers participate through current control streams.

Both modes invoke the same runtime interfaces. Placement is configuration, not an Agent-code concern.

## 12. Observability

The run/step event hierarchy maps to existing trace IDs and spans:

```text
run
  coordinator.superstep
    agent.turn
      model.call
      tool.call
      agent.dispatch
    checkpoint.save
```

Tracing supports redaction and disabling sensitive payload capture. Usage and budgets aggregate across nested Agent/team runs. Durable events and telemetry events share identifiers but are separate durability concerns.

## 13. Compatibility and Migration

- No behavioral change to `GatewayWorker.process_command`.
- Existing protocol commands/events remain valid; new native runtime wire types are additive and versioned.
- `AgentConfigAdapter` performs explicit best-effort conversion and emits validation errors for unsupported executable semantics.
- LangGraph and ADK packages continue passing their current tests and can be called as remote Agent nodes.
- Existing `AgentContext` methods become adapters into native runtime when a native run context is present; direct legacy behavior remains otherwise.

## 14. Packaging Direction

Keep core protocols and lightweight engine abstractions in the root package only when every deployment needs them. Put concrete OpenAI-compatible and MCP dependencies in optional workspace packages. A likely structure is:

```text
src/by_framework/agent/
  api/ compiler/ graph/ runtime/ stores/ tools/
libs/
  by-framework-model-openai/
  by-framework-tools-mcp/
```

The exact file split should be validated by a vertical-slice prototype before freezing public imports.

## 15. Key Trade-offs

- Agent-first improves onboarding; compiling to a visible plan preserves graph-level control.
- Event sourcing adds storage complexity but enables distributed deduplication, audit and replay.
- Logical single-writer coordination limits write contention and ambiguity; Step execution still scales horizontally.
- Strong compiled specs add an authoring/compilation boundary but make resumption and versioning safe.
- Shipping both embedded and server modes increases validation scope but prevents separate local and production runtimes.

## 16. Rollout and Rollback

Ship behind additive APIs and optional packages. Implement vertical slices in independent milestones. Existing Worker execution remains the rollback path throughout. Do not route legacy agent types to the native Coordinator automatically.
