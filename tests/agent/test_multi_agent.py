"""Multi-agent APIs lowered onto the ordinary graph runtime."""

import asyncio

import pytest

from by_framework.agent import (
    Agent,
    AgentContextRemoteDispatcher,
    AgentTool,
    FakeModel,
    FunctionTool,
    GraphRunner,
    Handoff,
    HandoffTeam,
    Interrupt,
    ModelResponse,
    ParallelTeam,
    RemoteAgentResult,
    RunContext,
    RunIdentity,
    Runner,
    ScriptedModel,
    SupervisorTeam,
    ToolCall,
    ToolExecutor,
    ToolExecutionContext,
    Usage,
    WorkflowTeam,
)
from by_framework.agent.graph import GraphError
from by_framework.agent.multi_agent import MultiAgentError


def agent(name: str, text: str, usage: Usage = Usage()) -> Agent:
    return Agent(name, f"You are {name}.", FakeModel(text, usage=usage))


def stable_handoff_filter(value: str) -> str:
    return value


class RecordingDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch(self, agent_name, content, *, trace_context):
        self.calls.append((agent_name, content, trace_context))
        return RemoteAgentResult(
            "remote-result", Usage(3, 4), "remote-run", {"placement": "worker"}
        )


def test_agent_tool_runs_local_subagent_and_remote_dispatch_boundary():
    local = AgentTool(
        agent("researcher", "local-result", Usage(1, 2)),
        trace_context={"trace_id": "trace-1"},
    )
    local_result = asyncio.run(
        ToolExecutor({local.spec.name: local}).execute(
            ToolCall("call-local", "researcher", {"prompt": "research"})
        )
    )
    assert local_result.output["output"] == "local-result"
    assert local_result.output["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
    }
    assert local_result.output["trace_context"]["trace_id"] == "trace-1"

    dispatcher = RecordingDispatcher()
    remote = AgentTool(
        remote_agent_name="external",
        dispatcher=dispatcher,
        trace_context={"trace_id": "trace-2"},
    )
    remote_result = asyncio.run(
        ToolExecutor({"external": remote}).execute(
            ToolCall("call-remote", "external", {"prompt": "delegate"})
        )
    )
    assert remote_result.output["output"] == "remote-result"
    assert remote_result.output["metadata"]["placement"] == "worker"
    assert dispatcher.calls == [("external", "delegate", {"trace_id": "trace-2"})]


def test_agent_tool_usage_is_aggregated_into_parent_run():
    delegated = AgentTool(agent("delegate", "nested", Usage(3, 4)))
    parent = Agent(
        "parent",
        "",
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            delegated.spec.name,
                            {"prompt": "work"},
                        ),
                    ),
                    usage=Usage(1, 2),
                ),
                ModelResponse("done", usage=Usage(5, 6)),
            ]
        ),
        [delegated],
    )

    result = asyncio.run(Runner().run(parent, "go"))

    assert result.usage == Usage(9, 12)


def test_parallel_team_fans_out_and_aggregates_usage_on_graph_state():
    team = ParallelTeam(
        [
            agent("alpha", "A", Usage(1, 2)),
            agent("beta", "B", Usage(3, 4)),
        ]
    ).compile()

    result = asyncio.run(
        team.run("work", run_id="parallel-team", trace_context={"trace_id": "t"})
    )

    assert result.state["results"] == {"alpha": "A", "beta": "B"}
    assert result.state["usage"] == {
        "input_tokens": 4,
        "output_tokens": 6,
    }
    assert result.state["trace_context"] == {"trace_id": "t"}
    assert [node.id for node in team.plan.nodes] == [
        "join",
        "member:alpha",
        "member:beta",
    ]
    assert all(
        node.definition_hash
        for node in team.plan.nodes
        if node.id.startswith("member:")
    )


def test_workflow_team_passes_previous_output_to_next_member():
    second_model = FakeModel("final")
    team = WorkflowTeam(
        [agent("first", "draft", Usage(1, 1)), Agent("second", "", second_model)]
    ).compile()

    result = asyncio.run(team.run("initial"))

    assert result.state["output"] == "final"
    assert second_model.requests[0].messages[-1].content == "draft"
    assert result.state["usage"] == {
        "input_tokens": 1,
        "output_tokens": 1,
    }


def test_supervisor_team_selects_member_then_synthesizes():
    supervisor_model = ScriptedModel(
        [
            ModelResponse("researcher", usage=Usage(1, 1)),
            ModelResponse("synthesized", usage=Usage(2, 2)),
        ]
    )
    supervisor = Agent("supervisor", "Coordinate.", supervisor_model)
    team = SupervisorTeam(
        supervisor,
        [
            agent("researcher", "facts", Usage(3, 4)),
            agent("writer", "unused"),
        ],
    ).compile()

    result = asyncio.run(team.run("question"))

    assert result.state["active_agent"] == "researcher"
    assert result.state["results"] == {"researcher": "facts"}
    assert result.state["output"] == "synthesized"
    assert result.state["usage"] == {
        "input_tokens": 6,
        "output_tokens": 7,
    }
    assert len(supervisor_model.requests) == 2


