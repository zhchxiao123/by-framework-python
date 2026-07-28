"""Controlled plan serialization spike tests."""

from dataclasses import dataclass

import pytest

from by_framework.agent.runtime import (
    SerializationError,
    canonical_json,
    stable_plan_hash,
)


@dataclass(frozen=True)
class Plan:
    version: int
    nodes: list[dict]


def test_plan_hash_is_stable_across_mapping_insertion_order():
    left = {"version": 1, "nodes": [{"id": "model", "policy": {"b": 2, "a": 1}}]}
    right = {"nodes": [{"policy": {"a": 1, "b": 2}, "id": "model"}], "version": 1}

    assert canonical_json(left) == canonical_json(right)
    assert stable_plan_hash(left) == stable_plan_hash(right)
    assert stable_plan_hash(Plan(**left)) == stable_plan_hash(left)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"callable": lambda: None},
        {"bytes": b"pickle-looking-data"},
        {"set": {"a", "b"}},
        {"nan": float("nan")},
        {1: "non-string key"},
    ],
)
def test_controlled_serializer_rejects_unsafe_or_ambiguous_values(unsafe):
    with pytest.raises(SerializationError):
        canonical_json(unsafe)
