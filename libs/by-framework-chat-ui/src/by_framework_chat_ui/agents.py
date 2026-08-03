"""Live assistant discovery for the chat picker.

Deliberately reads straight from the registry rather than a hand-maintained
config: `WorkerRegistry` has no single key holding "the full set of
agent_types", so this derives it by unioning the `agent_types` declared by
every currently-online worker.
"""

from __future__ import annotations

from by_framework.core.registry import WorkerRegistry


async def list_online_agent_types(registry: WorkerRegistry) -> list[str]:
    """Return the sorted set of agent_types with at least one online worker."""
    workers = await registry.get_all_workers()
    agent_types = {
        agent_type
        for info in workers.values()
        for agent_type in info.get("agent_types", [])
    }
    return sorted(agent_types)
