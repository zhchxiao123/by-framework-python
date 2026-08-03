# pylint: disable=C0114,C0116
from by_framework.core.protocol.data_message import DataMessage

from by_framework_chat_ui.protocol import (
    AnswerChunk,
    AskUser,
    FinalAnswer,
    Other,
    StreamEnd,
    ToolCall,
    ToolResult,
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


def _tool_call_item(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _tool_call_message(event_type, call_id, name, arguments):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type=event_type,
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "content": None,
                        "tool_calls": [_tool_call_item(call_id, name, arguments)],
                    }
                }
            ],
        },
    )


def _tool_response_item(call_id, content):
    return {"tool_call_id": call_id, "content": content}


def _tool_response_message(event_type, call_id, content, metadata=None):
    return DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type=event_type,
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "role": "tool",
                        "content": None,
                        "tool_responses": [_tool_response_item(call_id, content)],
                    }
                }
            ],
        },
        metadata=metadata or {},
    )


def test_answer_delta_is_answer_chunk():
    msg = _chunk_message("answerDelta", "hel")
    assert interpret_data_message(msg) == [AnswerChunk(content="hel")]


def test_final_answer_is_final_answer():
    msg = _chunk_message("finalAnswer", "hello world")
    assert interpret_data_message(msg) == [FinalAnswer(content="hello world")]


def test_app_stream_response_is_stream_end():
    msg = DataMessage(
        trace_id="t1", session_id="s1", event_type="appStreamResponse", data={}
    )
    assert interpret_data_message(msg) == [StreamEnd()]


def test_reasoning_log_delta_with_task_user_input_is_ask_user():
    form = (
        '{"formStatus": 0, "pluginMachineFields": '
        '[{"formType": "textarea", "fieldName": "user_input", '
        '"fieldCode": "user_input", "description": "What is your name?", '
        '"required": true}]}'
    )
    msg = _chunk_message("reasoningLogDelta", form, content_type="3013")
    assert interpret_data_message(msg) == [AskUser(prompt="What is your name?")]


def test_reasoning_log_delta_with_ask_user_question_is_ask_user():
    raw = '{"questions": ["Which region?"]}'
    msg = _chunk_message("reasoningLogDelta", raw, content_type="3014")
    assert interpret_data_message(msg) == [AskUser(prompt=raw)]


def test_reasoning_log_delta_without_ask_user_content_type_is_other():
    msg = _chunk_message("reasoningLogDelta", "thinking...", content_type="3003")
    assert interpret_data_message(msg) == [Other(event_type="reasoningLogDelta")]


def test_unknown_event_type_is_other():
    msg = _chunk_message("taskCreate", "")
    assert interpret_data_message(msg) == [Other(event_type="taskCreate")]


def test_missing_choices_yields_empty_content():
    msg = DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={"contentType": "1002"},
    )
    assert interpret_data_message(msg) == [AnswerChunk(content="")]


def test_answer_delta_with_tool_calls_is_tool_call():
    msg = _tool_call_message(
        "answerDelta", "call_abc123", "calculate", '{"expression": "12 * (7 + 5)"}'
    )
    assert interpret_data_message(msg) == [
        ToolCall(
            call_id="call_abc123",
            name="calculate",
            arguments='{"expression": "12 * (7 + 5)"}',
        )
    ]


def test_reasoning_log_delta_with_tool_calls_is_tool_call():
    # Detection is by the presence of `delta.tool_calls`, not by event_type or
    # contentType — a sub-agent's tool call arrives as reasoningLogDelta with
    # the same generic "1002" contentType as any other chunk.
    msg = _tool_call_message("reasoningLogDelta", "call_xyz", "search", "{}")
    assert interpret_data_message(msg) == [
        ToolCall(call_id="call_xyz", name="search", arguments="{}")
    ]


def test_tool_call_chunk_is_not_misreported_as_an_empty_answer_chunk():
    # Regression: a tool-call-only chunk has `delta.content: null`; before the
    # fix this fell through to the AnswerChunk branch and silently rendered as
    # AnswerChunk(content=""), making the tool call invisible.
    msg = _tool_call_message("answerDelta", "call_1", "calculate", "{}")
    result = interpret_data_message(msg)
    assert not any(isinstance(event, AnswerChunk) for event in result)


def test_tool_responses_is_tool_result_with_tool_name_from_metadata():
    msg = _tool_response_message(
        "answerDelta", "call_abc123", "19", metadata={"tool_name": "calculate"}
    )
    assert interpret_data_message(msg) == [
        ToolResult(call_id="call_abc123", content="19", tool_name="calculate")
    ]


def test_tool_responses_without_metadata_defaults_to_empty_tool_name():
    msg = _tool_response_message("answerDelta", "call_abc123", "19")
    assert interpret_data_message(msg) == [
        ToolResult(call_id="call_abc123", content="19", tool_name="")
    ]


def test_multiple_tool_calls_in_one_delta_all_produce_events():
    # Regression: an earlier version only read tool_calls[0], silently
    # dropping every parallel tool call after the first.
    msg = DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "content": None,
                        "tool_calls": [
                            _tool_call_item("call_1", "calculate", "{}"),
                            _tool_call_item("call_2", "search", "{}"),
                        ],
                    }
                }
            ],
        },
    )

    assert interpret_data_message(msg) == [
        ToolCall(call_id="call_1", name="calculate", arguments="{}"),
        ToolCall(call_id="call_2", name="search", arguments="{}"),
    ]


def test_multiple_tool_responses_in_one_delta_all_produce_events():
    msg = DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "content": None,
                        "tool_responses": [
                            _tool_response_item("call_1", "2"),
                            _tool_response_item("call_2", "found 3 results"),
                        ],
                    }
                }
            ],
        },
        metadata={"tool_name": "calculate"},
    )

    assert interpret_data_message(msg) == [
        ToolResult(call_id="call_1", content="2", tool_name="calculate"),
        ToolResult(call_id="call_2", content="found 3 results", tool_name="calculate"),
    ]


def test_delta_with_both_tool_calls_and_tool_responses_returns_both():
    # Regression: an earlier version checked tool_calls before tool_responses
    # and returned on the first match, silently dropping a tool_result that
    # happened to arrive in the same delta as a tool_call.
    msg = DataMessage(
        trace_id="t1",
        session_id="s1",
        event_type="answerDelta",
        data={
            "contentType": "1002",
            "choices": [
                {
                    "delta": {
                        "content": None,
                        "tool_calls": [_tool_call_item("call_2", "search", "{}")],
                        "tool_responses": [_tool_response_item("call_1", "2")],
                    }
                }
            ],
        },
        metadata={"tool_name": "calculate"},
    )

    assert interpret_data_message(msg) == [
        ToolCall(call_id="call_2", name="search", arguments="{}"),
        ToolResult(call_id="call_1", content="2", tool_name="calculate"),
    ]
