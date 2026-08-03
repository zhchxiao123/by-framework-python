# pylint: disable=C0114,C0116
from unittest.mock import AsyncMock, MagicMock

from by_framework import GatewayClient

from by_framework_chat_ui.storage import ChatHistoryStore


class _AcquireCtx:

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def make_fake_redis_and_registry(
    *, online=True, xread_batches=(), agent_types_by_worker=None
):
    redis = AsyncMock()
    redis.xrevrange.return_value = []
    redis.xread.side_effect = list(xread_batches)

    registry = AsyncMock()
    registry.get_all_workers.return_value = agent_types_by_worker or {}
    registry.has_online_agent_type.return_value = (
        (True, ["worker-1"]) if online else (False, [])
    )
    return redis, registry


def make_gateway_client(redis, registry):
    return GatewayClient(redis_client=redis, registry=registry)


def make_fake_history_store():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetch.return_value = []
    pool = MagicMock()
    pool.acquire.return_value = _AcquireCtx(conn)
    store = ChatHistoryStore(connection_pool=pool)
    return store, conn
