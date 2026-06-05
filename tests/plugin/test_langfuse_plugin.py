import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from by_framework_trace_langfuse import (
    LangfuseConfig,
    LangfusePlugin,
    LangfuseTraceProviderFactory,
)

from by_framework import (
    AgentContext,
    AskAgentCommand,
    CancelTaskCommand,
    MessageHeader,
)


@dataclass
class FakeObservation:
    id: str
    updates: list[dict[str, Any]] = field(default_factory=list)
    ended_with: Optional[dict[str, Any]] = None

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def end(self, **kwargs: Any) -> None:
        self.ended_with = kwargs


class FakeTracer:
    """Simple tracer test double that records observation start payloads."""

    def __init__(self):
        self.start_calls: list[dict[str, Any]] = []
        self.shutdown_called = False

    def start_observation(self, request: Any) -> FakeObservation:
        self.start_calls.append(
            {
                "trace_id": request.trace_id,
                "name": request.name,
                "input": request.observation_input,
                "observation_input": request.observation_input,
                "metadata": request.metadata,
                "parent_observation_id": request.parent_observation_id,
                "span_id": request.span_id,
            }
        )
        return FakeObservation(id=f"obs-{len(self.start_calls)}")

    def shutdown(self) -> None:
        self.shutdown_called = True


class EndWithoutKwargsObservation:
    """Observation double whose end() does not accept keyword arguments."""

    def __init__(self):
        self.id = "obs-fallback"
        self.updates: list[dict[str, Any]] = []
        self.end_calls = 0

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.end_calls += 1


class FakeObservationStore:
    """In-memory observation mapping used by the plugin tests."""

    def __init__(self):
        self.mapping: dict[tuple[str, str], str] = {}

    async def get_observation_id(
        self, session_id: str, message_id: str
    ) -> Optional[str]:
        return self.mapping.get((session_id, message_id))

    async def set_observation_id(
        self, session_id: str, message_id: str, observation_id: str
    ) -> None:
        self.mapping[(session_id, message_id)] = observation_id


def _build_context(
    *,
    message_id: str = "msg-1",
    parent_message_id: str = "",
    trace_id: str = "12345678901234567890123456789012",
    session_id: str = "session-1",
    current_agent_id: str = "planner",
    content: Any = "hello",
) -> AgentContext:
    command = AskAgentCommand(
        header=MessageHeader(
            message_id=message_id,
            session_id=session_id,
            trace_id=trace_id,
            target_agent_type=current_agent_id,
            parent_message_id=parent_message_id,
            user_code="user-1",
            user_name="Alice",
        ),
        content=content,
    )
    return AgentContext(
        session_id=session_id,
        trace_id=trace_id,
        redis_client=object(),
        current_agent_id=current_agent_id,
        message_id=message_id,
        parent_message_id=parent_message_id,
        current_command=command,
        user_code="user-1",
        user_name="Alice",
    )


@pytest.mark.asyncio
async def test_langfuse_plugin_starts_observation_and_persists_mapping():
    tracer = FakeTracer()
    store = FakeObservationStore()
    store.mapping[("session-1", "msg-parent")] = "obs-parent"
    plugin = LangfusePlugin(tracer=tracer, observation_store=store)
    context = _build_context(message_id="msg-child", parent_message_id="msg-parent")

    await plugin.on_task_start(context)

    # Two native observations are created: worker.execute, then the agent task.
    assert len(tracer.start_calls) == 2
    worker_execute_call, agent_call = tracer.start_calls

    # worker.execute hangs under the parent task (store lookup for sub-agents).
    assert worker_execute_call["name"] == "worker.execute"
    assert worker_execute_call["parent_observation_id"] == "obs-parent"
    assert worker_execute_call["trace_id"] == "12345678901234567890123456789012"

    # The agent task nests under this execution's worker.execute observation.
    assert agent_call["name"] == "planner"
    assert agent_call["parent_observation_id"] == "obs-1"  # worker.execute obs id
    assert agent_call["input"] == "hello"
    assert agent_call["metadata"]["message_id"] == "msg-child"
    # The agent task observation id is what children look up as their parent.
    assert store.mapping[("session-1", "msg-child")] == "obs-2"


