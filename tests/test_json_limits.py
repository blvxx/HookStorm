from __future__ import annotations

import pytest

from hookstorm.json_limits import (
    MAX_JSON_NESTING,
    ensure_json_text_depth,
    ensure_value_depth,
)


def test_json_text_depth_ignores_brackets_inside_strings() -> None:
    ensure_json_text_depth(
        '{"message":"[{{}}] \\"quoted\\": \\ [ still text"}'
    )


def test_json_text_depth_rejects_excessive_object_and_array_nesting() -> None:
    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        ensure_json_text_depth("[" * (MAX_JSON_NESTING + 1) + "null" + "]" * (MAX_JSON_NESTING + 1))

    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        ensure_json_text_depth("{" * (MAX_JSON_NESTING + 1) + "}" * (MAX_JSON_NESTING + 1))


def test_python_value_depth_rejects_nested_containers_without_recursion() -> None:
    value: object = None
    for _ in range(MAX_JSON_NESTING + 1):
        value = [value]

    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        ensure_value_depth(value)


def test_python_value_depth_detects_cycles_and_handles_shared_values() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        ensure_value_depth(cyclic)

    shared = {"ok": True}
    ensure_value_depth({"left": shared, "right": shared})


def test_json_depth_limit_configuration_is_validated() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        ensure_json_text_depth("{}", max_depth=-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        ensure_value_depth({}, max_depth=True)
