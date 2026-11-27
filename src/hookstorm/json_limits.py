"""Deterministic recursion limits for untrusted JSON and scenario values."""

from __future__ import annotations

from typing import Any

MAX_JSON_NESTING = 100


def ensure_json_text_depth(
    source: str,
    *,
    max_depth: int = MAX_JSON_NESTING,
) -> None:
    """Reject JSON text whose object/array nesting exceeds a stable limit.

    This is a lexical pass, not a JSON parser. It ignores brackets inside
    quoted strings and escaped characters; the standard JSON parser remains
    responsible for syntax validation.
    """
    if not isinstance(source, str):
        raise TypeError("JSON source must be text")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")

    depth = 0
    in_string = False
    escaped = False
    for character in source:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError(
                    f"JSON nesting exceeds the {max_depth}-level safety limit"
                )
        elif character in "]}":
            # Invalid closing delimiters are left for json.loads to diagnose.
            if depth:
                depth -= 1


def ensure_value_depth(
    value: Any,
    *,
    max_depth: int = MAX_JSON_NESTING,
) -> None:
    """Bound nested Python JSON-like containers without recursive traversal.

    The iterative walk also handles cyclic input safely: each container is
    revisited only when reached at a greater depth, so cycles hit the limit
    instead of looping forever.
    """
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")

    # Root objects/arrays count as nesting level one, matching the text scanner.
    stack: list[tuple[Any, int]] = [(value, 1)]
    deepest_seen: dict[int, int] = {}
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list, tuple)):
            continue
        if depth > max_depth:
            raise ValueError(
                f"JSON nesting exceeds the {max_depth}-level safety limit"
            )

        identity = id(current)
        previous_depth = deepest_seen.get(identity)
        if previous_depth is not None and previous_depth >= depth:
            continue
        deepest_seen[identity] = depth

        children = current.values() if isinstance(current, dict) else current
        for child in children:
            if isinstance(child, (dict, list, tuple)):
                stack.append((child, depth + 1))
