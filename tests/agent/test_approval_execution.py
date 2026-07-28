"""End-to-end durable approval behavior in the native Agent tool loop."""

import asyncio

import pytest

from by_framework.agent import (
    Agent,
    ApprovalAction,
    ApprovalDecision,
    FunctionTool,
    ModelResponse,
    Runner,
    ScriptedModel,
    TerminalStatus,
    ToolCall,
)
from by_framework.agent.execution import RunError


def approval_agent(side_effects, *, call_arguments=None):
    def write_record(value: int) -> str:
        side_effects.append(value)
        return f"wrote:{value}"

    tool = FunctionTool(write_record, side_effect="write")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "call-write",
                        "write_record",
                        call_arguments or {"value": 1},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse("finished"),
        ]
    )
    return Agent("writer", "Use the tool.", model, [tool])


def test_risky_tool_interrupts_durably_then_approve_executes_exactly_once():
    effects = []
    runner = Runner()
    interrupted = asyncio.run(
        runner.run(approval_agent(effects), "write", run_id="approval-run")
    )

    assert interrupted.status == TerminalStatus.INTERRUPTED.value
    assert interrupted.approval_id == "approval-run:call-write"
    assert effects == []
    checkpoint = asyncio.run(runner.checkpoint_store.get("approval-run"))
    assert checkpoint is not None
    assert checkpoint.state["pending_approval"]["tool_call_id"] == "call-write"

    decision = ApprovalDecision(interrupted.approval_id, ApprovalAction.APPROVE)
    completed = asyncio.run(runner.resume(interrupted.approval_id, decision))
    duplicate = asyncio.run(runner.resume(interrupted.approval_id, decision))
    assert completed.status == TerminalStatus.COMPLETED.value
    assert completed.output == "finished"
    assert duplicate == completed
    assert effects == [1]
    _, _, _, events = runner.store.snapshot("approval-run")
    assert [event.kind for event in events] == [
        "RunCreated",
        "ModelCompleted",
        "ApprovalRequested",
        "RunInterrupted",
        "ApprovalResolved",
        "ToolCompleted",
        "ModelCompleted",
        "RunCompleted",
    ]


def test_side_effect_observes_durable_interrupt_and_checkpoint_first():
    observations = []
    runner = Runner()

    async def write_record(value: int) -> str:
        _, _, _, events = runner.store.snapshot("ordered-approval")
        checkpoint = await runner.checkpoint_store.get("ordered-approval")
        observations.append(
            (
                value,
                [event.kind for event in events],
                checkpoint.state["pending_approval"]["tool_call_id"],
            )
        )
        return "written"

    agent = Agent(
        "ordered",
        "",
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("ordered-call", "write_record", {"value": 1}),)
                ),
                ModelResponse("done"),
            ]
        ),
        [FunctionTool(write_record, side_effect="write")],
    )
    interrupted = asyncio.run(runner.run(agent, "write", run_id="ordered-approval"))
    asyncio.run(
        runner.resume(
            interrupted.approval_id,
            ApprovalDecision(interrupted.approval_id, ApprovalAction.APPROVE),
        )
    )
    assert observations == [
        (
            1,
            [
                "RunCreated",
                "ModelCompleted",
                "ApprovalRequested",
                "RunInterrupted",
            ],
            "ordered-call",
        )
    ]


def test_resume_correlation_does_not_consume_pending_approval():
    effects = []
    runner = Runner()
    interrupted = asyncio.run(
        runner.run(approval_agent(effects), "write", run_id="correlated")
    )
    with pytest.raises(RunError, match="different request"):
        asyncio.run(
            runner.resume(
                interrupted.approval_id,
                ApprovalDecision("another-approval", ApprovalAction.APPROVE),
            )
        )
    completed = asyncio.run(
        runner.resume(
            interrupted.approval_id,
            ApprovalDecision(interrupted.approval_id, ApprovalAction.APPROVE),
        )
    )
    assert completed.status == TerminalStatus.COMPLETED.value
    assert effects == [1]


def test_edited_arguments_are_revalidated_and_only_edited_values_execute():
    effects = []
    runner = Runner()
    interrupted = asyncio.run(
        runner.run(approval_agent(effects), "write", run_id="edit-run")
    )
    result = asyncio.run(
        runner.resume(
            interrupted.approval_id,
            ApprovalDecision(
                interrupted.approval_id,
                ApprovalAction.EDIT,
                {"value": 7},
            ),
        )
    )
    assert result.status == TerminalStatus.COMPLETED.value
    assert effects == [7]

    invalid_effects = []
    invalid_runner = Runner()
    invalid = asyncio.run(
        invalid_runner.run(
            approval_agent(invalid_effects), "write", run_id="invalid-edit"
        )
    )
    failed = asyncio.run(
        invalid_runner.resume(
            invalid.approval_id,
            ApprovalDecision(
                invalid.approval_id,
                ApprovalAction.EDIT,
                {"value": "not-an-integer"},
            ),
        )
    )
    assert failed.status == TerminalStatus.FAILED.value
    assert invalid_effects == []


