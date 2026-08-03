"""Dispatch a chat message and consume the turn's data-stream events.

Wraps `GatewayClient.send_message` + `read_data_messages` so the rest of
chat-ui can either await a single `TurnResult` (slice 1's full-reply flow)
or async-iterate live events as they arrive (slice 2's streaming flow),
without hand-rolling data-stream consumption twice. `send_message`'s default
`route_policy=FAIL_FAST` already returns `SendMessageResponse(success=False,
...)` when the target `agent_type` has no online worker; this module turns
that into `AgentUnavailableError` for callers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from by_framework.client.client import GatewayClient
from by_framework.common.constants import RedisKeys
from by_framework.core.protocol.action_type import ActionType

from .protocol import (
    AnswerChunk,
    AskUser,
    FinalAnswer,
    ParsedEvent,
    StreamEnd,
    ToolCall,
    ToolResult,
    interpret_data_message,
)

DEFAULT_TIMEOUT_SECONDS = 60.0


class AgentUnavailableError(Exception):
    """The target agent_type has no online worker to dispatch to."""

    def __init__(self, agent_type: str):
        super().__init__(f"'{agent_type}' is currently unavailable")
        self.agent_type = agent_type


class TurnTimeoutError(Exception):
    """No terminal event arrived on the data stream within the deadline."""


@dataclass(frozen=True)
class TurnResult:
    """The outcome of one dispatched turn."""

    status: str  # "completed" or "waiting_user"
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def accumulate_tool_event(
    tool_calls: dict[str, dict[str, Any]], event: ToolCall | ToolResult
) -> None:
    """Merge a `ToolCall`/`ToolResult` event into a turn's tool-call record.

    One dict per `call_id`, keyed so a later `ToolResult` fills in the
    `result` field of the matching earlier `ToolCall` instead of creating a
    separate entry — used by both `dispatch_and_await` (below) and
    `server.py`'s streaming `_ws_conversation` handler, since both need to
    end up with the same one-row-per-turn persisted shape.
    """
    if isinstance(event, ToolCall):
        # Unconditional overwrite, not setdefault: a second ToolCall for the
        # same call_id (a legitimate retry, however unlikely) must win with
        # its fresh arguments rather than keep whatever was recorded first.
        tool_calls[event.call_id] = {
            "call_id": event.call_id,
            "name": event.name,
            "arguments": event.arguments,
            "result": None,
        }
        return

    entry = tool_calls.setdefault(
        event.call_id,
        {
            "call_id": event.call_id,
            "name": event.tool_name,
            "arguments": "",
            "result": None,
        },
    )
    entry["result"] = event.content
    if event.tool_name and not entry.get("name"):
        entry["name"] = event.tool_name


async def _tail_stream_id(client: GatewayClient, session_id: str) -> str:
    """Return the data stream's current last id, or "0-0" if it doesn't exist yet.

    Captured *before* dispatch so the consume loop only ever sees entries
    produced by this turn — race-free against entries from an earlier turn
    on the same Conversation.
    """
    stream_name = RedisKeys.session_data_stream(session_id)
    entries = await client.redis.xrevrange(stream_name, count=1)
    if not entries:
        return "0-0"
    stream_id, fields = entries[0]
    del fields
    return stream_id.decode() if isinstance(stream_id, bytes) else stream_id


async def _dispatch(
    client: GatewayClient,
    *,
    session_id: str,
    agent_type: str,
    content: str,
    action_type: str,
    message_id: str = "",
    parent_message_id: str = "",
) -> None:
    try:
        response = await client.send_message(
            target_agent_type=agent_type,
            session_id=session_id,
            content=content,
            action_type=action_type,
            message_id=message_id or None,
            parent_message_id=parent_message_id,
        )
    except ValueError as exc:
        raise AgentUnavailableError(agent_type) from exc

    if not response.success:
        raise AgentUnavailableError(agent_type)


async def stream_turn(
    client: GatewayClient,
    *,
    session_id: str,
    agent_type: str,
    content: str,
    action_type: str = ActionType.ASK_AGENT.value,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    message_id: str = "",
    parent_message_id: str = "",
) -> AsyncIterator[ParsedEvent]:
    """Dispatch one turn and yield its events as they arrive on the data stream.

    Stops (the generator returns) once an `AskUser` or `StreamEnd` event is
    yielded — both are terminal for a turn, per the framework's contract that
    `APP_STREAM_RESPONSE` is withheld exactly when the task suspended on
    `ask_user`. Raises `TurnTimeoutError` if neither arrives in time.
    `ToolCall`/`ToolResult` are non-terminal, like `AnswerChunk` — they don't
    end the turn.

    `message_id`/`parent_message_id` must be the caller's own
    `Conversation.last_message_id` (generated once per `ASK_AGENT`, reused
    unchanged for the matching `RESUME`) — see
    `Conversation.generate_message_id`'s docstring for why leaving these
    blank on a RESUME orphans the suspended execution.
    """
    current_id = await _tail_stream_id(client, session_id)
    await _dispatch(
        client,
        session_id=session_id,
        agent_type=agent_type,
        content=content,
        action_type=action_type,
        message_id=message_id,
        parent_message_id=parent_message_id,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TurnTimeoutError(
                f"Timed out waiting for a reply on session '{session_id}'"
            )
        try:
            entries = await asyncio.wait_for(
                client.read_data_messages(
                    session_id=session_id, last_id=current_id, block_ms=5000, count=100
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TurnTimeoutError(
                f"Timed out waiting for a reply on session '{session_id}'"
            ) from exc

        for entry in entries:
            current_id = entry.stream_id
            for parsed in interpret_data_message(entry.message):
                if isinstance(
                    parsed,
                    (
                        AnswerChunk,
                        FinalAnswer,
                        AskUser,
                        StreamEnd,
                        ToolCall,
                        ToolResult,
                    ),
                ):
                    yield parsed
                if isinstance(parsed, (AskUser, StreamEnd)):
                    return


async def dispatch_and_await(
    client: GatewayClient,
    *,
    session_id: str,
    agent_type: str,
    content: str,
    action_type: str = ActionType.ASK_AGENT.value,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    message_id: str = "",
    parent_message_id: str = "",
) -> TurnResult:
    """Dispatch one turn and block until it completes or asks the user something."""
    final_text = ""
    tool_calls: dict[str, dict[str, Any]] = {}
    async for event in stream_turn(
        client,
        session_id=session_id,
        agent_type=agent_type,
        content=content,
        action_type=action_type,
        timeout_seconds=timeout_seconds,
        message_id=message_id,
        parent_message_id=parent_message_id,
    ):
        if isinstance(event, FinalAnswer):
            final_text = event.content
        elif isinstance(event, (ToolCall, ToolResult)):
            accumulate_tool_event(tool_calls, event)
        elif isinstance(event, AskUser):
            return TurnResult(
                status="waiting_user",
                content=event.prompt,
                tool_calls=list(tool_calls.values()),
            )
        elif isinstance(event, StreamEnd):
            return TurnResult(
                status="completed",
                content=final_text,
                tool_calls=list(tool_calls.values()),
            )

    raise TurnTimeoutError(
        f"Data stream for session '{session_id}' ended without a terminal event"
    )
