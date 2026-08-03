# pylint: disable=C0114,C0116
from by_framework.core.protocol.action_type import ActionType

from by_framework_chat_ui.conversations import IDLE, WAITING_USER, ConversationStore


def test_new_conversation_starts_idle_and_uses_ask_agent():
    conversation = ConversationStore().create("planner")

    assert conversation.turn_state == IDLE
    assert conversation.next_action_type() == ActionType.ASK_AGENT.value


def test_completed_turn_keeps_conversation_idle():
    conversation = ConversationStore().create("planner")

    conversation.apply_turn_result("completed")

    assert conversation.turn_state == IDLE
    assert conversation.next_action_type() == ActionType.ASK_AGENT.value


def test_waiting_user_turn_switches_to_resume_for_the_next_send():
    conversation = ConversationStore().create("planner")

    conversation.apply_turn_result("waiting_user")

    assert conversation.turn_state == WAITING_USER
    assert conversation.next_action_type() == ActionType.RESUME.value


def test_completing_a_resumed_turn_returns_to_idle():
    conversation = ConversationStore().create("planner")
    conversation.apply_turn_result("waiting_user")

    conversation.apply_turn_result("completed")

    assert conversation.turn_state == IDLE
    assert conversation.next_action_type() == ActionType.ASK_AGENT.value


def test_new_conversation_starts_unlocked():
    conversation = ConversationStore().create("planner")

    assert conversation.locked is False


def test_try_lock_succeeds_when_unlocked_and_marks_it_locked():
    conversation = ConversationStore().create("planner")

    assert conversation.try_lock() is True
    assert conversation.locked is True


def test_try_lock_fails_when_already_locked():
    conversation = ConversationStore().create("planner")
    conversation.try_lock()

    assert conversation.try_lock() is False


def test_unlock_allows_locking_again():
    conversation = ConversationStore().create("planner")
    conversation.try_lock()

    conversation.unlock()

    assert conversation.locked is False
    assert conversation.try_lock() is True


def test_rehydrate_defaults_to_idle():
    conversation = ConversationStore().rehydrate("s1", "planner")

    assert conversation.turn_state == IDLE
    assert conversation.next_action_type() == ActionType.ASK_AGENT.value


def test_rehydrate_preserves_a_persisted_waiting_user_state():
    conversation = ConversationStore().rehydrate(
        "s1", "planner", turn_state=WAITING_USER
    )

    assert conversation.turn_state == WAITING_USER
    assert conversation.next_action_type() == ActionType.RESUME.value
