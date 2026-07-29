"""Real OpenAI Chat Completions ReAct example."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from by_framework.agent import Agent, FunctionTool, Runner
from by_framework_model_openai import OpenAICompatibleModel, TransportError

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6-terra"


class OpenAIChatTransport:
    """Stream decoded OpenAI-compatible Chat Completions chunks."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
    ):
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    async def stream(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> AsyncIterator[Mapping[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._http_client is not None:
                async for chunk in self._stream_with_client(
                    self._http_client, payload, headers, timeout
                ):
                    yield chunk
                return
            async with httpx.AsyncClient() as client:
                async for chunk in self._stream_with_client(
                    client, payload, headers, timeout
                ):
                    yield chunk
        except httpx.TimeoutException as exc:
            raise asyncio.TimeoutError from exc
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

    async def _stream_with_client(
        self,
        client: httpx.AsyncClient,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
    ) -> AsyncIterator[Mapping[str, Any]]:
        async with client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=dict(payload),
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise TransportError(
                    body.decode("utf-8", errors="replace"),
                    status_code=response.status_code,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data or data == "[DONE]":
                    continue
                yield json.loads(data)


def get_weather(city: str) -> dict[str, str]:
    """Get the current weather for a city."""
    # Replace this deterministic body with a real weather provider if needed.
    return {"city": city, "temperature": "18°C", "condition": "sunny"}


def build_agent() -> Agent:
    """Build an Agent backed by a real OpenAI-compatible endpoint."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    transport = OpenAIChatTransport(
        api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
    )
    model = OpenAICompatibleModel(
        os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        transport,
        timeout=60,
        max_retries=2,
        provider_id="openai",
    )
    return Agent(
        "weather-agent",
        (
            "You are a weather assistant. When asked about weather, call "
            "get_weather and answer in Chinese using only the tool result."
        ),
        model,
        [FunctionTool(get_weather)],
    )


async def run_example() -> None:
    """Run the live ReAct loop and print important stream events."""
    stream = Runner().run_streamed(
        build_agent(),
        "旧金山今天天气怎么样？",
    )
    async for event in stream:
        if event.kind == "text_delta":
            print(event.data["text"], end="", flush=True)
        elif event.kind == "tool_call":
            print(f"\nMODEL -> TOOL: {event.data}")
        elif event.kind == "tool_result":
            print(f"TOOL -> MODEL: {event.data}")
    result = await stream.result()
    print(f"\n\nFINAL RESULT: {result.output}")
    print(f"USAGE: {result.usage}")


if __name__ == "__main__":
    asyncio.run(run_example())
