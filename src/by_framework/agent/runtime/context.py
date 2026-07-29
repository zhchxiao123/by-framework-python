"""Execution identity and session-capability seam for native runs."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .serialization import canonical_json


class RunContextError(RuntimeError):
    """The supplied runtime context is invalid or lacks a capability."""


@dataclass(frozen=True)
class RunIdentity:
    """JSON-safe identity shared by local and Worker-hosted runs."""

    session_id: str
    run_id: str
    agent_id: str
    user_code: str = "default"
    user_name: str = ""
    trace_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        projection = dict(self.trace_context)
        canonical_json(projection)
        object.__setattr__(self, "trace_context", MappingProxyType(projection))

    def projection(self) -> dict[str, Any]:
        projection = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "user_code": self.user_code,
            "user_name": self.user_name,
            "trace_context": dict(self.trace_context),
        }
        canonical_json(projection)
        return projection


@dataclass(frozen=True)
class RunContext:
    """Run identity plus optional process-local session capabilities."""

    identity: RunIdentity
    private_files: Any | None = None
    shared_files: Any | None = None
    conversation: Any | None = None
    agent_configs: Any | None = None

    def projection(self) -> dict[str, Any]:
        """Return the only context representation allowed in durable state."""
        return self.identity.projection()

    def require(self, capability: str) -> Any:
        value = getattr(self, capability, None)
        if value is None:
            raise RunContextError(
                f"runtime capability {capability!r} is not available"
            )
        return value


@dataclass(frozen=True)
class ToolExecutionContext:
    """Context injected into an explicitly annotated function-tool parameter."""

    run: RunContext
    tool_call_id: str
    tool_name: str


def local_run_context(
    agent_id: str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    trace_context: Mapping[str, Any] | None = None,
) -> RunContext:
    """Create an offline context without Worker or Redis dependencies."""
    resolved_run_id = run_id or f"run-{uuid.uuid4().hex}"
    return RunContext(
        RunIdentity(
            session_id or f"session-{uuid.uuid4().hex}",
            resolved_run_id,
            agent_id,
            trace_context=dict(trace_context or {}),
        )
    )


def resolve_run_context(
    agent_id: str,
    run_id: str | None,
    context: RunContext | None,
) -> RunContext:
    """Resolve defaults once and reject conflicting caller identities."""
    if context is None:
        return local_run_context(agent_id, run_id=run_id)
    identity = context.identity
    if run_id is not None and run_id != identity.run_id:
        raise RunContextError("run_id does not match runtime context identity")
    if identity.agent_id != agent_id:
        raise RunContextError("agent_id does not match runtime context identity")
    identity.projection()
    return context
