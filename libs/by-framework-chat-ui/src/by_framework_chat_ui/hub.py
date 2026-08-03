"""Broadcast to every WebSocket connection subscribed to a Conversation.

Because history is fully global (no auth — see CONTEXT.md), the same
Conversation may genuinely be open in more than one tab, or by more than one
person, at once. When one of them locks the Conversation by sending a
message, every other subscriber needs to see that lock state live rather
than have their own send silently rejected with no explanation.
"""

from __future__ import annotations

from typing import Any, Protocol


class _SendsJson(Protocol):

    async def send_json(self, data: Any) -> None:
        ...


class WebSocketHub:
    """Tracks which WebSocket connections are subscribed to which session_id."""

    def __init__(self) -> None:
        self._connections: dict[str, set[_SendsJson]] = {}

    def subscribe(self, session_id: str, ws: _SendsJson) -> None:
        self._connections.setdefault(session_id, set()).add(ws)

    def unsubscribe(self, session_id: str, ws: _SendsJson) -> None:
        connections = self._connections.get(session_id)
        if connections is None:
            return
        connections.discard(ws)
        if not connections:
            del self._connections[session_id]

    async def broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        for ws in list(self._connections.get(session_id, ())):
            try:
                await ws.send_json(payload)
            except ConnectionResetError:
                # The client is gone; it'll be unsubscribed by its own
                # connection-close handler, not from here.
                continue
