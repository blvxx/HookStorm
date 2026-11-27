from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hookstorm.pytest_plugin import (
    HookStormRunner,
    _enabled,
    _target_from_environment,
    load_hookstorm_scenario,
)


def scenario() -> dict:
    return {
        "version": 1,
        "name": "pytest plugin contract",
        "events": [
            {"id": "invoice.paid", "event_id": "evt_pytest_001", "payload": {}}
        ],
    }


def test_load_hookstorm_scenario_accepts_mapping_and_yaml_path(tmp_path: Path) -> None:
    mapping = load_hookstorm_scenario(scenario())
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(
        "version: 1\nname: from file\nevents:\n  - id: ping\n    event_id: evt_file\n",
        encoding="utf-8",
    )

    loaded = load_hookstorm_scenario(scenario_file)

    assert mapping["name"] == "pytest plugin contract"
    assert loaded["name"] == "from file"
    assert loaded["events"][0]["event_id"] == "evt_file"


def test_load_hookstorm_scenario_rejects_invalid_input() -> None:
    with pytest.raises(TypeError, match="mapping or a YAML file path"):
        load_hookstorm_scenario(42)  # type: ignore[arg-type]


def test_target_resolution_prefers_explicit_url_and_ignores_blank_values() -> None:
    assert _target_from_environment(
        {
            "HOOKSTORM_TARGET_URL": " https://staging.example.test/webhook ",
            "HOOKSTORM_TARGET": "http://127.0.0.1:8000/webhook",
        }
    ) == "https://staging.example.test/webhook"
    assert _target_from_environment(
        {"HOOKSTORM_TARGET_URL": "  ", "HOOKSTORM_TARGET": "http://127.0.0.1:8000"}
    ) == "http://127.0.0.1:8000"
    assert _target_from_environment({"HOOKSTORM_TARGET_URL": "  "}) is None
    assert _target_from_environment({"HOOKSTORM_TARGET": "http://127.0.0.1:8000"}) == (
        "http://127.0.0.1:8000"
    )


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("true", False), (None, False)])
def test_environment_opt_ins_require_exact_one(value: str | None, expected: bool) -> None:
    assert _enabled(value) is expected


def test_async_runner_validates_and_passes_only_configured_secret(monkeypatch) -> None:
    observed: dict[str, object] = {}

    async def fake_run(
        loaded: dict,
        target: str,
        *,
        allow_remote: bool,
        allow_insecure_http: bool,
        secret: str | None,
    ) -> dict:
        observed.update(
            scenario=loaded,
            target=target,
            allow_remote=allow_remote,
            allow_insecure_http=allow_insecure_http,
            secret=secret,
        )
        return {"summary": {"success": True}}

    monkeypatch.setattr("hookstorm.pytest_plugin.run_scenario", fake_run)
    runner = HookStormRunner(
        target="https://staging.example.test/hook",
        allow_remote=True,
        allow_insecure_http=False,
        secret_env="TEST_HOOK_SECRET",
        environ={"TEST_HOOK_SECRET": "test-only-secret"},
    )

    result = asyncio.run(runner.run(scenario()))

    assert result["summary"]["success"] is True
    assert observed["scenario"]["name"] == "pytest plugin contract"
    assert observed["target"] == "https://staging.example.test/hook"
    assert observed["allow_remote"] is True
    assert observed["allow_insecure_http"] is False
    assert observed["secret"] == "test-only-secret"


def test_async_runner_does_not_fall_back_when_selected_secret_is_missing(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    async def fake_run(
        loaded: dict,
        target: str,
        *,
        allow_remote: bool,
        allow_insecure_http: bool,
        secret: str | None,
    ) -> dict:
        observed["secret"] = secret
        return {"summary": {"success": True}}

    monkeypatch.setenv("HOOKSTORM_SECRET", "must-not-be-used")
    monkeypatch.setattr("hookstorm.pytest_plugin.run_scenario", fake_run)
    runner = HookStormRunner(
        target="http://127.0.0.1:8000/webhook",
        secret_env="CUSTOM_HOOK_SECRET",
        environ={},
    )

    asyncio.run(runner.run(scenario()))

    assert observed["secret"] == ""


def test_sync_runner_executes_outside_event_loop(monkeypatch) -> None:
    async def fake_run(
        loaded: dict,
        target: str,
        *,
        allow_remote: bool,
        allow_insecure_http: bool,
        secret: str | None,
    ) -> dict:
        return {"target": target, "success": True}

    monkeypatch.setattr("hookstorm.pytest_plugin.run_scenario", fake_run)
    runner = HookStormRunner(target="http://127.0.0.1:8000/webhook")

    assert runner.run_sync(scenario()) == {
        "target": "http://127.0.0.1:8000/webhook",
        "success": True,
    }


def test_sync_runner_explains_active_event_loop(monkeypatch) -> None:
    runner = HookStormRunner(target="http://127.0.0.1:8000/webhook")

    async def invoke() -> None:
        with pytest.raises(RuntimeError, match="use await hookstorm.run"):
            runner.run_sync(scenario())

    asyncio.run(invoke())


def test_hookstorm_fixture_uses_explicit_environment_flags(monkeypatch) -> None:
    from hookstorm.pytest_plugin import hookstorm

    monkeypatch.setenv("HOOKSTORM_ALLOW_REMOTE", "1")
    monkeypatch.setenv("HOOKSTORM_ALLOW_INSECURE_HTTP", "0")
    monkeypatch.setenv("HOOKSTORM_SECRET_ENV", "STAGING_WEBHOOK_SECRET")

    runner = hookstorm.__wrapped__("https://staging.example.test/webhook")

    assert runner.target == "https://staging.example.test/webhook"
    assert runner.allow_remote is True
    assert runner.allow_insecure_http is False
    assert runner.secret_env == "STAGING_WEBHOOK_SECRET"


def test_plugin_fixture_skips_when_target_is_missing(monkeypatch) -> None:
    from hookstorm.pytest_plugin import hookstorm_target

    monkeypatch.delenv("HOOKSTORM_TARGET_URL", raising=False)
    monkeypatch.delenv("HOOKSTORM_TARGET", raising=False)

    with pytest.raises(pytest.skip.Exception):
        hookstorm_target.__wrapped__()
