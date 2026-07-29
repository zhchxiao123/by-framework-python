"""Offline tests for the real-provider example transport."""

import asyncio
import json
import runpy
from pathlib import Path

import httpx

EXAMPLE = Path(__file__).parents[2] / "examples/native_agent/openai_weather_agent.py"


def test_openai_transport_streams_sse_without_live_network():
    example = runpy.run_path(EXAMPLE, run_name="openai_weather_agent")
    transport_type = example["OpenAIChatTransport"]
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        body = (
            'data: {"choices":[{"delta":{"content":"你好"},'
            '"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":2,'
            '"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = transport_type("test-key", http_client=client)
            return [
                chunk
                async for chunk in transport.stream(
                    {"model": "test", "stream": True},
                    timeout=5,
                )
            ]

    chunks = asyncio.run(scenario())
    assert captured == {
        "authorization": "Bearer test-key",
        "payload": {"model": "test", "stream": True},
    }
    assert chunks[0]["choices"][0]["delta"]["content"] == "你好"
    assert chunks[1]["usage"] == {"prompt_tokens": 2, "completion_tokens": 1}


def test_live_agent_requires_api_key(monkeypatch):
    example = runpy.run_path(EXAMPLE, run_name="openai_weather_agent")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        example["build_agent"]()
    except RuntimeError as exc:
        assert str(exc) == "OPENAI_API_KEY is required"
    else:  # pragma: no cover
        raise AssertionError("build_agent should reject a missing API key")
