# pylint: disable=C0114,C0116
import pytest
from aiohttp.test_utils import TestClient, TestServer
from by_framework.core.protocol.data_message import DataMessage
from support import (
    make_fake_history_store,
    make_fake_redis_and_registry,
    make_gateway_client,
)

from by_framework_chat_ui.server import create_app


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
    import json

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


def _make_app(*, agent_types_by_worker=None, xread_batches=(), online=True):
    redis, registry = make_fake_redis_and_registry(
        online=online,
        xread_batches=xread_batches,
        agent_types_by_worker=agent_types_by_worker,
    )
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )
    return app, redis, registry


@pytest.mark.asyncio
async def test_lists_online_agents():
    app, redis, registry = _make_app(
        agent_types_by_worker={"w1": {"agent_types": ["planner"]}}
    )
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/api/agents")
        assert resp.status == 200
        assert await resp.json() == {"agent_types": ["planner"]}


@pytest.mark.asyncio
async def test_create_conversation_binds_to_one_agent_type():
    app, redis, registry = _make_app()
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.post("/api/conversations", json={"agent_type": "planner"})
        assert resp.status == 200
        body = await resp.json()
        assert body["agent_type"] == "planner"
        assert body["session_id"]


@pytest.mark.asyncio
async def test_create_conversation_requires_agent_type():
    app, redis, registry = _make_app()
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.post("/api/conversations", json={})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_send_message_returns_full_reply():
    batch = _xread_batch(_final_answer("hi there"), _stream_end())
    app, redis, registry = _make_app(xread_batches=[batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        send_resp = await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "hi"}
        )
        assert send_resp.status == 200
        body = await send_resp.json()
        assert body == {
            "role": "assistant",
            "status": "completed",
            "content": "hi there",
            "tool_calls": [],
        }


@pytest.mark.asyncio
async def test_send_message_to_unknown_conversation_is_404():
    app, redis, registry = _make_app()
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.post(
            "/api/conversations/does-not-exist/messages", json={"content": "hi"}
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_send_message_to_offline_agent_reports_unavailable():
    app, redis, registry = _make_app(online=False)
    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        send_resp = await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "hi"}
        )
        assert send_resp.status == 409
        body = await send_resp.json()
        assert "该助手当前不可用" in body["error"]


@pytest.mark.asyncio
async def test_index_serves_the_chat_page():
    app, redis, registry = _make_app()
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "by-framework Chat" in body


@pytest.mark.asyncio
async def test_send_message_reports_waiting_user_status():
    batch = _xread_batch(_ask_user_form("What is your name?"))
    app, redis, registry = _make_app(xread_batches=[batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        send_resp = await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "hi"}
        )
        body = await send_resp.json()
        assert body["status"] == "waiting_user"
        assert body["content"] == "What is your name?"


@pytest.mark.asyncio
async def test_reply_after_ask_user_is_sent_as_resume_not_ask_agent():
    import json as jsonlib

    from by_framework.core.protocol.action_type import ActionType

    ask_batch = _xread_batch(_ask_user_form("What is your name?"))
    reply_batch = _xread_batch(
        _final_answer("Nice to meet you"), _stream_end(), start_id=5
    )
    app, redis, registry = _make_app(xread_batches=[ask_batch, reply_batch])

    async with TestClient(TestServer(app)) as tc:
        create_resp = await tc.post(
            "/api/conversations", json={"agent_type": "planner"}
        )
        session_id = (await create_resp.json())["session_id"]

        await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "hi"}
        )
        await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "Alice"}
        )

    first_call = redis.xadd.call_args_list[-2]
    first_payload = jsonlib.loads(first_call.args[1]["data"])
    second_call = redis.xadd.call_args_list[-1]
    payload = jsonlib.loads(second_call.args[1]["data"])
    assert payload["action_type"] == ActionType.RESUME.value
    # Regression: RESUME must reuse the ASK_AGENT dispatch's message_id so
    # GatewayClient.send_message can look the suspended execution back up
    # (get_execution_by_message_id) — a fresh id silently orphans it.
    assert payload["header"]["message_id"] == first_payload["header"]["message_id"]


