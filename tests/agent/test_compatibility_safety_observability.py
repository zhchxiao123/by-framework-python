"""Compatibility, approvals, durable bridges, and native observability tests."""

import asyncio

import pytest

from by_framework.agent import (
    AgentConfigAdapter,
    ApprovalAction,
    ApprovalController,
    ApprovalDecision,
    ApprovalMode,
    ApprovalRejectedError,
    DefinitionCatalog,
    FakeModel,
    FunctionTool,
    LegacyInterruptBridge,
    NativeEventWriter,
    NativeRuntimeMetrics,
    NativeTraceContext,
    NativeTraceProjector,
    TerminalStatus,
    ToolCall,
    register_agent_config,
)
from by_framework.agent.runtime import InMemoryRunStore, RunEvent
from by_framework.agent.tools import ToolSpec
from by_framework.agent.tools import ToolValidationError
from by_framework.core.extensions.agent_config import AgentConfig
from by_framework.core.protocol.commands import ResumeCommand
from by_framework.core.protocol.message_header import MessageHeader
from by_framework.trace import ObservabilityConfig, SpanRecorder


def test_agent_config_adapter_registers_beside_native_catalog_definitions():
    def tool_resolver(name, config):
        def lookup(query: str) -> str:
            prefix = config["prefix"]
            return f"{prefix}:{query}"

        return FunctionTool(lookup, name=name)

    config = AgentConfig(
        "legacy-agent",
        description="Legacy",
        prompts={"system": "Converted instructions"},
        tools={"lookup": {"prefix": "result"}},
        skills={"legacy-skill": {}},
    )
    adapter = AgentConfigAdapter(
        lambda _config: FakeModel("converted"),
        tool_resolver=tool_resolver,
    )
    catalog = DefinitionCatalog()

    compiled, warnings = asyncio.run(register_agent_config(catalog, config, adapter))

    assert compiled.spec.name == "legacy-agent"
    assert compiled.spec.instructions == "Converted instructions"
    assert compiled.spec.tools[0].name == "lookup"
    assert warnings == ("skills remains plugin-managed and was not compiled",)
    assert catalog.snapshot().definitions["legacy-agent"] == compiled.plan.plan_hash


class LegacyContext:
    def __init__(self):
        self.questions = []
        self.calls = []

    async def ask_user(self, prompt):
        self.questions.append(prompt)
        return {"status": "WAITING_USER"}

    async def call_agent(self, target, content, **kwargs):
        self.calls.append((target, content, kwargs))
        return {"reply_data": "remote answer"}


def test_legacy_ask_call_and_resume_map_to_durable_native_events():
    store = InMemoryRunStore()
    writer = NativeEventWriter(
        store, "compat-run", version=0, fencing_token=1, state={}
    )
    bridge = LegacyInterruptBridge(writer)
    context = LegacyContext()

    async def scenario():
        question = await bridge.ask_user(context, "Continue?", key="question-1")
        remote, result = await bridge.call_agent(
            context, "researcher", "research", key="agent-1"
        )
        resumed = await bridge.resume(
            ResumeCommand(
                MessageHeader(
                    "message",
                    "session",
                    "trace",
                    source_agent_type="researcher",
                ),
                status="COMPLETED",
                reply_data={"answer": 42},
                extra_payload={"native_interrupt_key": "agent-1"},
            )
        )
        return question, remote, result, resumed

    question, remote, result, resumed = asyncio.run(scenario())
    assert question.key == "question-1"
    assert remote.key == "agent-1"
    assert result == {"reply_data": "remote answer"}
    assert resumed == {"answer": 42}
    assert context.questions == ["Continue?"]
    assert context.calls[0][2]["metadata"]["native_interrupt_key"] == "agent-1"
    _, _, _, events = store.snapshot("compat-run")
    assert [event.kind for event in events] == [
        "RunInterrupted",
        "AgentDispatched",
        "RunResumed",
    ]
    assert store.snapshot("compat-run")[2]["_native_compatibility"]["completed"] == {
        "ask_user:question-1": "RunInterrupted",
        "call_agent:agent-1": "AgentDispatched",
        "resume:agent-1": "RunResumed",
    }


