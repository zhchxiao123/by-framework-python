"""Local single-agent durable execution vertical slice."""

import asyncio
import copy
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .definition import Agent, CompiledAgent
from .model import (
    AssistantMessage,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ResponseCompleted,
    SystemMessage,
    TextDelta,
    ToolCallCompleted,
    ToolMessage,
    Usage,
    UsageCompleted,
    UserMessage,
)
from .runtime.coordinator import CoordinatorLeaseManager
from .runtime.serialization import canonical_json
from .runtime.store import CommitRequest, InMemoryRunStore, RunEvent, RunStore
from .safety import (
    ApprovalController,
    ApprovalDecision,
    ApprovalRejectedError,
    TerminalStatus,
)
from .tools import ToolExecutor


class RunError(RuntimeError):
    """Base native run error."""


class RunBudgetExceededError(RunError):
    """The run exceeded its configured model-turn budget."""


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    data: dict[str, Any]


@dataclass(frozen=True)
class RunResult:
    run_id: str
    output: str
    usage: Usage
    messages: tuple[ModelMessage, ...]
    plan_hash: str
    state_version: int
    status: str = TerminalStatus.COMPLETED.value
    approval_id: str = ""


@dataclass
class _ApprovalSuspension:
    compiled: CompiledAgent
    run_id: str
    version: int
    fencing_token: int
    messages: list[ModelMessage]
    usage: Usage
    turn: int
    pending_call: Any
    remaining_calls: list[Any]


@dataclass(frozen=True)
class Checkpoint:
    run_id: str
    version: int
    state: dict[str, Any]
    plan_hash: str


class InMemoryCheckpointStore:
    """Materialized checkpoints for local execution and recovery tests."""

    def __init__(self):
        self._checkpoints: dict[str, Checkpoint] = {}
        self._history: dict[str, dict[int, Checkpoint]] = {}

    async def put(self, checkpoint: Checkpoint) -> None:
        checkpoint = copy.deepcopy(checkpoint)
        current = self._checkpoints.get(checkpoint.run_id)
        if current is not None and checkpoint.version < current.version:
            raise RunError("cannot replace a checkpoint with an older version")
        self._checkpoints[checkpoint.run_id] = checkpoint
        self._history.setdefault(checkpoint.run_id, {})[checkpoint.version] = checkpoint

    async def get(self, run_id: str, version: int | None = None) -> Checkpoint | None:
        if version is not None:
            checkpoint = self._history.get(run_id, {}).get(version)
        else:
            checkpoint = self._checkpoints.get(run_id)
        return copy.deepcopy(checkpoint)


class RunStream:
    """Async event stream whose final result becomes available after consumption."""

    def __init__(self, events: AsyncIterator[StreamEvent], result_future):
        self._events = events
        self._result_future = result_future

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._events

    async def result(self) -> RunResult:
        return await self._result_future


