# pylint: disable=C0114,C0116
from unittest.mock import AsyncMock

import pytest

from by_framework_chat_ui.hub import WebSocketHub


@pytest.mark.asyncio
async def test_broadcast_reaches_every_subscriber_of_that_session():
    hub = WebSocketHub()
    ws1, ws2 = AsyncMock(), AsyncMock()
    hub.subscribe("s1", ws1)
    hub.subscribe("s1", ws2)

    await hub.broadcast("s1", {"type": "locked"})

    ws1.send_json.assert_awaited_once_with({"type": "locked"})
    ws2.send_json.assert_awaited_once_with({"type": "locked"})


@pytest.mark.asyncio
async def test_broadcast_does_not_reach_subscribers_of_a_different_session():
    hub = WebSocketHub()
    ws_other = AsyncMock()
    hub.subscribe("other-session", ws_other)

    await hub.broadcast("s1", {"type": "locked"})

    ws_other.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsubscribe_stops_further_broadcasts():
    hub = WebSocketHub()
    ws = AsyncMock()
    hub.subscribe("s1", ws)
    hub.unsubscribe("s1", ws)

    await hub.broadcast("s1", {"type": "locked"})

    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_tolerates_a_dead_connection():
    hub = WebSocketHub()
    dead = AsyncMock()
    dead.send_json.side_effect = ConnectionResetError()
    alive = AsyncMock()
    hub.subscribe("s1", dead)
    hub.subscribe("s1", alive)

    await hub.broadcast("s1", {"type": "locked"})

    alive.send_json.assert_awaited_once_with({"type": "locked"})
