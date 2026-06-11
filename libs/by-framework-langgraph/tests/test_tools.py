"""Tests for tools module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from by_framework_langgraph.tools import (
    _langfuse_observation_id_from_callbacks,
    make_ask_user_tool,
    make_remote_agent_tool,
)


def _make_mock_context(session_id: str = "test-session"):
    """Create a mock AgentContext for testing."""
    ctx = MagicMock()
    ctx.session_id = session_id
    ctx.redis = AsyncMock()
    ctx.call_agent = AsyncMock()
    ctx.ask_user = AsyncMock(return_value={"status": "WAITING_USER"})
    return ctx


class TestMakeRemoteAgentTool:
    """Tests for make_remote_agent_tool."""

    def test_returns_tool_with_correct_name(self):
        """Verify the returned tool uses the provided tool_name."""
        ctx = _make_mock_context()
        tool = make_remote_agent_tool(ctx, "invoke_poet", "poet-agent", "Invoke poet")
        assert tool.name == "invoke_poet"

    def test_returns_tool_with_description(self):
        """Verify the returned tool carries the provided description."""
        ctx = _make_mock_context()
        tool = make_remote_agent_tool(
            ctx, "invoke_poet", "poet-agent", "Invoke the poet agent"
        )
        assert "Invoke the poet agent" in tool.description

    def test_resolves_langfuse_observation_id_from_callbacks(self):
        """The active tool observation can be read from Langfuse callback state."""
        run_id = object()
        handler = SimpleNamespace(_runs={run_id: SimpleNamespace(id="obs-tool")})
        callbacks = SimpleNamespace(parent_run_id=run_id, handlers=[handler])

        assert _langfuse_observation_id_from_callbacks(callbacks) == "obs-tool"

    @pytest.mark.asyncio
    async def test_passes_tool_observation_id_to_call_agent(self, monkeypatch):
        """Remote calls are parented to the current LangGraph tool observation."""
        ctx = _make_mock_context()
        ctx.redis.exists.return_value = False
        tool = make_remote_agent_tool(
            ctx,
            "query_weather",
            "weather-agent",
            "Query weather",
        )
        monkeypatch.setattr(
            "by_framework_langgraph.tools.interrupt",
            lambda _message: "queued",
        )

        run_id = object()
        handler = SimpleNamespace(_runs={run_id: SimpleNamespace(id="obs-tool")})
        callbacks = SimpleNamespace(parent_run_id=run_id, handlers=[handler])
        run_manager = SimpleNamespace(get_child=lambda: callbacks)

        result = await tool._arun(
            "Beijing weather",
            tool_call_id="tool-call-1",
            run_manager=run_manager,
            config={},
        )

        assert result == "queued"
        ctx.call_agent.assert_awaited_once_with(
            target_agent_type="weather-agent",
            content="Beijing weather",
            metadata={"langfuse_parent_observation_id": "obs-tool"},
        )


class TestMakeAskUserTool:
    """Tests for make_ask_user_tool."""

    def test_returns_tool_with_default_name(self):
        """Verify the default tool name is 'ask_user'."""
        ctx = _make_mock_context()
        tool = make_ask_user_tool(ctx)
        assert tool.name == "ask_user"

    def test_returns_tool_with_custom_name(self):
        """Verify a custom tool_name overrides the default."""
        ctx = _make_mock_context()
        tool = make_ask_user_tool(ctx, tool_name="confirm_action")
        assert tool.name == "confirm_action"

    def test_returns_tool_with_description(self):
        """Verify the returned tool carries the provided description."""
        ctx = _make_mock_context()
        tool = make_ask_user_tool(ctx, description="Ask user for confirmation")
        assert "Ask user for confirmation" in tool.description
