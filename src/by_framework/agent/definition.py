"""Agent authoring and compilation into stable runtime definitions."""

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from .model import Model
from .runtime.serialization import stable_plan_hash
from .tools import FunctionTool, ToolSpec, freeze_json_schema


class AgentDefinitionError(ValueError):
    """An authoring object cannot compile into a valid definition."""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    instructions: str
    tools: tuple[ToolSpec, ...]
    model_ref: str
    output_schema: Mapping[str, Any] | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class PlanNode:
    id: str
    kind: str


@dataclass(frozen=True)
class PlanEdge:
    source: str
    target: str
    condition: str


@dataclass(frozen=True)
class ExecutionPlan:
    agent: AgentSpec
    nodes: tuple[PlanNode, ...]
    edges: tuple[PlanEdge, ...]
    schema_version: int = 1
    plan_hash: str = ""


@dataclass(frozen=True)
class CompiledAgent:
    spec: AgentSpec
    plan: ExecutionPlan
    model: Model
    tools: tuple[FunctionTool, ...]


class Agent:
    """Dynamic authoring object compiled before execution."""

    def __init__(
        self,
        name: str,
        instructions: str,
        model: Model,
        tools: tuple[FunctionTool, ...] | list[FunctionTool] = (),
        *,
        output_schema: Mapping[str, Any] | None = None,
    ):
        self.name = name
        self.instructions = instructions
        self.model = model
        self.tools = tuple(tools)
        self.output_schema = output_schema

    def compile(self) -> CompiledAgent:
        if not self.name:
            raise AgentDefinitionError("agent name must not be empty")
        names = [tool.spec.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise AgentDefinitionError("tool names must be unique")
        model_ref = getattr(self.model, "model_ref", None)
        if not isinstance(model_ref, str) or not model_ref:
            model_type = type(self.model)
            model_ref = f"{model_type.__module__}:{model_type.__qualname__}"
        spec = AgentSpec(
            self.name,
            self.instructions,
            tuple(t.spec for t in self.tools),
            model_ref,
            (
                None
                if self.output_schema is None
                else freeze_json_schema(self.output_schema)
            ),
        )
        unhashed = ExecutionPlan(
            agent=spec,
            nodes=(
                PlanNode("prepare_input", "input"),
                PlanNode("call_model", "model"),
                PlanNode("execute_tools", "tools"),
                PlanNode("final", "output"),
            ),
            edges=(
                PlanEdge("prepare_input", "call_model", "always"),
                PlanEdge("call_model", "execute_tools", "has_tool_calls"),
                PlanEdge("execute_tools", "call_model", "always"),
                PlanEdge("call_model", "final", "no_tool_calls"),
            ),
        )
        plan_hash = stable_plan_hash(unhashed)
        plan = ExecutionPlan(
            agent=unhashed.agent,
            nodes=unhashed.nodes,
            edges=unhashed.edges,
            schema_version=unhashed.schema_version,
            plan_hash=plan_hash,
        )
        return CompiledAgent(spec, plan, self.model, self.tools)
