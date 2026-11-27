from __future__ import annotations

import json
from pathlib import Path

import pytest

from hookstorm.cli import main
from hookstorm.history import list_runs, save_run


def _scenario_file(tmp_path) -> str:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """version: 1
name: CLI history smoke
events:
  - id: ping
    event_id: evt_cli_history
    payload:
      ok: true
""",
        encoding="utf-8",
    )
    return str(path)


def test_run_command_persists_history_and_history_commands_read_it(
    receiver, tmp_path, capsys
) -> None:
    base, _ = receiver
    scenario = _scenario_file(tmp_path)
    database = tmp_path / "history.sqlite3"
    report = tmp_path / "run.html"

    exit_code = main([
        "run",
        scenario,
        "--target",
        base,
        "--history-db",
        str(database),
        "--output",
        str(report),
    ])
    assert exit_code == 0
    assert report.is_file()
    output = capsys.readouterr().out
    assert "History:" in output
    run_id = list_runs(database)[0]["run_id"]

    assert main(["history", "list", "--db", str(database)]) == 0
    listing = capsys.readouterr().out
    assert run_id in listing
    assert "CLI history smoke" in listing

    assert main(["history", "show", run_id, "--db", str(database)]) == 0
    details = capsys.readouterr().out
    assert "evt_cli_history" in details
    assert "Pass rate: 100.00%" in details


def test_run_command_can_opt_out_of_history(receiver, tmp_path, capsys) -> None:
    base, _ = receiver
    scenario = _scenario_file(tmp_path)
    database = tmp_path / "history.sqlite3"

    exit_code = main([
        "run",
        scenario,
        "--target",
        base,
        "--history-db",
        str(database),
        "--no-history",
        "--output",
        str(tmp_path / "no-history.html"),
    ])
    assert exit_code == 0
    assert list_runs(database) == []
    assert "History: disabled" in capsys.readouterr().out


def test_history_compare_command_returns_nonzero_for_regression(
    tmp_path, capsys
) -> None:
    database = tmp_path / "history.sqlite3"

    baseline = {
        "run_id": "run_base",
        "scenario": "comparison scenario",
        "started_at": "2026-11-26T10:00:00+00:00",
        "target": "http://127.0.0.1:8000/hook",
        "duration_ms": 20.0,
        "summary": {"success": True, "total_deliveries": 1, "passed": 1, "failed": 0, "attempts": 1},
        "deliveries": [{
            "event_type": "payment.succeeded",
            "event_id": "evt_compare",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 10.0, "error": None}],
        }],
    }
    current = {
        **baseline,
        "run_id": "run_current",
        "started_at": "2026-11-26T10:01:00+00:00",
        "duration_ms": 30.0,
        "summary": {"success": False, "total_deliveries": 1, "passed": 0, "failed": 1, "attempts": 1},
        "deliveries": [{
            "event_type": "payment.succeeded",
            "event_id": "evt_compare",
            "duplicate_index": 0,
            "passed": False,
            "attempts": [{"status_code": 500, "latency_ms": 15.0, "error": None}],
        }],
    }
    save_run(baseline, database)
    save_run(current, database)

    exit_code = main([
        "history",
        "compare",
        "run_base",
        "--current",
        "run_current",
        "--db",
        str(database),
    ])
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "REGRESSIONS" in output
    assert "run_current" in output


def test_history_compare_command_returns_zero_without_regression(
    tmp_path, capsys
) -> None:
    database = tmp_path / "history.sqlite3"
    baseline = {
        "run_id": "run_base",
        "scenario": "healthy",
        "started_at": "2026-11-26T10:00:00+00:00",
        "target": "http://127.0.0.1:8000/hook",
        "duration_ms": 20.0,
        "summary": {"success": True, "total_deliveries": 1, "passed": 1, "failed": 0, "attempts": 1},
        "deliveries": [{
            "event_type": "ping",
            "event_id": "evt_same",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 10.0, "error": None}],
        }],
    }
    current = {
        **baseline,
        "run_id": "run_current",
        "started_at": "2026-11-26T10:01:00+00:00",
        "duration_ms": 18.0,
        "deliveries": [{
            "event_type": "ping",
            "event_id": "evt_same",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 9.0, "error": None}],
        }],
    }
    save_run(baseline, database)
    save_run(current, database)

    exit_code = main([
        "history",
        "compare",
        "run_base",
        "--current",
        "run_current",
        "--db",
        str(database),
    ])
    assert exit_code == 0
    assert "NO REGRESSIONS DETECTED" in capsys.readouterr().out



