"""Typed function tools with definition/execution separation."""

import inspect
import json
import math
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Union, get_args, get_origin, get_type_hints

from .model import ToolCall, ToolDeclaration, Usage


class ToolError(RuntimeError):
    """Base tool contract error."""


class ToolValidationError(ToolError):
    """Tool arguments do not satisfy the callable contract."""


class ToolExecutionError(ToolError):
    """A tool implementation failed."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    side_effect: str = "none"
    implementation_ref: str | None = None
    aggregates_usage: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", freeze_json_schema(self.input_schema))
        object.__setattr__(
            self, "output_schema", freeze_json_schema(self.output_schema)
        )

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(self.name, self.description, self.input_schema)


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    tool_name: str
    output: Any
    usage: Usage = Usage()

    def model_content(self) -> str:
        if isinstance(self.output, str):
            return self.output
        return json.dumps(
            self.output, allow_nan=False, ensure_ascii=False, sort_keys=True
        )


class FunctionTool:
    """A Python callable plus its serializable tool definition."""

    def __init__(
        self,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        side_effect: str = "none",
        implementation_ref: str | None = None,
        aggregates_usage: bool = False,
    ):
        self.function = function
        self.spec = ToolSpec(
            name=name or function.__name__,
            description=description or inspect.getdoc(function) or "",
            input_schema=_function_schema(function),
            output_schema=_return_schema(function),
            side_effect=side_effect,
            implementation_ref=implementation_ref,
            aggregates_usage=aggregates_usage,
        )


class ToolExecutor:
    """Executes registered function tools with signature validation."""

    def __init__(self, tools: Mapping[str, FunctionTool]):
        self._tools = dict(tools)

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            raise ToolValidationError(f"unknown tool {call.name!r}")
        try:
            validate_tool_call(call, tool.spec)
        except ToolValidationError as exc:
            raise ToolValidationError(
                f"invalid arguments for tool {call.name!r}: {exc}"
            ) from exc
        try:
            signature = inspect.signature(tool.function)
            bound = signature.bind(**call.arguments)
            bound.apply_defaults()
        except TypeError as exc:
            raise ToolValidationError(
                f"invalid arguments for tool {call.name!r}: {exc}"
            ) from exc
        try:
            output = tool.function(*bound.args, **bound.kwargs)
            if inspect.isawaitable(output):
                output = await output
            _validate_value(output, tool.spec.output_schema, "$")
            nested_usage = Usage()
            if tool.spec.aggregates_usage:
                if not isinstance(output, Mapping) or not isinstance(
                    output.get("usage"), Mapping
                ):
                    raise ToolValidationError(
                        "usage-aggregating tool output requires a usage mapping"
                    )
                usage = output["usage"]
                nested_usage = Usage(
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                )
            result = ToolResult(call.id, call.name, output, nested_usage)
            result.model_content()
        except Exception as exc:
            raise ToolExecutionError(f"tool {call.name!r} failed: {exc}") from exc
        return result


def validate_tool_call(call: ToolCall, spec: ToolSpec) -> None:
    """Validate a call against a tool definition without executing it."""
    if call.name != spec.name:
        raise ToolValidationError(
            f"tool call name {call.name!r} does not match spec {spec.name!r}"
        )
    _validate_value(call.arguments, spec.input_schema, "$")


def _function_schema(function: Callable[..., Any]) -> dict[str, Any]:
    signature = inspect.signature(function)
    hints = get_type_hints(function)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            raise ToolValidationError(
                f"tool {function.__name__!r} has unsupported parameter {name!r}"
            )
        annotation = hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            raise ToolValidationError(
                f"tool parameter {function.__name__}.{name} requires a type annotation"
            )
        properties[name] = _annotation_schema(annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        else:
            properties[name]["default"] = parameter.default
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _return_schema(function: Callable[..., Any]) -> dict[str, Any]:
    hints = get_type_hints(function)
    annotation = hints.get("return", inspect.signature(function).return_annotation)
    if annotation is inspect.Signature.empty:
        raise ToolValidationError(
            f"tool {function.__name__!r} requires a return type annotation"
        )
    return _annotation_schema(annotation)


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    primitive = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if annotation in primitive:
        return {"type": primitive[annotation]}
    if annotation is list:
        return {"type": "array", "items": {}}
    if annotation is dict:
        return {"type": "object"}
    if annotation is Any:
        return {}
    origin = get_origin(annotation)
    if origin is list:
        (item_type,) = get_args(annotation)
        return {"type": "array", "items": _annotation_schema(item_type)}
    if origin is dict:
        key_type, value_type = get_args(annotation)
        if key_type is not str:
            raise ToolValidationError("tool mappings must use string keys")
        return {
            "type": "object",
            "additionalProperties": _annotation_schema(value_type),
        }
    if origin in (Union, types.UnionType):
        options = get_args(annotation)
        return {"anyOf": [_annotation_schema(option) for option in options]}
    if annotation is types.NoneType:
        return {"type": "null"}
    raise ToolValidationError(f"unsupported tool annotation {annotation!r}")


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    """Validate JSON-like arguments against the generated schema subset."""
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                _validate_value(value, option, path)
                return
            except ToolValidationError as exc:
                errors.append(str(exc))
        error_details = "; ".join(errors)
        raise ToolValidationError(
            f"{path}: value does not match any allowed type ({error_details})"
        )

    expected = schema.get("type")
    valid = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: (
            isinstance(item, (int, float)) and not isinstance(item, bool)
        ),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, Mapping),
    }
    if expected is not None and not valid[expected](value):
        raise ToolValidationError(f"{path}: expected {expected}")
    if expected == "number" and isinstance(value, float) and not math.isfinite(value):
        raise ToolValidationError(f"{path}: expected a finite number")

    if expected == "array":
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
    elif expected == "object":
        properties = schema.get("properties")
        if properties is not None:
            missing = set(schema.get("required", ())) - value.keys()
            if missing:
                raise ToolValidationError(
                    f"{path}: missing required properties {sorted(missing)!r}"
                )
            if schema.get("additionalProperties") is False:
                extra = value.keys() - properties.keys()
                if extra:
                    raise ToolValidationError(
                        f"{path}: unexpected properties {sorted(extra)!r}"
                    )
            for name, item in value.items():
                if name in properties:
                    _validate_value(item, properties[name], f"{path}.{name}")
        else:
            value_schema = schema.get("additionalProperties")
            if isinstance(value_schema, Mapping):
                for name, item in value.items():
                    _validate_value(item, value_schema, f"{path}.{name}")


def freeze_json_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_json_schema(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_json_schema(item) for item in value)
    return value
