"""Tests for the offline native ReAct weather example."""

import asyncio
import runpy
from pathlib import Path

from by_framework.agent import FunctionTool, ToolMessage

EXAMPLE = Path(__file__).parents[2] / "examples/native_agent/react_weather_agent.py"


def test_react_weather_agent_completes_two_model_turns(capsys):
    example = runpy.run_path(EXAMPLE, run_name="react_weather_agent")
    final_answer = example["FINAL_ANSWER"]
    get_weather = example["get_weather"]
    run_example = example["run_example"]
    events, result, model = asyncio.run(run_example())

    weather_tool = FunctionTool(get_weather)
    assert weather_tool.spec.input_schema == {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ("city",),
        "additionalProperties": False,
    }
    assert weather_tool.spec.output_schema == {
        "type": "object",
        "additionalProperties": {"anyOf": ({"type": "string"}, {"type": "integer"})},
    }

    assert len(model.requests) == 2
    assert model.requests[0].tools[0].name == "get_weather"
    tool_message = model.requests[1].messages[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "weather-1"
    assert '"city": "San Francisco"' in tool_message.content
    assert '"temperature_c": 18' in tool_message.content

    event_kinds = [event.kind for event in events]
    assert event_kinds[0] == "run_started"
    assert "tool_call" in event_kinds
    assert "tool_result" in event_kinds
    assert "text_delta" in event_kinds
    assert event_kinds[-1] == "run_completed"
    assert result.output == final_answer
    tool_call = next(event for event in events if event.kind == "tool_call")
    assert tool_call.data == {
        "id": "weather-1",
        "name": "get_weather",
        "arguments": {"city": "San Francisco"},
    }
    tool_result = next(event for event in events if event.kind == "tool_result")
    assert tool_result.data == {
        "id": "weather-1",
        "name": "get_weather",
        "output": {
            "city": "San Francisco",
            "condition": "sunny",
            "temperature_c": 18,
        },
    }

    output = capsys.readouterr().out
    assert "MODEL -> TOOL: get_weather" in output
    assert "TOOL -> MODEL:" in output
    assert f"FINAL RESULT: {final_answer}" in output
