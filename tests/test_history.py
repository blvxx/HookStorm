from __future__ import annotations

import json
import math
import os
import sqlite3
import stat

import pytest

from hookstorm import history as history_module
from hookstorm.history import (
    HistoryError,
    compare_history_file,
    compare_history_runs,
    compare_results,
    export_history_run,
    get_run,
    list_runs,
    load_history_export,
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

def test_in_memory_history_persists_across_connections() -> None:
    result = make_result("run_memory0")

    assert save_run(result, ":memory:") == "run_memory0"
    assert get_run("run_memory0", ":memory:")["run_id"] == "run_memory0"
    assert [item["run_id"] for item in list_runs(":memory:")] == ["run_memory0"]




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


def test_compare_rejects_changed_scenario_configuration_by_default() -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    baseline["scenario_fingerprint"] = "a" * 64
    current["scenario_fingerprint"] = "b" * 64

    comparison = compare_results(current, baseline)

    assert comparison["same_configuration"] is False
    assert comparison["has_regressions"] is True
    assert any(
        item["reason"] == "scenario configuration changed"
        for item in comparison["regressions"]
    )


def test_compare_allows_intentional_configuration_change_with_override() -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    baseline["scenario_fingerprint"] = "a" * 64
    current["scenario_fingerprint"] = "b" * 64

    comparison = compare_results(current, baseline, allow_config_change=True)

    assert comparison["same_configuration"] is False
    assert comparison["allow_config_change"] is True
    assert comparison["has_regressions"] is False


def test_compare_marks_legacy_baselines_as_configuration_unknown() -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    current["scenario_fingerprint"] = "a" * 64

    comparison = compare_results(current, baseline)

    assert comparison["same_configuration"] is None
    assert comparison["has_regressions"] is False


@pytest.mark.parametrize("duplicate_side", ["baseline", "current"])
def test_compare_rejects_duplicate_delivery_identities(duplicate_side: str) -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    duplicate_deliveries = [delivery("evt_duplicate"), delivery("evt_duplicate")]
    target = baseline if duplicate_side == "baseline" else current
    target["deliveries"] = duplicate_deliveries
    target["summary"]["total_deliveries"] = 2
    target["summary"]["passed"] = 2
    target["summary"]["failed"] = 0
    target["summary"]["attempts"] = 2

    with pytest.raises(HistoryError, match="duplicate delivery identity"):
        compare_results(current, baseline)


def test_compare_rejects_malformed_scenario_fingerprints() -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    current["scenario_fingerprint"] = "not-a-sha256"

    with pytest.raises(HistoryError, match="64-character lowercase SHA-256"):
        compare_results(current, baseline)


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



def test_history_whitelists_metadata_and_never_persists_payload_headers_or_secrets(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    result = make_result("run_private")
    result["payload"] = {"card_number": "PRIVATE_PAYLOAD_MARKER"}
    result["secret"] = "PRIVATE_SECRET_MARKER"
    result["request_headers"] = {"Authorization": "PRIVATE_HEADER_MARKER"}
    result["response_body"] = "PRIVATE_RESPONSE_MARKER"
    result["deliveries"][0]["payload"] = {"private": "DELIVERY_PAYLOAD_MARKER"}
    result["deliveries"][0]["attempts"][0]["response_body"] = "ATTEMPT_BODY_MARKER"

    save_run(result, database)
    stored = get_run("run_private", database)
    raw_database = database.read_bytes()

    assert "payload" not in stored
    assert "secret" not in stored
    assert "request_headers" not in stored
    assert "response_body" not in stored
    assert "payload" not in stored["deliveries"][0]
    assert "response_body" not in stored["deliveries"][0]["attempts"][0]
    for marker in (
        b"PRIVATE_PAYLOAD_MARKER",
        b"PRIVATE_SECRET_MARKER",
        b"PRIVATE_HEADER_MARKER",
        b"PRIVATE_RESPONSE_MARKER",
        b"DELIVERY_PAYLOAD_MARKER",
        b"ATTEMPT_BODY_MARKER",
    ):
        assert marker not in raw_database


def test_compare_latest_rejects_baseline_against_itself(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_001"), database)
    with pytest.raises(HistoryError, match="selected baseline"):
        compare_history_runs("run_001", path=database)



def test_history_export_is_portable_and_sanitized(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    output = tmp_path / "baselines" / "baseline.json"
    result = make_result("run_export1")
    result["scenario_fingerprint"] = "c" * 64
    result["payload"] = {"private": "BASELINE_PRIVATE_PAYLOAD"}
    result["secret"] = "BASELINE_PRIVATE_SECRET"
    save_run(result, database)

    exported_path = export_history_run("latest", output, database)
    assert output.is_file()
    assert exported_path == str(output.resolve())
    raw = output.read_text(encoding="utf-8")
    assert "BASELINE_PRIVATE_PAYLOAD" not in raw
    assert "BASELINE_PRIVATE_SECRET" not in raw

    loaded = load_history_export(output)
    assert loaded["run_id"] == "run_export1"
    assert loaded["scenario"] == "payment webhook"
    assert loaded["scenario_fingerprint"] == "c" * 64
    assert "payload" not in loaded
    assert "secret" not in loaded


def test_compare_history_file_detects_regression_from_another_database(tmp_path) -> None:
    baseline_db = tmp_path / "baseline.sqlite3"
    current_db = tmp_path / "current.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_baseline"), baseline_db)
    export_history_run("latest", baseline_file, baseline_db)

    current = make_result(
        "run_current2",
        deliveries=[delivery("evt_payment_001", passed=False, latency_ms=20.0, status_code=500)],
    )
    save_run(current, current_db)

    comparison = compare_history_file(baseline_file, path=current_db)
    assert comparison["baseline_run_id"] == "run_baseline"
    assert comparison["current_run_id"] == "run_current2"
    assert comparison["has_regressions"] is True


def test_load_history_export_validates_optional_scenario_fingerprint(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_fingerprin1"), database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    document["run"]["scenario_fingerprint"] = "invalid"
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(HistoryError, match="64-character lowercase SHA-256"):
        load_history_export(baseline_file)


def test_load_history_export_rejects_unknown_format(tmp_path) -> None:
    baseline_file = tmp_path / "bad-baseline.json"
    baseline_file.write_text(
        '{"format":"other-tool","format_version":1,"run":{}}',
        encoding="utf-8",
    )
    with pytest.raises(HistoryError, match="Unsupported baseline file format"):
        load_history_export(baseline_file)


def test_load_history_export_rejects_private_fields_by_whitelisting(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    result = make_result("run_safe")
    save_run(result, database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    document["run"]["payload"] = {"secret": "SHOULD_NOT_SURVIVE"}
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_history_export(baseline_file)
    assert "payload" not in loaded



def test_business_state_verification_failure_is_a_history_regression() -> None:
    baseline = make_result("baseline1")
    baseline["verification"] = {
        "method": "GET",
        "target": "http://127.0.0.1:8000/state",
        "status_code": 200,
        "latency_ms": 1.0,
        "expected_status": True,
        "expected_json": True,
        "passed": True,
    }
    current = make_result("current1")
    current["verification"] = {
        "method": "GET",
        "target": "http://127.0.0.1:8000/state",
        "status_code": 200,
        "latency_ms": 1.1,
        "error": "response JSON value 'processed_events' did not match the expected value",
        "expected_status": True,
        "expected_json": False,
        "passed": False,
    }

    current["summary"]["success"] = False
    comparison = compare_results(current, baseline)

    assert comparison["has_regressions"] is True
    assert any(item["reason"] == "business-state verification failed" for item in comparison["regressions"])


def test_business_state_verification_removal_is_a_history_regression() -> None:
    baseline = make_result("baseline2")
    baseline["verification"] = {"passed": True}
    current = make_result("current2")

    comparison = compare_results(current, baseline)

    assert any(item["reason"] == "business-state verification was removed" for item in comparison["regressions"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not portable to Windows")
def test_default_history_and_exported_baseline_use_owner_only_permissions(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("HOOKSTORM_HISTORY_DB", raising=False)
    database = tmp_path / ".hookstorm" / "history.sqlite3"
    monkeypatch.setattr(history_module, "_DEFAULT_DB", database)

    save_run(make_result("run_private_permission1"))

    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    baseline = tmp_path / "shared" / "baseline.json"
    export_history_run("latest", baseline)

    assert stat.S_IMODE(baseline.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not portable to Windows")
def test_explicit_history_database_file_is_owner_only(tmp_path) -> None:
    database = tmp_path / "custom" / "history.sqlite3"

    save_run(make_result("run_custom_permission2"), database)

    assert stat.S_IMODE(database.stat().st_mode) == 0o600



@pytest.mark.parametrize(
    "duplicate_index",
    [True, -1, 101, 1.0, "1", None],
)
def test_compare_rejects_invalid_duplicate_indices(duplicate_index: object) -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    current["deliveries"][0]["duplicate_index"] = duplicate_index

    with pytest.raises(HistoryError, match="duplicate_index"):
        compare_results(current, baseline)


@pytest.mark.parametrize("field", ["event_type", "event_id"])
@pytest.mark.parametrize("value", ["", "   ", None, 1, "x" * 257])
def test_compare_rejects_invalid_delivery_identity_fields(
    field: str, value: object
) -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    current["deliveries"][0][field] = value

    with pytest.raises(HistoryError, match=field):
        compare_results(current, baseline)


@pytest.mark.parametrize("latency", [math.nan, math.inf, -math.inf, -0.1, True, "12"])
def test_compare_rejects_invalid_attempt_latency(latency: object) -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    current["deliveries"][0]["attempts"][0]["latency_ms"] = latency

    with pytest.raises(HistoryError, match="latency_ms"):
        compare_results(current, baseline)


def test_compare_rejects_summary_counts_inconsistent_with_deliveries() -> None:
    baseline = make_result("run_001")
    current = make_result("run_002")
    current["summary"]["passed"] = 0
    current["summary"]["failed"] = 1

    with pytest.raises(HistoryError, match="summary.passed"):
        compare_results(current, baseline)


def test_save_run_rejects_summary_attempt_count_inconsistent_with_deliveries(
    tmp_path,
) -> None:
    result = make_result("run_inconsisten0")
    result["summary"]["attempts"] = 2

    with pytest.raises(HistoryError, match="summary.attempts"):
        save_run(result, tmp_path / "history.sqlite3")


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity"])
def test_load_history_export_rejects_non_standard_json_numbers(
    tmp_path, number: str
) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_jsonstric1"), database)
    export_history_run("latest", baseline_file, database)

    raw = baseline_file.read_text(encoding="utf-8")
    raw = raw.replace('"latency_ms": 20.0', f'"latency_ms": {number}', 1)
    baseline_file.write_text(raw, encoding="utf-8")

    with pytest.raises(HistoryError, match="non-standard JSON number"):
        load_history_export(baseline_file)


@pytest.mark.parametrize("duplicate_index", [True, -1, 101, 1.5, "1"])
def test_load_history_export_rejects_invalid_duplicate_indices(
    tmp_path, duplicate_index: object
) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_badinde2"), database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    document["run"]["deliveries"][0]["duplicate_index"] = duplicate_index
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(HistoryError, match="duplicate_index"):
        load_history_export(baseline_file)


def test_load_history_export_rejects_inconsistent_summary_metrics(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_badummar3"), database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    document["run"]["summary"]["total_deliveries"] = 2
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(HistoryError, match="total_deliveries"):
        load_history_export(baseline_file)


def test_compare_rejects_delivery_pass_flag_that_disagrees_with_final_attempt() -> None:
    current = make_result("current1", deliveries=[delivery("evt_ok")])
    current["deliveries"][0]["passed"] = False

    with pytest.raises(HistoryError, match="does not match its final attempt"):
        compare_results(current, make_result("baseline1"))


def test_compare_rejects_summary_success_flag_that_disagrees_with_outcomes() -> None:
    current = make_result("current1", deliveries=[delivery("evt_failed", passed=False)])
    current["summary"]["success"] = True

    with pytest.raises(HistoryError, match="summary.success"):
        compare_results(current, make_result("baseline1"))


def test_history_reads_reject_non_object_corrupt_database_rows(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_corrup7"), database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE runs SET result_json = ? WHERE run_id = ?",
            (json.dumps(["not", "a", "run"]), "run_corrup7"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(HistoryError, match="Stored run must be a JSON object"):
        get_run("run_corrup7", database)
    with pytest.raises(HistoryError, match="Stored run must be a JSON object"):
        list_runs(database)


def test_history_reads_reject_sqlite_scenario_column_mismatch(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_meta7"), database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE runs SET scenario = ? WHERE run_id = ?",
            ("tampered scenario", "run_meta7"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(HistoryError, match="database scenario does not match"):
        get_run("run_meta7", database)
    with pytest.raises(HistoryError, match="database scenario does not match"):
        list_runs(database)


def test_history_reads_reject_sqlite_metric_column_mismatch(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_meta9"), database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE runs SET attempts = ? WHERE run_id = ?",
            (99, "run_meta9"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(HistoryError, match="database attempts does not match"):
        get_run("run_meta9", database)
    with pytest.raises(HistoryError, match="database attempts does not match"):
        list_runs(database)



def test_history_reads_reject_sqlite_success_column_mismatch(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_meta8"), database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE runs SET success = ? WHERE run_id = ?",
            (0, "run_meta8"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(HistoryError, match="database success does not match"):
        get_run("run_meta8", database)
    with pytest.raises(HistoryError, match="database success does not match"):
        list_runs(database)


def test_load_history_export_rejects_empty_attempt_history(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_noattempt4"), database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    document["run"]["deliveries"][0]["attempts"] = []
    document["run"]["summary"]["attempts"] = 0
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(HistoryError, match="attempts must be a non-empty"):
        load_history_export(baseline_file)



def test_save_run_rejects_duplicate_delivery_identities(tmp_path) -> None:
    result = make_result(
        "run_duplicate_save",
        deliveries=[delivery("evt_same"), delivery("evt_same")],
    )

    with pytest.raises(HistoryError, match="duplicate delivery identity"):
        save_run(result, tmp_path / "history.sqlite3")


def test_load_history_export_rejects_duplicate_delivery_identities(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_duplicate_impor6"), database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    duplicate = dict(document["run"]["deliveries"][0])
    document["run"]["deliveries"].append(duplicate)
    document["run"]["summary"]["total_deliveries"] += 1
    document["run"]["summary"]["passed"] += 1
    document["run"]["summary"]["attempts"] += len(duplicate["attempts"])
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(HistoryError, match="duplicate delivery identity"):
        load_history_export(baseline_file)



def test_load_history_export_rejects_non_object_delivery_entries(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    baseline_file = tmp_path / "baseline.json"
    save_run(make_result("run_nonobjec7"), database)
    export_history_run("latest", baseline_file, database)

    document = json.loads(baseline_file.read_text(encoding="utf-8"))
    document["run"]["deliveries"][0] = None
    baseline_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(HistoryError, match="delivery 0 must be a JSON object"):
        load_history_export(baseline_file)


@pytest.mark.parametrize("format_version", [True, 1.0, "1", None])
def test_load_history_export_requires_integer_format_version(
    tmp_path, format_version: object
) -> None:
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        json.dumps({
            "format": "hookstorm-history",
            "format_version": format_version,
            "run": {},
        }),
        encoding="utf-8",
    )

    with pytest.raises(HistoryError, match="Unsupported baseline file format"):
        load_history_export(baseline_file)


def test_load_history_export_rejects_duplicate_json_keys(tmp_path) -> None:
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        '{"format":"hookstorm-history","format":"other","format_version":1,"run":{}}',
        encoding="utf-8",
    )

    with pytest.raises(HistoryError, match="duplicate JSON key"):
        load_history_export(baseline_file)



def test_history_preserves_validated_injected_fault_markers(tmp_path) -> None:
    result = make_result("run_fault_histor5")
    result["deliveries"][0]["attempts"][0]["injected_fault"] = "response_loss"

    save_run(result, tmp_path / "history.sqlite3")

    stored = get_run("run_fault_histor5", tmp_path / "history.sqlite3")
    assert stored["deliveries"][0]["attempts"][0]["injected_fault"] == "response_loss"


@pytest.mark.parametrize("fault", ["random_packet_loss", [], None])
def test_history_rejects_unknown_injected_fault_markers(
    tmp_path, fault: object
) -> None:
    result = make_result("run_invalid_faul6")
    result["deliveries"][0]["attempts"][0]["injected_fault"] = fault

    with pytest.raises(HistoryError, match="injected_fault"):
        save_run(result, tmp_path / "history.sqlite3")

def test_history_rejects_deeply_nested_stored_json(tmp_path) -> None:
    database = tmp_path / "history.sqlite3"
    save_run(make_result("run_deep01"), database)
    nested_json = "[" * 10_000 + "0" + "]" * 10_000

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE runs SET result_json = ? WHERE run_id = ?",
            (nested_json, "run_deep01"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(HistoryError, match="Cannot read"):
        get_run("run_deep01", database)
    with pytest.raises(HistoryError, match="Cannot read"):
        list_runs(database)


def test_history_export_rejects_deeply_nested_json(tmp_path) -> None:
    baseline = tmp_path / "deep.json"
    baseline.write_text(
        "[" * 10_000 + "0" + "]" * 10_000,
        encoding="utf-8",
    )

    with pytest.raises(HistoryError, match="not valid JSON"):
        load_history_export(baseline)
