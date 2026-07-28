"""Agent compilation tests."""

import asyncio

import pytest

from by_framework.agent import Agent, FakeModel, FunctionTool, Runner


def test_agent_compiles_to_inspectable_stable_plan_without_callable_serialization():
    def lookup(city: str) -> str:
        return city

    first = Agent(
        "weather",
        "Be concise.",
        FakeModel("done"),
        [FunctionTool(lookup)],
    ).compile()
    second = Agent(
        "weather",
        "Be concise.",
        FakeModel("different runtime object"),
        [FunctionTool(lookup)],
    ).compile()

    assert first.plan.plan_hash == second.plan.plan_hash
    assert first.plan.plan_hash.startswith("sha256:")
    assert [node.id for node in first.plan.nodes] == [
        "prepare_input",
        "call_model",
        "execute_tools",
        "final",
    ]
    assert first.spec.tools[0].name == "lookup"
    with pytest.raises(TypeError):
        first.spec.tools[0].input_schema["type"] = "array"
    assert first.plan.plan_hash == second.plan.plan_hash


def test_agent_output_schema_is_immutable_and_reaches_model_request():
    model = FakeModel('{"ok":true}')
    agent = Agent(
        "structured",
        "",
        model,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
        },
    )

    asyncio.run(Runner().run(agent, "go"))
    schema = model.requests[0].output_schema
    assert schema is not None
    with pytest.raises(TypeError):
        schema["type"] = "array"