def test_reject_is_terminal_and_never_executes_side_effect():
    effects = []
    runner = Runner()
    interrupted = asyncio.run(
        runner.run(approval_agent(effects), "write", run_id="reject-run")
    )
    rejected = asyncio.run(
        runner.resume(
            interrupted.approval_id,
            ApprovalDecision(
                interrupted.approval_id,
                ApprovalAction.REJECT,
                reason="user denied",
            ),
        )
    )
    assert rejected.status == TerminalStatus.APPROVAL_REJECTED.value
    assert rejected.output == "user denied"
    assert effects == []


def test_post_approval_failure_is_durable_and_duplicate_does_not_reexecute():
    effects = []

    def write_record(value: int) -> str:
        effects.append(value)
        return "written"

    # Use a small explicit async generator model to emit the first tool response.
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("failed-call", "write_record", {"value": 1}),)
            ),
        ]
    )

    class Model:
        def __init__(self):
            self.first = model
            self.turn = 0

        async def stream(self, request):
            self.turn += 1
            if self.turn == 1:
                async for event in self.first.stream(request):
                    yield event
                return
            raise RuntimeError("model failed after approved tool")

    runner = Runner()
    interrupted = asyncio.run(
        runner.run(
            Agent(
                "failure-after-approval",
                "",
                Model(),
                [FunctionTool(write_record, side_effect="write")],
            ),
            "write",
            run_id="approval-post-failure",
        )
    )
    decision = ApprovalDecision(interrupted.approval_id, ApprovalAction.APPROVE)
    with pytest.raises(RuntimeError, match="model failed"):
        asyncio.run(runner.resume(interrupted.approval_id, decision))
    with pytest.raises(RuntimeError, match="model failed"):
        asyncio.run(runner.resume(interrupted.approval_id, decision))
    assert effects == [1]
    version, _, _, events = runner.store.snapshot("approval-post-failure")
    checkpoint = asyncio.run(runner.checkpoint_store.get("approval-post-failure"))
    assert checkpoint is not None
    assert checkpoint.version == version
    assert events[-1].kind == "RunFailed"


def test_low_risk_tool_behavior_remains_uninterrupted():
    effects = []

    def read_value(value: int) -> str:
        effects.append(value)
        return str(value)

    agent = Agent(
        "reader",
        "",
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("read", "read_value", {"value": 3}),)
                ),
                ModelResponse("done"),
            ]
        ),
        [FunctionTool(read_value, side_effect="read")],
    )
    result = asyncio.run(Runner().run(agent, "read", run_id="low-risk"))
    assert result.status == TerminalStatus.COMPLETED.value
    assert effects == [3]


def test_multiple_risky_calls_interrupt_and_resume_one_at_a_time():
    effects = []

    def write_record(value: int) -> str:
        effects.append(value)
        return f"wrote:{value}"

    agent = Agent(
        "batch-writer",
        "",
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("write-1", "write_record", {"value": 1}),
                        ToolCall("write-2", "write_record", {"value": 2}),
                    )
                ),
                ModelResponse("done"),
            ]
        ),
        [FunctionTool(write_record, side_effect="write")],
    )
    runner = Runner()
    first = asyncio.run(runner.run(agent, "write both", run_id="two-approvals"))
    assert first.approval_id == "two-approvals:write-1"
    assert effects == []

    first_decision = ApprovalDecision(first.approval_id, ApprovalAction.APPROVE)
    second = asyncio.run(runner.resume(first.approval_id, first_decision))
    duplicate_first = asyncio.run(runner.resume(first.approval_id, first_decision))
    assert second.status == TerminalStatus.INTERRUPTED.value
    assert second.approval_id == "two-approvals:write-2"
    assert duplicate_first == second
    assert effects == [1]

    completed = asyncio.run(
        runner.resume(
            second.approval_id,
            ApprovalDecision(second.approval_id, ApprovalAction.APPROVE),
        )
    )
    assert completed.status == TerminalStatus.COMPLETED.value
    assert completed.output == "done"
    assert effects == [1, 2]