@pytest.mark.asyncio
async def test_create_conversation_persists_to_postgres():
    redis, registry = make_fake_redis_and_registry()
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )

    async with TestClient(TestServer(app)) as tc:
        resp = await tc.post("/api/conversations", json={"agent_type": "planner"})
        session_id = (await resp.json())["session_id"]

    insert_call = conn.execute.await_args_list[-1]
    assert "INSERT INTO chat_ui_conversations" in insert_call.args[0]
    assert insert_call.args[1:] == (session_id, "planner")


@pytest.mark.asyncio
async def test_send_message_persists_user_and_assistant_messages():
    redis, registry = make_fake_redis_and_registry(
        xread_batches=[_xread_batch(_final_answer("hi there"), _stream_end())]
    )
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
        await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "hi"}
        )

    insert_calls = [
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO chat_ui_messages" in call.args[0]
    ]
    assert len(insert_calls) == 2
    assert insert_calls[0].args[1:] == (session_id, "user", "hi", False, "[]")
    assert insert_calls[1].args[1:] == (
        session_id,
        "assistant",
        "hi there",
        False,
        "[]",
    )


@pytest.mark.asyncio
async def test_send_message_persists_tool_calls_on_the_assistant_message():
    import json

    redis, registry = make_fake_redis_and_registry(
        xread_batches=[
            _xread_batch(
                _tool_call("call_1", "calculate", '{"expression": "1+1"}'),
                _tool_response("call_1", "2", tool_name="calculate"),
                _final_answer("the answer is 2"),
                _stream_end(),
            )
        ]
    )
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
        send_resp = await tc.post(
            f"/api/conversations/{session_id}/messages",
            json={"content": "what is 1+1?"},
        )
        assert send_resp.status == 200

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
async def test_get_conversation_returns_persisted_history():
    redis, registry = make_fake_redis_and_registry()
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

        conn.fetch.return_value = [
            {
                "role": "user",
                "content": "hi",
                "is_ask_user": False,
                "created_at": "2026-08-03T10:24:00+00:00",
                "tool_calls": "[]",
            },
            {
                "role": "assistant",
                "content": "hello",
                "is_ask_user": False,
                "created_at": "2026-08-03T10:24:05+00:00",
                "tool_calls": "[]",
            },
        ]

        resp = await tc.get(f"/api/conversations/{session_id}")
        assert resp.status == 200
        body = await resp.json()

    assert body["session_id"] == session_id
    assert body["agent_type"] == "planner"
    assert body["messages"] == [
        {
            "role": "user",
            "content": "hi",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:00+00:00",
            "tool_calls": [],
        },
        {
            "role": "assistant",
            "content": "hello",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:05+00:00",
            "tool_calls": [],
        },
    ]


@pytest.mark.asyncio
async def test_get_conversation_rehydrates_from_postgres_after_restart():
    # Simulates a fresh process: the conversation only exists in Postgres,
    # never in this app instance's in-memory ConversationStore.
    redis, registry = make_fake_redis_and_registry()
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    conn.fetchrow.return_value = {
        "session_id": "old-session",
        "agent_type": "planner",
        "title": "Hello",
        "turn_state": "IDLE",
        "last_message_id": "",
    }
    conn.fetch.return_value = [
        {
            "role": "user",
            "content": "hi",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:00+00:00",
            "tool_calls": "[]",
        }
    ]
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )

    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/api/conversations/old-session")
        assert resp.status == 200
        body = await resp.json()

    assert body == {
        "session_id": "old-session",
        "agent_type": "planner",
        "messages": [
            {
                "role": "user",
                "content": "hi",
                "is_ask_user": False,
                "created_at": "2026-08-03T10:24:00+00:00",
                "tool_calls": [],
            }
        ],
    }


