"""Embedded and server deployment composition tests."""

import asyncio
from typing import TypedDict

from by_framework.agent import (
    END,
    START,
    Agent,
    AgentServer,
    DefinitionCatalog,
    Interrupt,
    ModelResponse,
    NativeAgentWorker,
    NativeCommandService,
    NativeStepWorker,
    Runner,
    ScriptedModel,
    StateGraph,
    StepExecutorRegistry,
)
from by_framework.agent.runtime import RedisRemoteResultStore
from by_framework.agent.runtime import RedisDefinitionStore
from by_framework.core.protocol.commands import AskAgentCommand
from by_framework.core.protocol.message_header import MessageHeader


class NoopRedis:
    pass


class DefinitionRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)


class Context:
    def __init__(self):
        self.chunks = []

    async def emit_chunk(self, content):
        self.chunks.append(content)


def command(target: str, content: str, *, extra_payload=None):
    return AskAgentCommand(
        MessageHeader(
            "message-1",
            "session-1",
            "trace-1",
            target_agent_type=target,
        ),
        content,
        extra_payload=extra_payload or {},
    )


def test_same_agent_and_plan_hash_run_embedded_and_server():
    model = ScriptedModel([ModelResponse("embedded"), ModelResponse("server")])
    definition = Agent("echo", "Reply.", model)
    catalog = DefinitionCatalog()
    plan_hash = asyncio.run(catalog.register(definition))
    snapshot = catalog.snapshot()
    assert snapshot.definitions["echo"] == plan_hash

    worker = NativeAgentWorker("embedded-worker", catalog, redis_client=NoopRedis())
    context = Context()
    embedded = asyncio.run(worker.process_command(command("echo", "hello"), context))
    assert embedded.content == "embedded"
    assert embedded.metadata["native_plan_hash"] == plan_hash
    assert context.chunks == ["embedded"]

    server = AgentServer(catalog=catalog)

    async def run_server():
        await server.start()
        return await server.run("echo", "hello", snapshot=snapshot)

    served = asyncio.run(run_server())
    assert served.output == "server"
    assert served.plan_hash == plan_hash


def test_catalog_persistence_and_executable_binding_are_immutable():
    redis = DefinitionRedis()
    catalog = DefinitionCatalog(RedisDefinitionStore(redis))
    first = Agent("echo", "Same spec.", ScriptedModel([ModelResponse("first")]))
    replacement = Agent(
        "echo", "Same spec.", ScriptedModel([ModelResponse("replacement")])
    )

    async def scenario():
        first_hash = await catalog.register(first)
        second_hash = await catalog.register(replacement)
        result = await Runner().run(catalog.get("echo"), "go")
        return first_hash, second_hash, result

    first_hash, second_hash, result = asyncio.run(scenario())
    assert first_hash == second_hash
    assert result.output == "first"


class Coordinator:
    def __init__(self):
        self.results = []

    async def submit_result(self, run_id, result):
        self.results.append((run_id, result))
        return True

    async def get_result(self, run_id, lease):
        del run_id, lease
        return None

    async def validate_step_lease(self, run_id, lease):
        del run_id, lease


def test_generic_step_worker_uses_gateway_process_command_contract():
    handlers = StepExecutorRegistry()
    handlers.register(
        "sha256:plan", "increment", lambda state: {"value": state["value"] + 1}
    )
    coordinator = Coordinator()
    worker = NativeStepWorker(
        "step-worker",
        handlers,
        coordinator,
        redis_client=NoopRedis(),
    )
    payload = {
        "native_step": {
            "run_id": "run-step",
            "plan_hash": "sha256:plan",
            "node_id": "increment",
            "lease": {
                "step_id": "step-1",
                "attempt": 1,
                "run_version": 0,
                "coordinator_token": 2,
                "lease_token": 3,
            },
            "input_state": {"value": 4},
        }
    }

    result = asyncio.run(
        worker.process_command(
            command(NativeStepWorker.AGENT_TYPE, "execute", extra_payload=payload),
            Context(),
        )
    )
    assert result.reply_data == {"accepted": True, "writes": {"value": 5}}
    assert coordinator.results[0][0] == "run-step"
    assert coordinator.results[0][1].writes == {"value": 5}


