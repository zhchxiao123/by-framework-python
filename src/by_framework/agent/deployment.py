"""Embedded Worker and server composition for native definitions."""

import asyncio
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from by_framework.core.protocol.commands import AskAgentCommand, GatewayCommand
from by_framework.core.protocol.results import AgentTaskResult
from by_framework.worker.worker import GatewayWorker

from .definition import Agent, CompiledAgent
from .execution import Runner
from .graph import CompiledGraph, GraphRunResult, GraphRunner, StateGraph
from .runtime.coordinator import StepLease, StepResult
from .runtime.context import RunContext, RunIdentity
from .runtime.distributed import (
    RedisCoordinatorStore,
    RedisDefinitionStore,
    RedisRemoteResultStore,
)
from .runtime.serialization import canonical_json, stable_plan_hash


class DeploymentError(RuntimeError):
    """Native deployment composition error."""


def worker_run_context(
    context,
    command: AskAgentCommand,
    *,
    run_id: str,
    agent_id: str,
) -> RunContext:
    """Adapt an AgentContext without coupling the native runtime to Worker."""
    runtime_state = getattr(context, "agent_runtime_state", None)
    if runtime_state is None:
        return RunContext(
            RunIdentity(
                command.header.session_id,
                run_id,
                agent_id,
                command.header.user_code or "default",
                command.header.user_name,
                {"trace_id": command.header.trace_id},
            )
        )
    session = runtime_state.session_manager
    return RunContext(
        RunIdentity(
            session.session_id,
            run_id,
            agent_id,
            session.user_code or "default",
            session.user_name or "",
            {
                "trace_id": command.header.trace_id,
                "parent_message_id": command.header.parent_message_id,
                "trace_parent_span_id": command.header.trace_parent_span_id,
                "langfuse_parent_observation_id": (
                    command.header.langfuse_parent_observation_id
                ),
            },
        ),
        private_files=session.private_file_manager,
        shared_files=session.shared_file_manager,
        conversation=session.history,
        agent_configs=runtime_state.config_manager,
    )


@dataclass(frozen=True)
class CatalogSnapshot:
    snapshot_hash: str
    definitions: Mapping[str, str]


class DefinitionCatalog:
    """In-process executable catalog with immutable definition snapshots."""

    def __init__(self, definition_store: RedisDefinitionStore | None = None):
        self._agents: dict[str, CompiledAgent] = {}
        self._definition_store = definition_store

    async def register(self, agent: Agent | CompiledAgent) -> str:
        compiled = agent.compile() if isinstance(agent, Agent) else agent
        existing = self._agents.get(compiled.spec.name)
        if existing is not None and existing.plan.plan_hash != compiled.plan.plan_hash:
            raise DeploymentError(
                f"agent {compiled.spec.name!r} is already registered differently"
            )
        if self._definition_store is not None:
            persisted_definition = json.loads(
                canonical_json(compiled.plan).decode("utf-8")
            )
            persisted_definition["plan_hash"] = ""
            persisted_hash = await self._definition_store.put(persisted_definition)
            if persisted_hash != compiled.plan.plan_hash:
                raise DeploymentError("persisted definition hash does not match plan")
        if existing is None:
            self._agents[compiled.spec.name] = compiled
        return compiled.plan.plan_hash

    def get(self, name: str, snapshot: CatalogSnapshot | None = None) -> CompiledAgent:
        try:
            agent = self._agents[name]
        except KeyError as exc:
            raise DeploymentError(f"agent {name!r} is not registered") from exc
        if (
            snapshot is not None
            and snapshot.definitions.get(name) != agent.plan.plan_hash
        ):
            raise DeploymentError(
                f"agent {name!r} does not match bound catalog snapshot"
            )
        return agent

    def snapshot(self) -> CatalogSnapshot:
        definitions = {
            name: agent.plan.plan_hash for name, agent in sorted(self._agents.items())
        }
        snapshot_hash = stable_plan_hash(
            {"schema_version": 1, "definitions": definitions}
        )
        return CatalogSnapshot(snapshot_hash, MappingProxyType(definitions))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))


