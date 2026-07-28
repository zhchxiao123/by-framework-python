"""Additive adapters between legacy configuration/protocols and native APIs."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from by_framework.core.extensions.agent_config import AgentConfig
from by_framework.core.protocol.commands import ResumeCommand

from .definition import Agent, CompiledAgent
from .graph import Interrupt
from .model import Model
from .runtime.store import CommitRequest, RunEvent, RunStore
from .tools import FunctionTool


class AgentConfigConversionError(ValueError):
    """A legacy AgentConfig cannot be converted without guessing semantics."""


@dataclass(frozen=True)
class AgentConfigConversion:
    agent: Agent
    warnings: tuple[str, ...]


class AgentConfigAdapter:
    """Explicit best-effort conversion of plugin AgentConfig declarations."""

    def __init__(
        self,
        model_resolver: Callable[[AgentConfig], Model],
        *,
        tool_resolver: Callable[[str, Any], FunctionTool] | None = None,
    ):
        self._model_resolver = model_resolver
        self._tool_resolver = tool_resolver

    def convert(self, config: AgentConfig) -> AgentConfigConversion:
        if not config.agent_id:
            raise AgentConfigConversionError("AgentConfig.agent_id is required")
        prompt = config.prompts.get("system", config.prompts.get("instructions", ""))
        if prompt and not isinstance(prompt, str):
            raise AgentConfigConversionError("system/instructions prompt must be text")
        tools = []
        if config.tools and self._tool_resolver is None:
            raise AgentConfigConversionError(
                "AgentConfig tools require an explicit tool_resolver"
            )
        tool_resolver = self._tool_resolver
        for name, tool_config in config.tools.items():
            assert tool_resolver is not None
            tool = tool_resolver(name, tool_config)
            if not isinstance(tool, FunctionTool):
                raise AgentConfigConversionError(
                    f"tool_resolver returned an invalid tool for {name!r}"
                )
            if tool.spec.name != name:
                raise AgentConfigConversionError(
                    f"resolved tool name {tool.spec.name!r} does not match {name!r}"
                )
            tools.append(tool)
        warnings = []
        for field_name in ("callbacks", "skills", "knowledge_bases", "sub_agents"):
            if getattr(config, field_name):
                warnings.append(
                    f"{field_name} remains plugin-managed and was not compiled"
                )
        return AgentConfigConversion(
            Agent(
                config.agent_id,
                prompt or config.description,
                self._model_resolver(config),
                tools,
            ),
            tuple(warnings),
        )


class NativeEventWriter:
    """Small expected-version writer for compatibility bridge events."""

    def __init__(
        self,
        store: RunStore,
        run_id: str,
        *,
        version: int,
        fencing_token: int,
        state: Mapping[str, Any],
    ):
        self.store = store
        self.run_id = run_id
        self.version = version
        self.fencing_token = fencing_token
        self.state = dict(state)

    async def append(self, event: RunEvent) -> int:
        result = await self.store.commit(
            CommitRequest(
                self.run_id,
                self.version,
                self.fencing_token,
                (event,),
                self.state,
            )
        )
        self.version = result.version
        return result.version

    async def append_once(self, idempotency_key: str, event: RunEvent) -> bool:
        """Append an adapter event once, persisting the replay key in run state."""
        compatibility = dict(self.state.get("_native_compatibility", {}))
        completed = dict(compatibility.get("completed", {}))
        if idempotency_key in completed:
            return False
        completed[idempotency_key] = event.kind
        compatibility["completed"] = completed
        next_state = dict(self.state)
        next_state["_native_compatibility"] = compatibility
        result = await self.store.commit(
            CommitRequest(
                self.run_id,
                self.version,
                self.fencing_token,
                (event,),
                next_state,
            )
        )
        self.version = result.version
        self.state = next_state
        return True


class LegacyInterruptBridge:
    """Map legacy user/agent control operations to native durable interrupts."""

    def __init__(self, writer: NativeEventWriter):
        self.writer = writer

    async def ask_user(self, context, prompt: str, *, key: str) -> Interrupt:
        interrupt = Interrupt(key, prompt, "resume_value")
        appended = await self.writer.append_once(
            f"ask_user:{key}",
            RunEvent(
                "RunInterrupted",
                {"kind": "human", "key": key, "prompt": prompt},
            ),
        )
        if appended:
            await context.ask_user(prompt)
        return interrupt

    async def call_agent(
        self,
        context,
        target_agent_type: str,
        content: Any,
        *,
        key: str,
    ) -> tuple[Interrupt, Any]:
        interrupt = Interrupt(key, f"Await agent {target_agent_type}", "resume_value")
        appended = await self.writer.append_once(
            f"call_agent:{key}",
            RunEvent(
                "AgentDispatched",
                {
                    "kind": "remote_agent",
                    "key": key,
                    "target_agent_type": target_agent_type,
                },
            ),
        )
        result = None
        if appended:
            result = await context.call_agent(
                target_agent_type,
                content,
                wait_for_reply=True,
                metadata={"native_interrupt_key": key},
            )
        return interrupt, result

    async def resume(self, command: ResumeCommand) -> Any:
        key = str(command.extra_payload.get("native_interrupt_key", ""))
        if not key:
            raise ValueError("ResumeCommand is missing native_interrupt_key")
        completed = self.writer.state.get("_native_compatibility", {}).get(
            "completed", {}
        )
        if f"ask_user:{key}" not in completed and f"call_agent:{key}" not in completed:
            raise ValueError(f"ResumeCommand targets unknown native interrupt {key!r}")
        await self.writer.append_once(
            f"resume:{key}",
            RunEvent(
                "RunResumed",
                {
                    "key": key,
                    "status": command.status,
                    "source_agent_type": command.header.source_agent_type,
                },
            ),
        )
        return command.reply_data if command.reply_data is not None else command.content


async def register_agent_config(
    catalog,
    config: AgentConfig,
    adapter: AgentConfigAdapter,
) -> tuple[CompiledAgent, tuple[str, ...]]:
    """Register a converted config beside ordinary native definitions."""
    conversion = adapter.convert(config)
    await catalog.register(conversion.agent)
    return catalog.get(config.agent_id), conversion.warnings
