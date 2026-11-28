from __future__ import annotations

import pytest

from hookstorm.json_safety import (
    MAX_JSON_NESTING_DEPTH,
    JSONNestingError,
    ensure_json_nesting_depth,
)


def test_accepts_json_at_the_maximum_nesting_depth() -> None:
    document = "[" * MAX_JSON_NESTING_DEPTH + "0" + "]" * MAX_JSON_NESTING_DEPTH

    ensure_json_nesting_depth(document)


def test_rejects_json_over_the_maximum_nesting_depth() -> None:
    document = "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (
        MAX_JSON_NESTING_DEPTH + 1
    )

    with pytest.raises(JSONNestingError, match="maximum depth of 128"):
        ensure_json_nesting_depth(document, label="response JSON")


def test_ignores_brackets_and_braces_inside_json_strings() -> None:
    ensure_json_nesting_depth(
        '{"message":"these are characters: [{[{}]}]", "items":[{"ok":true}]}'
    )


def test_handles_bytes_and_escaped_quotes_in_json_strings() -> None:
    ensure_json_nesting_depth(
        b'{"message":"escaped quote: \\" and bracket: ]", "nested":[0]}'
    )
