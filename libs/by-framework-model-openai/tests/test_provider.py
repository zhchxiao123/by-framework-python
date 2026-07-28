"""OpenAI-compatible provider contract tests with an injected transport."""

import asyncio
import json

import pytest

from by_framework.agent import (
    ModelRequest,
    ReasoningDelta,
    TextDelta,
    ToolCallCompleted,
    UsageCompleted,
    UserMessage,
)
from by_framework_model_openai import (
    AuthenticationError,
    InvalidRequestError,
    InvalidResponseError,
    OpenAICompatibleModel,
    RetryableProviderError,
    TransportError,
)


class Transport:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.payloads = []

    async def stream(self, payload, *, timeout):
        self.payloads.append((payload, timeout))
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        for chunk in script:
            yield chunk


def collect(model, request):
    async def run():
        return [event async for event in model.stream(request)]

    return asyncio.run(run())


def test_normalizes_text_reasoning_fragmented_tool_usage_and_metadata():
    transport = Transport(
        [
            [
                {
                    "id": "response-1",
                    "model": "compatible-model",
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "think",
                                "content": "Hello ",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "weather",
                                            "arguments": '{"city":',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "world",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '"LA"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 4},
                },
            ]
        ]
    )
    model = OpenAICompatibleModel("test", transport)
    events = collect(model, ModelRequest((UserMessage("hello"),)))

    assert [event.text for event in events if isinstance(event, TextDelta)] == [
        "Hello ",
        "world",
    ]
    assert [event.text for event in events if isinstance(event, ReasoningDelta)] == [
        "think"
    ]
    tool = next(
        event.tool_call for event in events if isinstance(event, ToolCallCompleted)
    )
    assert (tool.id, tool.name, tool.arguments) == (
        "call-1",
        "weather",
        {"city": "LA"},
    )
    assert (
        next(
            event.usage for event in events if isinstance(event, UsageCompleted)
        ).input_tokens
        == 7
    )
    response = events[-1].response
    assert response.finish_reason == "tool_calls"
    assert response.provider_metadata == {
        "response_id": "response-1",
        "model": "compatible-model",
    }


def test_structured_output_request_and_retry_before_any_output():
    transport = Transport(
        [
            TransportError("temporary", status_code=503),
            [
                {
                    "choices": [
                        {
                            "delta": {"content": '{"ok":true}'},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ],
        ]
    )
    model = OpenAICompatibleModel("test", transport, timeout=5, max_retries=1)
    events = collect(
        model,
        ModelRequest(
            (UserMessage("json"),),
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        ),
    )
    assert events[-1].response.text == '{"ok":true}'
    assert len(transport.payloads) == 2
    assert transport.payloads[0][0]["response_format"]["json_schema"]["strict"] is True
    json.dumps(transport.payloads[0][0])
    assert transport.payloads[0][1] == 5


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TransportError("bad key", status_code=401), AuthenticationError),
        (TransportError("bad request", status_code=400), InvalidRequestError),
        (TransportError("server", status_code=500), RetryableProviderError),
        (asyncio.TimeoutError(), RetryableProviderError),
    ],
)
def test_normalizes_transport_errors(error, expected):
    with pytest.raises(expected):
        collect(
            OpenAICompatibleModel("test", Transport([error])),
            ModelRequest((UserMessage("hello"),)),
        )


def test_rejects_incomplete_or_invalid_tool_call():
    transport = Transport(
        [
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call",
                                        "function": {
                                            "name": "tool",
                                            "arguments": "{bad",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        ]
    )
    with pytest.raises(InvalidResponseError):
        collect(
            OpenAICompatibleModel("test", transport),
            ModelRequest((UserMessage("hello"),)),
        )


def test_does_not_retry_after_normalized_output_was_emitted():
    class PartialTransport:
        def __init__(self):
            self.calls = 0

        async def stream(self, payload, *, timeout):
            del payload, timeout
            self.calls += 1
            yield {"choices": [{"delta": {"reasoning_content": "started"}}]}
            raise TransportError("server", status_code=503)

    transport = PartialTransport()
    with pytest.raises(RetryableProviderError):
        collect(
            OpenAICompatibleModel("test", transport, max_retries=3),
            ModelRequest((UserMessage("hello"),)),
        )
    assert transport.calls == 1


def test_stream_cancellation_closes_injected_transport_iterator():
    class BlockingTransport:
        def __init__(self):
            self.closed = False

        async def stream(self, payload, *, timeout):
            del payload, timeout
            try:
                yield {"choices": [{"delta": {"content": "first"}}]}
                await asyncio.Future()
            finally:
                self.closed = True

    transport = BlockingTransport()
    model = OpenAICompatibleModel("test", transport)

    async def scenario():
        events = model.stream(ModelRequest((UserMessage("hello"),)))
        assert isinstance(await anext(events), TextDelta)
        await events.aclose()

    asyncio.run(scenario())
    assert transport.closed


def test_rejects_truncated_stream_without_finish_reason():
    transport = Transport([[{"choices": [{"delta": {"content": "partial"}}]}]])
    with pytest.raises(InvalidResponseError, match="finish reason"):
        collect(
            OpenAICompatibleModel("test", transport),
            ModelRequest((UserMessage("hello"),)),
        )
