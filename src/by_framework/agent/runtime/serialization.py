"""Controlled serialization used for persisted native runtime definitions."""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any


class SerializationError(TypeError):
    """Raised when a value is outside the controlled persistence domain."""


def _normalize(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SerializationError(f"{path}: non-finite floats are not supported")
        return value
    if isinstance(value, Enum):
        return _normalize(value.value, path)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalize(getattr(value, field.name), f"{path}.{field.name}")
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SerializationError(f"{path}: object keys must be strings")
            normalized[key] = _normalize(item, f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise SerializationError(
        f"{path}: unsupported persisted value type {type(value).__name__}"
    )


def canonical_json(value: Any) -> bytes:
    """Encode a value as deterministic UTF-8 JSON without unsafe fallbacks."""
    normalized = _normalize(value, "$")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def stable_plan_hash(plan: Any) -> str:
    """Return the content-addressed identifier for a compiled plan."""
    return f"sha256:{hashlib.sha256(canonical_json(plan)).hexdigest()}"
