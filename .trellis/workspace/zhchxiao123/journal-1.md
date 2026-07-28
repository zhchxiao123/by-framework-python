# Journal - zhchxiao123 (Part 1)

> AI development session journal
> Started: 2026-07-28

---



## Session 1: Native Agent Framework

**Date**: 2026-07-28
**Task**: Native Agent Framework
**Package**: by-framework-history-postgres
**Branch**: `main`

### Summary

Implemented an Agent-first, graph-capable native framework with durable event/checkpoint state, multi-Agent teams, Redis coordination and Step Workers, embedded/server deployment, OpenAI-compatible and MCP optional packages, approvals, compatibility bridges, observability, and full tests.

### Git Commits

| Hash | Message |
|------|---------|
| `5f8001c` | (see git log) |
| `7190dd1` | (see git log) |

### Status

[OK] **Completed**


## Session 2: Native ReAct weather agent example

**Date**: 2026-07-28
**Task**: Native ReAct weather agent example
**Package**: by-framework-history-postgres
**Branch**: `main`

### Summary

Added an offline deterministic ReAct example using Agent, FunctionTool, ScriptedModel, and Runner, with documentation and regression coverage.

### Main Changes

- Added a runnable native ReAct weather agent example.
- Documented provider replacement and the Reason-Act-Observe loop.

### Git Commits

| Hash | Message |
|------|---------|
| `08d5380` | (see git log) |

### Testing

- [OK] uv run python examples/native_agent/react_weather_agent.py
- [OK] uv run pytest tests/agent -q (85 passed)

### Status

[OK] **Completed**
