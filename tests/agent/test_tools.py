"""Function tool schema and execution tests."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from by_framework.agent import (
    FunctionTool,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
)
from by_framework.agent.tools import ToolExecutionError, ToolValidationError


def test_function_tool_builds_closed_schema_and_executes_sync_callable():
    def add(left: int, right: int = 1) -> int:
        """Add two integers."""
        return left + right

    tool = FunctionTool(add)
    assert tool.spec.description == "Add two integers."
    assert tool.spec.input_schema == {
        "type": "object",
        "properties": {
            "left": {"type": "integer"},
            "right": {"type": "integer", "default": 1},
        },
        "required": ("left",),
        "additionalProperties": False,
    }
    assert tool.spec.output_schema == {"type": "integer"}
    assert isinstance(tool.spec.input_schema, Mapping)
    with pytest.raises(TypeError):
        tool.spec.input_schema["type"] = "array"

    result = asyncio.run(
        ToolExecutor({"add": tool}).execute(ToolCall("call-1", "add", {"left": 2}))
    )
    assert result.output == 3
    assert result.model_content() == "3"


def test_tool_executor_supports_async_and_wraps_business_failure():
    async def fail(value: str) -> str:
        raise LookupError(value)

    executor = ToolExecutor({"fail": FunctionTool(fail)})
    with pytest.raises(ToolExecutionError, match="tool 'fail' failed"):
        asyncio.run(executor.execute(ToolCall("call-1", "fail", {"value": "bad"})))


def test_tool_executor_rejects_output_that_violates_declared_type():
    def broken() -> int:
        return "not an integer"  # type: ignore[return-value]

    executor = ToolExecutor({"broken": FunctionTool(broken)})
    with pytest.raises(ToolExecutionError, match="expected integer"):
        asyncio.run(executor.execute(ToolCall("call-1", "broken", {})))


def test_tool_executor_rejects_non_json_output_even_when_annotated_any():
    def broken() -> Any:
        return b"not-json"

    executor = ToolExecutor({"broken": FunctionTool(broken)})
    with pytest.raises(ToolExecutionError, match="JSON serializable"):
        asyncio.run(executor.execute(ToolCall("call-1", "broken", {})))


def test_tool_executor_rejects_unknown_tool_and_bad_arguments():
    def echo(value: str) -> str:
        return value

    executor = ToolExecutor({"echo": FunctionTool(echo)})
    with pytest.raises(ToolValidationError, match="unknown tool"):
        asyncio.run(executor.execute(ToolCall("1", "missing", {})))
    with pytest.raises(ToolValidationError, match="invalid arguments"):
        asyncio.run(executor.execute(ToolCall("2", "echo", {"extra": True})))
    with pytest.raises(ToolValidationError, match="expected string"):
        asyncio.run(executor.execute(ToolCall("3", "echo", {"value": 42})))


def test_tool_executor_validates_nested_collection_arguments():
    def summarize(items: list[dict[str, int]]) -> int:
        return sum(item["value"] for item in items)

    executor = ToolExecutor({"summarize": FunctionTool(summarize)})
    with pytest.raises(
        ToolValidationError, match=r"\$\.items\[0\]\.value: expected integer"
    ):
        asyncio.run(
            executor.execute(
                ToolCall("1", "summarize", {"items": [{"value": "wrong"}]})
            )
        )


def test_context_aware_tool_requires_executor_context():
    def contextual(context: ToolExecutionContext) -> str:
        return context.run.identity.run_id

    tool = FunctionTool(contextual)
    assert tool.spec.input_schema["properties"] == {}
    with pytest.raises(ToolValidationError, match="requires a runtime context"):
        asyncio.run(
            ToolExecutor({"contextual": tool}).execute(
                ToolCall("call-1", "contextual", {})
            )
        )