class NativeAgentWorker(GatewayWorker):
    """Embedded native Agent entry point reusing ``GatewayWorker`` lifecycle."""

    def __init__(
        self,
        worker_id: str,
        catalog: DefinitionCatalog,
        *,
        runner: Runner | None = None,
        **kwargs,
    ):
        super().__init__(worker_id, **kwargs)
        self.catalog = catalog
        self.runner = runner or Runner()

    def get_agent_types(self) -> list[str]:
        return list(self.catalog.names())

    async def process_command(
        self, command: GatewayCommand, context
    ) -> AgentTaskResult:
        if not isinstance(command, AskAgentCommand):
            raise DeploymentError("NativeAgentWorker accepts AskAgentCommand only")
        target = command.header.target_agent_type
        snapshot = self.catalog.snapshot()
        agent = self.catalog.get(target, snapshot)
        content = command.content
        if not isinstance(content, str):
            raise DeploymentError("native Agent input must be text")
        run_id = f"run-{command.header.session_id}-{command.header.message_id}"
        run_context = worker_run_context(
            context, command, run_id=run_id, agent_id=agent.spec.name
        )
        stream = self.runner.run_streamed(
            agent,
            content,
            run_id=run_id,
            context=run_context,
        )
        async for event in stream:
            if event.kind == "text_delta":
                await context.emit_chunk(event.data["text"])
        result = await stream.result()
        return AgentTaskResult(
            content=result.output,
            reply_data={"output": result.output, "run_id": result.run_id},
            metadata={
                "native_plan_hash": result.plan_hash,
                "catalog_snapshot_hash": snapshot.snapshot_hash,
            },
        )


