"""Discover and execute MCP tools without a hard MCP SDK dependency."""

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from by_framework.agent.tools import ToolSpec


class MCPError(RuntimeError):
    """Base normalized MCP failure."""


class MCPConnectionError(MCPError):
    """MCP session setup or transport failed."""


class MCPRemoteError(MCPError):
    """A remote MCP tool returned a structured error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "remote_error",
        data: Any = None,
    ):
        super().__init__(message)
        self.code = code
        self.data = data


class MCPSession(Protocol):
    async def initialize(self) -> None: ...

    async def list_tools(self) -> list[Mapping[str, Any]]: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, request_id: str
    ) -> Mapping[str, Any]: ...

    async def cancel(self, request_id: str) -> None: ...

    async def close(self) -> None: ...


class MCPClient(Protocol):
    async def connect(self) -> MCPSession: ...


class MCPTool:
    """FunctionTool-compatible dynamic MCP capability."""

    def __init__(self, toolset: "MCPToolset", definition: Mapping[str, Any]):
        self._toolset = toolset
        tool_name = str(definition["name"])
        self.spec = ToolSpec(
            name=tool_name,
            description=str(definition.get("description", "")),
            input_schema=dict(
                definition.get(
                    "inputSchema",
                    {"type": "object", "properties": {}},
                )
            ),
            output_schema=dict(definition.get("outputSchema", {})),
            side_effect=str(definition.get("sideEffect", "remote")),
            implementation_ref=f"mcp:{toolset.server_id}:{tool_name}",
        )
        self.function = self._invoke

    async def _invoke(self, **arguments: Any) -> Any:
        return await self._toolset.call(self.spec.name, arguments)


class MCPToolset:
    """Own one MCP session and expose discovered tools."""

    def __init__(self, client: MCPClient, *, server_id: str = "default"):
        if not server_id:
            raise ValueError("server_id must not be empty")
        self._client = client
        self.server_id = server_id
        self._session: MCPSession | None = None
        self._tools: dict[str, MCPTool] = {}
        self._active_requests: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._closing = False

    async def start(self) -> None:
        if self._session is not None:
            return
        session = None
        try:
            session = await self._client.connect()
            await session.initialize()
        except asyncio.CancelledError:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
            raise MCPConnectionError(
                f"failed to initialize MCP session: {exc}"
            ) from exc
        self._session = session

    async def discover(self, *, refresh: bool = False) -> tuple[MCPTool, ...]:
        session = self._require_session()
        if self._tools and not refresh:
            return tuple(self._tools[name] for name in sorted(self._tools))
        try:
            definitions = await session.list_tools()
            tools = {
                str(definition["name"]): MCPTool(self, definition)
                for definition in definitions
            }
        except Exception as exc:
            raise MCPConnectionError(
                f"invalid MCP tool discovery response: {exc}"
            ) from exc
        if len(tools) != len(definitions):
            raise MCPConnectionError("MCP discovery returned duplicate tool names")
        self._tools = tools
        return tuple(tools[name] for name in sorted(tools))

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        session = self._require_session()
        if name not in self._tools:
            raise MCPRemoteError(
                f"unknown discovered MCP tool {name!r}", code="unknown_tool"
            )
        request_id = f"mcp-{uuid.uuid4().hex}"
        self._active_requests.add(request_id)
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[request_id] = task
        try:
            response = await session.call_tool(
                name, dict(arguments), request_id=request_id
            )
        except asyncio.CancelledError:
            if not self._closing:
                try:
                    await session.cancel(request_id)
                except Exception:
                    # Local cancellation remains authoritative even when the
                    # remote transport cannot acknowledge it.
                    pass
            raise
        except Exception as exc:
            raise MCPConnectionError(f"MCP call {name!r} failed: {exc}") from exc
        finally:
            self._active_requests.discard(request_id)
            self._active_tasks.pop(request_id, None)
        if not isinstance(response, Mapping):
            raise MCPRemoteError(
                "MCP tool returned a non-object response",
                code="invalid_response",
            )
        if response.get("isError"):
            error = response.get("error", {})
            if not isinstance(error, Mapping):
                raise MCPRemoteError(
                    f"MCP tool {name!r} returned an invalid error payload",
                    code="invalid_response",
                    data=error,
                )
            raise MCPRemoteError(
                str(error.get("message", f"MCP tool {name!r} failed")),
                code=str(error.get("code", "remote_error")),
                data=error.get("data"),
            )
        if "structuredContent" in response:
            return response["structuredContent"]
        return response.get("content")

    async def close(self) -> None:
        if self._session is None:
            return
        session = self._session
        self._closing = True
        close_error: Exception | None = None
        try:
            for request_id in tuple(self._active_requests):
                try:
                    await session.cancel(request_id)
                except Exception as exc:
                    close_error = close_error or exc
            tasks = tuple(self._active_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._active_requests.clear()
            self._active_tasks.clear()
            self._session = None
            self._tools.clear()
            try:
                await session.close()
            except Exception as exc:
                close_error = close_error or exc
            self._closing = False
        if close_error is not None:
            raise MCPConnectionError(f"failed to close MCP session: {close_error}")

    async def __aenter__(self) -> "MCPToolset":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    def _require_session(self) -> MCPSession:
        if self._session is None:
            raise MCPConnectionError("MCP toolset is not started")
        return self._session
