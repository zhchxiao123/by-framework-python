"""Native runtime-context contract tests."""

import asyncio
from typing import TypedDict

import pytest

from by_framework.agent import (
    Agent,
    FunctionTool,
    ModelResponse,
    RunContext,
    RunContextError,
    RunIdentity,
    Runner,
    ScriptedModel,
    ToolCall,
    ToolExecutionContext,
)
from by_framework.agent import StateGraph, GraphRunner
from by_framework.agent.graph import END, START


class GraphState(TypedDict):
    value: int


def test_runner_resolves_local_identity_and_persists_projection_only():
    runner = Runner()
    result = asyncio.run(
        runner.run(
            Agent("echo", "Reply.", ScriptedModel([ModelResponse("ok")])),
            "hello",
            run_id="run-local",
        )
    )
    _, _, state, _ = runner.store.snapshot(result.run_id)
    assert state["runtime_context"] == {
        "session_id": state["runtime_context"]["session_id"],
        "run_id": "run-local",
        "agent_id": "echo",
        "user_code": "default",
        "user_name": "",
        "trace_context": {},
    }


def test_runner_rejects_identity_mismatch_before_execution():
    context = RunContext(RunIdentity("session", "different", "echo"))
    with pytest.raises(RunContextError, match="run_id"):
        asyncio.run(
            Runner().run(
                Agent("echo", "Reply.", ScriptedModel([ModelResponse("ok")])),
                "hello",
                run_id="requested",
                context=context,
            )
        )


def test_context_aware_tool_receives_capabilities_but_hides_context_schema():
    seen = {}

    def inspect_run(value: str, context: ToolExecutionContext) -> str:
        seen["identity"] = context.run.identity
        seen["call_id"] = context.tool_call_id
        return value

    tool = FunctionTool(inspect_run)
    assert tuple(tool.spec.input_schema["properties"]) == ("value",)
    model = ScriptedModel(
        [
            ModelResponse(
                "",
                (ToolCall("call-1", "inspect_run", {"value": "done"}),),
            ),
            ModelResponse("done"),
        ]
    )
    context = RunContext(
        RunIdentity("session-1", "run-1", "context-agent"),
        private_files=object(),
    )
    result = asyncio.run(
        Runner().run(
            Agent("context-agent", "Use the tool.", model, [tool]),
            "go",
            context=context,
        )
    )
    assert result.output == "done"
    assert seen == {"identity": context.identity, "call_id": "call-1"}


def test_graph_persists_supplied_context_projection():
    graph = StateGraph(GraphState)
    graph.add_node("done", lambda state: {"value": state["value"] + 1})
    graph.add_edge(START, "done")
    graph.add_edge("done", END)
    context = RunContext(RunIdentity("session-graph", "run-graph", "graph"))
    runner = GraphRunner()
    result = asyncio.run(
        runner.run(graph, {"value": 0}, context=context)
    )
    checkpoint = asyncio.run(runner.replay(result.run_id))
    assert checkpoint.state["runtime_context"] == context.projection()
