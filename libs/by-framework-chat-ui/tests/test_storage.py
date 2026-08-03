# pylint: disable=C0114,C0116
from unittest.mock import AsyncMock, MagicMock

import pytest

from by_framework_chat_ui.storage import ChatHistoryStore


class _AcquireCtx:

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_store():
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value = _AcquireCtx(conn)
    store = ChatHistoryStore(connection_pool=pool)
    return store, conn


@pytest.mark.asyncio
async def test_create_conversation_inserts_with_empty_title():
    store, conn = _make_store()

    await store.create_conversation("s1", "planner")

    insert_call = conn.execute.await_args_list[-1]
    assert "INSERT INTO chat_ui_conversations" in insert_call.args[0]
    assert insert_call.args[1:] == ("s1", "planner")


@pytest.mark.asyncio
async def test_set_initial_title_only_fills_a_blank_title():
    store, conn = _make_store()

    await store.set_initial_title("s1", "Hello there, how")

    update_call = conn.execute.await_args_list[-1]
    assert "UPDATE chat_ui_conversations" in update_call.args[0]
    assert "title = ''" in update_call.args[0] or "$2" in update_call.args[0]
    assert update_call.args[1:] == ("s1", "Hello there, how")


@pytest.mark.asyncio
async def test_save_message_inserts_and_touches_conversation():
    store, conn = _make_store()

    await store.save_message("s1", "user", "hi there")

    insert_call = conn.execute.await_args_list[-2]
    touch_call = conn.execute.await_args_list[-1]
    assert "INSERT INTO chat_ui_messages" in insert_call.args[0]
    assert insert_call.args[1:] == ("s1", "user", "hi there", False)
    assert "UPDATE chat_ui_conversations" in touch_call.args[0]
    assert touch_call.args[1] == "s1"


@pytest.mark.asyncio
async def test_save_message_marks_ask_user_messages():
    store, conn = _make_store()

    await store.save_message("s1", "assistant", "What is your name?", is_ask_user=True)

    insert_call = conn.execute.await_args_list[-2]
    assert insert_call.args[1:] == ("s1", "assistant", "What is your name?", True)


@pytest.mark.asyncio
async def test_get_conversation_returns_none_when_missing():
    store, conn = _make_store()
    conn.fetchrow.return_value = None

    result = await store.get_conversation("missing")

    assert result is None


@pytest.mark.asyncio
async def test_get_conversation_returns_agent_type_and_title():
    store, conn = _make_store()
    conn.fetchrow.return_value = {
        "session_id": "s1",
        "agent_type": "planner",
        "title": "Hello",
        "turn_state": "IDLE",
        "last_message_id": "",
    }

    result = await store.get_conversation("s1")

    assert result == {
        "session_id": "s1",
        "agent_type": "planner",
        "title": "Hello",
        "turn_state": "IDLE",
        "last_message_id": "",
    }


@pytest.mark.asyncio
async def test_get_messages_returns_rows_in_order():
    store, conn = _make_store()
    conn.fetch.return_value = [
        {
            "role": "user",
            "content": "hi",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:00+00:00",
        },
        {
            "role": "assistant",
            "content": "hello",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:05+00:00",
        },
    ]

    result = await store.get_messages("s1")

    assert result == [
        {
            "role": "user",
            "content": "hi",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:00+00:00",
        },
        {
            "role": "assistant",
            "content": "hello",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:05+00:00",
        },
    ]


@pytest.mark.asyncio
async def test_list_conversations_returns_summaries():
    store, conn = _make_store()
    conn.fetch.return_value = [
        {
            "session_id": "s1",
            "agent_type": "planner",
            "title": "Hello",
            "last_active_at": None,
        }
    ]

    result = await store.list_conversations()

    assert result == [
        {
            "session_id": "s1",
            "agent_type": "planner",
            "title": "Hello",
            "last_active_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_set_turn_state_updates_conversation():
    store, conn = _make_store()

    await store.set_turn_state("s1", "WAITING_USER")

    update_call = conn.execute.await_args_list[-1]
    assert "UPDATE chat_ui_conversations" in update_call.args[0]
    assert "turn_state" in update_call.args[0]
    assert update_call.args[1:] == ("s1", "WAITING_USER")


@pytest.mark.asyncio
async def test_get_conversation_includes_turn_state():
    store, conn = _make_store()
    conn.fetchrow.return_value = {
        "session_id": "s1",
        "agent_type": "planner",
        "title": "Hello",
        "turn_state": "WAITING_USER",
        "last_message_id": "",
    }

    result = await store.get_conversation("s1")

    assert result == {
        "session_id": "s1",
        "agent_type": "planner",
        "title": "Hello",
        "turn_state": "WAITING_USER",
        "last_message_id": "",
    }


@pytest.mark.asyncio
async def test_set_last_message_id_updates_conversation():
    store, conn = _make_store()

    await store.set_last_message_id("s1", "msg-abc123")

    update_call = conn.execute.await_args_list[-1]
    assert "UPDATE chat_ui_conversations" in update_call.args[0]
    assert "last_message_id" in update_call.args[0]
    assert update_call.args[1:] == ("s1", "msg-abc123")


@pytest.mark.asyncio
async def test_get_conversation_includes_last_message_id():
    store, conn = _make_store()
    conn.fetchrow.return_value = {
        "session_id": "s1",
        "agent_type": "planner",
        "title": "Hello",
        "turn_state": "WAITING_USER",
        "last_message_id": "msg-abc123",
    }

    result = await store.get_conversation("s1")

    assert result["last_message_id"] == "msg-abc123"
