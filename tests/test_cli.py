from __future__ import annotations

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
