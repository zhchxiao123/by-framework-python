"""Single-agent model/tool/durability vertical-slice tests."""

import asyncio

import pytest

from by_framework.agent import (
    Agent,
    FunctionTool,
    ModelResponse,
    Runner,
    ScriptedModel,
    ToolCall,
    Usage,
)
from by_framework.agent.execution import RunBudgetExceededError
from by_framework.agent.model import ResponseCompleted
from by_framework.agent.runtime import InMemoryRunStore
from by_framework.agent.tools import ToolExecutionError


def test_runner_executes_multiple_model_turns_and_tool_with_durable_state():
    def weather(city: str) -> dict:
        """Return deterministic weather."""
        return {"city": city, "temperature": 72}

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("call-weather", "weather", {"city": "LA"}),),
                usage=Usage(5, 1),
                finish_reason="tool_calls",
            ),
            ModelResponse(text="It is 72°F in LA.", usage=Usage(8, 6)),
        ],
        text_chunk_size=5,
    )
    store = InMemoryRunStore()
    runner = Runner(store=store)
    agent = Agent("weather", "Use tools.", model, [FunctionTool(weather)])

    result = asyncio.run(runner.run(agent, "What is the weather?", run_id="run-test"))

    assert result.output == "It is 72°F in LA."
    assert result.usage == Usage(13, 7)
    assert result.state_version == 3
    assert len(model.requests) == 2
    assert model.requests[1].messages[-1].role == "tool"
    assert '"temperature": 72' in model.requests[1].messages[-1].content
    version, _, state, events = store.snapshot("run-test")
    assert version == 3
    assert state["messages"][-1].content == result.output
    assert [event.kind for event in events] == [
        "RunCreated",
        "ModelCompleted",
        "ToolCompleted",
        "ModelCompleted",
        "RunCompleted",
    ]
    checkpoint = asyncio.run(runner.checkpoint_store.get("run-test"))
    assert checkpoint is not None
    assert checkpoint.version == 3


def test_run_streamed_emits_text_chunks_and_exposes_final_result():
    runner = Runner()
    agent = Agent(
        "writer",
        "Write.",
        ScriptedModel([ModelResponse(text="hello")], text_chunk_size=2),
    )

    async def consume():
        stream = runner.run_streamed(agent, "go", run_id="run-stream")
        events = [event async for event in stream]
        return events, await stream.result()

    events, result = asyncio.run(consume())
    assert [event.data["text"] for event in events if event.kind == "text_delta"] == [
        "he",
        "ll",
        "o",
    ]
    assert events[0].kind == "run_started"
    assert events[-1].kind == "run_completed"
    assert result.output == "hello"


def test_runner_enforces_model_turn_budget():
    def noop() -> str:
        return "ok"

    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall("1", "noop", {}),)),
            ModelResponse(tool_calls=(ToolCall("2", "noop", {}),)),
        ]
    )
    runner = Runner(max_model_turns=1)
    agent = Agent("loop", "Loop.", model, [FunctionTool(noop)])

    with pytest.raises(RunBudgetExceededError):
        asyncio.run(runner.run(agent, "go", run_id="run-budget"))

    version, _, _, events = runner.store.snapshot("run-budget")
    assert version == 3
    assert events[-1].kind == "RunFailed"
    assert events[-1].payload["error_type"] == "RunBudgetExceededError"


def test_runner_uses_completed_response_as_normalized_turn_fallback():
    class CompletionOnlyModel:
        def __init__(self):
            self.turn = 0

        async def stream(self, request):
            del request
            self.turn += 1
            if self.turn == 1:
                yield ResponseCompleted(
                    ModelResponse(
                        tool_calls=(ToolCall("call-1", "echo", {"value": "ok"}),),
                        usage=Usage(2, 1),
                    )
                )
            else:
                yield ResponseCompleted(ModelResponse(text="done", usage=Usage(3, 2)))

    def echo(value: str) -> str:
        return value

    result = asyncio.run(
        Runner().run(
            Agent("fallback", "", CompletionOnlyModel(), [FunctionTool(echo)]),
            "go",
        )
    )

    assert result.output == "done"
    assert result.usage == Usage(5, 3)


def test_runner_durably_records_tool_failure_terminal_state():
    def fail() -> str:
        raise RuntimeError("boom")

    runner = Runner()
    agent = Agent(
        "failure",
        "",
        ScriptedModel([ModelResponse(tool_calls=(ToolCall("call-1", "fail", {}),))]),
        [FunctionTool(fail)],
    )

    with pytest.raises(ToolExecutionError):
        asyncio.run(runner.run(agent, "go", run_id="run-failed-tool"))

    version, _, _, events = runner.store.snapshot("run-failed-tool")
    assert version == 2
    assert [event.kind for event in events[-2:]] == ["ModelCompleted", "RunFailed"]