@pytest.mark.asyncio
async def test_get_unknown_conversation_is_404():
    redis, registry = make_fake_redis_and_registry()
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )

    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/api/conversations/does-not-exist")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_list_conversations_returns_all_conversations_most_recent_first():
    redis, registry = make_fake_redis_and_registry()
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    conn.fetch.return_value = [
        {
            "session_id": "s2",
            "agent_type": "coder",
            "title": "Fix the bug",
            "last_active_at": "2026-08-02T12:00:00Z",
        },
        {
            "session_id": "s1",
            "agent_type": "planner",
            "title": "Plan the trip",
            "last_active_at": "2026-08-01T09:00:00Z",
        },
    ]
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )

    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/api/conversations")
        assert resp.status == 200
        body = await resp.json()

    assert body == {
        "conversations": [
            {
                "session_id": "s2",
                "agent_type": "coder",
                "title": "Fix the bug",
                "last_active_at": "2026-08-02T12:00:00Z",
            },
            {
                "session_id": "s1",
                "agent_type": "planner",
                "title": "Plan the trip",
                "last_active_at": "2026-08-01T09:00:00Z",
            },
        ]
    }


@pytest.mark.asyncio
async def test_second_send_while_first_in_flight_is_rejected():
    import asyncio

    redis, registry = make_fake_redis_and_registry()
    release_first = asyncio.Event()
    call_count = 0

    async def _blocking_then_terminal_xread(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await release_first.wait()
        return _xread_batch(_final_answer("done"), _stream_end())

    redis.xread.side_effect = _blocking_then_terminal_xread
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

        first_task = asyncio.ensure_future(
            tc.post(f"/api/conversations/{session_id}/messages", json={"content": "hi"})
        )
        await asyncio.sleep(0.01)  # let the first request acquire the lock

        second_resp = await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "again"}
        )
        assert second_resp.status == 423
        second_body = await second_resp.json()
        assert second_body["error"] == "请等待当前回复完成"

        release_first.set()
        first_resp = await first_task
        assert first_resp.status == 200

        # Now that the first turn resolved, the lock must be free again.
        third_resp = await tc.post(
            f"/api/conversations/{session_id}/messages", json={"content": "once more"}
        )
        assert third_resp.status == 200


@pytest.mark.asyncio
async def test_rehydrated_waiting_user_conversation_replies_with_resume():
    # Regression test: a Conversation that was WAITING_USER when the process
    # last restarted must stay WAITING_USER on rehydration — dispatching its
    # next reply with ASK_AGENT instead of RESUME would orphan the suspended
    # execution (by-framework-python/CLAUDE.md's "Core mental model" #2).
    import json as jsonlib

    from by_framework.core.protocol.action_type import ActionType

    reply_batch = _xread_batch(_final_answer("hi Alice"), _stream_end())
    redis, registry = make_fake_redis_and_registry(xread_batches=[reply_batch])
    client = make_gateway_client(redis, registry)
    history_store, conn = make_fake_history_store()
    conn.fetchrow.return_value = {
        "session_id": "old-session",
        "agent_type": "planner",
        "title": "What is your name?",
        "turn_state": "WAITING_USER",
        "last_message_id": "msg-original123",
    }
    app = create_app(
        gateway_client=client, registry=registry, history_store=history_store
    )

    async with TestClient(TestServer(app)) as tc:
        resp = await tc.post(
            "/api/conversations/old-session/messages", json={"content": "Alice"}
        )
        assert resp.status == 200

    last_call = redis.xadd.call_args_list[-1]
    payload = jsonlib.loads(last_call.args[1]["data"])
    assert payload["action_type"] == ActionType.RESUME.value
    # Regression: the RESUME must reuse the message_id persisted before the
    # restart, not a freshly-generated one — GatewayClient.send_message uses
    # this id to look the suspended execution back up
    # (get_execution_by_message_id); a fresh id would silently orphan it.
    assert payload["header"]["message_id"] == "msg-original123"


@pytest.mark.asyncio
async def test_serves_built_frontend_assets_by_extension():
    app, redis, registry = _make_app()
    async with TestClient(TestServer(app)) as tc:
        js_resp = await tc.get("/app.js")
        assert js_resp.status == 200
        assert js_resp.content_type == "application/javascript"

        css_resp = await tc.get("/styles.css")
        assert css_resp.status == 200
        assert css_resp.content_type == "text/css"


@pytest.mark.asyncio
async def test_unknown_static_asset_is_404():
    app, redis, registry = _make_app()
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/does-not-exist.js")
        assert resp.status == 404
