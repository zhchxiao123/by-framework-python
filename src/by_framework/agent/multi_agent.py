"""Multi-agent APIs compiled entirely to ordinary state graphs."""

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from .definition import Agent, CompiledAgent
from .execution import Runner
from .graph import (
    END,
    START,
    CompiledGraph,
    GraphRunResult,
    GraphRunner,
    Interrupt,
    StateGraph,
)
from .model import Usage
from .runtime.serialization import stable_plan_hash
from .runtime.context import RunContext, RunIdentity
from .tools import FunctionTool


class MultiAgentError(RuntimeError):
    """Base multi-agent authoring or execution error."""


@dataclass(frozen=True)
class RemoteAgentResult:
    output: Any
    usage: Usage = Usage()
    run_id: str = ""
    metadata: Mapping[str, Any] | None = None


class RemoteAgentDispatcher(Protocol):
    """Placement-neutral boundary for dispatching an external agent."""

    async def dispatch(
        self,
        agent_name: str,
        content: Any,
        *,
        trace_context: Mapping[str, Any],
    ) -> RemoteAgentResult:
        """Dispatch and await one remote invocation."""


class AgentContextRemoteDispatcher:
    """Map remote dispatch onto the existing ``AgentContext.call_agent`` API."""

    def __init__(self, context):
        self._context = context

    async def dispatch(
        self,
        agent_name: str,
        content: Any,
        *,
        trace_context: Mapping[str, Any],
    ) -> RemoteAgentResult:
        payload = await self._context.call_agent(
            agent_name,
            content,
            metadata=dict(trace_context),
            wait_for_reply=True,
        )
        if "reply_data" not in payload:
            raise MultiAgentError(
                "AgentContext dispatched the remote agent but did not return a "
                "reply; durable remote-result resume is deferred to Milestone 4"
            )
        output = payload.get("reply_data", payload)
        usage_payload = payload.get("usage", {})
        usage = Usage(
            int(usage_payload.get("input_tokens", 0)),
            int(usage_payload.get("output_tokens", 0)),
        )
        return RemoteAgentResult(
            output,
            usage,
            str(payload.get("execution_id", "")),
            payload.get("metadata"),
        )


class AgentTool(FunctionTool):
    """Expose a local sub-agent or remote agent through the normal tool loop."""

    def __init__(
        self,
        agent: Agent | CompiledAgent | None = None,
        *,
        remote_agent_name: str | None = None,
        dispatcher: RemoteAgentDispatcher | None = None,
        name: str | None = None,
        trace_context: Mapping[str, Any] | None = None,
        max_model_turns: int = 32,
    ):
        local = agent is not None
        remote = dispatcher is not None and remote_agent_name is not None
        if local == remote:
            raise MultiAgentError(
                "configure exactly one local agent or remote dispatcher/agent name"
            )
        self.agent = _compile_agent(agent) if agent is not None else None
        self.remote_agent_name = remote_agent_name
        self.dispatcher = dispatcher
        self.trace_context = dict(trace_context or {})
        self.max_model_turns = max_model_turns
        default_name = (
            self.agent.spec.name if self.agent is not None else str(remote_agent_name)
        )
        super().__init__(
            self._invoke,
            name=name or default_name,
            description=f"Delegate work to agent {default_name}.",
            side_effect="remote" if dispatcher is not None else "none",
            implementation_ref=(
                self.agent.plan.plan_hash
                if self.agent is not None
                else f"remote-agent:{remote_agent_name}"
            ),
            aggregates_usage=True,
        )

    async def _invoke(self, prompt: str) -> dict[str, Any]:
        if self.agent is not None:
            local_result = await Runner(max_model_turns=self.max_model_turns).run(
                self.agent, prompt
            )
            return {
                "output": local_result.output,
                "run_id": local_result.run_id,
                "usage": _usage_dict(local_result.usage),
                "trace_context": self.trace_context,
            }
        assert self.dispatcher is not None
        assert self.remote_agent_name is not None
        remote_result = await self.dispatcher.dispatch(
            self.remote_agent_name,
            prompt,
            trace_context=self.trace_context,
        )
        return {
            "output": remote_result.output,
            "run_id": remote_result.run_id,
            "usage": _usage_dict(remote_result.usage),
            "metadata": dict(remote_result.metadata or {}),
            "trace_context": self.trace_context,
        }


InputFilter = Callable[[str], str | Interrupt]
HistoryFilter = Callable[[Sequence[str]], Sequence[str]]


def identity_input(value: str) -> str:
    return value


def identity_history(history: Sequence[str]) -> Sequence[str]:
    return tuple(history)