def test_handoff_filters_input_and_history_before_control_transfer():
    target_model = FakeModel("resolved", usage=Usage(2, 3))
    source = agent("triage", "handoff:specialist:private details", Usage(1, 1))
    target = Agent("specialist", "Resolve.", target_model)

    def redact(value):
        return value.replace("private", "redacted")

    def agent_names(history):
        return [item.split(":", 1)[0] for item in history]

    handoff = Handoff(
        "triage",
        target,
        input_filter=redact,
        history_filter=agent_names,
    )

    result = asyncio.run(HandoffTeam(source, [handoff]).compile().run("help"))

    assert result.state["output"] == "resolved"
    target_prompt = target_model.requests[0].messages[-1].content
    assert target_prompt == "triage\nredacted details"
    assert result.state["usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
    }


def test_team_failure_and_handoff_interrupt_use_graph_terminal_semantics():
    failing = Agent("broken", "", ScriptedModel([]))
    with pytest.raises(Exception, match="no response remaining"):
        asyncio.run(ParallelTeam([failing]).compile().run("fail"))

    source = agent("triage", "handoff:specialist:needs approval")
    target = agent("specialist", "done")

    def require_approval(value):
        if value != "approved":
            return Interrupt("approve-handoff", "Allow transfer?", "input")
        return "approved"

    handoff = Handoff(
        "triage",
        target,
        input_filter=require_approval,
    )
    compiled = HandoffTeam(source, [handoff]).compile()
    runner = GraphRunner()

    async def interrupt_and_resume():
        interrupted = await compiled.run(
            "help", run_id="handoff-interrupt", graph_runner=runner
        )
        resumed = await runner.resume("handoff-interrupt", "approved")
        return interrupted, resumed

    interrupted, resumed = asyncio.run(interrupt_and_resume())
    assert interrupted.status == "interrupted"
    assert interrupted.interrupt is not None
    assert interrupted.interrupt.key == "approve-handoff"
    assert resumed.status == "completed"
    assert resumed.state["output"] == "done"


def test_supervisor_unknown_selection_fails_at_graph_route_boundary():
    team = SupervisorTeam(
        agent("supervisor", "missing"),
        [agent("known", "answer")],
    ).compile()
    with pytest.raises(GraphError, match="unknown route"):
        asyncio.run(team.run("question"))


def test_agent_tool_and_handoff_behavior_are_bound_into_plan_hashes():
    first = AgentTool(agent("delegate", "one"))
    second = AgentTool(Agent("delegate", "Different instructions.", FakeModel("two")))
    assert first.spec.implementation_ref != second.spec.implementation_ref

    def identity(value):
        return value

    def redact(value):
        return value.replace("secret", "redacted")

    target = agent("target", "done")
    first_team = HandoffTeam(
        agent("source", "handoff:target:secret"),
        [Handoff("source", target, input_filter=identity)],
    ).compile()
    second_team = HandoffTeam(
        agent("source", "handoff:target:secret"),
        [Handoff("source", target, input_filter=redact)],
    ).compile()
    assert first_team.plan.plan_hash != second_team.plan.plan_hash


def test_agent_context_adapter_rejects_queued_dispatch_as_completed_reply():
    class QueuedContext:
        async def call_agent(self, *args, **kwargs):
            del args, kwargs
            return {"status": "QUEUED", "message_id": "msg-1"}

    dispatcher = AgentContextRemoteDispatcher(QueuedContext())
    with pytest.raises(MultiAgentError, match="did not return a reply"):
        asyncio.run(
            dispatcher.dispatch("remote", "work", trace_context={"trace_id": "t"})
        )


@pytest.mark.parametrize(
    "build",
    [
        lambda: ParallelTeam([agent("a", "A"), agent("b", "B")]),
        lambda: WorkflowTeam([agent("a", "A"), agent("b", "B")]),
        lambda: SupervisorTeam(
            agent("supervisor", "a"),
            [agent("a", "A"), agent("b", "B")],
        ),
        lambda: HandoffTeam(
            agent("source", "handoff:target:work"),
            [
                Handoff(
                    "source",
                    agent("target", "done"),
                    input_filter=stable_handoff_filter,
                )
            ],
        ),
    ],
)
def test_all_team_compilers_produce_stable_ordinary_graph_plans(build):
    first = build().compile()
    second = build().compile()

    assert first.plan.plan_hash == second.plan.plan_hash
    assert first.graph.plan is first.plan
    assert all(node.definition_hash for node in first.plan.nodes if node.id != "join")


def test_team_propagates_session_capabilities_to_member_tools():
    seen = {}

    def inspect_context(context: ToolExecutionContext) -> str:
        seen["session_id"] = context.run.identity.session_id
        seen["agent_id"] = context.run.identity.agent_id
        seen["files"] = context.run.private_files
        return "observed"

    member = Agent(
        "member",
        "Inspect context.",
        ScriptedModel(
            [
                ModelResponse(
                    "",
                    (ToolCall("call-1", "inspect_context", {}),),
                ),
                ModelResponse("done"),
            ]
        ),
        [FunctionTool(inspect_context)],
    )
    files = object()
    context = RunContext(
        RunIdentity("session-team", "run-team", "team"),
        private_files=files,
    )
    result = asyncio.run(
        ParallelTeam([member]).compile().run("work", context=context)
    )
    assert result.status == "completed"
    assert seen == {
        "session_id": "session-team",
        "agent_id": "member",
        "files": files,
    }
