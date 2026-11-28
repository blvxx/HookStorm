from __future__ import annotations

from pathlib import Path

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


def test_run_uses_hookstorm_target_url_when_target_flag_is_omitted(
    receiver, tmp_path, monkeypatch, capsys
) -> None:
    base, _state = receiver
    monkeypatch.setenv("HOOKSTORM_TARGET_URL", base)
    scenario = _scenario_file(tmp_path)

    assert main([
        "run", scenario, "--format", "json", "--no-history",
    ]) == 0
    output = capsys.readouterr().out
    assert '"success": true' in output


def test_run_explains_how_to_configure_a_missing_target(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("HOOKSTORM_TARGET_URL", raising=False)
    scenario = _scenario_file(tmp_path)

    assert main(["run", scenario]) == 2
    assert "Set HOOKSTORM_TARGET_URL or pass --target URL" in capsys.readouterr().err


def test_demo_runs_locally_and_writes_a_report(tmp_path, capsys) -> None:
    report = tmp_path / "demo.html"

    assert main(["demo", "--output", str(report)]) == 0

    output = capsys.readouterr().out
    assert "HookStorm demo · PASS" in output
    assert "Deliveries: 2/2 passed" in output
    assert "HTTP attempts: 4" in output
    assert report.is_file()


def test_init_template_flag_selects_provider_and_event(tmp_path, monkeypatch, capsys) -> None:
    from hookstorm.config import load_scenario

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--template", "stripe", "--no-workflow"]) == 0

    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")
    assert scenario["delivery"]["signature_provider"] == "stripe"
    assert scenario["events"][0]["id"] == "payment_intent.succeeded"
    assert scenario["events"][0]["payload"]["type"] == "payment_intent.succeeded"
    capsys.readouterr()


def test_update_workflow_command_refreshes_only_the_install_pin(
    tmp_path, monkeypatch, capsys
) -> None:
    from hookstorm.scaffold import HOOKSTORM_INSTALL_REF, initialize_project

    monkeypatch.chdir(tmp_path)
    initialize_project()
    workflow = tmp_path / ".github" / "workflows" / "webhook-regression.yml"
    old_ref = "b" * 40
    original = workflow.read_text(encoding="utf-8").replace(HOOKSTORM_INSTALL_REF, old_ref)
    workflow.write_text(original, encoding="utf-8")

    assert main(["update-workflow"]) == 0
    updated = workflow.read_text(encoding="utf-8")
    assert HOOKSTORM_INSTALL_REF in updated
    assert updated == original.replace(old_ref, HOOKSTORM_INSTALL_REF)
    capsys.readouterr()


def test_watch_executes_once_then_stops_cleanly_on_keyboard_interrupt(
    receiver, tmp_path, monkeypatch, capsys
) -> None:
    base, _state = receiver
    monkeypatch.setenv("HOOKSTORM_TARGET_URL", base)
    scenario = _scenario_file(tmp_path)
    report = tmp_path / "watch.html"

    def stop_watching(_interval: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("hookstorm.cli.time.sleep", stop_watching)
    assert main([
        "watch", scenario, "--no-history", "--output", str(report),
    ]) == 130
    assert report.is_file()
    assert "Change detected" in capsys.readouterr().out


def test_run_shows_live_delivery_progress_in_interactive_terminal(
    receiver, tmp_path, monkeypatch
) -> None:
    import io

    class TerminalStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    base, _state = receiver
    monkeypatch.setenv("HOOKSTORM_TARGET_URL", base)
    scenario = _scenario_file(tmp_path)
    terminal = TerminalStream()
    monkeypatch.setattr("hookstorm.cli.sys.stderr", terminal)

    assert main([
        "run", scenario, "--no-history", "--output", str(tmp_path / "progress.html"),
    ]) == 0
    assert "1/1" in terminal.getvalue()
    assert "CLI history smoke" not in terminal.getvalue()


def test_init_can_generate_gitlab_ci_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--ci-provider", "gitlab"]) == 0

    scenario = tmp_path / "scenarios" / "webhook.yaml"
    workflow = tmp_path / ".gitlab-ci.yml"
    assert scenario.is_file()
    assert workflow.is_file()
    output = capsys.readouterr().out
    assert "GitLab CI/CD variables" in output
    assert "HOOKSTORM_TARGET_URL" in output
    assert ".gitlab-ci.yml" in output