@dataclass(frozen=True)
class Handoff:
    """Explicit control transfer with input and history filtering."""

    source: str
    target: Agent | CompiledAgent
    input_filter: InputFilter = identity_input
    history_filter: HistoryFilter = identity_history

    @property
    def target_name(self) -> str:
        return (
            self.target.name
            if isinstance(self.target, Agent)
            else self.target.spec.name
        )

    def prepare(
        self, prompt: str, history: Sequence[str]
    ) -> tuple[str, tuple[str, ...]] | Interrupt:
        filtered_input = self.input_filter(prompt)
        if isinstance(filtered_input, Interrupt):
            return filtered_input
        return filtered_input, tuple(self.history_filter(history))


class TeamState(TypedDict):
    input: str
    output: str
    results: dict[str, Any]
    usage: dict[str, int]
    history: list[str]
    active_agent: str
    trace_context: dict[str, Any]


def merge_mapping(
    current: Mapping[str, Any] | None, update: Mapping[str, Any]
) -> dict[str, Any]:
    return {**dict(current or {}), **dict(update)}


def merge_usage(
    current: Mapping[str, int] | None, update: Mapping[str, int]
) -> dict[str, int]:
    current = current or {}
    return {
        "input_tokens": current.get("input_tokens", 0) + update.get("input_tokens", 0),
        "output_tokens": current.get("output_tokens", 0)
        + update.get("output_tokens", 0),
    }


def append_history(current: Sequence[str] | None, update: Sequence[str]) -> list[str]:
    return [*(current or ()), *update]


@dataclass(frozen=True)
class CompiledTeam:
    """A team definition lowered to an ordinary compiled graph."""

    graph: CompiledGraph
    max_steps: int

    @property
    def plan(self):
        return self.graph.plan

    async def run(
        self,
        user_input: str,
        *,
        run_id: str | None = None,
        trace_context: Mapping[str, Any] | None = None,
        graph_runner: GraphRunner | None = None,
        context: RunContext | None = None,
    ) -> GraphRunResult:
        runner = graph_runner or GraphRunner(max_steps=self.max_steps)
        return await runner.run(
            self.graph,
            _initial_state(user_input, trace_context),
            run_id=run_id,
            context=context,
        )


class ParallelTeam:
    """Fan out to every member and deterministically join their results."""

    def __init__(
        self,
        members: Sequence[Agent | CompiledAgent],
        *,
        synthesizer: Agent | CompiledAgent | None = None,
        max_steps: int = 100,
        max_model_turns: int = 32,
    ):
        self.members = _unique_members(members)
        self.synthesizer = synthesizer
        self.max_steps = max_steps
        self.max_model_turns = max_model_turns

    def compile(self) -> CompiledTeam:
        graph = _team_graph("parallel_team")
        member_ids = []
        for member in self.members:
            compiled = _compile_agent(member)
            node_id = f"member:{compiled.spec.name}"
            member_ids.append(node_id)
            graph.add_node(
                node_id,
                _member_node(compiled, self.max_model_turns),
                definition_hash=compiled.plan.plan_hash,
            )
            graph.add_edge(START, node_id)
        if self.synthesizer is None:
            graph.add_node("join", _join_node)
        else:
            compiled = _compile_agent(self.synthesizer)
            graph.add_node(
                "join",
                _synthesis_node(compiled, self.max_model_turns),
                definition_hash=compiled.plan.plan_hash,
            )
        for node_id in member_ids:
            graph.add_edge(node_id, "join")
        graph.add_edge("join", END)
        return CompiledTeam(graph.compile(), self.max_steps)


class WorkflowTeam:
    """Run members in an explicit sequence."""

    def __init__(
        self,
        members: Sequence[Agent | CompiledAgent],
        *,
        max_steps: int = 100,
        max_model_turns: int = 32,
    ):
        self.members = _unique_members(members)
        self.max_steps = max_steps
        self.max_model_turns = max_model_turns

    def compile(self) -> CompiledTeam:
        graph = _team_graph("workflow_team")
        previous = START
        for index, member in enumerate(self.members):
            compiled = _compile_agent(member)
            node_id = f"step:{index}:{compiled.spec.name}"
            graph.add_node(
                node_id,
                _member_node(compiled, self.max_model_turns, use_previous=True),
                definition_hash=compiled.plan.plan_hash,
            )
            graph.add_edge(previous, node_id)
            previous = node_id
        graph.add_edge(previous, END)
        return CompiledTeam(graph.compile(), self.max_steps)


