"""
Agent context module.

Provides the AgentContext class which serves as the runtime context for agent
task execution, providing access to session state, event emission,
and inter-agent communication.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from typing_extensions import deprecated

from by_framework.common.constants import (
    EXECUTION_ID_PREFIX,
    MESSAGE_ID_PREFIX,
    TASK_GROUP_FIELD_COMPLETED,
    TASK_GROUP_FIELD_SOURCE_AGENT,
    TASK_GROUP_FIELD_TOTAL,
    TASK_GROUP_ID_PREFIX,
    TASK_GROUP_TTL_SECONDS,
    RedisKeys,
)
from by_framework.common.emitter import DataLayoutBuilder, GatewayDataEmitter
from by_framework.common.logger import logger
from by_framework.common.metrics import REGISTRY_FAILURES_COUNTER, record_failure
from by_framework.common.redis_client import Redis, get_redis
from by_framework.core.availability import (
    AvailabilityRouter,
    AvailabilityStatus,
    DeliveryIntent,
    RoutePolicy,
)
from by_framework.core.extensions import AgentConfig
from by_framework.core.protocol.agent_state import AgentState
from by_framework.core.protocol.commands import AskAgentCommand, ResumeCommand
from by_framework.core.protocol.content_codec import ContentCodec, WireContent
from by_framework.core.protocol.content_type import SseReasonMessageType
from by_framework.core.protocol.event_type import EventType
from by_framework.core.protocol.events import (
    ArtifactEvent,
    AskUserEvent,
    StateChangeEvent,
    StreamChunkEvent,
)
from by_framework.core.protocol.message_header import MessageHeader
from by_framework.core.runtime import AgentRuntimeState
from by_framework.core.runtime.file_permissions import FilePermissionPolicy
from by_framework.core.runtime.filestore.base import FileStorage
from by_framework.worker._response_buffer import ResponseBuffer

if TYPE_CHECKING:
    from by_framework.core.extensions import PluginRegistry


# Context variable for tracking current (message_id, parent_message_id)
_current_ids_var: ContextVar[tuple[str, str]] = ContextVar(
    "_current_ids_var", default=("", "")
)

# Exception buckets used to discriminate "network" vs "schema/protocol"
# failures when downgrading past registry / execution-tracking errors.
# Defined as tuples so they can be used directly in ``except`` clauses
# without a custom base class. ``redis.ResponseError`` /
# ``redis.exceptions.ConnectionError`` / ``redis.exceptions.DataError``
# are imported lazily inside call sites that need them, because the
# ``redis`` package may not be installed in every test environment.
def _build_registry_error_buckets() -> tuple[
    tuple[type[BaseException], ...],
    tuple[type[BaseException], ...],
]:
    """Return (network_bucket, schema_bucket) for registry-call downgrade paths.

    Splitting the buckets lets the call site log a different message
    depending on whether the registry was unreachable (transient, can
    retry next request) or rejected the payload (almost certainly a
    protocol/schema bug we want to see immediately).
    """
    schema_bucket: list[type[BaseException]] = [
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
    ]
    try:
        from redis.exceptions import (  # type: ignore[import-not-found]
            ConnectionError as RedisConnectionError,
            DataError,
            ResponseError,
            TimeoutError as RedisTimeoutError,
        )

        network_bucket: tuple[type[BaseException], ...] = (
            asyncio.TimeoutError,
            ConnectionError,
            RedisConnectionError,
            RedisTimeoutError,
        )
        schema_bucket.extend([ResponseError, DataError])
    except ImportError:
        # ``redis`` is not installed; fall back to stdlib only.
        network_bucket = (asyncio.TimeoutError, ConnectionError)
    return tuple(network_bucket), tuple(schema_bucket)


_RegistryNetworkError, _RegistrySchemaError = _build_registry_error_buckets()


class AgentContext:
    """Agent runtime context.

    Provides access to session state, event emission, and inter-agent
    communication during task execution.

    Args:
        session_id: Session ID
        trace_id: Trace ID
        redis_client: Redis client instance
        data_stream_name: Data stream name
        current_agent_id: Current agent ID
        current_message_id: Current message ID
        current_command: Current command
        cancel_event: Cancel event
        cancel_reason: Cancel reason
        plugin_registry: Plugin registry
    """

    def __init__(
        self,
        session_id: str,
        trace_id: str,
        redis_client: Optional[Redis] = None,
        data_stream_name: Optional[str] = None,
        current_agent_id: str = "",
        message_id: str = "",
        parent_message_id: str = "",
        current_command: Optional[Any] = None,
        cancel_event: Optional[asyncio.Event] = None,
        cancel_reason: str = "",
        plugin_registry: Optional[PluginRegistry] = None,
        user_code: Optional[str] = None,
        user_name: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        agent_configs: Optional[list[AgentConfig]] = None,
        agent_configs_version: int = 0,
        storage: Optional[FileStorage] = None,
        permission_policy: Optional[FilePermissionPolicy] = None,
        content_codec: Optional[ContentCodec] = None,
        layout_builder: Optional[DataLayoutBuilder] = None,
        is_sub_agent: bool = False,
    ):
        self.redis = redis_client or get_redis()
        self.session_id = session_id
        self.trace_id = trace_id
        self.data_stream_name = data_stream_name
        self.current_agent_id = current_agent_id

        # Record initial IDs
        self._initial_message_id = message_id
        self._initial_parent_message_id = parent_message_id

        # Initialize IDs for current execution context
        # Note: Here we set the initial ID for the current coroutine
        _current_ids_var.set((message_id, parent_message_id))

        self.current_command = current_command
        self.cancel_event = cancel_event
        self.cancel_reason = cancel_reason
        self.emitter = GatewayDataEmitter(
            self.redis, data_stream_name, layout_builder=layout_builder
        )
        self.content_codec = content_codec
        self.plugin_registry = plugin_registry
        self.agent_configs_version = agent_configs_version
        self.is_sub_agent = is_sub_agent

        # New: AgentRuntimeState for unified state management
        self._agent_runtime_state = AgentRuntimeState(
            session_id=session_id,
            user_code=user_code,
            user_name=user_name,
            storage=storage,
            workspace_dir=workspace_dir,
            agent_configs=agent_configs,
            permission_policy=permission_policy,
            agent_id=current_agent_id,
        )

        # Streaming response buffer + per-stream lifecycle flags.
        # parent_message_id is read via a callable because it is stored
        # in a ContextVar and may change during this context's lifetime
        # (see AgentContext.parent_message_id setter).
        self._buffer = ResponseBuffer(
            history=self._agent_runtime_state.session_manager.history,
            trace_id=self.trace_id,
            agent_id=self.current_agent_id,
            parent_message_id_provider=lambda: self.parent_message_id,
        )

    @property
    def message_id(self) -> str:
        """Get the current context's message ID."""
        return _current_ids_var.get()[0]

    @message_id.setter
    def message_id(self, value: str) -> None:
        """Manually set the current context's message ID."""
        _, p = _current_ids_var.get()
        _current_ids_var.set((value, p))

    @property
    def parent_message_id(self) -> str:
        """Get the current context's parent message ID."""
        return _current_ids_var.get()[1]

    @parent_message_id.setter
    def parent_message_id(self, value: str) -> None:
        """Manually set the current context's parent message ID."""
        m, _ = _current_ids_var.get()
        _current_ids_var.set((m, value))

    @property
    def initial_message_id(self) -> str:
        """Get the original task's message ID."""
        return self._initial_message_id

    @property
    def initial_parent_message_id(self) -> str:
        """Get the original task's parent message ID."""
        return self._initial_parent_message_id

    @asynccontextmanager
    async def sub_step(
        self,
        title: str,
        content_type: str = SseReasonMessageType.think_text.value,
        event_type: str = EventType.REASONING_LOG_DELTA.value,
    ):
        """Start a hierarchical internal sub-step.

        This context manager automatically:
        1. Generate a new sub_id.
        2. Send a status message identifying the start of this step.
        3. Automatically switch the current context's message_id within the block.

        Args:
            title: Step name
        """
        sub_id = self.generate_message_id()
        parent_id = self.message_id

        # 1. Emit hierarchical title (usually displayed as reasoning log)
        await self.emit_chunk(
            event=title,
            message_id=sub_id,
            parent_message_id=parent_id,
            content_type=content_type,
            event_type=event_type,
        )

        # 2. Push new ID context
        token = _current_ids_var.set((sub_id, parent_id))
        try:
            yield sub_id, parent_id
        finally:
            # 3. Restore old ID context
            _current_ids_var.reset(token)

    def get_trace_stack(self) -> List[tuple[str, str]]:
        """Get the current ID trace stack."""
        # ContextVar doesn't support stack traversal; return current pair.
        return [_current_ids_var.get()]

    @property
    def agent_runtime_state(self) -> AgentRuntimeState:
        """Get the unified agent runtime state container.

        Provides access to:
        - session_manager: Session management and file management
        - config_manager: Agent configuration management

        Returns:
            AgentRuntimeState instance
        """
        return self._agent_runtime_state

    @property
    def agent_configs(self) -> list[AgentConfig]:
        """Get the list of agent configurations.

        Deprecated: Use agent_runtime_state.config_manager.list_configs() instead.
        """
        return self._agent_runtime_state.config_manager.list_configs()

    @deprecated("use agent_runtime_state.config_manager instead")
    def set_agent_configs(self, new_configs: list[AgentConfig]) -> None:
        self._agent_runtime_state.config_manager.set_configs(new_configs)

    @deprecated("use agent_runtime_state.config_manager instead")
    def get_agent_config(self, agent_id: str) -> AgentConfig | None:
        return self._agent_runtime_state.config_manager.get_config(agent_id)

    @deprecated("use agent_runtime_state.config_manager instead")
    def list_agent_configs(self) -> list[AgentConfig]:
        return self._agent_runtime_state.config_manager.list_configs()

    def is_cancel_requested(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    # ------------------------------------------------------------------
    # Backward-compatible shims for the legacy ``_response_buffer`` /
    # ``_is_*`` / ``_permission_transferred`` attributes. The state now
    # lives in ``self._buffer`` (a ``ResponseBuffer``); these properties
    # delegate to it so that external readers and writers (worker.py,
    # processor.py, test_gateway_worker.py) keep working unchanged.
    # ------------------------------------------------------------------
    @property
    def _response_buffer(self) -> List[str]:
        """Return the underlying response chunks list.

        Retained for backward compatibility. Callers should not mutate
        the returned list; new code should go through
        ``self._buffer`` / ``self._buffer.append``.
        """
        return self._buffer.chunks()

    @property
    def _is_history_saved(self) -> bool:
        return self._buffer.is_history_saved()

    @property
    def _is_stream_finished(self) -> bool:
        return self._buffer.is_finished()

    @_is_stream_finished.setter
    def _is_stream_finished(self, value: bool) -> None:
        if value:
            self._buffer.mark_finished()

    @property
    def _permission_transferred(self) -> bool:
        return self._buffer.is_permission_transferred()

    @_permission_transferred.setter
    def _permission_transferred(self, value: bool) -> None:
        if value:
            self._buffer.mark_permission_transferred()

    @property
    def _is_suspended(self) -> bool:
        return self._buffer.is_suspended()

    @_is_suspended.setter
    def _is_suspended(self, value: bool) -> None:
        if value:
            self._buffer.mark_suspended()

    async def check_cancelled(self) -> None:
        if self.is_cancel_requested():
            raise asyncio.CancelledError(self.cancel_reason or "task cancelled")

    async def get_active_workers(self) -> Dict[str, Any]:
        """
        Get all active workers and their capability information in the cluster
        """
        from by_framework.core.registry import WorkerRegistry

        registry = WorkerRegistry(self.redis)
        return await registry.get_all_workers()

    def generate_message_id(self) -> str:
        """Generate a new message ID.

        Uses the predefined MESSAGE_ID_PREFIX and UUID fragment.
        """
        return f"{MESSAGE_ID_PREFIX}{uuid.uuid4().hex[:8]}"

    async def emit_chunk(
        self,
        event: Union[StreamChunkEvent, str],
        event_type: Optional[str] = None,
        content_type: Optional[str] = None,
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
    ) -> None:
        """Emit a streaming chunk event.

        Sub-agent content becomes reasoning logs; otherwise defaults to ANSWER_DELTA.
        """
        # Forced policy: sub-agents emit reasoning logs, else use ANSWER_DELTA
        if self.is_sub_agent:
            event_type = EventType.REASONING_LOG_DELTA.value
        elif event_type is None:
            event_type = EventType.ANSWER_DELTA.value

        # 1. Collect content
        content = ""
        if isinstance(event, StreamChunkEvent):
            content = event.content or ""
        elif isinstance(event, str):
            content = event

        if content:
            self._buffer.append(content)

        # Check if this is a stream end marker
        if event_type == EventType.APP_STREAM_RESPONSE.value:
            # Permission: if invoked by another agent, must return to caller
            is_agent_return = isinstance(self.current_command, ResumeCommand)
            has_source_agent = (
                self.current_command
                and bool(self.current_command.header.source_agent_type)
                and not is_agent_return
            )
            if has_source_agent:
                logger.warning(
                    "[%s] Agent %s attempted APP_STREAM_RESPONSE, "
                    "but permission held by caller. Event dropped.",
                    self.trace_id,
                    self.current_agent_id,
                )
                await self._buffer.flush_to_history()
                return

            self._buffer.mark_finished()

        # 2. Send raw chunk
        await self.emitter.emit_chunk(
            self.session_id,
            self.trace_id,
            event,
            self.current_agent_id,
            message_id=message_id if message_id else self.message_id,
            parent_message_id=parent_message_id
            if parent_message_id
            else self.parent_message_id,
            event_type=event_type,
            content_type=content_type,
        )

        # 3. If it's a stream end marker, trigger persistence to history
        if event_type == EventType.APP_STREAM_RESPONSE.value:
            await self._buffer.flush_to_history()

    async def flush_to_history(self) -> None:
        """Persist the current buffer content as an assistant reply to history"""
        await self._buffer.flush_to_history()

    async def emit_state(
        self,
        event: Union[StateChangeEvent, str],
        event_type: Optional[str] = None,
        content_type: Optional[str] = None,
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
    ) -> None:
        await self.emitter.emit_state(
            self.session_id,
            self.trace_id,
            event,
            self.current_agent_id,
            message_id=message_id if message_id else self.message_id,
            parent_message_id=parent_message_id
            if parent_message_id
            else self.parent_message_id,
            event_type=event_type,
            content_type=content_type,
        )

    async def emit_artifact(
        self,
        event: Union[ArtifactEvent, str],
        event_type: Optional[str] = None,
        content_type: Optional[str] = None,
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
    ) -> None:
        await self.emitter.emit_artifact(
            self.session_id,
            self.trace_id,
            event,
            self.current_agent_id,
            message_id=message_id if message_id else self.message_id,
            parent_message_id=parent_message_id
            if parent_message_id
            else self.parent_message_id,
            event_type=event_type,
            content_type=content_type,
        )

    async def ask_user(
        self,
        event: Union[AskUserEvent, str],
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
    ) -> dict:
        """
        Suspend execution and ask the user for a prompt.
        Accepts an AskUserEvent or a raw string prompt.
        """
        await self.emitter.ask_user(
            self.session_id,
            self.trace_id,
            event,
            self.current_agent_id,
            message_id=message_id if message_id else self.message_id,
            parent_message_id=parent_message_id
            if parent_message_id
            else self.parent_message_id,
        )
        self._buffer.mark_suspended()
        return {"status": AgentState.WAITING_USER.value}

    async def update_execution_state(self, status: str) -> None:
        """Update the underlying execution state of the current task.

        Does not mix state data into the data stream returned to the frontend;
        operates entirely on the control channel.

        Failures are downgraded (logged as warnings + recorded on
        ``REGISTRY_FAILURES_COUNTER``) rather than raised, because the
        control-channel state is an observability aid and must never
        break a running agent.
        """
        from by_framework.core.registry import WorkerRegistry

        registry = WorkerRegistry(self.redis)
        if not hasattr(registry, "update_execution_status_by_message"):
            logger.info(
                "[%s] registry lacks update_execution_status_by_message; "
                "skipping execution state update for status=%s",
                self.trace_id,
                status,
            )
            return

        try:
            await registry.update_execution_status_by_message(
                self.message_id, self.session_id, status
            )
        except _RegistryNetworkError as net_err:
            # Network / connectivity — almost always transient. We continue
            # because the caller's primary work is unaffected.
            record_failure(
                REGISTRY_FAILURES_COUNTER,
                operation="update_execution_state",
                error=net_err,
            )
            logger.warning(
                "[%s] registry unreachable while updating execution state; "
                "continuing without execution tracking. status=%s err=%s",
                self.trace_id,
                status,
                net_err,
            )
        except _RegistrySchemaError as schema_err:
            # Schema / protocol mismatch with the registry backend. Not
            # transient; we surface the details but do not break the agent.
            record_failure(
                REGISTRY_FAILURES_COUNTER,
                operation="update_execution_state",
                error=schema_err,
            )
            logger.warning(
                "[%s] registry rejected execution state update "
                "(schema/protocol error); continuing. status=%s err=%s",
                self.trace_id,
                status,
                schema_err,
            )

    async def call_agent(
        self,
        target_agent_type: str,
        content: object,
        extra_payload: Optional[Dict[str, Any]] = None,
        wait_for_reply: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        route_policy: str = RoutePolicy.FAIL_FAST,
        availability_timeout_ms: int = 30000,
        region: Optional[str] = None,
        priority: int = 0,
    ) -> dict:
        """Push a control-flow message to another agent.

        If wait_for_reply is True, source_agent_type is injected for routing.

        Args:
            route_policy: Controls online checks and unavailable-agent behavior.
        """
        message_id = message_id or self.generate_message_id()
        parent_message_id = parent_message_id if parent_message_id else self.message_id
        merged_extra_payload = dict(extra_payload or {})
        if wait_for_reply:
            merged_extra_payload["wait_for_reply"] = True
            self._buffer.mark_suspended()
        else:
            self._buffer.mark_permission_transferred()

        serialized_content = self._serialize_outbound_content(content)

        command = AskAgentCommand(
            header=MessageHeader(
                message_id=message_id,
                session_id=self.session_id,
                trace_id=self.trace_id,
                source_agent_type=self.current_agent_id if wait_for_reply else "",
                target_agent_type=target_agent_type,
                parent_message_id=parent_message_id,
                user_code=self.agent_runtime_state.session_manager.user_code,
                user_name=self.agent_runtime_state.session_manager.user_name,
                metadata=metadata or {},
            ),
            content=serialized_content,
            wait_for_reply=wait_for_reply,
            extra_payload={
                k: v for k, v in merged_extra_payload.items() if k != "wait_for_reply"
            },
        )
        execution_id = f"{EXECUTION_ID_PREFIX}{uuid.uuid4().hex[:8]}"

        availability = await AvailabilityRouter(self.redis).prepare_delivery(
            DeliveryIntent(
                execution_id=execution_id,
                message_id=message_id,
                session_id=self.session_id,
                trace_id=self.trace_id,
                source=self.current_agent_id,
                target_agent_type=target_agent_type,
                user_code=self.agent_runtime_state.session_manager.user_code,
                region=region or "",
                priority=priority,
                policy=route_policy,
                timeout_ms=availability_timeout_ms,
                command_payload=command.to_dict(),
                metadata=metadata or {},
            )
        )
        if availability.status not in (
            AvailabilityStatus.DELIVER_NOW,
            AvailabilityStatus.WAIT_AND_DELIVER,
            AvailabilityStatus.FALLBACK_TO_OTHER_AGENT_TYPE,
            AvailabilityStatus.QUEUE_PENDING,
        ):
            return {
                "status": AgentState.FAILED.value,
                "message_id": "",
                "parent_message_id": parent_message_id or "",
                "target_agent_type": target_agent_type,
                "error": availability.error,
                "error_code": availability.error_code or "AGENT_TYPE_UNAVAILABLE",
            }
        should_dispatch_control = (
            availability.status != AvailabilityStatus.QUEUE_PENDING
        )
        if availability.selected_agent_type:
            target_agent_type = availability.selected_agent_type
            command.header.target_agent_type = availability.selected_agent_type
        delivery_stream = availability.stream_name or RedisKeys.ctrl_stream(
            target_agent_type
        )

        if self.plugin_registry:
            await self.plugin_registry.on_call_agent_start(self, command)

        from by_framework.core.registry import WorkerRegistry

        registry = WorkerRegistry(self.redis)
        if hasattr(registry, "initialize_execution"):
            try:
                await registry.initialize_execution(
                    {
                        "execution_id": execution_id,
                        "message_id": message_id,
                        "session_id": self.session_id,
                        "trace_id": self.trace_id,
                        "parent_message_id": parent_message_id or "",
                        "source_agent_type": self.current_agent_id
                        if wait_for_reply
                        else "",
                        "target_agent_type": target_agent_type,
                        "stream_name": delivery_stream,
                        "status": "QUEUED",
                    }
                )
            except _RegistryNetworkError as net_err:
                # Network is down or Redis timed out — we can still
                # dispatch the command on the data stream. The
                # execution-tracking row will simply be missing; we
                # surface the gap on the registry-failure counter and
                # a single warning line.
                record_failure(
                    REGISTRY_FAILURES_COUNTER,
                    operation="call_agent.initialize_execution",
                    error=net_err,
                )
                logger.warning(
                    "[%s] registry not available, continuing without "
                    "execution tracking. target_agent_type=%s execution_id=%s "
                    "err=%s",
                    self.trace_id,
                    target_agent_type,
                    execution_id,
                    net_err,
                )
            except _RegistrySchemaError as schema_err:
                # Schema / protocol mismatch — the registry is up but
                # the payload we built does not match its expectations.
                # This is a bug we want to see in the logs, but it must
                # not abort the dispatch.
                record_failure(
                    REGISTRY_FAILURES_COUNTER,
                    operation="call_agent.initialize_execution",
                    error=schema_err,
                )
                logger.warning(
                    "[%s] registry rejected execution payload "
                    "(schema/protocol error); continuing without "
                    "execution tracking. target_agent_type=%s "
                    "execution_id=%s err=%s",
                    self.trace_id,
                    target_agent_type,
                    execution_id,
                    schema_err,
                )

        try:
            if should_dispatch_control:
                await self.redis.xadd(delivery_stream, command.to_redis_payload())
        except Exception as error:
            if self.plugin_registry:
                await self.plugin_registry.on_call_agent_error(self, command, error)
            raise

        result = {
            "status": AgentState.QUEUED.value,
            "message_id": message_id,
            "parent_message_id": parent_message_id,
            "target_agent_type": target_agent_type,
        }
        if self.plugin_registry:
            await self.plugin_registry.on_call_agent_complete(self, command, result)
        return result

    async def dispatch_group(
        self,
        tasks: list[dict[str, Any]],
        wait_for_reply: bool = True,
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
    ) -> dict:
        """
        Dispatch multiple tasks concurrently as a group.
        The caller agent will be resumed ONLY when ALL tasks in the group are completed.

        Args:
            tasks: A list of dicts, each containing:
                   {
                       "target_agent_type": str,
                       "content": str,
                       "extra_payload": Optional[Dict[str, Any]],
                       "metadata": Optional[Dict[str, Any]]
                   }
            wait_for_reply: bool. If True, sets up Redis counters to wait for all.
        """
        if not tasks:
            return {"status": "EMPTY", "task_group_id": ""}

        task_group_id = f"{TASK_GROUP_ID_PREFIX}{uuid.uuid4().hex[:8]}"
        total_tasks = len(tasks)

        if wait_for_reply:
            group_key = RedisKeys.task_group(task_group_id)
            await self.redis.hset(
                group_key,
                mapping={
                    TASK_GROUP_FIELD_TOTAL: str(total_tasks),
                    TASK_GROUP_FIELD_COMPLETED: "0",
                    TASK_GROUP_FIELD_SOURCE_AGENT: self.current_agent_id,
                },
            )
            # Ensure the key expires to prevent leak
            await self.redis.expire(group_key, TASK_GROUP_TTL_SECONDS)
            self._buffer.mark_suspended()
        else:
            self._buffer.mark_permission_transferred()

        dispatched = []
        for task in tasks:
            target_agent_type = task["target_agent_type"]
            content = task.get("content", "")
            extra_payload = task.get("extra_payload", {})
            metadata = task.get("metadata", {})
            serialized_content = self._serialize_outbound_content(content)

            current_message_id = message_id or self.generate_message_id()
            parent_message_id = (
                parent_message_id if parent_message_id else self.message_id
            )
            merged_extra_payload = dict(extra_payload)
            if wait_for_reply:
                merged_extra_payload["wait_for_reply"] = True

            command = AskAgentCommand(
                header=MessageHeader(
                    message_id=current_message_id,
                    session_id=self.session_id,
                    trace_id=self.trace_id,
                    source_agent_type=self.current_agent_id if wait_for_reply else "",
                    target_agent_type=target_agent_type,
                    parent_message_id=parent_message_id,
                    task_group_id=task_group_id,
                    user_code=self.agent_runtime_state.session_manager.user_code,
                    user_name=self.agent_runtime_state.session_manager.user_name,
                    metadata=metadata,
                ),
                content=serialized_content,
                wait_for_reply=wait_for_reply,
                extra_payload={
                    k: v
                    for k, v in merged_extra_payload.items()
                    if k != "wait_for_reply"
                },
            )

            execution_id = f"{EXECUTION_ID_PREFIX}{uuid.uuid4().hex[:8]}"
            from by_framework.core.registry import WorkerRegistry

            registry = WorkerRegistry(self.redis)
            if hasattr(registry, "initialize_execution"):
                try:
                    await registry.initialize_execution(
                        {
                            "execution_id": execution_id,
                            "message_id": current_message_id,
                            "session_id": self.session_id,
                            "trace_id": self.trace_id,
                            "parent_message_id": parent_message_id or "",
                            "source_agent_type": self.current_agent_id
                            if wait_for_reply
                            else "",
                            "target_agent_type": target_agent_type,
                            "stream_name": RedisKeys.ctrl_stream(target_agent_type),
                            "status": "QUEUED",
                        }
                    )
                except _RegistryNetworkError as net_err:
                    record_failure(
                        REGISTRY_FAILURES_COUNTER,
                        operation="dispatch_group.initialize_execution",
                        error=net_err,
                    )
                    logger.warning(
                        "[%s] registry not available for task group; "
                        "continuing without execution tracking. "
                        "task_group_id=%s target_agent_type=%s "
                        "execution_id=%s err=%s",
                        self.trace_id,
                        task_group_id,
                        target_agent_type,
                        execution_id,
                        net_err,
                    )
                except _RegistrySchemaError as schema_err:
                    record_failure(
                        REGISTRY_FAILURES_COUNTER,
                        operation="dispatch_group.initialize_execution",
                        error=schema_err,
                    )
                    logger.warning(
                        "[%s] registry rejected task-group execution "
                        "payload (schema/protocol error); continuing. "
                        "task_group_id=%s target_agent_type=%s "
                        "execution_id=%s err=%s",
                        self.trace_id,
                        task_group_id,
                        target_agent_type,
                        execution_id,
                        schema_err,
                    )

            await self.redis.xadd(
                RedisKeys.ctrl_stream(target_agent_type), command.to_redis_payload()
            )

            dispatched.append(
                {
                    "message_id": current_message_id,
                    "target_agent_type": target_agent_type,
                }
            )

        return {
            "status": "GROUP_QUEUED",
            "task_group_id": task_group_id,
            "dispatched_tasks": dispatched,
        }

    def _serialize_outbound_content(self, content: object) -> WireContent:
        if self.content_codec is not None:
            return self.content_codec.serialize(content)

        if self._is_wire_content(content):
            return content

        raise TypeError(
            "AgentContext requires a content codec to serialize non-wire content"
        )

    @staticmethod
    def _is_wire_content(content: object) -> bool:
        if isinstance(content, str):
            return True
        if not isinstance(content, list):
            return False
        return all(isinstance(item, dict) for item in content)

    async def collect_group_results(
        self,
        task_group_id: str,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Collect results of all subtasks in the task group.

        Waits for the task group to complete (or ``timeout`` to elapse)
        by combining an initial ``HGETALL`` snapshot with an
        ``XREAD BLOCK`` against ``task_group_results_stream``. Each
        notification on the stream signals that a new result has been
        HSET into the results hash, so the collector re-snapshots the
        hash and resumes blocking. This eliminates the previous
        100 ms-polling loop when the writer is a cooperating worker
        that emits notifications (``worker.py`` does so on every
        completed subtask). When notifications are missing for any
        reason (older workers, partial failures), the loop falls back
        to a 200 ms safety poll so the API is still progress-bounded.

        Args:
            task_group_id: task_group_id returned by dispatch_group
            timeout: Maximum timeout in seconds to wait

        Returns:
            List of subtask results, each includes:
            {
                "message_id": str,
                "status": str,
                "reply_data": Any,
                "content": Optional[str]
            }
        """
        if not task_group_id:
            return []

        results_key = RedisKeys.task_group_results(task_group_id)
        group_key = RedisKeys.task_group(task_group_id)
        results_stream = RedisKeys.task_group_results_stream(task_group_id)

        total_str = await self.redis.hget(group_key, TASK_GROUP_FIELD_TOTAL)
        if total_str is None:
            # No group found, try to get whatever results exist
            total = float("inf")
        else:
            total = int(total_str)

        def _parse_results(
            raw_results: dict[Any, Any],
        ) -> list[dict[str, Any]]:
            return [
                {
                    "message_id": msg_id,
                    **json.loads(data),
                }
                for msg_id, data in raw_results.items()
            ]

        start_time = asyncio.get_running_loop().time()

        # 1) Take an initial snapshot to handle the "writer finished
        #    before we got here" race — if every subtask already
        #    completed before this call, we must not block waiting
        #    for a notification that will never arrive.
        raw_results = await self.redis.hgetall(results_key)
        results = _parse_results(raw_results) if raw_results else []
        if len(results) >= total:
            return results

        # 2) Block on the per-group notification stream. Each XADD in
        #    the writer fires one entry; we re-snapshot the hash to
        #    read the full, ordered result set.
        while True:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed >= timeout:
                break

            remaining_ms = max(1, int((timeout - elapsed) * 1000))
            # Bound each XREAD BLOCK call to a fraction of the
            # remaining budget so timeout enforcement is precise.
            block_ms = min(remaining_ms, 2000)

            try:
                response = await self.redis.xread(
                    streams={results_stream: "$"},
                    count=1,
                    block=block_ms,
                )
            except (TypeError, ValueError):
                # Some test doubles (e.g. LocalMemoryMQ) don't accept
                # ``block``/``count`` kwargs. Fall back to a short
                # sleep so the loop remains cooperative.
                await asyncio.sleep(0.05)
                response = None
            except Exception:
                # Don't let a transport error kill the collector; fall
                # back to a short sleep and let the next iteration
                # retry.
                await asyncio.sleep(0.05)
                response = None

            if response:
                # Drain any other queued notifications: we only need
                # one HGETALL, so skip ahead to the last entry.
                for _ in response[-1][1][1:]:
                    pass
                raw_results = await self.redis.hgetall(results_key)
                if raw_results:
                    results = _parse_results(raw_results)
                if len(results) >= total:
                    return results
                # Got a notification but results still short; the
                # total may have been misreported (race with
                # dispatch_group). Re-block and keep waiting.
                continue

            # 3) No notification this round. Re-snapshot periodically
            #    in case the writer is an older worker that doesn't
            #    emit notifications. Re-snapshot at most every
            #    POLL_INTERVAL_SECONDS so we don't hammer Redis.
            POLL_INTERVAL_SECONDS = 0.2
            if raw_results or elapsed >= POLL_INTERVAL_SECONDS:
                raw_results = await self.redis.hgetall(results_key)
                if raw_results:
                    results = _parse_results(raw_results)
                    if len(results) >= total:
                        return results

        return results
