"""MCP discovery, execution, lifecycle, cancellation, and error contracts."""

import asyncio

import pytest

from by_framework.agent import ToolCall, ToolExecutor
from by_framework_tools_mcp import MCPConnectionError, MCPRemoteError, MCPToolset


class Session:
    def __init__(self):
        self.initialized = False
        self.closed = False
        self.cancelled = []
        self.calls = []
        self.block = False

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return [
            {
                "name": "weather",
                "description": "Get weather",
                "inputSchema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {"temperature": {"type": "integer"}},
                    "required": ["temperature"],
                },
            }
        ]

    async def call_tool(self, name, arguments, *, request_id):
        self.calls.append((name, arguments, request_id))
        if self.block:
            await asyncio.Future()
        if arguments.get("city") == "error":
            return {
                "isError": True,
                "error": {
                    "code": "upstream",
                    "message": "weather unavailable",
                    "data": {"retryable": True},
                },
            }
        return {"structuredContent": {"temperature": 72}}

    async def cancel(self, request_id):
        self.cancelled.append(request_id)

    async def close(self):
        self.closed = True


class Client:
    def __init__(self, session):
        self.session = session

    async def connect(self):
        return self.session


def test_discovers_dynamic_tool_and_executes_through_core_tool_executor():
    session = Session()

    async def scenario():
        async with MCPToolset(Client(session)) as toolset:
            tools = await toolset.discover()
            result = await ToolExecutor(
                {tool.spec.name: tool for tool in tools}
            ).execute(ToolCall("call-1", "weather", {"city": "LA"}))
            return tools, result

    tools, result = asyncio.run(scenario())
    assert tools[0].spec.implementation_ref == "mcp:default:weather"
    assert result.output == {"temperature": 72}
    assert session.initialized
    assert session.closed


def test_remote_structured_error_is_normalized():
    session = Session()

    async def scenario():
        toolset = MCPToolset(Client(session))
        await toolset.start()
        await toolset.discover()
        try:
            await toolset.call("weather", {"city": "error"})
        finally:
            await toolset.close()

    with pytest.raises(MCPRemoteError) as captured:
        asyncio.run(scenario())
    assert captured.value.code == "upstream"
    assert captured.value.data == {"retryable": True}


def test_cancelled_call_propagates_cancellation_to_session():
    session = Session()
    session.block = True

    async def scenario():
        toolset = MCPToolset(Client(session))
        await toolset.start()
        await toolset.discover()
        task = asyncio.create_task(toolset.call("weather", {"city": "LA"}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await toolset.close()

    asyncio.run(scenario())
    assert len(session.cancelled) == 1


def test_requires_started_session():
    with pytest.raises(MCPConnectionError, match="not started"):
        asyncio.run(MCPToolset(Client(Session())).discover())


def test_refresh_replaces_cached_tools_and_specs_are_immutable():
    session = Session()

    async def scenario():
        toolset = MCPToolset(Client(session))
        await toolset.start()
        first = await toolset.discover()

        async def refreshed():
            return [
                {
                    "name": "forecast",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        session.list_tools = refreshed
        cached = await toolset.discover()
        second = await toolset.discover(refresh=True)
        await toolset.close()
        return first, cached, second

    first, cached, second = asyncio.run(scenario())
    assert cached[0] is first[0]
    assert [tool.spec.name for tool in second] == ["forecast"]
    with pytest.raises(TypeError):
        first[0].spec.input_schema["type"] = "array"


def test_close_cancels_active_local_and_remote_call():
    session = Session()
    session.block = True

    async def scenario():
        toolset = MCPToolset(Client(session))
        await toolset.start()
        await toolset.discover()
        task = asyncio.create_task(toolset.call("weather", {"city": "LA"}))
        await asyncio.sleep(0)
        await toolset.close()
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert len(session.cancelled) == 1
    assert session.closed


def test_malformed_remote_error_is_normalized():
    session = Session()

    async def malformed(*args, **kwargs):
        del args, kwargs
        return {"isError": True, "error": "bad"}

    session.call_tool = malformed

    async def scenario():
        async with MCPToolset(Client(session)) as toolset:
            await toolset.discover()
            await toolset.call("weather", {"city": "LA"})

    with pytest.raises(MCPRemoteError) as captured:
        asyncio.run(scenario())
    assert captured.value.code == "invalid_response"
