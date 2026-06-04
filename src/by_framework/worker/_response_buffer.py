"""
Response buffer module for AgentContext.

Holds the streaming response buffer, the per-stream lifecycle flags
(is_stream_finished / is_suspended / permission_transferred) and the
history-saved de-duplication flag, and exposes high-level helpers
(append / mark_* / is_* / flush_to_history) so that AgentContext can
delegate state management to a single small object.

The history backend is injected at construction time; dynamic fields
(trace_id / agent_id / parent_message_id) are passed either eagerly
or via a callable for parent_message_id which can change during the
lifetime of a single AgentContext (see AgentContext.message_id /
parent_message_id setters which update a ContextVar).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, List, Optional

if TYPE_CHECKING:
    from by_framework.core.runtime.history.history_manager import HistoryManager


class ResponseBuffer:
    """Streaming response buffer + per-stream lifecycle flags.

    Encapsulates the following state previously held directly on
    AgentContext:

    - ``_response_buffer``     : list of text chunks accumulated
      during streaming.
    - ``_is_history_saved``    : set after a full assistant reply
      has been persisted to the history backend (idempotent guard).
    - ``_is_stream_finished``  : set when an ``APP_STREAM_RESPONSE``
      marker has been emitted (terminal event for a stream).
    - ``_permission_transferred`` : set when this stream hands the
      right to emit a terminal response to a sub-call
      (``call_agent(..., wait_for_reply=False)`` or
      ``dispatch_group(..., wait_for_reply=False)``).
    - ``_is_suspended``        : set when execution has been
      suspended waiting for an external event (ask_user, sub-call
      with wait_for_reply, or dispatch_group with wait_for_reply).

    Args:
        history: ``HistoryManager`` used by ``flush_to_history``.
        trace_id: Trace identifier stored as history metadata.
        agent_id: Agent identifier stored as history metadata.
        parent_message_id_provider: Callable returning the current
            ``parent_message_id`` at flush time. Required because
            ``parent_message_id`` can change during a context's
            lifetime (the value lives in a ``ContextVar``).
    """

    def __init__(
        self,
        history: "HistoryManager",
        trace_id: str = "",
        agent_id: str = "",
        parent_message_id_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self._chunks: List[str] = []
        self._is_history_saved: bool = False
        self._is_stream_finished: bool = False
        self._permission_transferred: bool = False
        self._is_suspended: bool = False

        self._history = history
        self._trace_id = trace_id
        self._agent_id = agent_id
        self._parent_message_id_provider = parent_message_id_provider or (
            lambda: ""
        )

    # ------------------------------------------------------------------
    # Buffer operations
    # ------------------------------------------------------------------
    def append(self, text: str) -> None:
        """Append a chunk of text to the response buffer.

        Empty strings are ignored to keep the joined full text clean.
        """
        if text:
            self._chunks.append(text)

    def has_content(self) -> bool:
        """Return True if at least one chunk has been appended."""
        return bool(self._chunks)

    def full_text(self) -> str:
        """Return the concatenation of all appended chunks."""
        return "".join(self._chunks)

    def chunks(self) -> List[str]:
        """Return the underlying chunks list (read-only contract)."""
        return self._chunks

    # ------------------------------------------------------------------
    # Lifecycle flag mutators
    # ------------------------------------------------------------------
    def mark_finished(self) -> None:
        """Mark the stream as having emitted its terminal event."""
        self._is_stream_finished = True

    def mark_suspended(self) -> None:
        """Mark execution as suspended waiting for an external event."""
        self._is_suspended = True

    def mark_permission_transferred(self) -> None:
        """Mark that the permission to emit a terminal response was
        transferred to a sub-call (e.g. fire-and-forget call_agent)."""
        self._permission_transferred = True

    # ------------------------------------------------------------------
    # Lifecycle flag readers
    # ------------------------------------------------------------------
    def is_finished(self) -> bool:
        return self._is_stream_finished

    def is_suspended(self) -> bool:
        return self._is_suspended

    def is_permission_transferred(self) -> bool:
        return self._permission_transferred

    def is_history_saved(self) -> bool:
        return self._is_history_saved

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------
    async def flush_to_history(self) -> None:
        """Persist the current buffer as an assistant message.

        Idempotent: if the buffer is empty or the message was already
        saved, this is a no-op. ``trace_id`` / ``agent_id`` /
        ``parent_message_id`` are resolved at flush time so they
        always reflect the current AgentContext state.
        """
        if self._is_history_saved or not self._chunks:
            return

        full_content = "".join(self._chunks)
        await self._history.save_message(
            role="assistant",
            content=full_content,
            metadata={
                "trace_id": self._trace_id,
                "agent_id": self._agent_id,
                "parent_message_id": self._parent_message_id_provider(),
            },
        )
        self._is_history_saved = True