def test_history_compare_requires_explicit_override_for_config_change(
    tmp_path, capsys
) -> None:
    database = tmp_path / "history.sqlite3"
    baseline = {
        "run_id": "run_config_base",
        "scenario": "configuration check",
        "scenario_fingerprint": "a" * 64,
        "started_at": "2026-11-26T10:00:00+00:00",
        "target": "http://127.0.0.1:8000/hook",
        "duration_ms": 20.0,
        "summary": {"success": True, "total_deliveries": 1, "passed": 1, "failed": 0, "attempts": 1},
        "deliveries": [{
            "event_type": "ping",
            "event_id": "evt_config",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 10.0, "error": None}],
        }],
    }
    current = {
        **baseline,
        "run_id": "run_config_current",
        "scenario_fingerprint": "b" * 64,
        "started_at": "2026-11-26T10:01:00+00:00",
    }
    save_run(baseline, database)
    save_run(current, database)

    command = [
        "history", "compare", "run_config_base",
        "--current", "run_config_current", "--db", str(database),
    ]
    assert main(command) == 1
    output = capsys.readouterr().out
    assert "scenario configuration differs" in output
    assert "scenario configuration changed" in output

    assert main([*command, "--allow-config-change"]) == 0
    output = capsys.readouterr().out
    assert "explicit baseline override is active" in output
    assert "NO REGRESSIONS DETECTED" in output


def test_history_export_and_compare_file_cli(tmp_path, capsys) -> None:
    database = tmp_path / "history.sqlite3"
    baseline = {
        "run_id": "run_base_file",
        "scenario": "file comparison scenario",
        "started_at": "2026-11-26T10:00:00+00:00",
        "target": "http://127.0.0.1:8000/hook",
        "duration_ms": 20.0,
        "summary": {
            "success": True, "total_deliveries": 1, "passed": 1,
            "failed": 0, "attempts": 1,
        },
        "deliveries": [{
            "event_type": "payment.succeeded",
            "event_id": "evt_file_compare",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 10.0, "error": None}],
        }],
    }
    current = {
        **baseline,
        "run_id": "run_current_file",
        "started_at": "2026-11-26T10:01:00+00:00",
        "duration_ms": 25.0,
        "summary": {
            "success": False, "total_deliveries": 1, "passed": 0,
            "failed": 1, "attempts": 1,
        },
        "deliveries": [{
            "event_type": "payment.succeeded",
            "event_id": "evt_file_compare",
            "duplicate_index": 0,
            "passed": False,
            "attempts": [{"status_code": 500, "latency_ms": 12.0, "error": None}],
        }],
    }
    save_run(baseline, database)

    baseline_file = tmp_path / "baseline.json"
    assert main([
        "history", "export", "run_base_file",
        "--output", str(baseline_file), "--db", str(database),
    ]) == 0
    assert "Exported HookStorm baseline" in capsys.readouterr().out

    save_run(current, database)
    exit_code = main([
        "history", "compare-file", str(baseline_file), "--db", str(database),
    ])
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "REGRESSIONS" in output
    assert "run_current_file" in output


