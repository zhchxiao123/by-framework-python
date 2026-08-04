# pylint: disable=C0114,C0116
import json
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
async def test_ensure_schema_migrates_columns_added_after_the_initial_schema():
    # Regression: CREATE TABLE IF NOT EXISTS is a no-op against a table that
    # already exists from an older deployment — a column added after the
    # initial schema (tool_calls, last_message_id) never gets created on an
    # upgrade, and the first INSERT/SELECT referencing it fails with
    # asyncpg.exceptions.UndefinedColumnError. _ensure_schema must also run
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS for every column introduced
    # after the initial CREATE TABLE, so upgrades don't break.
    store, conn = _make_store()

    await store.create_conversation("s1", "planner")

    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert any(
        "ALTER TABLE chat_ui_conversations" in sql
        and "last_message_id" in sql
        and "ADD COLUMN IF NOT EXISTS" in sql
        for sql in statements
    )
    assert any(
        "ALTER TABLE chat_ui_messages" in sql
        and "tool_calls" in sql
        and "ADD COLUMN IF NOT EXISTS" in sql
        for sql in statements
    )


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
    assert insert_call.args[1:] == ("s1", "user", "hi there", False, "[]")
    assert "UPDATE chat_ui_conversations" in touch_call.args[0]
    assert touch_call.args[1] == "s1"


@pytest.mark.asyncio
async def test_save_message_marks_ask_user_messages():
    store, conn = _make_store()

    await store.save_message("s1", "assistant", "What is your name?", is_ask_user=True)

    insert_call = conn.execute.await_args_list[-2]
    assert insert_call.args[1:] == ("s1", "assistant", "What is your name?", True, "[]")


@pytest.mark.asyncio
async def test_save_message_persists_tool_calls_as_json():
    store, conn = _make_store()
    tool_calls = [
        {"call_id": "call_1", "name": "calculate", "arguments": "{}", "result": "2"}
    ]

    await store.save_message(
        "s1", "assistant", "the answer is 2", tool_calls=tool_calls
    )

    insert_call = conn.execute.await_args_list[-2]
    assert insert_call.args[1:] == (
        "s1",
        "assistant",
        "the answer is 2",
        False,
        json.dumps(tool_calls),
    )


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

    result = await store.get_messages("s1")

    assert result == [
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
async def test_get_messages_parses_tool_calls_json_string():
    store, conn = _make_store()
    tool_calls = [
        {"call_id": "call_1", "name": "calculate", "arguments": "{}", "result": "2"}
    ]
    conn.fetch.return_value = [
        {
            "role": "assistant",
            "content": "the answer is 2",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:00+00:00",
            "tool_calls": json.dumps(tool_calls),
        },
    ]

    result = await store.get_messages("s1")

    assert result[0]["tool_calls"] == tool_calls


@pytest.mark.asyncio
async def test_get_messages_accepts_already_decoded_tool_calls():
    # A fake connection double (like this test's own) may hand back a native
    # Python list rather than the JSON-string shape a real asyncpg
    # connection returns for a JSONB column without a registered codec.
    store, conn = _make_store()
    tool_calls = [{"call_id": "call_1", "name": "calculate", "arguments": "{}"}]
    conn.fetch.return_value = [
        {
            "role": "assistant",
            "content": "the answer is 2",
            "is_ask_user": False,
            "created_at": "2026-08-03T10:24:00+00:00",
            "tool_calls": tool_calls,
        },
    ]

    result = await store.get_messages("s1")

    assert result[0]["tool_calls"] == tool_calls


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