StepHandler = Callable[
    [dict[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]
]


class StepExecutorRegistry:
    """Executable node registry resolved by immutable plan hash and node ID."""

    def __init__(self):
        self._handlers: dict[tuple[str, str], StepHandler] = {}

    def register(self, plan_hash: str, node_id: str, handler: StepHandler) -> None:
        key = (plan_hash, node_id)
        if key in self._handlers and self._handlers[key] is not handler:
            raise DeploymentError(f"step handler {key!r} is already registered")
        self._handlers[key] = handler

    async def execute(
        self, plan_hash: str, node_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            handler = self._handlers[(plan_hash, node_id)]
        except KeyError as exc:
            raise DeploymentError(
                f"step handler {(plan_hash, node_id)!r} is not registered"
            ) from exc
        result = handler(state)
        if inspect.isawaitable(result):
            result = await result
        return dict(result)


class NativeStepWorker(GatewayWorker):
    """Generic native step executor on the existing WorkerRunner consume path."""

    AGENT_TYPE = "native-step"

    def __init__(
        self,
        worker_id: str,
        handlers: StepExecutorRegistry,
        coordinator_store: RedisCoordinatorStore,
        **kwargs,
    ):
        super().__init__(worker_id, **kwargs)
        self.handlers = handlers
        self.coordinator_store = coordinator_store

    def get_agent_types(self) -> list[str]:
        return [self.AGENT_TYPE]

    async def process_command(
        self,
        command: GatewayCommand,
        context,  # pylint: disable=unused-argument
    ) -> AgentTaskResult:
        if not isinstance(command, AskAgentCommand):
            raise DeploymentError("NativeStepWorker accepts AskAgentCommand only")
        payload = command.extra_payload.get("native_step")
        if not isinstance(payload, dict):
            raise DeploymentError("missing native_step payload")
        lease = StepLease(**payload["lease"])
        existing = await self.coordinator_store.get_result(payload["run_id"], lease)
        if existing is not None:
            return AgentTaskResult(
                reply_data={"accepted": False, "writes": existing.writes},
                metadata={
                    "native_run_id": payload["run_id"],
                    "native_step_id": lease.step_id,
                },
            )
        await self.coordinator_store.validate_step_lease(payload["run_id"], lease)
        writes = await self.handlers.execute(
            payload["plan_hash"], payload["node_id"], dict(payload["input_state"])
        )
        accepted = await self.coordinator_store.submit_result(
            payload["run_id"], StepResult(lease, writes)
        )
        return AgentTaskResult(
            reply_data={"accepted": accepted, "writes": writes},
            metadata={
                "native_run_id": payload["run_id"],
                "native_step_id": lease.step_id,
            },
        )


@dataclass
class ServerRun:
    run_id: str
    status: str
    task: asyncio.Task | None = None
    result: Any = None
    error: str = ""


class AgentServer:
    """Composition root for catalog, coordinators, and generic step services."""

    def __init__(
        self,
        *,
        catalog: DefinitionCatalog | None = None,
        runner: Runner | None = None,
        graph_runner: GraphRunner | None = None,
        remote_results: RedisRemoteResultStore | None = None,
        coordinator_store: RedisCoordinatorStore | None = None,
        step_handlers: StepExecutorRegistry | None = None,
    ):
        self.catalog = catalog or DefinitionCatalog()
        self.runner = runner or Runner()
        self.graph_runner = graph_runner or GraphRunner()
        self.remote_results = remote_results
        self.coordinator_store = coordinator_store
        self.step_handlers = step_handlers or StepExecutorRegistry()
        self.runs: dict[str, ServerRun] = {}
        self.started = False

    async def start(self) -> None:
        self.started = True

    def create_step_worker(self, worker_id: str, *, redis_client) -> NativeStepWorker:
        """Compose a generic step Worker without creating another consume loop."""
        if self.coordinator_store is None:
            raise DeploymentError("coordinator store is not configured")
        return NativeStepWorker(
            worker_id,
            self.step_handlers,
            self.coordinator_store,
            redis_client=redis_client,
        )

    async def register(self, agent: Agent | CompiledAgent) -> str:
        return await self.catalog.register(agent)

    async def run(
        self,
        agent_name: str,
        content: str,
        *,
        run_id: str | None = None,
        snapshot: CatalogSnapshot | None = None,
    ):
        self._require_started()
        resolved = run_id or f"run-{uuid.uuid4().hex}"
        if resolved in self.runs:
            raise DeploymentError(f"run {resolved!r} already exists")
        record = ServerRun(resolved, "running")
        self.runs[resolved] = record
        try:
            record.result = await self.runner.run(
                self.catalog.get(agent_name, snapshot), content, run_id=resolved
            )
            record.status = "completed"
            return record.result
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            raise

    async def submit_run(self, agent_name: str, content: str) -> str:
        run_id = f"run-{uuid.uuid4().hex}"
        task = asyncio.create_task(self.run(agent_name, content, run_id=run_id))
        task.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )
        await asyncio.sleep(0)
        self.runs[run_id].task = task
        return run_id

    async def run_graph(
        self,
        graph: StateGraph | CompiledGraph,
        state: Mapping[str, Any],
        *,
        run_id: str | None = None,
    ) -> GraphRunResult:
        self._require_started()
        resolved = run_id or f"run-{uuid.uuid4().hex}"
        if resolved in self.runs:
            raise DeploymentError(f"run {resolved!r} already exists")
        record = ServerRun(resolved, "running")
        self.runs[resolved] = record
        try:
            result = await self.graph_runner.run(graph, state, run_id=resolved)
            record.status = result.status
            record.result = result
            if (
                result.status == "interrupted"
                and result.interrupt is not None
                and self.remote_results is not None
            ):
                await self.remote_results.create(result.run_id, result.interrupt.key)
            return result
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            raise

    async def resolve_remote_result(
        self, run_id: str, correlation_id: str, value: Any
    ) -> GraphRunResult:
        if self.remote_results is None:
            raise DeploymentError("remote result store is not configured")
        record = self.runs.get(run_id)
        if record is None or not isinstance(record.result, GraphRunResult):
            raise DeploymentError("remote resume has no recorded graph result")
        if (
            record.result.status != "interrupted"
            or record.result.interrupt is None
            or record.result.interrupt.key != correlation_id
        ):
            if record.result.status != "interrupted":
                existing = await self.remote_results.get(run_id, correlation_id)
                if existing == value:
                    return record.result
            raise DeploymentError(
                f"correlation {correlation_id!r} does not match the active interrupt"
            )
        accepted = await self.remote_results.resolve(run_id, correlation_id, value)
        if not accepted:
            # A prior process-local resume may have failed after durable result
            # resolution. Retrying re-enters GraphRunner while suspension exists.
            if record.result.status != "interrupted":
                return record.result
        result = await self.graph_runner.resume(run_id, value)
        self.runs[run_id] = ServerRun(run_id, result.status, result=result)
        return result

    def status(self, run_id: str) -> ServerRun:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise DeploymentError(f"run {run_id!r} was not found") from exc

    async def cancel(self, run_id: str) -> None:
        record = self.status(run_id)
        if record.status in {"completed", "failed", "cancelled"}:
            return
        if record.task is not None and not record.task.done():
            record.task.cancel()
        await self.graph_runner.cancel(run_id)
        record.status = "cancelled"

    def _require_started(self) -> None:
        if not self.started:
            raise DeploymentError("AgentServer is not started")


class NativeCommandService:
    """Callable command layer for start/register/run/status/cancel/resume."""

    def __init__(self, server: AgentServer):
        self.server = server

    async def start(self) -> None:
        await self.server.start()

    async def register(self, agent: Agent | CompiledAgent) -> str:
        return await self.server.register(agent)

    async def run(self, agent_name: str, content: str) -> str:
        return await self.server.submit_run(agent_name, content)

    def status(self, run_id: str) -> dict[str, Any]:
        record = self.server.status(run_id)
        return {
            "run_id": record.run_id,
            "status": record.status,
            "error": record.error,
        }

    async def cancel(self, run_id: str) -> None:
        await self.server.cancel(run_id)

    async def resume(
        self, run_id: str, correlation_id: str, value: Any
    ) -> GraphRunResult:
        return await self.server.resolve_remote_result(run_id, correlation_id, value)
