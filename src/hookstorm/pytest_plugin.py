"""Opt-in pytest fixtures and helpers for HookStorm integration tests.

Enable this plugin from a project's pytest configuration with:

    pytest_plugins = ["hookstorm.pytest_plugin"]

The plugin is intentionally not auto-loaded. Install it with hookstorm[pytest].
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

import pytest

from hookstorm.config import load_scenario, validate_scenario
from hookstorm.runner import run_scenario

ScenarioInput: TypeAlias = dict[str, Any] | str | Path


def load_hookstorm_scenario(scenario: ScenarioInput) -> dict[str, Any]:
    """Load and validate a scenario mapping or a YAML file path."""
    if isinstance(scenario, dict):
        return validate_scenario(scenario)
    if isinstance(scenario, (str, Path)):
        return load_scenario(scenario)
    raise TypeError("scenario must be a mapping or a YAML file path")


def _enabled(value: str | None) -> bool:
    """Interpret the deliberately narrow environment-flag format."""
    return value == "1"


def _target_from_environment(environ: Mapping[str, str]) -> str | None:
    """Prefer the explicit URL variable while retaining the exporter alias."""
    for name in ("HOOKSTORM_TARGET_URL", "HOOKSTORM_TARGET"):
        value = environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class HookStormRunner:
    """Run validated HookStorm scenarios against one configured test endpoint."""

    target: str
    allow_remote: bool = False
    allow_insecure_http: bool = False
    secret_env: str = "HOOKSTORM_SECRET"
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    async def run(self, scenario: ScenarioInput) -> dict[str, Any]:
        """Execute a scenario asynchronously and return the standard result document."""
        normalized = load_hookstorm_scenario(scenario)
        secret = self.environ.get(self.secret_env) if self.secret_env else None
        return await run_scenario(
            normalized,
            self.target,
            allow_remote=self.allow_remote,
            allow_insecure_http=self.allow_insecure_http,
            secret=secret,
        )

    def run_sync(self, scenario: ScenarioInput) -> dict[str, Any]:
        """Execute from a synchronous test; use await run in async tests."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(scenario))
        raise RuntimeError(
            "HookStormRunner.run_sync() cannot run inside an active event loop; "
            "use await hookstorm.run(scenario) instead"
        )


@pytest.fixture
def hookstorm_target() -> str:
    """Return the configured test target, skipping when no target is provided."""
    target = _target_from_environment(os.environ)
    if target is None:
        pytest.skip(
            "Set HOOKSTORM_TARGET_URL to a dedicated webhook test endpoint "
            "to run HookStorm integration tests"
        )
    return target


@pytest.fixture
def hookstorm(hookstorm_target: str) -> HookStormRunner:
    """Provide a runner configured only from explicit test environment variables."""
    secret_env = os.getenv("HOOKSTORM_SECRET_ENV", "HOOKSTORM_SECRET")
    return HookStormRunner(
        target=hookstorm_target,
        allow_remote=_enabled(os.getenv("HOOKSTORM_ALLOW_REMOTE")),
        allow_insecure_http=_enabled(os.getenv("HOOKSTORM_ALLOW_INSECURE_HTTP")),
        secret_env=secret_env,
    )


@pytest.fixture
def hookstorm_scenario():
    """Provide a loader for a scenario mapping or YAML path."""
    return load_hookstorm_scenario