def test_init_command_creates_project_files_and_prints_next_steps(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([
        "init",
        "--signature-provider", "stripe",
        "--event-type", "invoice.created",
        "--event-id", "evt_init_invoice_001",
    ]) == 0

    scenario = tmp_path / "scenarios" / "webhook.yaml"
    workflow = tmp_path / ".github" / "workflows" / "webhook-regression.yml"
    assert scenario.is_file()
    assert workflow.is_file()
    assert "signature_provider: stripe" in scenario.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "Next steps:" in output
    assert "HOOKSTORM_TARGET_URL" in output
    assert "HOOKSTORM_SECRET" in output
    assert "No target URL or secret was written" in output


def test_init_command_refuses_to_overwrite_without_force(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    scenario = tmp_path / "scenarios" / "webhook.yaml"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("preserve existing scenario", encoding="utf-8")

    assert main(["init"]) == 2
    assert scenario.read_text(encoding="utf-8") == "preserve existing scenario"
    assert not (tmp_path / ".github" / "workflows" / "webhook-regression.yml").exists()
    assert "Use --force" in capsys.readouterr().err



def test_init_command_handles_working_directory_path_alias(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    # Simulate Windows returning a different cwd spelling than the one
    # used by resolved generated paths.
    simulated_cwd = tmp_path.parent / "cwd-alias"
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: simulated_cwd))

    assert main([
        "init",
        "--no-workflow",
        "--signature-provider",
        "generic",
    ]) == 0

    scenario = (tmp_path / "scenarios" / "webhook.yaml").resolve()
    output = capsys.readouterr().out
    assert scenario.is_file()
    assert str(scenario) in output


def test_init_interactive_wizard_builds_retry_and_duplicate_scenario(
    tmp_path, monkeypatch, capsys
) -> None:
    from hookstorm.config import load_scenario

    class InteractiveStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter([
        "stripe",
        "invoice.paid",
        "evt_invoice_paid_001",
        "3",
        "2",
    ])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hookstorm.cli.sys.stdin", InteractiveStdin())
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["init", "--no-workflow"]) == 0

    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")
    assert scenario["delivery"]["signature_provider"] == "stripe"
    assert scenario["delivery"]["duplicate_count"] == 2
    assert scenario["retry"]["max_attempts"] == 3
    assert scenario["events"][0]["id"] == "invoice.paid"
    assert scenario["events"][0]["event_id"] == "evt_invoice_paid_001"
    assert "Next steps:" in capsys.readouterr().out


def test_init_flags_support_noninteractive_scenario_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    from hookstorm.config import load_scenario

    monkeypatch.chdir(tmp_path)

    assert main([
        "init",
        "--no-workflow",
        "--signature-provider", "shopify",
        "--event-type", "order.updated",
        "--event-id", "evt_order_updated_001",
        "--max-attempts", "4",
        "--duplicate-count", "1",
    ]) == 0

    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")
    assert scenario["delivery"]["signature_provider"] == "shopify"
    assert scenario["delivery"]["duplicate_count"] == 1
    assert scenario["retry"]["max_attempts"] == 4
    assert scenario["events"][0]["id"] == "order.updated"
    assert scenario["events"][0]["event_id"] == "evt_order_updated_001"
    capsys.readouterr()


def test_run_and_history_show_preserve_injected_fault_diagnostics(
    receiver, tmp_path, capsys
) -> None:
    base, _state = receiver
    scenario = tmp_path / "fault-diagnostics.yaml"
    scenario.write_text(
        """version: 1
name: CLI injected fault diagnostics
retry:
  max_attempts: 2
  backoff_ms: 1
delivery:
  fault_injection:
    connect_error_attempts: [1]
events:
  - id: ping
    event_id: evt_cli_fault
    payload: {}
""",
        encoding="utf-8",
    )
    database = tmp_path / "history.sqlite3"

    assert main([
        "run",
        str(scenario),
        "--target",
        base,
        "--history-db",
        str(database),
        "--output",
        str(tmp_path / "fault-report.html"),
    ]) == 0

    output = capsys.readouterr().out
    assert "Injected faults: 1" in output
    assert "Connect fault" in output
    run_id = list_runs(database)[0]["run_id"]

    assert main(["history", "show", run_id, "--db", str(database)]) == 0
    details = capsys.readouterr().out
    assert "Injected faults: 1" in details
    assert "Connect fault" in details
    assert "before request reached the endpoint" in details


def test_history_compare_cli_exposes_minimum_absolute_latency_delta() -> None:
    from hookstorm.cli import _parser
    args = _parser().parse_args([
        "history", "compare-file", "baseline.json",
        "--latency-threshold-pct", "30", "--latency-min-delta-ms", "40",
    ])
    assert args.latency_threshold_pct == 30.0
    assert args.latency_min_delta_ms == 40.0