class LocalCoordinator:
    """Executes the compiled single-agent loop and durably commits each turn."""

    def __init__(
        self,
        store: RunStore,
        checkpoint_store: InMemoryCheckpointStore,
        *,
        max_model_turns: int = 32,
        approval_controller: ApprovalController | None = None,
    ):
        self._store = store
        self._checkpoint_store = checkpoint_store
        self._max_model_turns = max_model_turns
        self._approval_controller = approval_controller or ApprovalController()
        self._approvals: dict[str, _ApprovalSuspension] = {}
        self._decisions: dict[str, ApprovalDecision] = {}
        self._decision_results: dict[str, RunResult] = {}

    async def run(
        self,
        compiled: CompiledAgent,
        user_input: str,
        run_id: str,
    ) -> AsyncIterator[StreamEvent]:
        leases = CoordinatorLeaseManager(time.monotonic)
        coordinator = leases.acquire(f"local:{run_id}", ttl_seconds=3600)
        if coordinator is None:  # pragma: no cover - new manager is always free
            raise RunError("failed to acquire local coordinator")
        version = 0
        usage = Usage()
        messages: list[ModelMessage] = [
            SystemMessage(compiled.spec.instructions),
            UserMessage(user_input),
        ]
        version = await self._commit(
            run_id,
            version,
            coordinator.fencing_token,
            (RunEvent("RunCreated", {"plan_hash": compiled.plan.plan_hash}),),
            messages,
            usage,
            compiled.plan.plan_hash,
        )
        yield StreamEvent(
            "run_started", {"run_id": run_id, "plan_hash": compiled.plan.plan_hash}
        )
        async for event in self._drive(
            compiled,
            run_id,
            coordinator.fencing_token,
            version,
            messages,
            usage,
            start_turn=1,
        ):
            yield event

    async def _drive(
        self,
        compiled: CompiledAgent,
        run_id: str,
        fencing_token: int,
        version: int,
        messages: list[ModelMessage],
        usage: Usage,
        *,
        start_turn: int,
    ) -> AsyncIterator[StreamEvent]:
        executor = ToolExecutor({tool.spec.name: tool for tool in compiled.tools})
        specs = {tool.spec.name: tool.spec for tool in compiled.tools}
        output = ""
        for turn in range(start_turn, self._max_model_turns + 1):
            response: ModelResponse | None = None
            tool_calls = []
            turn_usage = Usage()
            request = ModelRequest(
                messages=tuple(messages),
                tools=tuple(tool.spec.declaration() for tool in compiled.tools),
                output_schema=compiled.spec.output_schema,
            )
            try:
                async for event in compiled.model.stream(request):
                    if isinstance(event, TextDelta):
                        yield StreamEvent("text_delta", {"text": event.text})
                    elif isinstance(event, ToolCallCompleted):
                        tool_calls.append(event.tool_call)
                        yield StreamEvent(
                            "tool_call",
                            {
                                "id": event.tool_call.id,
                                "name": event.tool_call.name,
                                "arguments": event.tool_call.arguments,
                            },
                        )
                    elif isinstance(event, UsageCompleted):
                        turn_usage += event.usage
                        yield StreamEvent(
                            "usage",
                            {
                                "input_tokens": event.usage.input_tokens,
                                "output_tokens": event.usage.output_tokens,
                            },
                        )
                    elif isinstance(event, ResponseCompleted):
                        response = event.response
            except Exception as exc:
                await self._commit_failure(
                    run_id,
                    version,
                    fencing_token,
                    messages,
                    usage + turn_usage,
                    compiled.plan.plan_hash,
                    exc,
                )
                raise
            if response is None:
                error = RunError("model stream ended without ResponseCompleted")
                await self._commit_failure(
                    run_id,
                    version,
                    fencing_token,
                    messages,
                    usage + turn_usage,
                    compiled.plan.plan_hash,
                    error,
                )
                raise error
            if not tool_calls:
                tool_calls = list(response.tool_calls)
            if turn_usage == Usage() and response.usage != Usage():
                turn_usage = response.usage
            usage += turn_usage
            messages.append(
                AssistantMessage(
                    response.text, tuple(tool_calls or response.tool_calls)
                )
            )
            output = response.text
            durable = [
                RunEvent(
                    "ModelCompleted",
                    {
                        "turn": turn,
                        "text": response.text,
                        "tool_call_count": len(tool_calls),
                    },
                )
            ]
            for call in tool_calls:
                request = self._approval_controller.request(
                    f"{run_id}:{call.id}", call, specs[call.name]
                )
                if request is not None:
                    spec_payload = json.loads(
                        canonical_json(specs[call.name]).decode("utf-8")
                    )
                    durable.extend(
                        (
                            self._approval_controller.requested_event(request),
                            RunEvent(
                                "RunInterrupted",
                                {
                                    "kind": "tool_approval",
                                    "approval_id": request.approval_id,
                                    "tool_call_id": call.id,
                                    "tool_name": call.name,
                                    "tool_spec": spec_payload,
                                },
                            ),
                        )
                    )
                    version = await self._commit(
                        run_id,
                        version,
                        fencing_token,
                        tuple(durable),
                        messages,
                        usage,
                        compiled.plan.plan_hash,
                        approval_state={
                            "approval_id": request.approval_id,
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "tool_spec": spec_payload,
                            "arguments": call.arguments,
                        },
                    )
                    self._approvals[request.approval_id] = _ApprovalSuspension(
                        compiled,
                        run_id,
                        version,
                        fencing_token,
                        messages,
                        usage,
                        turn,
                        call,
                        list(tool_calls[tool_calls.index(call) + 1 :]),
                    )
                    result = RunResult(
                        run_id,
                        "",
                        usage,
                        tuple(messages),
                        compiled.plan.plan_hash,
                        version,
                        TerminalStatus.INTERRUPTED.value,
                        request.approval_id,
                    )
                    yield StreamEvent(
                        "approval_requested",
                        {"request": request, "interrupt": request.interrupt()},
                    )
                    yield StreamEvent("run_interrupted", {"result": result})
                    return
                try:
                    tool_result = await executor.execute(call)
                except Exception as exc:
                    await self._commit(
                        run_id,
                        version,
                        fencing_token,
                        (
                            *durable,
                            RunEvent(
                                "RunFailed",
                                {
                                    "error_type": type(exc).__name__,
                                    "message": str(exc),
                                },
                            ),
                        ),
                        messages,
                        usage,
                        compiled.plan.plan_hash,
                    )
                    raise
                messages.append(ToolMessage(call.id, tool_result.model_content()))
                usage += tool_result.usage
                durable.append(
                    RunEvent(
                        "ToolCompleted",
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "output": tool_result.output,
                            "nested_usage": {
                                "input_tokens": tool_result.usage.input_tokens,
                                "output_tokens": tool_result.usage.output_tokens,
                            },
                        },
                    )
                )
                yield StreamEvent(
                    "tool_result",
                    {
                        "id": call.id,
                        "name": call.name,
                        "output": tool_result.output,
                    },
                )
            terminal = not tool_calls
            if terminal:
                durable.append(RunEvent("RunCompleted", {"output": output}))
            version = await self._commit(
                run_id,
                version,
                fencing_token,
                tuple(durable),
                messages,
                usage,
                compiled.plan.plan_hash,
            )
            if terminal:
                run_result = RunResult(
                    run_id,
                    output,
                    usage,
                    tuple(messages),
                    compiled.plan.plan_hash,
                    version,
                )
                yield StreamEvent("run_completed", {"result": run_result})
                return
        error = RunBudgetExceededError(
            f"run exceeded {self._max_model_turns} model turns"
        )
        await self._commit(
            run_id,
            version,
            fencing_token,
            (
                RunEvent(
                    "RunFailed",
                    {
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                ),
            ),
            messages,
            usage,
            compiled.plan.plan_hash,
        )
        raise error

    async def resume_approval(
        self, approval_id: str, decision: ApprovalDecision
    ) -> AsyncIterator[StreamEvent]:
        previous = self._decisions.get(approval_id)
        if previous is not None:
            if previous != decision:
                raise RunError("approval already resolved with a different decision")
            result = self._decision_results[approval_id]
            yield StreamEvent(
                "run_completed"
                if result.status != TerminalStatus.INTERRUPTED.value
                else "run_interrupted",
                {"result": result, "duplicate": True},
            )
            return
        suspension = self._approvals.pop(approval_id, None)
        if suspension is None:
            raise RunError(f"approval {approval_id!r} is not pending")
        call = suspension.pending_call
        spec = next(
            tool.spec
            for tool in suspension.compiled.tools
            if tool.spec.name == call.name
        )
        request = self._approval_controller.request(approval_id, call, spec)
        assert request is not None
        events = [self._approval_controller.resolved_event(decision)]
        self._decisions[approval_id] = decision
        try:
            approved_call = self._approval_controller.resolve(request, decision)
        except ApprovalRejectedError as exc:
            events.append(
                RunEvent(
                    "RunFailed",
                    {
                        "terminal_status": TerminalStatus.APPROVAL_REJECTED.value,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
            version = await self._commit(
                suspension.run_id,
                suspension.version,
                suspension.fencing_token,
                tuple(events),
                suspension.messages,
                suspension.usage,
                suspension.compiled.plan.plan_hash,
            )
            result = RunResult(
                suspension.run_id,
                str(exc),
                suspension.usage,
                tuple(suspension.messages),
                suspension.compiled.plan.plan_hash,
                version,
                TerminalStatus.APPROVAL_REJECTED.value,
                approval_id,
            )
            self._decision_results[approval_id] = result
            yield StreamEvent("run_completed", {"result": result})
            return
        except Exception as exc:
            events.append(
                RunEvent(
                    "RunFailed",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "failed_stage": "approval_argument_validation",
                    },
                )
            )
            version = await self._commit(
                suspension.run_id,
                suspension.version,
                suspension.fencing_token,
                tuple(events),
                suspension.messages,
                suspension.usage,
                suspension.compiled.plan.plan_hash,
            )
            result = RunResult(
                suspension.run_id,
                str(exc),
                suspension.usage,
                tuple(suspension.messages),
                suspension.compiled.plan.plan_hash,
                version,
                TerminalStatus.FAILED.value,
                approval_id,
            )
            self._decision_results[approval_id] = result
            yield StreamEvent("run_completed", {"result": result})
            return
        executor = ToolExecutor(
            {tool.spec.name: tool for tool in suspension.compiled.tools}
        )
        try:
            tool_result = await executor.execute(approved_call)
        except Exception as exc:
            events.append(
                RunEvent(
                    "RunFailed",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "failed_stage": "approval_tool_validation_or_execution",
                    },
                )
            )
            version = await self._commit(
                suspension.run_id,
                suspension.version,
                suspension.fencing_token,
                tuple(events),
                suspension.messages,
                suspension.usage,
                suspension.compiled.plan.plan_hash,
            )
            result = RunResult(
                suspension.run_id,
                str(exc),
                suspension.usage,
                tuple(suspension.messages),
                suspension.compiled.plan.plan_hash,
                version,
                TerminalStatus.FAILED.value,
                approval_id,
            )
            self._decision_results[approval_id] = result
            yield StreamEvent("run_completed", {"result": result})
            return
        suspension.messages.append(
            ToolMessage(approved_call.id, tool_result.model_content())
        )
        suspension.usage += tool_result.usage
        events.append(
            RunEvent(
                "ToolCompleted",
                {
                    "tool_call_id": approved_call.id,
                    "name": approved_call.name,
                    "output": tool_result.output,
                },
            )
        )
        yield StreamEvent(
            "tool_result",
            {
                "id": approved_call.id,
                "name": approved_call.name,
                "output": tool_result.output,
            },
        )
        for index, remaining_call in enumerate(suspension.remaining_calls):
            remaining_spec = next(
                tool.spec
                for tool in suspension.compiled.tools
                if tool.spec.name == remaining_call.name
            )
            next_approval_id = f"{suspension.run_id}:{remaining_call.id}"
            next_request = self._approval_controller.request(
                next_approval_id, remaining_call, remaining_spec
            )
            if next_request is not None:
                spec_payload = json.loads(
                    canonical_json(remaining_spec).decode("utf-8")
                )
                events.extend(
                    (
                        self._approval_controller.requested_event(next_request),
                        RunEvent(
                            "RunInterrupted",
                            {
                                "kind": "tool_approval",
                                "approval_id": next_approval_id,
                                "tool_call_id": remaining_call.id,
                                "tool_name": remaining_call.name,
                                "tool_spec": spec_payload,
                            },
                        ),
                    )
                )
                version = await self._commit(
                    suspension.run_id,
                    suspension.version,
                    suspension.fencing_token,
                    tuple(events),
                    suspension.messages,
                    suspension.usage,
                    suspension.compiled.plan.plan_hash,
                    approval_state={
                        "approval_id": next_approval_id,
                        "tool_call_id": remaining_call.id,
                        "tool_name": remaining_call.name,
                        "tool_spec": spec_payload,
                        "arguments": remaining_call.arguments,
                    },
                )
                self._approvals[next_approval_id] = _ApprovalSuspension(
                    suspension.compiled,
                    suspension.run_id,
                    version,
                    suspension.fencing_token,
                    suspension.messages,
                    suspension.usage,
                    suspension.turn,
                    remaining_call,
                    suspension.remaining_calls[index + 1 :],
                )
                result = RunResult(
                    suspension.run_id,
                    "",
                    suspension.usage,
                    tuple(suspension.messages),
                    suspension.compiled.plan.plan_hash,
                    version,
                    TerminalStatus.INTERRUPTED.value,
                    next_approval_id,
                )
                self._decision_results[approval_id] = result
                yield StreamEvent(
                    "approval_requested",
                    {
                        "request": next_request,
                        "interrupt": next_request.interrupt(),
                    },
                )
                yield StreamEvent("run_interrupted", {"result": result})
                return
            try:
                remaining_result = await executor.execute(remaining_call)
            except Exception as exc:
                events.append(
                    RunEvent(
                        "RunFailed",
                        {
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "failed_stage": "tool_validation_or_execution",
                        },
                    )
                )
                version = await self._commit(
                    suspension.run_id,
                    suspension.version,
                    suspension.fencing_token,
                    tuple(events),
                    suspension.messages,
                    suspension.usage,
                    suspension.compiled.plan.plan_hash,
                )
                result = RunResult(
                    suspension.run_id,
                    str(exc),
                    suspension.usage,
                    tuple(suspension.messages),
                    suspension.compiled.plan.plan_hash,
                    version,
                    TerminalStatus.FAILED.value,
                    approval_id,
                )
                self._decision_results[approval_id] = result
                yield StreamEvent("run_completed", {"result": result})
                return
            suspension.messages.append(
                ToolMessage(remaining_call.id, remaining_result.model_content())
            )
            suspension.usage += remaining_result.usage
            events.append(
                RunEvent(
                    "ToolCompleted",
                    {
                        "tool_call_id": remaining_call.id,
                        "name": remaining_call.name,
                        "output": remaining_result.output,
                    },
                )
            )
            yield StreamEvent(
                "tool_result",
                {
                    "id": remaining_call.id,
                    "name": remaining_call.name,
                    "output": remaining_result.output,
                },
            )
        version = await self._commit(
            suspension.run_id,
            suspension.version,
            suspension.fencing_token,
            tuple(events),
            suspension.messages,
            suspension.usage,
            suspension.compiled.plan.plan_hash,
        )
        final_result = None
        async for event in self._drive(
            suspension.compiled,
            suspension.run_id,
            suspension.fencing_token,
            version,
            suspension.messages,
            suspension.usage,
            start_turn=suspension.turn + 1,
        ):
            if event.kind in ("run_completed", "run_interrupted"):
                final_result = event.data["result"]
            yield event
        assert final_result is not None
        self._decision_results[approval_id] = final_result

    async def _commit_failure(
        self,
        run_id: str,
        version: int,
        fence: int,
        messages: list,
        usage: Usage,
        plan_hash: str,
        error: Exception,
    ) -> None:
        await self._commit(
            run_id,
            version,
            fence,
            (
                RunEvent(
                    "RunFailed",
                    {
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                ),
            ),
            messages,
            usage,
            plan_hash,
        )

    async def _commit(
        self,
        run_id: str,
        version: int,
        fence: int,
        events: tuple[RunEvent, ...],
        messages: list,
        usage: Usage,
        plan_hash: str,
        approval_state: dict[str, Any] | None = None,
    ) -> int:
        state = {
            "messages": messages,
            "usage": usage,
        }
        if approval_state is not None:
            state["pending_approval"] = approval_state
        result = await self._store.commit(
            CommitRequest(run_id, version, fence, events, state)
        )
        await self._checkpoint_store.put(
            Checkpoint(run_id, result.version, state, plan_hash=plan_hash)
        )
        return result.version


class Runner:
    """High-level entry point for local native agent execution."""

    def __init__(
        self,
        *,
        store: RunStore | None = None,
        checkpoint_store: InMemoryCheckpointStore | None = None,
        max_model_turns: int = 32,
        approval_controller: ApprovalController | None = None,
    ):
        self.store = store or InMemoryRunStore()
        self.checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        self._coordinator = LocalCoordinator(
            self.store,
            self.checkpoint_store,
            max_model_turns=max_model_turns,
            approval_controller=approval_controller,
        )
        self._approval_locks: dict[str, asyncio.Lock] = {}
        self._approval_outcomes: dict[
            str, tuple[ApprovalDecision, RunResult | Exception]
        ] = {}

    async def run(
        self,
        agent: Agent | CompiledAgent,
        user_input: str,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        stream = self.run_streamed(agent, user_input, run_id=run_id)
        async for _ in stream:
            pass
        return await stream.result()

    def run_streamed(
        self,
        agent: Agent | CompiledAgent,
        user_input: str,
        *,
        run_id: str | None = None,
    ) -> RunStream:
        compiled = agent.compile() if isinstance(agent, Agent) else agent
        resolved_run_id = run_id or f"run-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()
        # Iteration itself raises run errors. Mark the mirrored result future as
        # observed as well so callers that only iterate do not trigger asyncio's
        # "Future exception was never retrieved" warning.
        result_future.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )

        async def events():
            try:
                async for event in self._coordinator.run(
                    compiled, user_input, resolved_run_id
                ):
                    if event.kind in ("run_completed", "run_interrupted"):
                        result_future.set_result(event.data["result"])
                    yield event
            except Exception as exc:
                if not result_future.done():
                    result_future.set_exception(exc)
                raise

        return RunStream(events(), result_future)

    async def resume(self, approval_id: str, decision: ApprovalDecision) -> RunResult:
        if decision.approval_id != approval_id:
            raise RunError("approval decision targets a different request")
        lock = self._approval_locks.setdefault(approval_id, asyncio.Lock())
        async with lock:
            previous = self._approval_outcomes.get(approval_id)
            if previous is not None:
                previous_decision, outcome = previous
                if previous_decision != decision:
                    raise RunError(
                        "approval already resolved with a different decision"
                    )
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            result = None
            try:
                async for event in self._coordinator.resume_approval(
                    approval_id, decision
                ):
                    if event.kind in ("run_completed", "run_interrupted"):
                        result = event.data["result"]
                if result is None:
                    raise RunError("approval resume did not produce a run result")
            except Exception as exc:
                self._approval_outcomes[approval_id] = (decision, exc)
                raise
            self._approval_outcomes[approval_id] = (decision, result)
            return result
