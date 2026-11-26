from __future__ import annotations

import pytest

from hookstorm.history import (
    HistoryError,
    compare_history_runs,
    compare_results,
    get_run,
    list_runs,
    result_metrics,
    save_run,
)


def make_result(
    run_id: str,
    *,
    scenario: str = "payment webhook",
    deliveries: list[dict] | None = None,
    duration_ms: float = 100.0,
) -> dict:
    items = deliveries if deliveries is not None else [
        {
            "event_type": "payment.succeeded",
            "event_id": "evt_payment_001",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [
                {"status_code": 200, "latency_ms": 20.0, "error": None, "attempt": 1}
            ],
        }
    ]
    passed = sum(1 for item in items if item["passed"])
    attempts = sum(len(item["attempts"]) for item in items)
    return {
        "schema_version": 1,
        "tool": "HookStorm",
        "version": "0.1.0",
        "run_id": run_id,
        "scenario": scenario,
        "target": "http://127.0.0.1:8000/webhook",
        "started_at": f"2026-11-26T12:00:{int(run_id[-1], 16) % 60:02d}+00:00",
        "duration_ms": duration_ms,
        "summary": {
            "total_deliveries": len(items),
            "passed": passed,
            "failed": len(items) - passed,
            "attempts": attempts,
            "success": passed == len(items),
        },
        "deliveries": items,
    }


def delivery(
    event_id: str,
    *,
    passed: bool = True,
    latency_ms: float = 20.0,
    status_code: int | None = 200,
    duplicate_index: int = 0,
    event_type: str = "payment.succeeded",
) -> dict:
    return {
        "event_type": event_type,
        "event_id": event_id,
        "duplicate_index": duplicate_index,
        "passed": passed,
        "attempts": [{
            "attempt": 1,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "elapsed_ms": 1.0,
            "error": None if status_code is not None else "request timed out",
            "passed": passed,
        }],
    }


def test_save_list_and_get_run_round_trip(tmp_path) -> None:
    database = tmp_path / "hookstorm" / "history.sqlite3"
    result = make_result("run_001")
    assert save_run(result, database) == "run_001"

    listed = list_runs(database)
    assert len(listed) == 1
    assert listed[0]["run_id"] == "run_001"
    assert listed[0]["scenario"] == "payment webhook"
    assert listed[0]["success"] is True
    assert listed[0]["pass_rate_pct"] == 100.0
    assert listed[0]["p95_latency_ms"] == 20.0

    loaded = get_run("run_001", database)
    assert loaded["run_id"] == result["run_id"]
    assert loaded["deliveries"] == result["deliveries"]
    assert loaded["history"]["database_run_id"] == "run_001"


def test_history_list_filters_scenario_and_limit(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_001", scenario="payments"), database)
    save_run(make_result("run_002", scenario="invoices"), database)
    save_run(make_result("run_003", scenario="payments"), database)

    assert [item["run_id"] for item in list_runs(database, limit=2)] == [
        "run_003", "run_002"
    ]
    payments = list_runs(database, scenario="payments")
    assert {item["run_id"] for item in payments} == {"run_001", "run_003"}


def test_compare_detects_failed_delivery_and_latency_regression() -> None:
    baseline = make_result(
        "run_001",
        deliveries=[
            delivery("evt_1", latency_ms=10.0),
            delivery("evt_2", latency_ms=20.0),
        ],
        duration_ms=40.0,
    )
    current = make_result(
        "run_002",
        deliveries=[
            delivery("evt_1", passed=False, latency_ms=30.0, status_code=500),
            delivery("evt_2", latency_ms=50.0),
        ],
        duration_ms=100.0,
    )

    result = compare_results(current, baseline, latency_threshold_pct=50.0)
    assert result["has_regressions"] is True
    assert result["delta"]["failed_deliveries"] == 1
    assert result["delta"]["pass_rate_pct"] == -50.0
    assert any("previously passing delivery now fails" in item["reason"] for item in result["regressions"])
    assert any("latency increased" in item["reason"] for item in result["regressions"])


def test_compare_detects_recovery_and_latency_improvement() -> None:
    baseline = make_result(
        "run_001",
        deliveries=[delivery("evt_1", passed=False, latency_ms=100.0, status_code=503)],
    )
    current = make_result("run_002", deliveries=[delivery("evt_1", latency_ms=25.0)])
    result = compare_results(current, baseline, latency_threshold_pct=20.0)

    assert result["has_regressions"] is False
    assert any("previously failing delivery now passes" in item["reason"] for item in result["improvements"])
    assert any("latency improved" in item["reason"] for item in result["improvements"])


def test_compare_marks_removed_coverage_and_new_failures() -> None:
    baseline = make_result(
        "run_001",
        deliveries=[delivery("evt_1"), delivery("evt_2")],
    )
    current = make_result(
        "run_002",
        deliveries=[
            delivery("evt_1"),
            delivery("evt_3", passed=False, status_code=500),
        ],
    )
    result = compare_results(current, baseline)

    reasons = [item["reason"] for item in result["regressions"]]
    assert "delivery coverage decreased" in reasons
    assert "new delivery failed" in reasons


def test_compare_history_runs_defaults_current_to_latest(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_001"), database)
    save_run(
        make_result(
            "run_002",
            deliveries=[delivery("evt_1", passed=False, status_code=500)],
        ),
        database,
    )

    result = compare_history_runs("run_001", path=database)
    assert result["baseline_run_id"] == "run_001"
    assert result["current_run_id"] == "run_002"
    assert result["has_regressions"] is True


@pytest.mark.parametrize("limit", [0, 501, True, 1.5])
def test_rejects_invalid_history_limits(tmp_path, limit: object) -> None:
    with pytest.raises(HistoryError, match="limit"):
        list_runs(tmp_path / "history.sqlite3", limit=limit)


def test_missing_run_has_readable_error(tmp_path) -> None:
    with pytest.raises(HistoryError, match="was not found"):
        get_run("unknown", tmp_path / "history.sqlite3")


def test_rejects_latency_threshold_out_of_range() -> None:
    with pytest.raises(HistoryError, match="between 0 and 1000"):
        compare_results(make_result("run_002"), make_result("run_001"), latency_threshold_pct=2000)


def test_percentile_metrics_use_per_delivery_total_latency() -> None:
    result = make_result(
        "run_001",
        deliveries=[
            {
                **delivery("evt_1", latency_ms=10.0),
                "attempts": [
                    {"status_code": 503, "latency_ms": 10.0},
                    {"status_code": 200, "latency_ms": 20.0},
                ],
            },
            delivery("evt_2", latency_ms=40.0),
        ],
    )
    metrics = result_metrics(result)
    assert metrics["p50_latency_ms"] == 30.0
    assert metrics["p95_latency_ms"] == 40.0
