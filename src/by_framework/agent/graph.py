"""Typed, durable local state-graph engine.

This milestone intentionally keeps execution process-local. Cancellation is
cooperative between supersteps, synchronous work cannot be forcefully stopped
after a timeout, and interrupts inside parallel supersteps or subgraphs are
rejected until the distributed interrupt scheduler is introduced.
"""

import asyncio
import copy
import inspect
import time
import types
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Generic,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from .execution import Checkpoint, InMemoryCheckpointStore
from .runtime.serialization import stable_plan_hash
from .runtime.store import CommitRequest, InMemoryRunStore, RunEvent, RunStore

START = "__start__"
END = "__end__"
StateT = TypeVar("StateT", bound=Mapping[str, Any])
Reducer = Callable[[Any, Any], Any]
Node = Callable[[dict[str, Any]], Any]
Router = Callable[[dict[str, Any]], str]


class GraphError(RuntimeError):
    """Base graph engine error."""


class GraphValidationError(GraphError):
    """A graph definition is invalid."""


class GraphBudgetExceededError(GraphError):
    """A graph exceeded a configured execution budget."""


class GraphCancelledError(GraphError):
    """A graph run was cancelled."""


class StateValidationError(GraphError):
    """Graph state or node writes violate the state schema."""


@dataclass(frozen=True)
class Interrupt:
    """A durable request for an external resume value."""

    key: str
    prompt: str
    resume_field: str


