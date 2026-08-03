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
class Other:
    """An event this chat-ui build has no dedicated handling for."""

    event_type: str


ParsedEvent = AnswerChunk | FinalAnswer | AskUser | StreamEnd | Other


def _extract_delta_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return delta.get("content") or ""


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


def interpret_data_message(message: DataMessage) -> ParsedEvent:
    """Classify a decoded `DataMessage` into a chat-ui-domain event."""
    data = message.data or {}
    event_type = message.event_type

    if event_type == EventType.APP_STREAM_RESPONSE.value:
        return StreamEnd()

    if event_type == EventType.FINAL_ANSWER.value:
        return FinalAnswer(content=_extract_delta_content(data))

    if event_type == EventType.REASONING_LOG_DELTA.value:
        if data.get("contentType") in _ASK_USER_CONTENT_TYPES:
            prompt = _extract_ask_user_prompt(_extract_delta_content(data))
            return AskUser(prompt=prompt)
        return Other(event_type=event_type)

    if event_type == EventType.ANSWER_DELTA.value:
        return AnswerChunk(content=_extract_delta_content(data))

    return Other(event_type=event_type)
