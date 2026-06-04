# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Standardized Open Source governance files (CONTRIBUTING, CODE_OF_CONDUCT, SECURITY).
- GitHub Issue and Pull Request templates.
- Dependabot configuration for dependency monitoring.
- `InMemoryCounter` / `InMemoryGauge` / `record_failure` in `by_framework.common.metrics` plus three framework-wide failure counters (`REGISTRY_FAILURES_COUNTER`, `MESSAGE_PARSE_FAILURES_COUNTER`, `PLUGIN_RELOAD_FAILURES_COUNTER`).
- `ResponseBuffer` helper encapsulating the per-context response buffer and lifecycle flags, exposed via a new `by_framework.worker._response_buffer` module.
- `RedisKeys.task_group_results_stream(group_id)` notification stream key for `collect_group_results` wakeups.

### Changed
- Extract response buffer/flag state from `AgentContext` into `_response_buffer.py` (backward compatible via `@property` shims covering all 11 external access points in `worker.py` / `processor.py` / `test_gateway_worker.py`).
- Replace silent `except: pass` in registry fallback paths with explicit `logger.warning` + metric counter, bucketed into CancelledError / network / schema / 兜底 across `context.py`, `runner.py`, `processor.py`, `heartbeat.py`, and `_control_handling.py`.
- Refactor `AgentContext.collect_group_results` to use `XREAD BLOCK` on `task_group_results_stream` with a 200 ms polling fallback for legacy writers, dramatically reducing collector-side Redis QPS during waits.
- `WorkerRunner.is_agent_return` now emits a `XADD` notification after writing results so blocked collectors can wake up immediately.

### Tests
- Verify all 334 existing test cases still pass with zero modifications (covers P0-1, P0-2, and P0-3 refactors).
- Verify integration tests `test_scatter_gather.py` and `test_callback_flow.py` pass against the new XREAD-driven `collect_group_results` (12 passed in `tests/worker/test_context.py + tests/integration/`).

## [0.2.0] - 2026-05-13
### Initial release