def test_step_worker_deduplicates_before_handler_execution():
    calls = []
    handlers = StepExecutorRegistry()

    def handler(state):
        calls.append(state)
        return {"value": 99}

    handlers.register("sha256:plan", "node", handler)

    class DuplicateCoordinator(Coordinator):
        async def get_result(self, run_id, lease):
            del run_id
            from by_framework.agent.runtime import StepResult

            return StepResult(lease, {"value": 5})

    worker = NativeStepWorker(
        "worker",
        handlers,
        DuplicateCoordinator(),
        redis_client=NoopRedis(),
    )
    payload = {
        "native_step": {
            "run_id": "run",
            "plan_hash": "sha256:plan",
            "node_id": "node",
            "lease": {
                "step_id": "step",
                "attempt": 1,
                "run_version": 0,
                "coordinator_token": 1,
                "lease_token": 1,
            },
            "input_state": {"value": 1},
        }
    }
    result = asyncio.run(
        worker.process_command(
            command(NativeStepWorker.AGENT_TYPE, "execute", extra_payload=payload),
            Context(),
        )
    )
    assert result.reply_data == {"accepted": False, "writes": {"value": 5}}
    assert calls == []


class ApprovalState(TypedDict):
    approved: bool
    output: str


class HashRedis:
    def __init__(self):
        self.values = {}

    async def hsetnx(self, key, field, value):
        fields = self.values.setdefault(key, {})
        if field in fields:
            return False
        fields[field] = value
        return True

    async def hget(self, key, field):
        return self.values.get(key, {}).get(field)


def test_remote_result_automatically_resumes_interrupted_server_graph():
    graph = StateGraph(ApprovalState, name="approval")
    graph.add_node(
        "wait",
        lambda state: (
            {}
            if state["approved"]
            else Interrupt("remote-1", "Await remote agent", "approved")
        ),
    )
    graph.add_node("finish", lambda state: {"output": "done"})
    graph.add_edge(START, "wait").add_edge("wait", "finish").add_edge("finish", END)
    redis = HashRedis()
    remote = RedisRemoteResultStore(redis)
    server = AgentServer(remote_results=remote)

    async def scenario():
        await server.start()
        interrupted = await server.run_graph(
            graph,
            {"approved": False, "output": ""},
            run_id="graph-run",
        )
        resumed = await server.resolve_remote_result("graph-run", "remote-1", True)
        duplicate = await server.resolve_remote_result("graph-run", "remote-1", True)
        return interrupted, resumed, duplicate

    interrupted, resumed, duplicate = asyncio.run(scenario())
    assert interrupted.status == "interrupted"
    assert resumed.status == "completed"
    assert resumed.state == {"approved": True, "output": "done"}
    assert duplicate == resumed


def test_callable_command_service_covers_server_lifecycle():
    server = AgentServer()
    commands = NativeCommandService(server)

    async def scenario():
        await commands.start()
        plan_hash = await commands.register(
            Agent("echo", "", ScriptedModel([ModelResponse("ok")]))
        )
        run_id = await commands.run("echo", "hello")
        record = server.status(run_id)
        if record.task is not None:
            await record.task
        return plan_hash, commands.status(run_id)

    plan_hash, status = asyncio.run(scenario())
    assert plan_hash.startswith("sha256:")
    assert status["status"] == "completed"


def test_server_records_graph_failure_and_does_not_cancel_terminal_run():
    class FailureState(TypedDict):
        value: int

    graph = StateGraph(FailureState, name="failure")

    def fail(state):
        del state
        raise RuntimeError("boom")

    graph.add_node("fail", fail)
    graph.add_edge(START, "fail").add_edge("fail", END)
    server = AgentServer()

    async def scenario():
        await server.start()
        try:
            await server.run_graph(graph, {"value": 0}, run_id="failed-graph")
        except RuntimeError:
            pass
        before = server.status("failed-graph").status
        await server.cancel("failed-graph")
        return before, server.status("failed-graph")

    before, after = asyncio.run(scenario())
    assert before == "failed"
    assert after.status == "failed"
    assert after.error == "boom"
