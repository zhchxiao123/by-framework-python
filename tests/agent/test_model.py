"""Provider-neutral scripted model contract tests."""

import asyncio

import pytest

from by_framework.agent import (
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    TextDelta,
    ToolCall,
    Usage,
    UserMessage,
)
from by_framework.agent.model import (
    ModelExhaustedError,
    ResponseCompleted,
    ToolCallCompleted,
    UsageCompleted,
)


def test_scripted_model_normalizes_text_tool_calls_usage_and_completion():
    call = ToolCall("call-1", "weather", {"city": "LA"})
    model = ScriptedModel(
        [
            ModelResponse(
                text="checking",
                tool_calls=(call,),
                usage=Usage(4, 2),
                finish_reason="tool_calls",
                provider_metadata={"opaque": "allowed-at-boundary"},
            )
        ],
        text_chunk_size=4,
    )

    async def collect():
        return [
            event
            async for event in model.stream(
                ModelRequest(messages=(UserMessage("weather?"),))
            )
        ]

    events = asyncio.run(collect())
    assert [event.text for event in events if isinstance(event, TextDelta)] == [
        "chec",
        "king",
    ]
    assert [
        event.tool_call for event in events if isinstance(event, ToolCallCompleted)
    ] == [call]
    assert [event.usage for event in events if isinstance(event, UsageCompleted)] == [
        Usage(4, 2)
    ]
    assert isinstance(events[-1], ResponseCompleted)
    assert model.requests[0].messages == (UserMessage("weather?"),)


def test_scripted_model_fails_clearly_when_exhausted():
    model = ScriptedModel([])

    async def collect():
        return [event async for event in model.stream(ModelRequest(messages=()))]

    with pytest.raises(ModelExhaustedError):
        asyncio.run(collect())
