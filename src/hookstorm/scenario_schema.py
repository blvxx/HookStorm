"""Load the packaged JSON Schema for HookStorm scenario version 1."""

from __future__ import annotations

from importlib.resources import files


def read_scenario_schema() -> str:
    """Return the version 1 JSON Schema as UTF-8 JSON text."""
    return (
        files("hookstorm")
        .joinpath("schemas", "scenario-v1.schema.json")
        .read_text(encoding="utf-8")
    )
