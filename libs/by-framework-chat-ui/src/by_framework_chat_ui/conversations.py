"""In-process Conversation state.

A Conversation is one `session_id` bound to exactly one `agent_type` for its
entire lifetime (see CONTEXT.md's "Chat UI" section) — there is no operation
here to change that binding. State lives in this process's memory only;
slice 4 adds Postgres-backed persistence on top without changing this shape.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from by_framework.core.protocol.action_type import ActionType

IDLE = "IDLE"
WAITING_USER = "WAITING_USER"
MESSAGE_ID_PREFIX = "msg-"


@dataclass
class Conversation:
    """One chat thread, bound to one agent_type for its whole lifetime."""

    session_id: str
    agent_type: str
    turn_state: str = IDLE
    locked: bool = False
    last_message_id: str = ""

    def try_lock(self) -> bool:
        """Acquire the per-Conversation send lock.

        Returns False if a turn is already being processed. Safe without an
        `asyncio.Lock`: this is a plain flag check-and-set with no `await`
        in between, so it's race-free on the single-instance event loop
        (see decision #11/#12 in docs/chat-ui-design-log.md).
        """
        if self.locked:
            return False
        self.locked = True
        return True

    def unlock(self) -> None:
        """Release the per-Conversation send lock."""
        self.locked = False

    def next_action_type(self) -> str:
        """The `action_type` the next user message must be sent with.

        `RESUME` while `WAITING_USER` re-attaches to the suspended
        execution; sending `ASK_AGENT` instead would mint a new execution
        and orphan it.
        """
        if self.turn_state == WAITING_USER:
            return ActionType.RESUME.value
        return ActionType.ASK_AGENT.value

    def generate_message_id(self) -> str:
        """Mint a new `message_id` for an `ASK_AGENT` dispatch.

        `GatewayClient.send_message` reuses a RESUME's `message_id` to look
        the suspended execution back up (`get_execution_by_message_id`), so
        the caller must generate this once per turn and reuse the same
        value across a suspend/resume pair — never let the client
        auto-generate it, or a RESUME's fresh random id will fail that
        lookup and silently orphan the original execution.
        """
        return f"{MESSAGE_ID_PREFIX}{uuid.uuid4().hex[:8]}"

    def apply_turn_result(self, status: str) -> None:
        """Update turn state from a resolved turn's status.

        `status` is `TurnResult.status`: "completed" or "waiting_user".
        """
        self.turn_state = WAITING_USER if status == "waiting_user" else IDLE


class ConversationStore:
    """Tracks Conversations for the lifetime of this (single-instance) process."""

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}

    def create(self, agent_type: str) -> Conversation:
        conversation = Conversation(session_id=uuid.uuid4().hex, agent_type=agent_type)
        self._conversations[conversation.session_id] = conversation
        return conversation

    def get(self, session_id: str) -> Conversation | None:
        return self._conversations.get(session_id)

    def rehydrate(
        self,
        session_id: str,
        agent_type: str,
        turn_state: str = IDLE,
        last_message_id: str = "",
    ) -> Conversation:
        """Reconstruct an in-memory Conversation for a known `session_id`.

        Used when a Conversation exists in the Chat History Store (Postgres)
        but this process has no in-memory record of it yet — e.g. after a
        restart, or a request that arrives on a fresh process. `turn_state`
        must come from the persisted record, not default to `IDLE`: a
        Conversation that was `WAITING_USER` when the process restarted is
        still `WAITING_USER` — defaulting it to `IDLE` would send its next
        reply with `ASK_AGENT` instead of `RESUME` and orphan the suspended
        execution. `last_message_id` must likewise come from the persisted
        record: a restart-then-RESUME with a fresh, ungenerated
        `last_message_id` would send the resume under a new `message_id`
        that `get_execution_by_message_id` can't find, orphaning the
        suspended execution the exact same way.
        """
        conversation = Conversation(
            session_id=session_id,
            agent_type=agent_type,
            turn_state=turn_state,
            last_message_id=last_message_id,
        )
        self._conversations[session_id] = conversation
        return conversation