@pytest.mark.parametrize(
    ("provider", "route", "signature_header", "secret"),
    [
        ("stripe", "stripe", "Stripe-Signature", "test-secret"),
        ("github", "github", "X-Hub-Signature-256", "test-secret"),
        ("shopify", "shopify", "X-Shopify-Hmac-Sha256", "test-secret"),
        (
            "standard_webhooks",
            "standard",
            "webhook-signature",
            "whsec_aG9va3N0b3JtLXRlc3Qtc3RhbmRhcmQtd2ViaG9va3Mtc2VjcmV0",
        ),
    ],
)
def test_run_provider_flag_sends_valid_provider_signature(
    receiver, tmp_path, monkeypatch, capsys,
    provider: str, route: str, signature_header: str, secret: str,
) -> None:
    base, state = receiver
    scenario = _scenario_file(tmp_path)
    monkeypatch.setenv("HOOKSTORM_SECRET", secret)

    assert main([
        "run",
        scenario,
        "--target",
        f"{base}/signature/{route}",
        "--provider",
        provider,
        "--no-history",
        "--format",
        "json",
    ]) == 0

    output = capsys.readouterr().out
    assert f'"signature_provider": "{provider}"' in output
    assert state["requests"][0]["headers"].get(signature_header)
    if provider == "github":
        assert state["requests"][0]["headers"]["X-GitHub-Event"] == "ping"
        assert state["requests"][0]["headers"]["X-GitHub-Delivery"]
    if provider == "shopify":
        assert state["requests"][0]["headers"]["X-Shopify-Topic"] == "ping"
        assert state["requests"][0]["headers"]["X-Shopify-Webhook-Id"]
    if provider == "standard_webhooks":
        assert state["requests"][0]["headers"]["webhook-id"] == "evt_cli_history"




def test_init_provider_flag_generates_a_provider_profile(tmp_path, monkeypatch, capsys) -> None:
    from hookstorm.config import load_scenario

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--provider", "stripe", "--no-workflow"]) == 0
    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")

    assert scenario["provider"] == "stripe"
    assert scenario["delivery"]["signature_provider"] == "stripe"
    assert scenario["events"][0]["id"] == "payment_intent.succeeded"
    assert scenario["events"][0]["payload"]["object"] == "event"
    capsys.readouterr()



def test_fuzz_cli_executes_seeded_mutations_and_writes_json_summary(
    receiver, tmp_path, capsys
) -> None:
    base, _state = receiver
    scenario = _scenario_file(tmp_path)
    output = tmp_path / "fuzz-summary.json"

    assert main([
        "fuzz",
        scenario,
        "--target",
        base,
        "--seed",
        "123",
        "--iterations",
        "3",
        "--output",
        str(output),
    ]) == 0

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["seed"] == 123
    assert summary["iterations"] == 3
    assert summary["summary"]["success"] is True
    assert summary["summary"]["executions"] == 3
    assert summary["worst_case_http_attempts"] == 3
    assert len({case["event_id"] for case in summary["cases"]}) == 3
    assert "Fuzz summary:" in capsys.readouterr().out



def test_capture_cli_parser_defaults_to_loopback_capture_output() -> None:
    from hookstorm.cli import _parser

    args = _parser().parse_args(["capture"])
    assert args.output == "captured-webhooks.yaml"
    assert args.port == 8787
    assert args.max_events == 25
    assert args.include_sensitive_headers is False


def test_capture_cli_parser_requires_explicit_sensitive_header_opt_in() -> None:
    from hookstorm.cli import _parser

    args = _parser().parse_args([
        "capture",
        "--include-sensitive-headers",
        "--output",
        "capture.yaml",
        "--port",
        "0",
    ])
    assert args.include_sensitive_headers is True
    assert args.output == "capture.yaml"
    assert args.port == 0

def test_schema_command_prints_packaged_json_schema(capsys) -> None:
    exit_code = main(["schema"])
    output = capsys.readouterr().out

    assert exit_code == 0
    schema = json.loads(output)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:hookstorm:scenario:v1"


def test_schema_command_writes_without_overwriting_by_default(tmp_path, capsys) -> None:
    output = tmp_path / "nested" / "scenario.schema.json"

    assert main(["schema", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["$id"] == "urn:hookstorm:scenario:v1"
    capsys.readouterr()

    assert main(["schema", "--output", str(output)]) == 2
    assert "Refusing to overwrite" in capsys.readouterr().err

    assert main(["schema", "--output", str(output), "--force"]) == 0
    assert "JSON Schema written:" in capsys.readouterr().out