class SupervisorTeam:
    """A supervisor selects one member and synthesizes its result."""

    def __init__(
        self,
        supervisor: Agent | CompiledAgent,
        members: Sequence[Agent | CompiledAgent],
        *,
        max_steps: int = 100,
        max_model_turns: int = 32,
    ):
        self.supervisor = _compile_agent(supervisor)
        self.members = _unique_members(members)
        self.max_steps = max_steps
        self.max_model_turns = max_model_turns

    def compile(self) -> CompiledTeam:
        graph = _team_graph("supervisor_team")
        graph.add_node(
            "supervisor:select",
            _supervisor_select_node(self.supervisor, self.max_model_turns),
            definition_hash=self.supervisor.plan.plan_hash,
        )
        graph.add_edge(START, "supervisor:select")
        routes = {}
        for member in self.members:
            compiled = _compile_agent(member)
            member_id = f"member:{compiled.spec.name}"
            synth_id = f"synthesize:{compiled.spec.name}"
            routes[compiled.spec.name] = member_id
            graph.add_node(
                member_id,
                _member_node(compiled, self.max_model_turns),
                definition_hash=compiled.plan.plan_hash,
            )
            graph.add_node(
                synth_id,
                _synthesis_node(self.supervisor, self.max_model_turns),
                definition_hash=self.supervisor.plan.plan_hash,
            )
            graph.add_edge(member_id, synth_id).add_edge(synth_id, END)
        graph.add_conditional_edges(
            "supervisor:select", lambda state: state["active_agent"], routes
        )
        return CompiledTeam(graph.compile(), self.max_steps)


class HandoffTeam:
    """Run a source agent and transfer control through explicit Handoffs."""

    def __init__(
        self,
        source: Agent | CompiledAgent,
        handoffs: Sequence[Handoff],
        *,
        max_steps: int = 100,
        max_model_turns: int = 32,
    ):
        self.source = _compile_agent(source)
        self.handoffs = tuple(handoffs)
        if not self.handoffs:
            raise MultiAgentError("handoff team requires at least one handoff")
        if any(handoff.source != self.source.spec.name for handoff in self.handoffs):
            raise MultiAgentError("handoff source must match the team's source agent")
        target_names = [handoff.target_name for handoff in self.handoffs]
        if len(target_names) != len(set(target_names)):
            raise MultiAgentError("handoff target names must be unique")
        self.max_steps = max_steps
        self.max_model_turns = max_model_turns

    def compile(self) -> CompiledTeam:
        graph = _team_graph("handoff_team")
        graph.add_node(
            "source",
            _handoff_source_node(self.source, self.max_model_turns),
            definition_hash=self.source.plan.plan_hash,
        )
        graph.add_edge(START, "source")
        routes = {"done": END}
        for handoff in self.handoffs:
            target = _compile_agent(handoff.target)
            node_id = f"handoff:{target.spec.name}"
            routes[target.spec.name] = node_id
            graph.add_node(
                node_id,
                _handoff_target_node(handoff, target, self.max_model_turns),
                definition_hash=_handoff_definition_hash(handoff, target),
            )
            graph.add_edge(node_id, END)
        graph.add_conditional_edges(
            "source", lambda state: state["active_agent"] or "done", routes
        )
        return CompiledTeam(graph.compile(), self.max_steps)


def _team_graph(name: str) -> StateGraph[TeamState]:
    graph = StateGraph(TeamState, name=name)
    graph.reducer("results", merge_mapping)
    graph.reducer("usage", merge_usage)
    graph.reducer("history", append_history)
    return graph


def _initial_state(
    user_input: str, trace_context: Mapping[str, Any] | None
) -> TeamState:
    return {
        "input": user_input,
        "output": "",
        "results": {},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "history": [],
        "active_agent": "",
        "trace_context": dict(trace_context or {}),
    }


def _unique_members(
    members: Sequence[Agent | CompiledAgent],
) -> tuple[Agent | CompiledAgent, ...]:
    if not members:
        raise MultiAgentError("team requires at least one member")
    names = [
        member.name if isinstance(member, Agent) else member.spec.name
        for member in members
    ]
    if len(names) != len(set(names)):
        raise MultiAgentError("team member names must be unique")
    return tuple(members)


def _compile_agent(agent: Agent | CompiledAgent) -> CompiledAgent:
    return agent.compile() if isinstance(agent, Agent) else agent


