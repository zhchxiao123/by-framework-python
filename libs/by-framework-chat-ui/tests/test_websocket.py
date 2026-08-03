# pylint: disable=C0114,C0116
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from by_framework.core.protocol.data_message import DataMessage
from support import (
    make_fake_history_store,
    make_fake_redis_and_registry,
    make_gateway_client,
)

from by_framework_chat_ui.server import create_app


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


def _tool_call(call_id, name, arguments):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    }
                }
            ],
        },
    )


def _tool_response(call_id, content, tool_name=""):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "role": "tool",
                        "content": None,
                        "tool_responses": [
                            {"tool_call_id": call_id, "content": content}
                        ],
                    }
                }
            ],
        },
        metadata={"tool_name": tool_name} if tool_name else {},
    )


def _xread_batch(*messages, start_id=1):
    entries = [
        (f"{i}-0", message.to_redis_payload())
        for i, message in enumerate(messages, start=start_id)
    ]
    return [("stream", entries)]


def _make_app(*, xread_batches=(), online=True):
    redis, registry = make_fake_redis_and_registry(
        online=online, xread_batches=xread_batches
    )
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )
    return app, redis


@pytest.mark.asyncio
async def test_streams_chunks_then_final_then_turn_complete():
    batch = _xread_batch(
        _answer_delta("hel"), _answer_delta("lo"), _final_answer("hello"), _stream_end()
    )
    app, redis = _make_app(xread_batches=[batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with tc.ws_connect(f"/ws/conversations/{session_id}") as ws:
            await ws.send_json({"content": "hi"})

            messages = [await ws.receive_json() for _ in range(6)]

    assert messages == [
        {"type": "locked"},
        {"type": "chunk", "content": "hel"},
        {"type": "chunk", "content": "lo"},
        {"type": "final", "content": "hello"},
        {"type": "turn_complete"},
        {"type": "unlocked"},
    ]


@pytest.mark.asyncio
async def test_streams_tool_call_and_tool_result_events():
    batch = _xread_batch(
        _tool_call("call_1", "calculate", '{"expression": "1+1"}'),
        _tool_response("call_1", "2", tool_name="calculate"),
        _final_answer("the answer is 2"),
        _stream_end(),
    )
    app, redis = _make_app(xread_batches=[batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with tc.ws_connect(f"/ws/conversations/{session_id}") as ws:
            await ws.send_json({"content": "what is 1+1?"})
            messages = [await ws.receive_json() for _ in range(6)]

    assert messages == [
        {"type": "locked"},
        {
            "type": "tool_call",
            "call_id": "call_1",
            "name": "calculate",
            "arguments": '{"expression": "1+1"}',
        },
        {
            "type": "tool_result",
            "call_id": "call_1",
            "content": "2",
            "tool_name": "calculate",
        },
        {"type": "final", "content": "the answer is 2"},
        {"type": "turn_complete"},
        {"type": "unlocked"},
    ]


@pytest.mark.asyncio
async def test_ws_persists_the_turns_tool_calls_on_the_assistant_message():
    batch = _xread_batch(
        _tool_call("call_1", "calculate", '{"expression": "1+1"}'),
        _tool_response("call_1", "2", tool_name="calculate"),
        _final_answer("the answer is 2"),
        _stream_end(),
    )
    redis, registry = make_fake_redis_and_registry(xread_batches=[batch])
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with tc.ws_connect(f"/ws/conversations/{session_id}") as ws:
            await ws.send_json({"content": "what is 1+1?"})
            for _ in range(6):
                await ws.receive_json()

    insert_calls = [
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO chat_ui_messages" in call.args[0]
    ]
    assistant_insert = insert_calls[-1]
    assert assistant_insert.args[1:4] == (session_id, "assistant", "the answer is 2")
    persisted_tool_calls = json.loads(assistant_insert.args[5])
    assert persisted_tool_calls == [
        {
            "call_id": "call_1",
            "name": "calculate",
            "arguments": '{"expression": "1+1"}',
            "result": "2",
        }
    ]


@pytest.mark.asyncio
async def test_unknown_conversation_closes_with_error():
    app, redis = _make_app()

    async with TestClient(TestServer(app)) as tc:
        async with tc.ws_connect("/ws/conversations/does-not-exist") as ws:
            message = await ws.receive_json()
            assert message == {"type": "error", "message": "conversation not found"}


@pytest.mark.asyncio
async def test_offline_agent_reports_error_over_the_socket():
    app, redis = _make_app(online=False)

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with tc.ws_connect(f"/ws/conversations/{session_id}") as ws:
            await ws.send_json({"content": "hi"})
            messages = [await ws.receive_json() for _ in range(3)]

    assert messages == [
        {"type": "locked"},
        {"type": "error", "message": "该助手当前不可用"},
        {"type": "unlocked"},
    ]


@pytest.mark.asyncio
async def test_ws_reply_after_ask_user_uses_resume():
    from by_framework.core.protocol.action_type import ActionType

    ask_batch = _xread_batch(_ask_user_form("What is your name?"))
    reply_batch = _xread_batch(_final_answer("hi Alice"), _stream_end(), start_id=5)
    app, redis = _make_app(xread_batches=[ask_batch, reply_batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with tc.ws_connect(f"/ws/conversations/{session_id}") as ws:
            await ws.send_json({"content": "hi"})
            first_turn = [await ws.receive_json() for _ in range(3)]
            assert first_turn == [
                {"type": "locked"},
                {"type": "ask_user", "prompt": "What is your name?"},
                {"type": "unlocked"},
            ]

            await ws.send_json({"content": "Alice"})
            second_turn = [await ws.receive_json() for _ in range(4)]
            assert second_turn == [
                {"type": "locked"},
                {"type": "final", "content": "hi Alice"},
                {"type": "turn_complete"},
                {"type": "unlocked"},
            ]

    first_call = redis.xadd.call_args_list[-2]
    first_payload = json.loads(first_call.args[1]["data"])
    second_call = redis.xadd.call_args_list[-1]
    payload = json.loads(second_call.args[1]["data"])
    assert payload["action_type"] == ActionType.RESUME.value
    # Regression: RESUME must reuse the ASK_AGENT dispatch's message_id so
    # GatewayClient.send_message can look the suspended execution back up
    # (get_execution_by_message_id) — a fresh id silently orphans it.
    assert payload["header"]["message_id"] == first_payload["header"]["message_id"]


@pytest.mark.asyncio
async def test_second_tab_sees_broadcast_lock_state_without_sending():
    batch = _xread_batch(_final_answer("hi there"), _stream_end())
    app, redis = _make_app(xread_batches=[batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with (
            tc.ws_connect(f"/ws/conversations/{session_id}") as sender_ws,
            tc.ws_connect(f"/ws/conversations/{session_id}") as viewer_ws,
        ):
            await sender_ws.send_json({"content": "hi"})

            viewer_messages = [await viewer_ws.receive_json() for _ in range(2)]

    assert viewer_messages == [{"type": "locked"}, {"type": "unlocked"}]


@pytest.mark.asyncio
async def test_second_connection_send_while_first_in_flight_is_rejected():
    release_first = asyncio.Event()
    call_count = 0

    async def _blocking_then_terminal_xread(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await release_first.wait()
        return _xread_batch(_final_answer("done"), _stream_end())

    app, redis = _make_app()
    redis.xread.side_effect = _blocking_then_terminal_xread

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        async with (
            tc.ws_connect(f"/ws/conversations/{session_id}") as first_ws,
            tc.ws_connect(f"/ws/conversations/{session_id}") as second_ws,
        ):
            await first_ws.send_json({"content": "hi"})
            first_locked = await first_ws.receive_json()
            assert first_locked == {"type": "locked"}
            # second_ws is subscribed to the same Conversation, so it also
            # observes the broadcast lock state before sending anything.
            second_sees_lock = await second_ws.receive_json()
            assert second_sees_lock == {"type": "locked"}

            await second_ws.send_json({"content": "again"})
            second_message = await second_ws.receive_json()
            assert second_message == {"type": "error", "message": "请等待当前回复完成"}

            release_first.set()
            remaining = [await first_ws.receive_json() for _ in range(2)]
            assert remaining == [
                {"type": "final", "content": "done"},
                {"type": "turn_complete"},
            ]