@pytest.mark.asyncio
async def test_langfuse_plugin_top_level_nests_under_worker_execute():
    """Top-level chain: client.dispatch -> worker.execute -> agent task.

    Regression: worker.execute is materialised as a native Langfuse observation
    so it reliably appears (raw by-framework OTel spans are dropped by Langfuse's
    default filter in the worker process); the agent task nests under it, and
    worker.execute itself parents to client.dispatch.
    """
    from by_framework.observability.span_recorder import str_to_uint64

    tracer = FakeTracer()
    store = FakeObservationStore()
    plugin = LangfusePlugin(tracer=tracer, observation_store=store)
    # No execution_id on the context -> anchor falls back to message_id.
    context = _build_context(message_id="msg-1", parent_message_id="")

    await plugin.on_task_start(context)

    assert len(tracer.start_calls) == 2
    worker_execute_call, agent_call = tracer.start_calls

    client_dispatch_id = f"{str_to_uint64('msg-1:client.dispatch'):016x}"
    worker_execute_span_id = str_to_uint64("msg-1:worker.execute")
    agent_task_span_id = str_to_uint64("msg-1:agent.task")

    # worker.execute is its own node, parented to client.dispatch.
    assert worker_execute_call["name"] == "worker.execute"
    assert worker_execute_call["span_id"] == worker_execute_span_id
    assert worker_execute_call["parent_observation_id"] == client_dispatch_id

    # Agent task nests under worker.execute, with a distinct span id.
    assert agent_call["span_id"] == agent_task_span_id
    assert agent_call["span_id"] != worker_execute_span_id
    assert agent_call["parent_observation_id"] == "obs-1"  # worker.execute obs id


@pytest.mark.asyncio
async def test_langfuse_plugin_ends_observation_with_result_output():
    tracer = FakeTracer()
    store = FakeObservationStore()
    plugin = LangfusePlugin(tracer=tracer, observation_store=store)
    context = _build_context()

    await plugin.on_task_start(context)
    await plugin.on_task_complete(context, {"status": "COMPLETED", "answer": "done"})

    observation = context._langfuse_observation  # pylint: disable=protected-access
    assert observation.ended_with == {
        "output": {"status": "COMPLETED", "answer": "done"}
    }


@pytest.mark.asyncio
async def test_langfuse_plugin_marks_errors_on_task_failure():
    tracer = FakeTracer()
    store = FakeObservationStore()
    plugin = LangfusePlugin(tracer=tracer, observation_store=store)
    context = _build_context()

    await plugin.on_task_start(context)
    await plugin.on_task_error(context, RuntimeError("boom"))

    observation = context._langfuse_observation  # pylint: disable=protected-access
    assert observation.updates[-1]["level"] == "ERROR"
    assert observation.updates[-1]["status_message"] == "boom"
    assert observation.ended_with == {"output": {"error": "boom"}}


@pytest.mark.asyncio
async def test_langfuse_plugin_marks_cancellation_and_ends_observation():
    tracer = FakeTracer()
    store = FakeObservationStore()
    plugin = LangfusePlugin(tracer=tracer, observation_store=store)
    context = _build_context()
    cancel_command = CancelTaskCommand(
        header=MessageHeader(
            message_id="cancel-1",
            session_id="session-1",
            trace_id="trace-1",
        ),
        target_message_id="msg-1",
        reason="user cancelled",
    )

    await plugin.on_task_start(context)
    await plugin.on_task_cancel(context, cancel_command)

    observation = context._langfuse_observation  # pylint: disable=protected-access
    assert observation.updates[-1]["level"] == "WARNING"
    assert observation.updates[-1]["status_message"] == "user cancelled"
    assert observation.ended_with == {
        "output": {"cancelled": True, "reason": "user cancelled"}
    }


def test_langfuse_plugin_builds_sdk_client_with_constructor(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeLangfuseClient:

        def __init__(self, **kwargs: Any):
            captured.update(kwargs)

    fake_module = SimpleNamespace(Langfuse=FakeLangfuseClient)
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3000")

    plugin = LangfusePlugin()
    tracer = plugin._build_default_tracer()  # pylint: disable=protected-access

    assert tracer is not None
    assert captured == {
        "public_key": "pk-test",
        "secret_key": "sk-test",
        "base_url": "http://localhost:3000",
    }


def test_langfuse_config_prefers_clean_base_url_value():
    config = LangfuseConfig(
        secret_key="sk-test",
        public_key="pk-test",
        base_url=LangfuseConfig._clean_env_value(
            "“http://localhost:3000”"
        ),  # pylint: disable=protected-access
    )

    assert config.base_url == "http://localhost:3000"


def test_langfuse_plugin_end_falls_back_to_update_then_plain_end():
    plugin = LangfusePlugin()
    observation = EndWithoutKwargsObservation()
    context = SimpleNamespace(_langfuse_observation=observation)

    plugin._end_observation(context, output={"status": "ok"})  # pylint: disable=protected-access

    assert observation.updates[-1] == {"output": {"status": "ok"}}
    assert observation.end_calls == 1


def test_langfuse_trace_provider_factory_builds_plugin_from_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3000")

    plugin = LangfuseTraceProviderFactory().build_plugin_from_env()

    assert isinstance(plugin, LangfusePlugin)
