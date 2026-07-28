"""Executable graph engine tests."""

import asyncio
from typing import TypedDict

import pytest

from by_framework.agent import (
    END,
    START,
    GraphRunner,
    Interrupt,
    NodePolicy,
    StateGraph,
    add,
    append,
)
from by_framework.agent.graph import (
    GraphBudgetExceededError,
    GraphCancelledError,
    GraphError,
    GraphValidationError,
)
from by_framework.agent.runtime import InMemoryRunStore


class WorkflowState(TypedDict):
    count: int
    log: list
    approved: bool


def base_graph(name: str = "workflow") -> StateGraph[WorkflowState]:
    graph = StateGraph(WorkflowState, name=name)
    graph.reducer("count", add)
    graph.reducer("log", append)
    return graph


def test_graph_compiler_validates_reachability_and_has_stable_inspectable_plan():
    graph = base_graph()
    graph.add_node("increment", lambda state: {"count": 1})
    graph.add_edge(START, "increment").add_edge("increment", END)
    first = graph.compile()

    same = base_graph()
    same.add_node("increment", lambda state: {"count": 999})
    same.add_edge("increment", END).add_edge(START, "increment")
    second = same.compile()

    assert first.plan.plan_hash == second.plan.plan_hash
    assert first.plan.schema_fields["count"] == "int"
    assert first.plan.nodes[0].id == "increment"
    with pytest.raises(TypeError):
        first.plan.schema_fields["count"] = "str"
    with pytest.raises(TypeError):
        first.schema.reducers["count"] = lambda current, update: update

    invalid = base_graph()
    invalid.add_node("orphan", lambda state: {})
    invalid.add_node("entry", lambda state: {})
    invalid.add_edge(START, "entry").add_edge("entry", END)
    with pytest.raises(GraphValidationError, match="unreachable"):
        invalid.compile()

    bad_fallback = base_graph()
    bad_fallback.add_node(
        "entry", lambda state: {}, policy=NodePolicy(fallback="missing")
    )
    bad_fallback.add_edge(START, "entry").add_edge("entry", END)
    with pytest.raises(GraphValidationError, match="fallback.*unknown"):
        bad_fallback.compile()


def test_parallel_superstep_merges_in_node_id_order_and_fans_in_once():
    graph = base_graph()

    async def slow(state):
        del state
        await asyncio.sleep(0.01)
        return {"count": 2, "log": ["slow"]}

    async def fast(state):
        del state
        return {"count": 1, "log": ["fast"]}

    graph.add_node("b_slow", slow).add_node("a_fast", fast)
    graph.add_node("b_mid", lambda state: {"log": ["mid"]})

    def join(state):
        count = state["count"]
        return {"log": [f"total={count}"]}

    graph.add_node("join", join)
    graph.add_edge(START, "b_slow").add_edge(START, "a_fast")
    graph.add_edge("a_fast", "join").add_edge("b_slow", "b_mid")
    graph.add_edge("b_mid", "join")
    graph.add_edge("join", END)

    result = asyncio.run(
        GraphRunner().run(
            graph,
            {"count": 0, "log": [], "approved": False},
            run_id="parallel",
        )
    )

    assert result.state["count"] == 3
    assert result.state["log"] == ["fast", "slow", "mid", "total=3"]
    assert result.status == "completed"


def test_parallel_nodes_receive_isolated_nested_state():
    graph = base_graph("isolated")

    def mutating(state):
        state["log"].append("mutated")
        return {"log": ["a"]}

    def observing(state):
        log = state["log"]
        return {"log": [f"seen={len(log)}"]}

    graph.add_node("a_mutating", mutating).add_node("b_observing", observing)
    graph.add_node("join", lambda state: {})
    graph.add_edge(START, "a_mutating").add_edge(START, "b_observing")
    graph.add_edge("a_mutating", "join").add_edge("b_observing", "join")
    graph.add_edge("join", END)

    result = asyncio.run(
        GraphRunner().run(graph, {"count": 0, "log": [], "approved": False})
    )
    assert result.state["log"] == ["a", "seen=0"]


def test_conditional_loop_routes_until_state_condition_then_ends():
    graph = base_graph("loop")
    graph.add_node("increment", lambda state: {"count": 1})
    graph.add_conditional_edges(
        "increment",
        lambda state: "again" if state["count"] < 3 else "done",
        {"again": "increment", "done": END},
    )
    graph.add_edge(START, "increment")

    result = asyncio.run(
        GraphRunner(max_steps=5).run(graph, {"count": 0, "log": [], "approved": False})
    )
    assert result.state["count"] == 3


def test_retry_timeout_and_fallback_execute_real_node_policy():
    attempts = {"flaky": 0}
    graph = base_graph("policies")

    def flaky(state):
        del state
        attempts["flaky"] += 1
        raise RuntimeError("transient")

    async def too_slow(state):
        del state
        await asyncio.sleep(0.05)
        return {"log": ["too late"]}

    graph.add_node("flaky", flaky, policy=NodePolicy(max_attempts=2, fallback="backup"))
    graph.add_node("backup", lambda state: {"log": ["backup"]})
    graph.add_node(
        "timed",
        too_slow,
        policy=NodePolicy(timeout_seconds=0.001, fallback="timeout_backup"),
    )
    graph.add_node("timeout_backup", lambda state: {"log": ["timeout"]})
    graph.add_edge(START, "flaky").add_edge("flaky", "timed").add_edge("timed", END)
    # Fallback nodes are executable policies but also need a reachable definition
    # path for static inspection.
    graph.add_edge("backup", END).add_edge("timeout_backup", END)

    result = asyncio.run(
        GraphRunner().run(graph, {"count": 0, "log": [], "approved": False})
    )
    assert attempts["flaky"] == 2
    assert result.state["log"] == ["backup", "timeout"]


