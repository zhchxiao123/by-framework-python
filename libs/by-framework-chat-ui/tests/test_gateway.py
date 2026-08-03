# pylint: disable=C0114,C0116
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from by_framework import GatewayClient
from by_framework.core.protocol.action_type import ActionType
from by_framework.core.protocol.data_message import DataMessage

from by_framework_chat_ui.gateway import (
    AgentUnavailableError,
    TurnResult,
    TurnTimeoutError,
    dispatch_and_await,
    stream_turn,
)
from by_framework_chat_ui.protocol import (AnswerChunk, AskUser, FinalAnswer, StreamEnd)


def _answer_delta(content):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={"contentType": "1002", "choices": [{"delta": {"content": content}}]},
    )


def _final_answer(content):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="finalAnswer",
        data={"contentType": "1002", "choices": [{"delta": {"content": content}}]},
    )


def _stream_end():
    return DataMessage(
        trace_id="t1", session_id="s1", event_type="appStreamResponse", data={}
    )


def _ask_user_form(prompt):
    form = json.dumps(
        {
            "formStatus": 0,
            "pluginMachineFields": [
                {
                    "formType": "textarea",
                    "fieldName": "user_input",
                    "fieldCode": "user_input",
                    "description": prompt,
                    "required": True,
                }
            ],
        }
    )
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="reasoningLogDelta",
        data={"contentType": "3013", "choices": [{"delta": {"content": form}}]},
    )


def _xread_batch(*messages, start_id=1):
    entries = [
        (f"{i}-0", message.to_redis_payload())
        for i, message in enumerate(messages, start=start_id)
    ]
    return [("stream", entries)]


def _make_client(*, online=True, xread_batches=()):
    redis = AsyncMock()
    redis.xrevrange.return_value = []
    redis.xread.side_effect = list(xread_batches)
    registry = AsyncMock()
    registry.has_online_agent_type.return_value = (
        (True, ["worker-1"]) if online else (False, [])
    )
    client = GatewayClient(redis_client=redis, registry=registry)
    return client, redis


@pytest.mark.asyncio
async def test_rejects_dispatch_when_agent_offline():
    client, redis = _make_client(online=False)

    with pytest.raises(AgentUnavailableError) as exc_info:
        await dispatch_and_await(
            client, session_id="s1", agent_type="planner", content="hi"
        )

    assert exc_info.value.agent_type == "planner"
    redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_completed_turn_with_final_answer_text():
    client, redis = _make_client(
        xread_batches=[
            _xread_batch(_answer_delta("hel"), _answer_delta("lo")),
            _xread_batch(_final_answer("hello"), _stream_end(), start_id=3),
        ]
    )

    result = await dispatch_and_await(
        client, session_id="s1", agent_type="planner", content="hi"
    )

    assert result == TurnResult(status="completed", content="hello")
    redis.xadd.assert_awaited()  # send_message dispatched the command


@pytest.mark.asyncio
async def test_returns_waiting_user_turn_when_agent_asks_a_question():
    client, redis = _make_client(
        xread_batches=[_xread_batch(_ask_user_form("What is your name?"))]
    )

    result = await dispatch_and_await(
        client, session_id="s1", agent_type="planner", content="hi"
    )

    assert result == TurnResult(status="waiting_user", content="What is your name?")


@pytest.mark.asyncio
async def test_sends_with_the_given_action_type():
    client, redis = _make_client(
        xread_batches=[_xread_batch(_final_answer("ok"), _stream_end())]
    )

    await dispatch_and_await(
        client,
        session_id="s1",
        agent_type="planner",
        content="my answer",
        action_type=ActionType.RESUME.value,
    )

    args, kwargs = redis.xadd.call_args
    payload = json.loads(args[1]["data"])
    assert payload["action_type"] == ActionType.RESUME.value


@pytest.mark.asyncio
async def test_dispatch_and_await_passes_through_message_id_and_parent_message_id():
    client, redis = _make_client(
        xread_batches=[_xread_batch(_final_answer("ok"), _stream_end())]
    )

    await dispatch_and_await(
        client,
        session_id="s1",
        agent_type="planner",
        content="Alice",
        action_type=ActionType.RESUME.value,
        message_id="msg-fixed123",
        parent_message_id="msg-fixed123",
    )

    args, kwargs = redis.xadd.call_args
    payload = json.loads(args[1]["data"])
    assert payload["header"]["message_id"] == "msg-fixed123"
    assert payload["header"]["parent_message_id"] == "msg-fixed123"


@pytest.mark.asyncio
async def test_stream_turn_passes_through_message_id():
    client, redis = _make_client(
        xread_batches=[_xread_batch(_final_answer("ok"), _stream_end())]
    )

    events = [
        event
        async for event in stream_turn(
            client,
            session_id="s1",
            agent_type="planner",
            content="hi",
            message_id="msg-fixed456",
        )
    ]
    del events

    args, kwargs = redis.xadd.call_args
    payload = json.loads(args[1]["data"])
    assert payload["header"]["message_id"] == "msg-fixed456"


@pytest.mark.asyncio
async def test_times_out_when_no_terminal_event_ever_arrives():
    client, redis = _make_client()

    async def _never_ending_empty_reads(*_args, **_kwargs):
        # A real Redis xread always yields control back to the event loop;
        # an unconditionally-instant mock would starve asyncio.wait_for's
        # own timer and this test would hang instead of timing out.
        await asyncio.sleep(0)
        return [("stream", [])]

    redis.xread.side_effect = _never_ending_empty_reads

    with pytest.raises(TurnTimeoutError):
        await dispatch_and_await(
            client,
            session_id="s1",
            agent_type="planner",
            content="hi",
            timeout_seconds=0.05,
        )


@pytest.mark.asyncio
async def test_stream_turn_yields_chunks_then_final_answer_then_stops():
    client, redis = _make_client(
        xread_batches=[
            _xread_batch(_answer_delta("hel"), _answer_delta("lo")),
            _xread_batch(_final_answer("hello"), _stream_end(), start_id=3),
        ]
    )

    events = [
        event
        async for event in stream_turn(
            client, session_id="s1", agent_type="planner", content="hi"
        )
    ]

    assert events == [
        AnswerChunk(content="hel"),
        AnswerChunk(content="lo"),
        FinalAnswer(content="hello"),
        StreamEnd(),
    ]


@pytest.mark.asyncio
async def test_stream_turn_stops_at_ask_user_without_stream_end():
    client, redis = _make_client(
        xread_batches=[_xread_batch(_ask_user_form("Which region?"))]
    )

    events = [
        event
        async for event in stream_turn(
            client, session_id="s1", agent_type="planner", content="hi"
        )
    ]

    assert events == [AskUser(prompt="Which region?")]


@pytest.mark.asyncio
async def test_stream_turn_raises_agent_unavailable_before_yielding_anything():
    client, redis = _make_client(online=False)

    async def _consume():
        async for event in stream_turn(
            client, session_id="s1", agent_type="planner", content="hi"
        ):
            pass

    with pytest.raises(AgentUnavailableError):
        await _consume()

    redis.xadd.assert_not_awaited()