@dataclass(frozen=True)
class NodePolicy:
    max_attempts: int = 1
    timeout_seconds: float | None = None
    fallback: str | None = None

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class StateSchema:
    """Runtime field types and deterministic write reducers."""

    fields: Mapping[str, Any]
    reducers: Mapping[str, Reducer] = field(
        default_factory=dict, compare=False, repr=False
    )
    version: int = 1

    @classmethod
    def from_type(
        cls, state_type: type, *, reducers: Mapping[str, Reducer] | None = None
    ) -> "StateSchema":
        hints = get_type_hints(state_type)
        if not hints:
            raise StateValidationError("state type must declare annotated fields")
        return cls(dict(hints), dict(reducers or {}))

    def validate(self, state: Mapping[str, Any], *, partial: bool = False) -> None:
        unknown = set(state) - set(self.fields)
        if unknown:
            raise StateValidationError(f"unknown state fields: {sorted(unknown)}")
        if not partial:
            missing = set(self.fields) - set(state)
            if missing:
                raise StateValidationError(f"missing state fields: {sorted(missing)}")
        for name, value in state.items():
            expected = self.fields[name]
            if not _matches_annotation(value, expected):
                expected_name = getattr(expected, "__name__", repr(expected))
                raise StateValidationError(
                    f"field {name!r} requires {expected_name}, "
                    f"got {type(value).__name__}"
                )

    def merge(
        self,
        state: Mapping[str, Any],
        writes: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Merge a superstep in node-id order for replay-stable results."""
        result = dict(state)
        for node_id in sorted(writes):
            node_write = writes[node_id]
            self.validate(node_write, partial=True)
            for name, value in node_write.items():
                reducer = self.reducers.get(name, replace)
                result[name] = reducer(result.get(name), value)
        self.validate(result)
        return result


def replace(current: Any, update: Any) -> Any:  # pylint: disable=unused-argument
    """Replace a state field with the latest deterministic write."""
    return update


def append(current: Any, update: Any) -> list:
    """Append a value or sequence to an accumulated list."""
    base = list(current or [])
    if isinstance(update, list):
        return [*base, *update]
    return [*base, update]


def add(current: Any, update: Any) -> Any:
    """Add numeric updates."""
    return (current or 0) + update


@dataclass(frozen=True)
class GraphNodeSpec:
    id: str
    policy: NodePolicy
    definition_hash: str | None = None


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    condition: str = "always"


@dataclass(frozen=True)
class ConditionalRoute:
    source: str
    routes: Mapping[str, str]


@dataclass(frozen=True)
class GraphPlan:
    name: str
    schema_fields: Mapping[str, str]
    reducer_refs: Mapping[str, str]
    schema_version: int
    nodes: tuple[GraphNodeSpec, ...]
    edges: tuple[GraphEdge, ...]
    conditional_routes: tuple[ConditionalRoute, ...]
    plan_hash: str = ""


@dataclass(frozen=True)
class CompiledGraph:
    plan: GraphPlan
    schema: StateSchema = field(compare=False, repr=False)
    nodes: Mapping[str, Node] = field(compare=False, repr=False)
    routers: Mapping[str, Router] = field(compare=False, repr=False)
    subgraphs: Mapping[str, "CompiledGraph"] = field(compare=False, repr=False)


class StateGraph(Generic[StateT]):
    """Builder for a typed state graph."""

    def __init__(self, state_type: type[StateT], *, name: str = "graph"):
        self.name = name
        self.schema = StateSchema.from_type(state_type)
        self._nodes: dict[str, Node] = {}
        self._policies: dict[str, NodePolicy] = {}
        self._subgraphs: dict[str, CompiledGraph] = {}
        self._definition_hashes: dict[str, str] = {}
        self._edges: list[GraphEdge] = []
        self._routers: dict[str, tuple[Router, dict[str, str]]] = {}

    def reducer(self, field_name: str, reducer: Reducer) -> "StateGraph[StateT]":
        if field_name not in self.schema.fields:
            raise GraphValidationError(f"unknown reducer field {field_name!r}")
        if not isinstance(self.schema.reducers, dict):  # pragma: no cover
            raise GraphValidationError("compiled state schema is immutable")
        self.schema.reducers[field_name] = reducer
        return self

    def add_node(
        self,
        node_id: str,
        node: Node,
        *,
        policy: NodePolicy | None = None,
        definition_hash: str | None = None,
    ) -> "StateGraph[StateT]":
        self._validate_node_id(node_id)
        if node_id in self._nodes:
            raise GraphValidationError(f"duplicate node {node_id!r}")
        self._nodes[node_id] = node
        self._policies[node_id] = policy or NodePolicy()
        if definition_hash is not None:
            self._definition_hashes[node_id] = definition_hash
        return self

    def add_subgraph(
        self, node_id: str, graph: "StateGraph | CompiledGraph"
    ) -> "StateGraph[StateT]":
        compiled = graph.compile() if isinstance(graph, StateGraph) else graph
        self.add_node(node_id, lambda state: state)
        self._subgraphs[node_id] = compiled
        self._definition_hashes[node_id] = compiled.plan.plan_hash
        return self

    def add_edge(self, source: str, target: str) -> "StateGraph[StateT]":
        self._edges.append(GraphEdge(source, target))
        return self

    def add_conditional_edges(
        self, source: str, router: Router, routes: Mapping[str, str]
    ) -> "StateGraph[StateT]":
        if not routes:
            raise GraphValidationError("conditional routes must not be empty")
        if source in self._routers:
            raise GraphValidationError(
                f"conditional routes already exist for {source!r}"
            )
        self._routers[source] = (router, dict(routes))
        return self

    def compile(self) -> CompiledGraph:
        known = {START, END, *self._nodes}
        if not self._nodes:
            raise GraphValidationError("graph must contain at least one node")
        for edge in self._edges:
            if edge.source not in known or edge.target not in known:
                raise GraphValidationError(
                    f"edge references unknown node: {edge.source!r} -> {edge.target!r}"
                )
            if edge.source == END or edge.target == START:
                raise GraphValidationError(
                    "END cannot have outgoing and START incoming"
                )
        for source, (_, routes) in self._routers.items():
            if source not in self._nodes:
                raise GraphValidationError(
                    f"conditional source {source!r} is not a node"
                )
            unknown = set(routes.values()) - known
            if unknown:
                raise GraphValidationError(
                    f"conditional routes reference unknown nodes: {sorted(unknown)}"
                )
        for node_id, policy in self._policies.items():
            if policy.fallback is not None and policy.fallback not in self._nodes:
                raise GraphValidationError(
                    f"fallback for {node_id!r} references unknown node "
                    f"{policy.fallback!r}"
                )
            if policy.fallback == node_id:
                raise GraphValidationError(
                    f"fallback for {node_id!r} cannot reference itself"
                )
        if not any(edge.source == START for edge in self._edges):
            raise GraphValidationError("graph requires an edge from START")
        reachable = _reachable(
            self._edges,
            self._routers,
            {
                node_id: policy.fallback
                for node_id, policy in self._policies.items()
                if policy.fallback is not None
            },
        )
        unreachable = set(self._nodes) - reachable
        if unreachable:
            raise GraphValidationError(f"unreachable nodes: {sorted(unreachable)}")
        if END not in reachable:
            raise GraphValidationError("END is not reachable")
        specs = tuple(
            GraphNodeSpec(
                node_id,
                self._policies[node_id],
                self._definition_hashes.get(node_id),
            )
            for node_id in sorted(self._nodes)
        )
        conditional = tuple(
            ConditionalRoute(source, dict(sorted(routes.items())))
            for source, (_, routes) in sorted(self._routers.items())
        )
        unhashed = GraphPlan(
            self.name,
            {
                name: getattr(annotation, "__name__", repr(annotation))
                for name, annotation in sorted(self.schema.fields.items())
            },
            {
                name: _callable_ref(reducer)
                for name, reducer in sorted(self.schema.reducers.items())
            },
            self.schema.version,
            specs,
            tuple(sorted(self._edges, key=lambda edge: (edge.source, edge.target))),
            conditional,
        )
        plan = GraphPlan(
            unhashed.name,
            MappingProxyType(dict(unhashed.schema_fields)),
            MappingProxyType(dict(unhashed.reducer_refs)),
            unhashed.schema_version,
            unhashed.nodes,
            unhashed.edges,
            tuple(
                ConditionalRoute(route.source, MappingProxyType(dict(route.routes)))
                for route in unhashed.conditional_routes
            ),
            stable_plan_hash(unhashed),
        )
        runtime_schema = StateSchema(
            MappingProxyType(dict(self.schema.fields)),
            MappingProxyType(dict(self.schema.reducers)),
            self.schema.version,
        )
        return CompiledGraph(
            plan,
            runtime_schema,
            MappingProxyType(dict(self._nodes)),
            MappingProxyType(
                {source: route[0] for source, route in self._routers.items()}
            ),
            MappingProxyType(dict(self._subgraphs)),
        )

    @staticmethod
    def _validate_node_id(node_id: str) -> None:
        if not node_id or node_id in (START, END):
            raise GraphValidationError(f"invalid node id {node_id!r}")


@dataclass(frozen=True)
class GraphRunResult:
    run_id: str
    state: dict[str, Any]
    status: str
    state_version: int
    plan_hash: str
    interrupt: Interrupt | None = None


@dataclass
class _SuspendedRun:
    graph: CompiledGraph
    state: dict[str, Any]
    frontier: tuple[str, ...]
    version: int
    interrupt_node: str
    interrupt: Interrupt
    steps: int
    completed: set[str]


class GraphRunner:
    """Local superstep coordinator backed by durable run/checkpoint stores."""

    def __init__(
        self,
        *,
        store: RunStore | None = None,
        checkpoint_store: InMemoryCheckpointStore | None = None,
        max_steps: int = 100,
        max_seconds: float | None = None,
    ):
        self.store = store or InMemoryRunStore()
        self.checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self._suspended: dict[str, _SuspendedRun] = {}
        self._cancelled: set[str] = set()
        self._plans: dict[str, CompiledGraph] = {}

    async def run(
        self,
        graph: StateGraph | CompiledGraph,
        initial_state: Mapping[str, Any],
        *,
        run_id: str | None = None,
    ) -> GraphRunResult:
        compiled = graph.compile() if isinstance(graph, StateGraph) else graph
        resolved_id = run_id or f"run-{uuid.uuid4().hex}"
        compiled.schema.validate(initial_state)
        self._plans[compiled.plan.plan_hash] = compiled
        frontier = _targets(compiled.plan.edges, START)
        return await self._drive(
            compiled,
            resolved_id,
            copy.deepcopy(dict(initial_state)),
            frontier,
            version=0,
            steps=0,
            create=True,
            completed={START},
        )

    async def resume(self, run_id: str, value: Any) -> GraphRunResult:
        suspended = self._suspended.get(run_id)
        if suspended is None:
            raise GraphError(f"run {run_id!r} is not interrupted")
        state = copy.deepcopy(suspended.state)
        state[suspended.interrupt.resume_field] = value
        suspended.graph.schema.validate(state)
        self._suspended.pop(run_id)
        completed = set(suspended.completed)
        frontier = (suspended.interrupt_node,)
        return await self._drive(
            suspended.graph,
            run_id,
            state,
            frontier,
            suspended.version,
            suspended.steps,
            create=False,
            completed=completed,
            leading_event=RunEvent(
                "RunResumed",
                {"interrupt_key": suspended.interrupt.key, "value": value},
            ),
        )

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        suspended = self._suspended.pop(run_id, None)
        if suspended is not None:
            await self._commit(
                suspended.graph,
                run_id,
                suspended.version,
                (RunEvent("RunCancelled", {}),),
                suspended.state,
                suspended.frontier,
                "cancelled",
                suspended.steps,
                suspended.completed,
            )

    async def replay(self, run_id: str, version: int | None = None) -> Checkpoint:
        checkpoint = await self.checkpoint_store.get(run_id, version)
        if checkpoint is None:
            raise GraphError(f"checkpoint for run {run_id!r} was not found")
        return checkpoint

    async def fork(
        self, run_id: str, *, new_run_id: str | None = None, version: int | None = None
    ) -> GraphRunResult:
        checkpoint = await self.replay(run_id, version)
        graph = self._plans.get(checkpoint.plan_hash)
        if graph is None:
            raise GraphError(f"plan {checkpoint.plan_hash!r} is not registered")
        state = copy.deepcopy(checkpoint.state["graph_state"])
        frontier = tuple(checkpoint.state["frontier"])
        completed = set(checkpoint.state.get("completed", [START]))
        return await self._drive(
            graph,
            new_run_id or f"run-{uuid.uuid4().hex}",
            state,
            frontier,
            version=0,
            steps=0,
            create=True,
            completed=completed,
            leading_event=RunEvent(
                "RunForked",
                {"source_run_id": run_id, "source_version": checkpoint.version},
            ),
        )

    async def _drive(
        self,
        graph: CompiledGraph,
        run_id: str,
        state: dict[str, Any],
        frontier: tuple[str, ...],
        version: int,
        steps: int,
        *,
        create: bool,
        completed: set[str],
        leading_event: RunEvent | None = None,
    ) -> GraphRunResult:
        started = time.monotonic()
        if create:
            events = [
                RunEvent("RunCreated", {"plan_hash": graph.plan.plan_hash}),
            ]
            if leading_event:
                events.append(leading_event)
            version = await self._commit(
                graph,
                run_id,
                version,
                tuple(events),
                state,
                frontier,
                "running",
                steps,
                completed,
            )
        elif leading_event:
            version = await self._commit(
                graph,
                run_id,
                version,
                (leading_event,),
                state,
                frontier,
                "running",
                steps,
                completed,
            )
        if not frontier:
            version = await self._commit(
                graph,
                run_id,
                version,
                (RunEvent("RunCompleted", {}),),
                state,
                (),
                "completed",
                steps,
                completed,
            )
            return GraphRunResult(
                run_id, state, "completed", version, graph.plan.plan_hash
            )
        while frontier:
            try:
                self._check_budget(run_id, steps, started)
            except (GraphCancelledError, GraphBudgetExceededError) as exc:
                kind = (
                    "RunCancelled"
                    if isinstance(exc, GraphCancelledError)
                    else "RunFailed"
                )
                status = (
                    "cancelled" if isinstance(exc, GraphCancelledError) else "failed"
                )
                await self._commit(
                    graph,
                    run_id,
                    version,
                    (
                        RunEvent(
                            kind,
                            {
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                            },
                        ),
                    ),
                    state,
                    frontier,
                    status,
                    steps,
                    completed,
                )
                raise
            active = tuple(sorted(node for node in frontier if node != END))
            if not active:
                version = await self._commit(
                    graph,
                    run_id,
                    version,
                    (RunEvent("RunCompleted", {}),),
                    state,
                    (),
                    "completed",
                    steps,
                    completed,
                )
                return GraphRunResult(
                    run_id, state, "completed", version, graph.plan.plan_hash
                )
            results = await asyncio.gather(
                *(self._execute_node(graph, node_id, state) for node_id in active),
                return_exceptions=True,
            )
            writes: dict[str, Mapping[str, Any]] = {}
            transition_events: list[RunEvent] = []
            for node_id, result in zip(active, results):
                if isinstance(result, Interrupt):
                    error: GraphError | None = None
                    if not result.key:
                        error = GraphError("interrupt key must not be empty")
                    elif result.resume_field not in graph.schema.fields:
                        error = GraphError(
                            f"interrupt resume field {result.resume_field!r} "
                            "is not in the state schema"
                        )
                    elif len(active) != 1:
                        error = GraphError(
                            "an interrupt cannot share a parallel superstep"
                        )
                    if error is not None:
                        await self._commit(
                            graph,
                            run_id,
                            version,
                            (
                                RunEvent(
                                    "RunFailed",
                                    {
                                        "node_id": node_id,
                                        "error_type": type(error).__name__,
                                        "message": str(error),
                                    },
                                ),
                            ),
                            state,
                            active,
                            "failed",
                            steps,
                            completed,
                        )
                        raise error
                    version = await self._commit(
                        graph,
                        run_id,
                        version,
                        (
                            RunEvent(
                                "RunInterrupted",
                                {
                                    "node_id": node_id,
                                    "key": result.key,
                                    "prompt": result.prompt,
                                    "resume_field": result.resume_field,
                                },
                            ),
                        ),
                        state,
                        active,
                        "interrupted",
                        steps,
                        completed,
                    )
                    self._suspended[run_id] = _SuspendedRun(
                        graph,
                        state,
                        active,
                        version,
                        node_id,
                        result,
                        steps,
                        set(completed),
                    )
                    return GraphRunResult(
                        run_id,
                        state,
                        "interrupted",
                        version,
                        graph.plan.plan_hash,
                        result,
                    )
                if isinstance(result, BaseException):
                    version = await self._commit(
                        graph,
                        run_id,
                        version,
                        (
                            RunEvent(
                                "RunFailed",
                                {
                                    "node_id": node_id,
                                    "error_type": type(result).__name__,
                                    "message": str(result),
                                },
                            ),
                        ),
                        state,
                        active,
                        "failed",
                        steps,
                        completed,
                    )
                    raise result
                writes[node_id] = result
                transition_events.append(
                    RunEvent("NodeCompleted", {"node_id": node_id, "writes": result})
                )
            try:
                state = graph.schema.merge(state, writes)
                completed.update(active)
                frontier = self._next_frontier(graph, active, state, completed)
            except Exception as exc:
                await self._commit(
                    graph,
                    run_id,
                    version,
                    (
                        *transition_events,
                        RunEvent(
                            "RunFailed",
                            {
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                            },
                        ),
                    ),
                    state,
                    active,
                    "failed",
                    steps,
                    completed,
                )
                raise
            steps += len(active)
            transition_events.append(
                RunEvent(
                    "SuperstepCommitted",
                    {"nodes": list(active), "next": list(frontier), "steps": steps},
                )
            )
            version = await self._commit(
                graph,
                run_id,
                version,
                tuple(transition_events),
                state,
                frontier,
                "running",
                steps,
                completed,
            )
        raise GraphValidationError("graph frontier became empty without reaching END")

    async def _execute_node(
        self, graph: CompiledGraph, node_id: str, state: Mapping[str, Any]
    ) -> Mapping[str, Any] | Interrupt:
        if node_id in graph.subgraphs:
            return await self._execute_subgraph(graph.subgraphs[node_id], state)
        policy = next(spec.policy for spec in graph.plan.nodes if spec.id == node_id)
        last_error: BaseException | None = None
        for _ in range(policy.max_attempts):
            try:
                invocation = _invoke(graph.nodes[node_id], copy.deepcopy(dict(state)))
                if policy.timeout_seconds is not None:
                    result = await asyncio.wait_for(
                        invocation, timeout=policy.timeout_seconds
                    )
                else:
                    result = await invocation
                if isinstance(result, Interrupt):
                    return result
                if not isinstance(result, Mapping):
                    raise StateValidationError(
                        f"node {node_id!r} must return state writes or Interrupt"
                    )
                graph.schema.validate(result, partial=True)
                return result
            except Exception as exc:  # node boundary deliberately retries all failures
                last_error = exc
        if policy.fallback:
            return await self._execute_node(graph, policy.fallback, state)
        assert last_error is not None
        raise last_error

    async def _execute_subgraph(
        self, graph: CompiledGraph, state: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        frontier = _targets(graph.plan.edges, START)
        nested = copy.deepcopy(dict(state))
        aggregate: dict[str, Any] = {}
        nested_steps = 0
        completed = {START}
        while frontier and frontier != (END,):
            if nested_steps >= self.max_steps:
                raise GraphBudgetExceededError("subgraph exceeded step budget")
            active = tuple(sorted(node for node in frontier if node != END))
            results = await asyncio.gather(
                *(self._execute_node(graph, node, nested) for node in active)
            )
            if any(isinstance(result, Interrupt) for result in results):
                raise GraphError("interrupts inside subgraphs are not yet resumable")
            writes = {
                node_id: cast(Mapping[str, Any], result)
                for node_id, result in zip(active, results, strict=True)
            }
            nested = graph.schema.merge(nested, writes)
            for node_id in sorted(writes):
                for name, value in writes[node_id].items():
                    reducer = graph.schema.reducers.get(name, replace)
                    aggregate[name] = reducer(aggregate.get(name), value)
            completed.update(active)
            frontier = self._next_frontier(graph, active, nested, completed)
            nested_steps += len(active)
        return aggregate

    def _next_frontier(
        self,
        graph: CompiledGraph,
        just_completed: tuple[str, ...],
        state: dict[str, Any],
        completed: set[str],
    ) -> tuple[str, ...]:
        targets: set[str] = set()
        route_specs = {
            route.source: route.routes for route in graph.plan.conditional_routes
        }
        for node_id in just_completed:
            if node_id in graph.routers:
                route = graph.routers[node_id](copy.deepcopy(dict(state)))
                try:
                    targets.add(route_specs[node_id][route])
                except KeyError as exc:
                    raise GraphError(
                        f"router for {node_id!r} returned unknown route {route!r}"
                    ) from exc
            targets.update(_targets(graph.plan.edges, node_id))
        inbound = {
            target: {
                edge.source
                for edge in graph.plan.edges
                if edge.target == target and edge.source != START
            }
            for target in targets
        }
        ready = {
            target
            for target in targets
            if target == END
            or len(inbound[target]) <= 1
            or inbound[target].issubset(completed)
        }
        return tuple(sorted(ready))

    def _check_budget(self, run_id: str, steps: int, started: float) -> None:
        if run_id in self._cancelled:
            raise GraphCancelledError(f"run {run_id!r} was cancelled")
        if steps >= self.max_steps:
            raise GraphBudgetExceededError(f"run exceeded {self.max_steps} steps")
        if (
            self.max_seconds is not None
            and time.monotonic() - started >= self.max_seconds
        ):
            raise GraphBudgetExceededError(
                f"run exceeded {self.max_seconds} wall-clock seconds"
            )

    async def _commit(
        self,
        graph: CompiledGraph,
        run_id: str,
        version: int,
        events: tuple[RunEvent, ...],
        state: Mapping[str, Any],
        frontier: tuple[str, ...],
        status: str,
        steps: int,
        completed: set[str],
    ) -> int:
        durable_state = {
            "graph_state": copy.deepcopy(dict(state)),
            "frontier": list(frontier),
            "status": status,
            "steps": steps,
            "completed": sorted(completed),
        }
        result = await self.store.commit(
            CommitRequest(run_id, version, 1, events, durable_state)
        )
        await self.checkpoint_store.put(
            Checkpoint(run_id, result.version, durable_state, graph.plan.plan_hash)
        )
        return result.version


async def _invoke(node: Node, state: dict[str, Any]) -> Any:
    result = node(state)
    if inspect.isawaitable(result):
        return await result
    return result


def _targets(edges: tuple[GraphEdge, ...], source: str) -> tuple[str, ...]:
    return tuple(sorted({edge.target for edge in edges if edge.source == source}))


def _reachable(
    edges: list[GraphEdge],
    routers: Mapping[str, tuple[Router, dict[str, str]]],
    fallbacks: Mapping[str, str],
) -> set[str]:
    outgoing: dict[str, set[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source, set()).add(edge.target)
    for source, (_, routes) in routers.items():
        outgoing.setdefault(source, set()).update(routes.values())
    for source, target in fallbacks.items():
        outgoing.setdefault(source, set()).add(target)
    seen = {START}
    pending = [START]
    while pending:
        source = pending.pop()
        for target in outgoing.get(source, ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def _callable_ref(function: Callable[..., Any]) -> str:
    module = getattr(function, "__module__", None)
    qualname = getattr(function, "__qualname__", None)
    if not module or not qualname or "<lambda>" in qualname:
        raise GraphValidationError("reducers require a stable named callable")
    return f"{module}:{qualname}"


def _matches_annotation(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return any(
            _matches_annotation(value, option) for option in get_args(annotation)
        )
    if origin is list:
        (item_type,) = get_args(annotation)
        return isinstance(value, list) and all(
            _matches_annotation(item, item_type) for item in value
        )
    if origin is dict:
        key_type, value_type = get_args(annotation)
        return isinstance(value, dict) and all(
            _matches_annotation(key, key_type) and _matches_annotation(item, value_type)
            for key, item in value.items()
        )
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True
