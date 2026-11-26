from __future__ import annotations

from pathlib import Path

import pytest

from hookstorm.cli import main
from hookstorm.doctor import run_preflight


def _scenario_file(tmp_path: Path, *, signature: str = "valid") -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        f"""version: 1
name: Doctor smoke
delivery:
  signature: {signature}
  signature_provider: generic
events:
  - id: ping
    event_id: evt_doctor_001
    payload:
      ok: true
""",
        encoding="utf-8",
    )
    return path


def _check(checks, label: str, status: str | None = None):
    matches = [check for check in checks if check.label == label]
    if status is not None:
        matches = [check for check in matches if check.status == status]
    assert matches, f"Missing {status or ''} check for {label}: {checks}"
    return matches[0]


def test_preflight_checks_config_without_contacting_network(tmp_path, monkeypatch) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setattr(
        "hookstorm.doctor._check_connectivity",
        lambda *_args: pytest.fail("network helper must not run without --connect"),
    )

    checks = run_preflight(
        scenario,
        environment={
            "HOOKSTORM_TARGET_URL": "https://staging.example.test/webhook?token=private",
            "HOOKSTORM_SECRET": "private-secret",
        },
    )

    assert _check(checks, "Scenario", "PASS").message == "Doctor smoke (1 event(s))"
    assert _check(checks, "Target", "PASS").message.endswith("full URL hidden")
    assert _check(checks, "Target URL", "WARN")
    assert _check(checks, "Signing secret", "PASS").message == (
        "HOOKSTORM_SECRET is set (value hidden)"
    )
    assert _check(checks, "Network", "INFO")
    assert "private" not in " ".join(check.message for check in checks)


def test_preflight_fails_when_target_or_required_secret_is_missing(tmp_path) -> None:
    scenario = _scenario_file(tmp_path)
    checks = run_preflight(scenario, environment={})

    assert _check(checks, "Target", "FAIL")
    assert _check(checks, "Signing secret", "FAIL")
    assert not any(check.status == "PASS" for check in checks)


def test_preflight_does_not_require_secret_for_unsigned_scenario(tmp_path) -> None:
    scenario = _scenario_file(tmp_path, signature="none")
    checks = run_preflight(
        scenario,
        environment={"HOOKSTORM_TARGET_URL": "http://127.0.0.1:8000/webhook"},
    )

    assert _check(checks, "Signing secret", "PASS").message.startswith("not required")


@pytest.mark.parametrize(
    "target",
    [
        "ftp://staging.example.test/webhook",
        "https://user:password@staging.example.test/webhook",
        "https://staging.example.test:70000/webhook",
        "https://staging.example.test:0/webhook",
        "https://staging.example.test/webhook#fragment",
    ],
)
def test_preflight_rejects_unsafe_or_invalid_target_urls(tmp_path, target: str) -> None:
    scenario = _scenario_file(tmp_path)
    checks = run_preflight(
        scenario,
        target=target,
        environment={"HOOKSTORM_SECRET": "private-secret"},
    )

    assert _check(checks, "Target", "FAIL")
    assert _check(checks, "Network", "INFO")


def test_preflight_runs_socket_check_only_when_requested(tmp_path, monkeypatch) -> None:
    scenario = _scenario_file(tmp_path)
    captured = {}

    def fake_connect(parsed, timeout):
        captured["host"] = parsed.hostname
        captured["scheme"] = parsed.scheme
        captured["timeout"] = timeout
        return "TCP connection and TLS certificate verification succeeded"

    monkeypatch.setattr("hookstorm.doctor._check_connectivity", fake_connect)
    checks = run_preflight(
        scenario,
        target="https://staging.example.test:9443/webhook",
        environment={"HOOKSTORM_SECRET": "private-secret"},
        connect=True,
        timeout=7.5,
    )

    assert captured == {
        "host": "staging.example.test",
        "scheme": "https",
        "timeout": 7.5,
    }
    assert _check(checks, "Network", "PASS").message.startswith("TCP connection")


def test_preflight_reports_socket_errors(tmp_path, monkeypatch) -> None:
    scenario = _scenario_file(tmp_path)

    def fail_connect(*_args):
        raise TimeoutError("timed out")

    monkeypatch.setattr("hookstorm.doctor._check_connectivity", fail_connect)
    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={"HOOKSTORM_SECRET": "private-secret"},
        connect=True,
    )

    assert _check(checks, "Network", "FAIL").message.startswith(
        "could not reach the endpoint socket: TimeoutError"
    )


def test_preflight_reports_invalid_scenario_and_continues_other_checks(tmp_path) -> None:
    missing = tmp_path / "missing.yaml"
    checks = run_preflight(
        missing,
        target="http://127.0.0.1:8000/webhook",
        environment={},
    )

    assert _check(checks, "Scenario", "FAIL")
    assert _check(checks, "Target", "PASS")
    assert _check(checks, "Signing secret", "INFO")


def test_doctor_cli_does_not_print_target_tokens_or_secret(tmp_path, monkeypatch, capsys) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setenv(
        "HOOKSTORM_TARGET_URL",
        "https://staging.example.test/webhook?token=private-token",
    )
    monkeypatch.setenv("HOOKSTORM_SECRET", "private-secret")

    assert main(["doctor", str(scenario)]) == 0

    output = capsys.readouterr().out
    assert "Preflight: READY" in output
    assert "private-token" not in output
    assert "private-secret" not in output
    assert "full URL hidden" in output


def test_doctor_cli_returns_one_for_missing_staging_settings(tmp_path, monkeypatch, capsys) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.delenv("HOOKSTORM_TARGET_URL", raising=False)
    monkeypatch.delenv("HOOKSTORM_SECRET", raising=False)

    assert main(["doctor", str(scenario)]) == 1
    output = capsys.readouterr().out
    assert "Preflight: NOT READY" in output
    assert "HOOKSTORM_TARGET_URL" in output
    assert "HOOKSTORM_SECRET" in output


def test_doctor_cli_rejects_out_of_range_timeout(tmp_path, capsys) -> None:
    scenario = _scenario_file(tmp_path)

    assert main(["doctor", str(scenario), "--timeout", "60"]) == 2
    assert "--timeout must be between 0.1 and 30 seconds" in capsys.readouterr().err
