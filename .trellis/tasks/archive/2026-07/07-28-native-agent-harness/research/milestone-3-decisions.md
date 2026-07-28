# Milestone 3 Multi-Agent Decisions

- Teams are authoring/compiler objects only. `SupervisorTeam`, `HandoffTeam`,
  `ParallelTeam`, and `WorkflowTeam` compile to the existing `GraphPlan` and run
  through `GraphRunner`; no team-specific scheduler or persistence path exists.
- Team graph nodes bind the compiled member Agent plan hash so inspection and the
  team plan hash identify the exact nested definitions.
- Local sub-agents use `AgentTool` and the normal `Runner`. Remote calls use the
  `RemoteAgentDispatcher` protocol; `AgentContextRemoteDispatcher` maps that
  protocol to the existing `AgentContext.call_agent` shape only when an adapter
  receives an immediate `reply_data`. The current `AgentContext` normally
  returns a queued dispatch and suspends its Worker, so the adapter fails
  explicitly rather than misrepresenting that queued acknowledgement as a
  completed result; durable remote-result resume remains Milestone 4.
- Handoffs are explicit source/target definitions with separate input and history
  filters. Control transfer is represented by conditional graph routing.
- Nested usage is accumulated in typed team state and usage-marked `AgentTool`
  results are included in the parent Runner totals. Trace identifiers are
  retained in team state and passed to remote AgentTool dispatch metadata.
  Local nested span creation and a full span hierarchy are deferred to the
  observability milestone.
- Actual remote queue placement, durable remote-result interrupts, distributed
  Step Workers, and cross-process failover remain Milestone 4 work. The
  dispatcher boundary is deliberately executable with fakes and AgentContext,
  but does not introduce a second runtime.
