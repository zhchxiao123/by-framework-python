"""Postgres-backed Chat History Store.

Owned entirely by chat-ui — a new `chat_ui_conversations`/`chat_ui_messages`
schema, distinct from the framework's internal `HistoryManager`/
`history_backend` (a private per-worker memory mechanism, never exposed to
`GatewayClient`; see CONTEXT.md's "Chat History Store" entry). Connection
pooling follows the same shape as
`by-framework-history-postgres`'s `PostgresHistoryBackend`, but unlike that
backend this one does not silently no-op when unconfigured: persistence is
this product's whole point, not an optional add-on, so a missing pool/DSN
raises instead of degrading quietly.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Optional


class ChatHistoryStoreNotConfiguredError(Exception):
    """No connection pool and no DSN were provided."""


class ChatHistoryStore:
    """PostgreSQL storage for Conversations and their messages."""

    _CREATE_CONVERSATIONS_SQL = """
    CREATE TABLE IF NOT EXISTS chat_ui_conversations (
        session_id VARCHAR(64) PRIMARY KEY,
        agent_type VARCHAR(128) NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        turn_state VARCHAR(16) NOT NULL DEFAULT 'IDLE',
        last_message_id VARCHAR(64) NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """

    _CREATE_MESSAGES_SQL = """
    CREATE TABLE IF NOT EXISTS chat_ui_messages (
        id BIGSERIAL PRIMARY KEY,
        session_id VARCHAR(64) NOT NULL,
        role VARCHAR(16) NOT NULL,
        content TEXT NOT NULL,
        is_ask_user BOOLEAN NOT NULL DEFAULT FALSE,
        tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """

    _CREATE_MESSAGES_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_chat_ui_messages_session_created
    ON chat_ui_messages (session_id, created_at, id);
    """

    _INSERT_CONVERSATION_SQL = """
    INSERT INTO chat_ui_conversations (session_id, agent_type)
    VALUES ($1, $2)
    ON CONFLICT (session_id) DO NOTHING;
    """

    _SET_INITIAL_TITLE_SQL = """
    UPDATE chat_ui_conversations SET title = $2
    WHERE session_id = $1 AND title = '';
    """

    _SET_TURN_STATE_SQL = """
    UPDATE chat_ui_conversations SET turn_state = $2
    WHERE session_id = $1;
    """

    _SET_LAST_MESSAGE_ID_SQL = """
    UPDATE chat_ui_conversations SET last_message_id = $2
    WHERE session_id = $1;
    """

    _INSERT_MESSAGE_SQL = """
    INSERT INTO chat_ui_messages (session_id, role, content, is_ask_user, tool_calls)
    VALUES ($1, $2, $3, $4, $5);
    """

    _TOUCH_CONVERSATION_SQL = """
    UPDATE chat_ui_conversations SET last_active_at = NOW()
    WHERE session_id = $1;
    """

    _SELECT_CONVERSATION_SQL = """
    SELECT session_id, agent_type, title, turn_state, last_message_id
    FROM chat_ui_conversations
    WHERE session_id = $1;
    """

    _SELECT_MESSAGES_SQL = """
    SELECT role, content, is_ask_user, tool_calls, created_at
    FROM chat_ui_messages
    WHERE session_id = $1
    ORDER BY created_at, id;
    """

    _LIST_CONVERSATIONS_SQL = """
    SELECT session_id, agent_type, title, last_active_at
    FROM chat_ui_conversations
    ORDER BY last_active_at DESC;
    """

    def __init__(
        self,
        connection_pool: Any = None,
        dsn: Optional[str] = None,
        min_size: int = 1,
        max_size: int = 10,
        command_timeout: float = 30.0,
        pool_factory: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        self.pool = connection_pool
        self._external_pool = connection_pool is not None
        self._dsn = dsn or os.environ.get("CHAT_UI_PG_DSN", "")
        self._pool_min_size = min_size
        self._pool_max_size = max_size
        self._pool_command_timeout = command_timeout
        self._pool_factory = pool_factory or self._default_pool_factory

        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def _default_pool_factory(
        self, dsn: str, min_size: int, max_size: int, command_timeout: float
    ) -> Any:
        try:
            import asyncpg  # pylint: disable=import-outside-toplevel
        except ImportError as err:
            raise RuntimeError(
                "ChatHistoryStore requires `asyncpg`. "
                "Please install it or pass a pre-built connection pool."
            ) from err
        return await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=command_timeout,
        )

    async def _ensure_pool(self) -> None:
        if self.pool is not None:
            return
        if not self._dsn:
            raise ChatHistoryStoreNotConfiguredError(
                "ChatHistoryStore needs a connection_pool or a DSN "
                "(CHAT_UI_PG_DSN) — chat history persistence is required, "
                "not optional, in this product."
            )
        async with self._pool_lock:
            if self.pool is not None:
                return
            self.pool = await self._pool_factory(
                self._dsn,
                self._pool_min_size,
                self._pool_max_size,
                self._pool_command_timeout,
            )

    async def _ensure_schema(self) -> None:
        await self._ensure_pool()
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with self.pool.acquire() as conn:
                await conn.execute(self._CREATE_CONVERSATIONS_SQL)
                await conn.execute(self._CREATE_MESSAGES_SQL)
                await conn.execute(self._CREATE_MESSAGES_INDEX_SQL)
            self._schema_ready = True

    async def create_conversation(self, session_id: str, agent_type: str) -> None:
        """Persist a newly-created Conversation with an empty title."""
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            await conn.execute(self._INSERT_CONVERSATION_SQL, session_id, agent_type)

    async def set_initial_title(self, session_id: str, title: str) -> None:
        """Fill in the Conversation's title if it hasn't been set yet.

        Idempotent by design (`WHERE title = ''`) so callers don't need to
        track "is this the first message" themselves.
        """
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            await conn.execute(self._SET_INITIAL_TITLE_SQL, session_id, title)

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        is_ask_user: bool = False,
        tool_calls: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Persist one message and bump the Conversation's last-active time.

        `tool_calls` is one entry per tool the assistant invoked during this
        turn (`{"call_id", "name", "arguments", "result"}`), collected by
        `gateway.dispatch_and_await`/`stream_turn`'s callers — a turn's tool
        calls are stored on its one assistant message row, not as separate
        rows, so history rehydration stays a single per-turn record.
        """
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            await conn.execute(
                self._INSERT_MESSAGE_SQL,
                session_id,
                role,
                content,
                is_ask_user,
                json.dumps(tool_calls or []),
            )
            await conn.execute(self._TOUCH_CONVERSATION_SQL, session_id)

    async def set_turn_state(self, session_id: str, turn_state: str) -> None:
        """Persist a Conversation's turn state (`IDLE`/`WAITING_USER`).

        Without this, a service restart mid-`WAITING_USER` would rehydrate
        the Conversation as `IDLE` and the next reply would be dispatched
        with `ASK_AGENT` instead of `RESUME` — orphaning the suspended
        execution (the bug class `by-framework-python/CLAUDE.md`'s "Core
        mental model" #2 calls out as this repo's most-recurring failure).
        """
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            await conn.execute(self._SET_TURN_STATE_SQL, session_id, turn_state)

    async def set_last_message_id(self, session_id: str, last_message_id: str) -> None:
        """Persist the `message_id` the current/most-recent turn dispatched with.

        Without this, a service restart mid-`WAITING_USER` would rehydrate
        the Conversation with an empty `last_message_id`, so the next reply
        would be sent as a RESUME under a freshly-generated `message_id`
        that `get_execution_by_message_id` can't find — orphaning the
        suspended execution the same way an `ASK_AGENT`-instead-of-`RESUME`
        mistake would (see `set_turn_state`'s docstring).
        """
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            await conn.execute(
                self._SET_LAST_MESSAGE_ID_SQL, session_id, last_message_id
            )

    async def get_conversation(self, session_id: str) -> Optional[dict[str, Any]]:
        """Return `{session_id, agent_type, title, turn_state,
        last_message_id}`, or None."""
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(self._SELECT_CONVERSATION_SQL, session_id)
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "agent_type": row["agent_type"],
            "title": row["title"],
            "turn_state": row["turn_state"],
            "last_message_id": row["last_message_id"],
        }

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return this Conversation's messages in chronological order."""
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(self._SELECT_MESSAGES_SQL, session_id)
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "is_ask_user": r["is_ask_user"],
                "tool_calls": _decode_tool_calls(r["tool_calls"]),
                "created_at": _isoformat(r["created_at"]),
            }
            for r in rows
        ]

    async def list_conversations(self) -> list[dict[str, Any]]:
        """Return every Conversation, most recently active first."""
        await self._ensure_schema()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_CONVERSATIONS_SQL)
        return [
            {
                "session_id": r["session_id"],
                "agent_type": r["agent_type"],
                "title": r["title"],
                "last_active_at": _isoformat(r["last_active_at"]),
            }
            for r in rows
        ]


def _isoformat(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if isoformat is not None else value


def _decode_tool_calls(value: Any) -> list[dict[str, Any]]:
    """Parse a JSONB `tool_calls` column value.

    A real asyncpg connection (no custom type codec registered, matching
    `by-framework-history-postgres`'s convention) returns a JSONB column as
    a raw JSON string; a fake connection double in tests may hand back an
    already-decoded Python list.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value or []
