# pylint: disable=C0114,C0116
from unittest.mock import AsyncMock

import pytest

from by_framework_chat_ui.agents import list_online_agent_types


@pytest.mark.asyncio
async def test_lists_sorted_union_of_agent_types_across_workers():
    registry = AsyncMock()
    registry.get_all_workers.return_value = {
        "worker-a": {"agent_types": ["planner", "coder"]},
        "worker-b": {"agent_types": ["coder", "reviewer"]},
    }

    result = await list_online_agent_types(registry)

    assert result == ["coder", "planner", "reviewer"]


@pytest.mark.asyncio
async def test_no_online_workers_yields_empty_list():
    registry = AsyncMock()
    registry.get_all_workers.return_value = {}

    result = await list_online_agent_types(registry)

    assert result == []