def test_subgraph_executes_as_one_parent_node():
    child = base_graph("child")
    child.add_node("child_step", lambda state: {"count": 2, "log": ["child"]})
    child.add_edge(START, "child_step").add_edge("child_step", END)

    parent = base_graph("parent")
    parent.add_subgraph("nested", child)
    parent.add_node("after", lambda state: {"log": ["parent"]})
    parent.add_edge(START, "nested").add_edge("nested", "after").add_edge("after", END)

    result = asyncio.run(
        GraphRunner().run(parent, {"count": 5, "log": [], "approved": False})
    )
    assert result.state["count"] == 7
    assert result.state["log"] == ["child", "parent"]


def test_interrupt_checkpoint_resume_replay_and_fork():
    graph = base_graph("approval")

    def approval(state):
        if not state["approved"]:
            return Interrupt("approval-1", "Approve?", "approved")
        return {"log": ["approved"]}

    graph.add_node("approval", approval)
    graph.add_node("finish", lambda state: {"log": ["finished"]})
    graph.add_edge(START, "approval").add_edge("approval", "finish")
    graph.add_edge("finish", END)
    store = InMemoryRunStore()
    runner = GraphRunner(store=store)

    interrupted = asyncio.run(
        runner.run(
            graph,
            {"count": 0, "log": [], "approved": False},
            run_id="approval-run",
        )
    )
    assert interrupted.status == "interrupted"
    checkpoint = asyncio.run(runner.replay("approval-run"))
    assert checkpoint.state["status"] == "interrupted"

    completed = asyncio.run(runner.resume("approval-run", True))
    assert completed.status == "completed"
    assert completed.state["log"] == ["approved", "finished"]

    # Version 1 is the created checkpoint and retains the initial frontier.
    forked = asyncio.run(
        runner.fork("approval-run", new_run_id="approval-fork", version=1)
    )
    assert forked.status == "interrupted"
    assert forked.run_id == "approval-fork"

    replayed = asyncio.run(runner.replay("approval-run", version=1))
    replayed.state["graph_state"]["log"].append("external mutation")
    pristine = asyncio.run(runner.replay("approval-run", version=1))
    assert pristine.state["graph_state"]["log"] == []


def test_invalid_resume_value_does_not_consume_interrupt():
    graph = base_graph("resume-validation")

    def wait_for_approval(state):
        if not state["approved"]:
            return Interrupt("key", "Approve?", "approved")
        return {}

    graph.add_node("wait", wait_for_approval)
    graph.add_edge(START, "wait").add_edge("wait", END)
    runner = GraphRunner()
    interrupted = asyncio.run(
        runner.run(
            graph,
            {"count": 0, "log": [], "approved": False},
            run_id="retry-resume",
        )
    )
    assert interrupted.status == "interrupted"

    with pytest.raises(Exception, match="requires bool"):
        asyncio.run(runner.resume("retry-resume", "yes"))

    resumed = asyncio.run(runner.resume("retry-resume", True))
    assert resumed.status == "completed"


def test_invalid_interrupt_contract_is_durably_failed():
    graph = base_graph("bad-interrupt")
    graph.add_node("wait", lambda state: Interrupt("", "Prompt", "missing"))
    graph.add_edge(START, "wait").add_edge("wait", END)
    runner = GraphRunner()

    with pytest.raises(GraphError, match="interrupt key"):
        asyncio.run(
            runner.run(
                graph,
                {"count": 0, "log": [], "approved": False},
                run_id="bad-interrupt",
            )
        )

    _, _, state, events = runner.store.snapshot("bad-interrupt")
    assert state["status"] == "failed"
    assert events[-1].kind == "RunFailed"


def test_fork_from_completed_checkpoint_is_completed_and_isolated():
    graph = base_graph("completed-fork")
    graph.add_node("done", lambda state: {"log": ["done"]})
    graph.add_edge(START, "done").add_edge("done", END)
    runner = GraphRunner()
    original = asyncio.run(
        runner.run(
            graph,
            {"count": 0, "log": [], "approved": False},
            run_id="original-complete",
        )
    )

    forked = asyncio.run(
        runner.fork(
            original.run_id,
            new_run_id="completed-fork",
            version=original.state_version,
        )
    )
    assert forked.status == "completed"
    assert forked.state == original.state


def test_step_budget_and_cancel_are_enforced():
    graph = base_graph("budget")
    graph.add_node("loop", lambda state: {"count": 1})
    graph.add_edge(START, "loop").add_edge("loop", "loop")

    with pytest.raises(GraphValidationError, match="END is not reachable"):
        graph.compile()

    cancellable = base_graph("cancel")
    cancellable.add_node("done", lambda state: {})
    cancellable.add_edge(START, "done").add_edge("done", END)
    runner = GraphRunner()
    asyncio.run(runner.cancel("cancelled"))
    with pytest.raises(GraphCancelledError):
        asyncio.run(
            runner.run(
                cancellable,
                {"count": 0, "log": [], "approved": False},
                run_id="cancelled",
            )
        )

    looping = base_graph("bounded-loop")
    looping.add_node("loop", lambda state: {"count": 1})
    looping.add_conditional_edges(
        "loop", lambda state: "loop", {"loop": "loop", "unused": END}
    )
    looping.add_edge(START, "loop")
    with pytest.raises(GraphBudgetExceededError):
        asyncio.run(
            GraphRunner(max_steps=2).run(
                looping, {"count": 0, "log": [], "approved": False}
            )
        )
