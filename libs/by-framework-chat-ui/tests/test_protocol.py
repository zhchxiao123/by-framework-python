# pylint: disable=C0114,C0116
from by_framework.core.protocol.data_message import DataMessage

from by_framework_chat_ui.protocol import (
    AnswerChunk,
    AskUser,
    FinalAnswer,
    Other,
    StreamEnd,
    interpret_data_message,
)


def _chunk_message(event_type, content, content_type="1002"):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type=event_type,
        data={
            "contentType": content_type,
            "choices": [{"delta": {"content": content}}],
        },
    )


def test_answer_delta_is_answer_chunk():
    msg = _chunk_message("answerDelta", "hel")
    assert interpret_data_message(msg) == AnswerChunk(content="hel")


def test_final_answer_is_final_answer():
    msg = _chunk_message("finalAnswer", "hello world")
    assert interpret_data_message(msg) == FinalAnswer(content="hello world")


def test_app_stream_response_is_stream_end():
    msg = DataMessage(
        trace_id="t1", session_id="s1", event_type="appStreamResponse", data={}
    )
    assert interpret_data_message(msg) == StreamEnd()


def test_reasoning_log_delta_with_task_user_input_is_ask_user():
    form = (
        '{"formStatus": 0, "pluginMachineFields": '
        '[{"formType": "textarea", "fieldName": "user_input", '
        '"fieldCode": "user_input", "description": "What is your name?", '
        '"required": true}]}'
    )
    msg = _chunk_message("reasoningLogDelta", form, content_type="3013")
    assert interpret_data_message(msg) == AskUser(prompt="What is your name?")


def test_reasoning_log_delta_with_ask_user_question_is_ask_user():
    raw = '{"questions": ["Which region?"]}'
    msg = _chunk_message("reasoningLogDelta", raw, content_type="3014")
    assert interpret_data_message(msg) == AskUser(prompt=raw)


def test_reasoning_log_delta_without_ask_user_content_type_is_other():
    msg = _chunk_message("reasoningLogDelta", "thinking...", content_type="3003")
    assert interpret_data_message(msg) == Other(event_type="reasoningLogDelta")


def test_unknown_event_type_is_other():
    msg = _chunk_message("taskCreate", "")
    assert interpret_data_message(msg) == Other(event_type="taskCreate")


def test_missing_choices_yields_empty_content():
    msg = DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={"contentType": "1002"},
    )
    assert interpret_data_message(msg) == AnswerChunk(content="")
