"""
Control message handling module for WorkerRunner.

Handles control commands like CancelTaskCommand.
"""

import asyncio
import json

from by_framework.common.constants import RedisKeys
from by_framework.common.logger import logger
from by_framework.common.metrics import (
    PLUGIN_RELOAD_FAILURES_COUNTER,
    record_failure,
)
from by_framework.core.protocol.commands import (
    AskAgentCommand,
    CancelTaskCommand,
    ReloadPluginsCommand,
    ResumeCommand,
    command_from_dict,
)
from by_framework.errors import UnsupportedCommandError


async def parse_control_command(
    data_dict: dict,
) -> CancelTaskCommand | ReloadPluginsCommand | AskAgentCommand | ResumeCommand:
    """
    Parse and validate a control or task command from the worker control stream.

    Args:
        data_dict: Parsed JSON data

    Returns:
        CancelTaskCommand, ReloadPluginsCommand, AskAgentCommand,
        or ResumeCommand instance

    Raises:
        UnsupportedCommandError: If command type is not supported on the control stream
    """
    try:
        command = command_from_dict(data_dict)
    except ValueError as e:
        raise UnsupportedCommandError(str(e)) from e
    if isinstance(command, CancelTaskCommand):
        return command
    if isinstance(command, ReloadPluginsCommand):
        return command
    if isinstance(command, (AskAgentCommand, ResumeCommand)):
        # AskAgentCommand/ResumeCommand on worker_ctrl_stream means direct routing
        return command
    raise UnsupportedCommandError(type(command).__name__)


async def handle_cancel_task(
    command: CancelTaskCommand,
    active_executions: dict,
    message_to_execution: dict,
    redis_client,  # pylint: disable=unused-argument
    group_name: str,  # pylint: disable=unused-argument
    worker,
) -> None:
    """
    Handle a CancelTaskCommand.

    Triggers cancellation for the target execution and notifies plugins.
    """
    # Find execution ID
    execution_id = command.target_execution_id or message_to_execution.get(
        command.target_message_id
    )
    reason = command.reason
    running = active_executions.get(execution_id) if execution_id else None

    registry = getattr(worker, "registry", None)
    target_session_id = running.session_id if running else command.header.session_id

    # Mark execution as cancelling
    if execution_id and registry and hasattr(registry, "mark_execution_cancelling"):
        await registry.mark_execution_cancelling(
            execution_id, target_session_id, reason
        )

    # Trigger cancellation
    if running:
        running.cancel_reason = reason
        running.cancel_event.set()

        # Notify worker plugins
        if running.context and worker.plugin_registry:
            asyncio.create_task(
                worker.plugin_registry.on_task_cancel(running.context, command)
            )
        asyncio.create_task(worker.on_cancel_task(command))

        # Cancel the task
        import sys

        if sys.version_info >= (3, 9):
            running.task.cancel(msg=reason)
        else:
            running.task.cancel()


async def handle_reload_plugins(command: ReloadPluginsCommand, worker) -> None:
    """Handle a ReloadPluginsCommand by replaying the plugin reload chain."""
    plugin_registry = getattr(worker, "plugin_registry", None)
    if plugin_registry is None or not hasattr(plugin_registry, "reload_plugins"):
        raise UnsupportedCommandError("worker has no reloadable plugin registry")
    worker_id = _get_explicit_attr(worker, "worker_id", "")
    status_payload = {
        "reload_id": command.reload_id,
        "worker_id": worker_id,
        "status": "failure",
        "reason": command.reason,
        "version_before": getattr(plugin_registry, "agent_configs_version", 0),
        "version_after": getattr(plugin_registry, "agent_configs_version", 0),
        "error": "",
    }

    try:
        await plugin_registry.reload_plugins(
            reload_id=command.reload_id,
            reason=command.reason,
        )
        recorded = await _get_reload_status(plugin_registry, command.reload_id)
        if recorded:
            status_payload.update(recorded)
        else:
            status_payload.update(
                {
                    "status": "success",
                    "version_after": getattr(
                        plugin_registry, "agent_configs_version", 0
                    ),
                }
            )
    except asyncio.CancelledError:
        # Cooperative cancellation — propagate.
        raise
    except (OSError, ConnectionError) as conn_err:
        # Redis is down or the network blipped. The reload itself is
        # already in-flight in the plugin_registry, so we count it,
        # log the cause, and re-raise so the outer runner can decide
        # whether to back off.
        record_failure(
            PLUGIN_RELOAD_FAILURES_COUNTER,
            operation="handle_reload_plugins.reload_plugins",
            error=conn_err,
        )
        status_payload["error"] = f"connection error: {conn_err}"
        await _publish_reload_ack(worker, status_payload)
        logger.error(
            "[%s] Connection error during plugin reload %s: %s",
            worker_id,
            command.reload_id,
            conn_err,
        )
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
    ) as plugin_err:
        # Plugin code itself raised a non-network error. This is almost
        # always a real bug in a plugin's reload implementation; we want
        # the full stack so SREs can debug it.
        record_failure(
            PLUGIN_RELOAD_FAILURES_COUNTER,
            operation="handle_reload_plugins.reload_plugins",
            error=plugin_err,
        )
        status_payload["error"] = str(plugin_err)
        await _publish_reload_ack(worker, status_payload)
        logger.exception(
            "[%s] Plugin reload %s failed: %s",
            worker_id,
            command.reload_id,
            plugin_err,
        )
        raise

    await _publish_reload_ack(worker, status_payload)


async def _publish_reload_ack(worker, status_payload: dict) -> None:
    """Publish reload handling status to the ACK stream when Redis is available."""
    redis_client = _get_explicit_attr(worker, "redis", None)
    if redis_client is None or not hasattr(redis_client, "xadd"):
        return

    await redis_client.xadd(
        RedisKeys.plugin_reload_ack_stream(status_payload["reload_id"]),
        {"data": json.dumps(status_payload)},
    )


async def _get_reload_status(plugin_registry, reload_id: str) -> dict | None:
    """Read reload status from sync or async registries."""
    if not hasattr(plugin_registry, "get_reload_status"):
        return None

    result = plugin_registry.get_reload_status(reload_id)
    if asyncio.iscoroutine(result):
        result = await result
    return result if isinstance(result, dict) else None


def _get_explicit_attr(obj, name: str, default):
    """Read only explicitly assigned attributes, avoiding Mock auto-creation."""
    values = getattr(obj, "__dict__", None)
    if isinstance(values, dict) and name in values:
        return values[name]
    return default
