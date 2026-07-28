# Milestone 7 Compatibility, Approval, and Observability Decisions

- `AgentConfigAdapter` is explicit: callers supply model and optional tool
  resolvers. Prompts and tools compile into the native Agent; callbacks, skills,
  knowledge bases, and sub-agent declarations remain plugin-managed and produce
  warnings instead of silently inventing executable semantics.
- Converted configurations register in the same `DefinitionCatalog` as native
  Agents and bind the same immutable plan hashes. Existing PluginRegistry and
  GatewayWorker paths are unchanged.
- `LegacyInterruptBridge` maps `ask_user`, `call_agent`, and `ResumeCommand` to
  additive native durable events while still invoking the existing
  `AgentContext` methods. Legacy callers that do not opt into the bridge retain
  their original behavior.
- Approval is enforced automatically at the native `Runner` tool boundary.
  Risk-based or always-approve policies durably interrupt before side effects;
  each pending request is bound to the run, tool call, arguments, and immutable
  `ToolSpec`. Approve preserves arguments, edit replaces and revalidates them,
  reject produces an explicit terminal result, and repeated decisions are
  idempotent. Multiple risky calls from one model turn resume one at a time.
- Native observability projects durable events into existing `TraceSpan` and
  metrics APIs. Run, coordinator, node, model, tool, agent, handoff, checkpoint,
  interrupt, and approval event kinds have stable operation/component names.
- Sensitive inputs, outputs, prompts, content, tool arguments, and credential
  fields are redacted unless capture is explicitly enabled. The existing global
  `redact_inputs` setting always wins.
- Native `RunResult.status` values distinguish completed, failed, cancelled,
  interrupted, budget exhaustion, and approval rejection. Existing legacy
  status strings remain compatibility behavior and are neither changed nor
  rerouted through the native status model.

## Compatibility Validation

- Root native/Worker/context/plugin/ask-user suites pass together.
- LangGraph and ADK suites pass when invoked in their own workspace package
  environments so their optional dependencies are installed and duplicate test
  basenames do not collide.
- The bridge and trace projector are opt-in additive APIs; this milestone does
  not reroute legacy Workers, plugin hooks, or external-framework integrations
  through the native runtime automatically.