def _member_node(
    agent: CompiledAgent, max_model_turns: int, *, use_previous: bool = False
):
    async def invoke(
        state: TeamState, context: RunContext
    ) -> dict[str, Any]:
        prompt = state["output"] if use_previous and state["output"] else state["input"]
        result = await Runner(max_model_turns=max_model_turns).run(
            agent, prompt, context=_child_context(context, agent.spec.name)
        )
        return _agent_writes(agent.spec.name, result.output, result.usage)

    return invoke


def _supervisor_select_node(agent: CompiledAgent, max_model_turns: int):
    async def select(state: TeamState, context: RunContext) -> dict[str, Any]:
        user_input = state["input"]
        result = await Runner(max_model_turns=max_model_turns).run(
            agent,
            f"Select one member for: {user_input}",
            context=_child_context(context, agent.spec.name),
        )
        return {
            "active_agent": result.output.strip(),
            "usage": _usage_dict(result.usage),
            "history": [f"{agent.spec.name}:selected:{result.output.strip()}"],
        }

    return select


def _synthesis_node(agent: CompiledAgent, max_model_turns: int):
    async def synthesize(
        state: TeamState, context: RunContext
    ) -> dict[str, Any]:
        prompt = json.dumps(state["results"], ensure_ascii=False, sort_keys=True)
        result = await Runner(max_model_turns=max_model_turns).run(
            agent, prompt, context=_child_context(context, agent.spec.name)
        )
        return {
            "output": result.output,
            "usage": _usage_dict(result.usage),
            "history": [f"{agent.spec.name}:{result.output}"],
        }

    return synthesize


def _join_node(state: dict[str, Any]) -> dict[str, Any]:
    return {"output": json.dumps(state["results"], ensure_ascii=False, sort_keys=True)}


def _handoff_source_node(agent: CompiledAgent, max_model_turns: int):
    async def source(state: TeamState, context: RunContext) -> dict[str, Any]:
        result = await Runner(max_model_turns=max_model_turns).run(
            agent,
            state["input"],
            context=_child_context(context, agent.spec.name),
        )
        prefix, _, payload = result.output.partition(":")
        if prefix == "handoff" and ":" in payload:
            target_name, _, handoff_input = payload.partition(":")
            return {
                "input": handoff_input,
                "active_agent": target_name,
                "usage": _usage_dict(result.usage),
                "history": [f"{agent.spec.name}:{result.output}"],
            }
        return {
            "output": result.output,
            "active_agent": "",
            "usage": _usage_dict(result.usage),
            "history": [f"{agent.spec.name}:{result.output}"],
        }

    return source


def _handoff_target_node(handoff: Handoff, agent: CompiledAgent, max_model_turns: int):
    async def target(
        state: TeamState, context: RunContext
    ) -> dict[str, Any] | Interrupt:
        prepared = handoff.prepare(state["input"], state["history"])
        if isinstance(prepared, Interrupt):
            return prepared
        prompt, history = prepared
        result = await Runner(max_model_turns=max_model_turns).run(
            agent,
            "\n".join((*history, prompt)),
            context=_child_context(context, agent.spec.name),
        )
        return _agent_writes(agent.spec.name, result.output, result.usage)

    return target


def _child_context(parent: RunContext, agent_id: str) -> RunContext:
    return RunContext(
        RunIdentity(
            parent.identity.session_id,
            f"run-{uuid.uuid4().hex}",
            agent_id,
            parent.identity.user_code,
            parent.identity.user_name,
            parent.identity.trace_context,
        ),
        parent.private_files,
        parent.shared_files,
        parent.conversation,
        parent.agent_configs,
    )


def _agent_writes(name: str, output: str, usage: Usage) -> dict[str, Any]:
    return {
        "output": output,
        "results": {name: output},
        "usage": _usage_dict(usage),
        "history": [f"{name}:{output}"],
    }


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


def _handoff_definition_hash(handoff: Handoff, target: CompiledAgent) -> str:
    return stable_plan_hash(
        {
            "target_plan_hash": target.plan.plan_hash,
            "input_filter": _callable_ref(handoff.input_filter),
            "history_filter": _callable_ref(handoff.history_filter),
        }
    )


def _callable_ref(function: Callable[..., Any]) -> str:
    module = getattr(function, "__module__", None)
    qualname = getattr(function, "__qualname__", None)
    if not module or not qualname or "<lambda>" in qualname:
        raise MultiAgentError("handoff filters require stable named callables")
    return f"{module}:{qualname}"


def new_trace_context(parent_trace_id: str | None = None) -> dict[str, str]:
    """Create lightweight identifiers propagated through nested invocations."""
    return {
        "trace_id": parent_trace_id or f"trace-{uuid.uuid4().hex}",
        "span_id": f"span-{uuid.uuid4().hex}",
    }
