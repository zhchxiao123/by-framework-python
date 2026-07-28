"""Provider-neutral model contracts and deterministic test implementations."""

from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SystemMessage:
    content: str
    role: Literal["system"] = "system"


@dataclass(frozen=True)
class UserMessage:
    content: str
    role: Literal["user"] = "user"


@dataclass(frozen=True)
class AssistantMessage:
    content: str = ""
    tool_calls: tuple["ToolCall", ...] = ()
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True)
class ToolMessage:
    tool_call_id: str
    content: str
    role: Literal["tool"] = "tool"


ModelMessage = SystemMessage | UserMessage | AssistantMessage | ToolMessage


@dataclass(frozen=True)
class ToolDeclaration:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDeclaration, ...] = ()
    output_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ToolCallCompleted:
    tool_call: ToolCall


@dataclass(frozen=True)
class UsageCompleted:
    usage: Usage


@dataclass(frozen=True)
class ResponseCompleted:
    response: ModelResponse


ModelStreamEvent = (
    TextDelta | ReasoningDelta | ToolCallCompleted | UsageCompleted | ResponseCompleted
)


class Model(Protocol):
    """A model emits only normalized core stream events."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Stream one model turn."""


class ScriptedModel:
    """Returns predefined turns and records requests for deterministic tests."""

    def __init__(
        self,
        responses: Iterable[ModelResponse],
        *,
        text_chunk_size: int | None = None,
    ):
        self._responses = deque(responses)
        self.model_ref = "by-framework:scripted-model"
        self._text_chunk_size = text_chunk_size
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        if not self._responses:
            raise ModelExhaustedError("scripted model has no response remaining")
        response = self._responses.popleft()
        for chunk in _chunks(response.text, self._text_chunk_size):
            yield TextDelta(chunk)
        for tool_call in response.tool_calls:
            yield ToolCallCompleted(tool_call)
        if response.usage != Usage():
            yield UsageCompleted(response.usage)
        yield ResponseCompleted(response)


class FakeModel(ScriptedModel):
    """Convenience deterministic model for a single text response."""

    def __init__(self, text: str, *, usage: Usage | None = None):
        super().__init__([ModelResponse(text=text, usage=usage or Usage())])


class ModelError(RuntimeError):
    """Base error raised by normalized model implementations."""


class ModelExhaustedError(ModelError):
    """A scripted model received more requests than configured turns."""


def _chunks(text: str, size: int | None) -> Sequence[str]:
    if not text:
        return ()
    if size is None:
        return (text,)
    if size <= 0:
        raise ValueError("text_chunk_size must be positive")
    return tuple(text[index : index + size] for index in range(0, len(text), size))
