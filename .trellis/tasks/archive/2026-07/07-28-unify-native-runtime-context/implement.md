# Implementation Plan

## Ordered Work

1. Add native run identity/context protocols, local implementation, JSON-safe
   durable projection, and focused unit tests.
2. Extend `FunctionTool` schema inspection and `ToolExecutor` to support one
   explicitly annotated `ToolExecutionContext`.
3. Thread optional context through `Runner`, `GraphRunner`, and compiled Team
   entry points while preserving existing signatures.
4. Include the context projection in run/checkpoint state and validate identity
   consistency during recovery/resume paths.
5. Add the Worker adapter and update `NativeAgentWorker` to construct it from
   `AgentContext` and command headers.
6. Verify existing Worker history remains single-write and add a regression
   covering native Worker execution.
7. Export the public contracts and document standalone/Worker composition.
8. Update the backend specification with the session-scope/run-scope contract.

## Validation

```bash
uv run pytest tests/agent -q
uv run pytest tests/worker -q
uv run pytest tests/integration -q
make lint
make test
```

Focused regressions must cover:

- identity generation and mismatch rejection;
- JSON-safe projection;
- context-aware tool schema omission and injection;
- ordinary tools unchanged;
- graph/Team context propagation;
- Worker adapter capability mapping;
- no duplicate Worker history messages;
- checkpoint/recovery compatibility.

## Risky Files

- `src/by_framework/agent/execution.py`
- `src/by_framework/agent/tools.py`
- `src/by_framework/agent/graph.py`
- `src/by_framework/agent/multi_agent.py`
- `src/by_framework/agent/deployment.py`
- `src/by_framework/worker/context.py`

Changes to these files must remain additive, and the implementation commit must
be path-limited because the worktree contains unrelated staged bootstrap files.

## Pre-Start Checks

- Re-read public exports in `src/by_framework/agent/__init__.py`.
- Search all Runner/GraphRunner construction and run call sites.
- Confirm Worker user/assistant history ownership in `worker.py` and
  `context.py`.
- Confirm no new Redis keys or protocol fields are necessary.