def test_legacy_bridge_is_opt_in_idempotent_and_resume_is_correlated():
    store = InMemoryRunStore()
    writer = NativeEventWriter(
        store, "compat-replay", version=0, fencing_token=1, state={}
    )
    bridge = LegacyInterruptBridge(writer)
    context = LegacyContext()

    async def scenario():
        await bridge.ask_user(context, "Continue?", key="question-1")
        await bridge.ask_user(context, "Continue?", key="question-1")
        command = ResumeCommand(
            MessageHeader("message", "session", "trace"),
            status="COMPLETED",
            extra_payload={"native_interrupt_key": "question-1"},
        )
        await bridge.resume(command)
        await bridge.resume(command)
        with pytest.raises(ValueError, match="unknown native interrupt"):
            await bridge.resume(
                ResumeCommand(
                    MessageHeader("other", "session", "trace"),
                    status="COMPLETED",
                    extra_payload={"native_interrupt_key": "wrong"},
                )
            )

    asyncio.run(scenario())
    assert context.questions == ["Continue?"]
    assert [event.kind for event in store.snapshot("compat-replay")[3]] == [
        "RunInterrupted",
        "RunResumed",
    ]


def test_approval_policy_approve_edit_reject_and_durable_events():
    call = ToolCall("call-1", "delete_record", {"record_id": "old"})
    spec = ToolSpec(
        "delete_record",
        "Delete",
        {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
        {},
        side_effect="destructive",
    )
    controller = ApprovalController(ApprovalMode.RISK_BASED)
    request = controller.request("approval-1", call, spec)
    assert request is not None
    assert request.interrupt().key == "approval-1"
    assert (
        controller.resolve(
            request, ApprovalDecision("approval-1", ApprovalAction.APPROVE)
        )
        == call
    )
    edited = controller.resolve(
        request,
        ApprovalDecision(
            "approval-1",
            ApprovalAction.EDIT,
            {"record_id": "reviewed"},
        ),
    )
    assert edited.arguments == {"record_id": "reviewed"}
    with pytest.raises(ToolValidationError, match="expected string"):
        controller.resolve(
            request,
            ApprovalDecision(
                "approval-1",
                ApprovalAction.EDIT,
                {"record_id": 123},
            ),
        )
    with pytest.raises(ApprovalRejectedError):
        controller.resolve(
            request,
            ApprovalDecision("approval-1", ApprovalAction.REJECT, reason="unsafe"),
        )
    assert controller.requested_event(request).kind == "ApprovalRequested"
    assert (
        controller.resolved_event(
            ApprovalDecision("approval-1", ApprovalAction.REJECT)
        ).payload["action"]
        == "reject"
    )


class Exporter:
    def __init__(self):
        self.spans = []

    async def export_span(self, span):
        self.spans.append(span)


def test_native_events_project_to_existing_trace_hierarchy_with_redaction():
    exporter = Exporter()
    config = ObservabilityConfig(
        redis_enabled=False,
        langfuse_enabled=False,
    )
    recorder = SpanRecorder(exporters=[exporter], config=config)
    projector = NativeTraceProjector(recorder, config=config, capture_sensitive=False)
    context = NativeTraceContext(
        "trace-1", "run-span", "node-span", "session-1", "execution-1"
    )

    async def scenario():
        run = await projector.record(
            RunEvent("RunCreated", {"input": "secret prompt"}), context
        )
        model = await projector.record(
            RunEvent(
                "ModelCompleted",
                {"content": "secret answer", "tokens": 3},
            ),
            context,
        )
        approval = await projector.record(
            RunEvent(
                "ApprovalRequested",
                {"arguments": {"password": "hidden"}},
            ),
            context,
        )
        return run, model, approval

    run, model, approval = asyncio.run(scenario())
    assert run.operation == "run"
    assert run.span_id == "run-span"
    assert run.parent_span_id == ""
    assert run.output["input"] == "[REDACTED]"
    assert model.operation == "model.call"
    assert model.parent_span_id == "node-span"
    assert model.output["content"] == "[REDACTED]"
    assert approval.output["arguments"] == "[REDACTED]"
    assert exporter.spans == [run, model, approval]


def test_native_metrics_and_structured_terminal_states():
    metrics = NativeRuntimeMetrics()
    metrics.record(RunEvent("RunCreated", {}), agent_type="native-test")
    metrics.record(
        RunEvent("RunFailed", {"error_type": "ModelError"}),
        agent_type="native-test",
    )
    assert metrics.counters == {"RunCreated": 1, "RunFailed": 1}
    assert TerminalStatus.BUDGET_EXHAUSTED.value == "budget_exhausted"
    assert TerminalStatus.APPROVAL_REJECTED.value == "approval_rejected"


def test_native_metrics_deduplicate_replayed_event_ids():
    metrics = NativeRuntimeMetrics()
    event = RunEvent("RunCreated", {})
    metrics.record(event, event_id="run:1")
    metrics.record(event, event_id="run:1")
    assert metrics.counters == {"RunCreated": 1}
