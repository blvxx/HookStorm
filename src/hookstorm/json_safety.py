"""Bounded JSON structure checks for untrusted scenario and response data."""

from __future__ import annotations

MAX_JSON_NESTING_DEPTH = 128


class JSONNestingError(ValueError):
    """Raised when JSON input exceeds HookStorm's supported nesting depth."""


def ensure_json_nesting_depth(document: str | bytes, *, label: str = "JSON") -> None:
    """Reject excessive object/array nesting without parsing or allocating a JSON tree.

    The scanner understands quoted strings and backslash escapes, so braces and
    brackets embedded in JSON string values do not contribute to the nesting depth.
    """
    codes = (ord(character) for character in document) if isinstance(document, str) else document
    depth = 0
    in_string = False
    escaped = False

    for code in codes:
        if in_string:
            if escaped:
                escaped = False
            elif code == 0x5C:  # backslash
                escaped = True
            elif code == 0x22:  # double quote
                in_string = False
            continue

        if code == 0x22:
            in_string = True
        elif code in (0x5B, 0x7B):  # [ or {
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise JSONNestingError(
                    f"{label} nesting exceeds the maximum depth of "
                    f"{MAX_JSON_NESTING_DEPTH}"
                )
        elif code in (0x5D, 0x7D):  # ] or }
            depth = max(0, depth - 1)
