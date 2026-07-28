"""Normalize OpenAI-compatible streaming chunks into core model events."""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from by_framework.agent.model import (
    AssistantMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ReasoningDelta,
    ResponseCompleted,
    TextDelta,
    ToolCall,
    ToolCallCompleted,
    ToolMessage,
    Usage,
    UsageCompleted,
)


class ProviderError(RuntimeError):
    """Base normalized provider failure."""


class AuthenticationError(ProviderError):
    """Provider credentials were rejected."""


class RateLimitError(ProviderError):
    """Provider quota or request rate was exceeded."""


class RetryableProviderError(ProviderError):
    """A transient timeout or server failure."""


class InvalidResponseError(ProviderError):
    """Provider stream data violated the expected contract."""


class InvalidRequestError(ProviderError):
    """Provider rejected the normalized request."""


class TransportError(RuntimeError):
    """Optional transport error shape understood by the adapter."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class StreamingTransport(Protocol):
    """Injected HTTP/SDK transport boundary."""

    def stream(
        self, payload: Mapping[str, Any], *, timeout: float
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Yield decoded OpenAI-compatible stream chunks."""


class OpenAICompatibleModel:
    """OpenAI-compatible chat stream adapter."""

    def __init__(
        self,
        model: str,
        transport: StreamingTransport,
        *,
        timeout: float = 60,
        max_retries: int = 0,
        provider_id: str = "openai-compatible",
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.model = model
        self.model_ref = f"{provider_id}:{model}"
        self.transport = transport
        self.timeout = timeout
        self.max_retries = max_retries

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        payload = self._request_payload(request)
        attempt = 0
        while True:
            emitted = False
            try:
                async for event in self._stream_once(payload):
                    emitted = True
                    yield event
                return
            except (RetryableProviderError, RateLimitError):
                if emitted or attempt >= self.max_retries:
                    raise
                attempt += 1

    async def _stream_once(
        self, payload: Mapping[str, Any]
    ) -> AsyncIterator[ModelStreamEvent]:
        text = []
        finish_reason = "stop"
        usage = Usage()
        saw_finish = False
        metadata: dict[str, Any] = {}
        calls: dict[int, dict[str, str]] = {}
        try:
            async for chunk in self.transport.stream(payload, timeout=self.timeout):
                if chunk.get("id"):
                    metadata["response_id"] = chunk["id"]
                if chunk.get("model"):
                    metadata["model"] = chunk["model"]
                choices = chunk.get("choices", [])
                if choices:
                    choice = choices[0]
                    if choice.get("finish_reason") is not None:
                        finish_reason = str(choice["finish_reason"])
                        saw_finish = True
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if content:
                        if not isinstance(content, str):
                            raise TypeError("content delta must be a string")
                        text.append(content)
                        yield TextDelta(content)
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        if not isinstance(reasoning, str):
                            raise TypeError("reasoning delta must be a string")
                        yield ReasoningDelta(reasoning)
                    for call_delta in delta.get("tool_calls", []):
                        if "index" not in call_delta:
                            raise KeyError("tool call delta is missing index")
                        index = int(call_delta["index"])
                        current = calls.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        current["id"] += call_delta.get("id", "")
                        function = call_delta.get("function", {})
                        current["name"] += function.get("name", "")
                        current["arguments"] += function.get("arguments", "")
                if chunk.get("usage"):
                    raw_usage = chunk["usage"]
                    usage = Usage(
                        int(raw_usage.get("prompt_tokens", 0)),
                        int(raw_usage.get("completion_tokens", 0)),
                    )
                    if usage.input_tokens < 0 or usage.output_tokens < 0:
                        raise ValueError("usage token counts must not be negative")
        except asyncio.TimeoutError as exc:
            raise RetryableProviderError("provider stream timed out") from exc
        except TransportError as exc:
            raise _map_transport_error(exc) from exc
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise InvalidResponseError(
                f"provider returned an invalid stream chunk: {exc}"
            ) from exc
        if not saw_finish:
            raise InvalidResponseError("provider stream ended without a finish reason")
        tool_calls = tuple(
            _finish_tool_call(index, value) for index, value in sorted(calls.items())
        )
        for tool_call in tool_calls:
            yield ToolCallCompleted(tool_call)
        if usage != Usage():
            yield UsageCompleted(usage)
        yield ResponseCompleted(
            ModelResponse(
                "".join(text),
                tool_calls,
                usage,
                finish_reason,
                metadata,
            )
        )

    def _request_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [_message_payload(message) for message in request.messages],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _plain_json(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
        if request.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": _plain_json(request.output_schema),
                },
            }
        return payload


def _message_payload(message) -> dict[str, Any]:
    payload = {"role": message.role, "content": message.content}
    if isinstance(message, AssistantMessage) and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, allow_nan=False, separators=(",", ":")
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _finish_tool_call(index: int, value: Mapping[str, str]) -> ToolCall:
    try:
        arguments = json.loads(value["arguments"] or "{}")
    except json.JSONDecodeError as exc:
        raise InvalidResponseError(
            f"tool call {index} contained invalid JSON arguments"
        ) from exc
    if not isinstance(arguments, dict) or not value["id"] or not value["name"]:
        raise InvalidResponseError(f"tool call {index} was incomplete")
    return ToolCall(value["id"], value["name"], arguments)


def _map_transport_error(error: TransportError) -> ProviderError:
    status = error.status_code
    if status in (401, 403):
        return AuthenticationError(str(error))
    if status == 429:
        return RateLimitError(str(error))
    if status is not None and (status >= 500 or status == 408):
        return RetryableProviderError(str(error))
    if status is not None and 400 <= status < 500:
        return InvalidRequestError(str(error))
    if status is None:
        return RetryableProviderError(str(error))
    return ProviderError(str(error))


def _plain_json(value: Any) -> Any:
    """Convert immutable core schema containers into transport JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value
