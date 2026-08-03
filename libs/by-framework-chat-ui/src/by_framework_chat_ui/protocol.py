"""Interpret decoded data-stream `DataMessage`s into chat-ui-facing events.

The wire shape (`event_type`, `data.contentType`, `data.choices[0].delta.content`)
is the Byai SSE-compatible layout produced by
`by_framework.common.emitter.DefaultSseLayoutBuilder`. This module is the one
place that understands that shape so the rest of the chat-ui codebase can work
with a small, explicit set of chat-ui-domain events instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from by_framework.core.protocol.content_type import SseReasonMessageType
from by_framework.core.protocol.data_message import DataMessage
from by_framework.core.protocol.event_type import EventType

_ASK_USER_CONTENT_TYPES = {
    SseReasonMessageType.task_user_input.value,
    SseReasonMessageType.ask_user_question.value,
}


@dataclass(frozen=True)
class AnswerChunk:
    """One incremental piece of the assistant's answer."""

    content: str


@dataclass(frozen=True)
class FinalAnswer:
    """The complete assistant answer, sent once a turn finishes successfully."""

    content: str


@dataclass(frozen=True)
class AskUser:
    """The assistant is asking the user a clarifying question and is waiting."""

    prompt: str


@dataclass(frozen=True)
class StreamEnd:
    """The data stream for this turn is definitively closed."""


@dataclass(frozen=True)
class ToolCall:
    """The assistant invoked a tool mid-turn."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResult:
    """A previously-invoked tool call's result arrived."""

    call_id: str
    content: str
    tool_name: str = ""


@dataclass(frozen=True)
class Other:
    """An event this chat-ui build has no dedicated handling for."""

    event_type: str


ParsedEvent = (
    AnswerChunk | FinalAnswer | AskUser | StreamEnd | ToolCall | ToolResult | Other
)


def _extract_delta(data: dict) -> dict:
    choices = data.get("choices") or []
    if not choices:
        return {}
    return choices[0].get("delta") or {}


def _extract_delta_content(data: dict) -> str:
    return _extract_delta(data).get("content") or ""


def _extract_tool_calls(delta: dict) -> list[ToolCall]:
    events = []
    for item in delta.get("tool_calls") or []:
        function = item.get("function") or {}
        events.append(
            ToolCall(
                call_id=item.get("id", ""),
                name=function.get("name", ""),
                arguments=function.get("arguments", ""),
            )
        )
    return events


def _extract_tool_results(delta: dict, metadata: dict) -> list[ToolResult]:
    events = []
    for item in delta.get("tool_responses") or []:
        events.append(
            ToolResult(
                call_id=item.get("tool_call_id", ""),
                content=item.get("content", ""),
                tool_name=metadata.get("tool_name", ""),
            )
        )
    return events


def _extract_ask_user_prompt(raw_content: str) -> str:
    try:
        parsed = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return raw_content
    if not isinstance(parsed, dict):
        return raw_content
    fields = parsed.get("pluginMachineFields")
    if fields:
        return fields[0].get("description", raw_content)
    return raw_content


def interpret_data_message(message: DataMessage) -> list[ParsedEvent]:
    """Classify a decoded `DataMessage` into a list of chat-ui-domain events.

    Almost always a single-element list; a list because a single delta can
    legitimately carry more than one tool call/result (parallel tool calls),
    and returning only the first — as an earlier version of this function
    did — silently dropped the rest. Same reasoning covers a delta that
    somehow carries both `tool_calls` and `tool_responses`: both are
    returned rather than one silently winning.

    Tool-call/result detection happens first and by field presence
    (`delta.tool_calls`/`delta.tool_responses`), not by `event_type` or
    `contentType`: both fields ride as siblings of `delta.content` at the
    generic "1002" contentType regardless of whether the emitting agent is
    top-level (`answerDelta`) or a sub-agent (`reasoningLogDelta`). Checking
    this before the event_type branches below also fixes a silent bug where
    a tool-call-only chunk (`delta.content` is null) used to fall through to
    the `AnswerChunk` branch and render as an empty, invisible chunk.
    """
    data = message.data or {}
    event_type = message.event_type
    delta = _extract_delta(data)

    tool_events: list[ParsedEvent] = [
        *_extract_tool_calls(delta),
        *_extract_tool_results(delta, message.metadata or {}),
    ]
    if tool_events:
        return tool_events

    if event_type == EventType.APP_STREAM_RESPONSE.value:
        return [StreamEnd()]

    if event_type == EventType.FINAL_ANSWER.value:
        return [FinalAnswer(content=_extract_delta_content(data))]

    if event_type == EventType.REASONING_LOG_DELTA.value:
        if data.get("contentType") in _ASK_USER_CONTENT_TYPES:
            prompt = _extract_ask_user_prompt(_extract_delta_content(data))
            return [AskUser(prompt=prompt)]
        return [Other(event_type=event_type)]

    if event_type == EventType.ANSWER_DELTA.value:
        return [AnswerChunk(content=_extract_delta_content(data))]

    return [Other(event_type=event_type)]
