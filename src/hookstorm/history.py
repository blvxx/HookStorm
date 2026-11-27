"""Local run history and regression comparison for HookStorm."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".hookstorm" / "history.sqlite3"
_SCHEMA_VERSION = 1
_MAX_HISTORY_EXPORT_BYTES = 16 * 1024 * 1024
_INJECTED_FAULTS = frozenset(
    {
        "connect_error",
        "response_loss",
        "response_body_error",
        "malformed_json",
        "wrong_content_type",
        "gzip_body",
        "deflate_body",
        "invalid_gzip",
        "invalid_deflate",
        "chunked_transfer",
        "malformed_chunked",
        "partial_upload",
        "content_length_mismatch",
    }
)


class HistoryError(RuntimeError):
    """Raised when a local run-history operation cannot be completed."""


def _fchmod_private(descriptor: int, mode: int) -> None:
    """Apply a file mode through the POSIX-only descriptor API when available."""
    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        raise OSError("The current POSIX platform does not support descriptor chmod")
    fchmod(descriptor, mode)


def resolve_history_db(path: str | Path | None = None) -> str:
    """Resolve an explicit path, the environment override, or the user-local default."""
    raw = str(path) if path is not None else os.getenv("HOOKSTORM_HISTORY_DB")
    if not raw:
        raw = str(_DEFAULT_DB)
    if raw == ":memory:":
        return raw
    return str(Path(raw).expanduser())


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
    location = resolve_history_db(path)
    is_default_location = path is None and not os.getenv("HOOKSTORM_HISTORY_DB")
    try:
        if location != ":memory:":
            database_path = Path(location)
            database_path.parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700 if is_default_location else 0o777,
            )
            if os.name == "posix":
                if is_default_location:
                    os.chmod(database_path.parent, 0o700)
                # Pre-create the file privately so the default umask cannot expose
                # run metadata between SQLite creation and a later chmod.
                descriptor = os.open(
                    database_path,
                    os.O_CREAT | os.O_RDWR,
                    0o600,
                )
                try:
                    _fchmod_private(descriptor, 0o600)
                finally:
                    os.close(descriptor)

        connection = sqlite3.connect(location, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise HistoryError(f"Cannot open HookStorm history database: {exc}") from exc


def _write_private_text(path: Path, content: str) -> None:
    """Atomically write a portable text file with private POSIX permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1  # The stream now owns and closes the descriptor.
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _initialize(connection: sqlite3.Connection) -> None:
    """Create or migrate the local history schema under a write lock.

    Version 0 is the unversioned schema shipped before migrations were added.
    Its runs table already has the version 1 columns, so migration 1 only
    records the schema version and preserves every stored run unchanged.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise HistoryError(
                f"History database schema version {version} is newer than "
                f"this HookStorm version supports ({_SCHEMA_VERSION}). Upgrade HookStorm."
            )
        if version < 0:
            raise HistoryError(f"History database has an invalid schema version: {version}")

        if version == 0:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    scenario TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    target TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    total_deliveries INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    result_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_scenario_started "
                "ON runs(scenario, started_at DESC)"
            )
            connection.execute("PRAGMA user_version = 1")
        elif version == 1:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_scenario_started "
                "ON runs(scenario, started_at DESC)"
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _latencies(result: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for delivery in result.get("deliveries", []):
        attempts = delivery.get("attempts", [])
        total = sum(float(item.get("latency_ms", 0.0)) for item in attempts)
        values.append(total)
    return values


def _percentile(values: list[float], percentile: float) -> float:
    """Return a nearest-rank percentile; empty samples yield 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return round(ordered[index], 2)


def result_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Return stable run metrics used by the history list and comparisons."""
    summary = result.get("summary", {})
    total = int(summary.get("total_deliveries", len(result.get("deliveries", []))))
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", max(0, total - passed)))
    latencies = _latencies(result)
    pass_rate = round((passed / total) * 100, 2) if total else 0.0
    return {
        "total_deliveries": total,
        "passed": passed,
        "failed": failed,
        "pass_rate_pct": pass_rate,
        "attempts": int(summary.get("attempts", sum(len(d.get("attempts", [])) for d in result.get("deliveries", [])))),
        "duration_ms": round(float(result.get("duration_ms", 0.0)), 2),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
    }


def _sanitize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only run metadata and assertion outcomes; never persist request/response data."""
    summary_keys = ("total_deliveries", "passed", "failed", "attempts", "success")
    delivery_keys = (
        "event_id", "event_type", "duplicate_index", "schedule_after_ms",
        "signature_provider", "passed"
    )
    attempt_keys = (
        "attempt", "elapsed_ms", "status_code", "latency_ms", "error",
        "expected_status", "expected_json", "passed", "injected_fault",
    )
    sanitized = {
        key: result[key]
        for key in (
            "schema_version", "tool", "version", "run_id", "scenario",
            "scenario_fingerprint", "target", "started_at", "duration_ms",
        )
        if key in result
    }
    sanitized["summary"] = {
        key: result["summary"][key]
        for key in summary_keys
        if key in result["summary"]
    }
    sanitized["deliveries"] = []
    for delivery in result.get("deliveries", []):
        item = {key: delivery[key] for key in delivery_keys if key in delivery}
        item["attempts"] = [
            {key: attempt[key] for key in attempt_keys if key in attempt}
            for attempt in delivery.get("attempts", [])
        ]
        sanitized["deliveries"].append(item)
    verification = result.get("verification")
    if verification is not None:
        verify_keys = (
            "method", "target", "status_code", "latency_ms", "error",
            "expected_status", "expected_json", "passed",
        )
        sanitized["verification"] = {
            key: verification[key] for key in verify_keys if key in verification
        }
    else:
        sanitized["verification"] = None
    return sanitized


def _read_scenario_fingerprint(result: dict[str, Any], label: str) -> str | None:
    """Validate and return an optional SHA-256 scenario fingerprint."""
    fingerprint = result.get("scenario_fingerprint")
    if fingerprint is None:
        return None
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise HistoryError(f"{label} scenario_fingerprint must be a 64-character lowercase SHA-256 hex digest")
    return fingerprint


def _delivery_index(
    deliveries: list[dict[str, Any]], label: str
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Index deliveries without silently discarding duplicate identities."""
    indexed: dict[tuple[str, str, int], dict[str, Any]] = {}
    for index, item in enumerate(deliveries):
        if not isinstance(item, dict):
            raise HistoryError(f"{label} delivery {index} must be a JSON object")
        key = _delivery_key(item)
        if key in indexed:
            raise HistoryError(
                f"{label} contains duplicate delivery identity at position {index}: "
                f"{key[0]} / {key[1]} / duplicate {key[2]}"
            )
        indexed[key] = item
    return indexed


def _require_non_negative_integer(value: Any, label: str, *, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or (maximum is not None and value > maximum)
    ):
        bounds = "a non-negative integer" if maximum is None else f"an integer between 0 and {maximum}"
        raise HistoryError(f"{label} must be {bounds}")
    return value


def _require_finite_non_negative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryError(f"{label} must be a finite non-negative number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise HistoryError(f"{label} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise HistoryError(f"{label} must be a finite non-negative number")
    return number


def _validate_run_result(result: dict[str, Any], label: str) -> None:
    """Validate the metrics and delivery identities used by history comparisons."""
    for key in ("run_id", "scenario", "started_at", "target"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HistoryError(f"{label} {key} must be a non-empty string")

    if "duration_ms" in result:
        _require_finite_non_negative_number(result["duration_ms"], f"{label} duration_ms")
    _read_scenario_fingerprint(result, label)

    summary = result.get("summary")
    if not isinstance(summary, dict):
        raise HistoryError(f"{label} summary must be a JSON object")
    count_fields = ("total_deliveries", "passed", "failed", "attempts")
    for field in count_fields:
        if field not in summary:
            raise HistoryError(f"{label} summary is missing '{field}'")
        _require_non_negative_integer(summary[field], f"{label} summary.{field}")
    if not isinstance(summary.get("success"), bool):
        raise HistoryError(f"{label} summary.success must be a boolean")

    deliveries = result.get("deliveries")
    if not isinstance(deliveries, list):
        raise HistoryError(f"{label} deliveries must be a JSON array")
    if summary["total_deliveries"] != len(deliveries):
        raise HistoryError(
            f"{label} summary.total_deliveries does not match the delivery count"
        )

    _delivery_index(deliveries, label)
    actual_passed = 0
    actual_attempts = 0
    for index, item in enumerate(deliveries):
        if not isinstance(item, dict):
            raise HistoryError(f"{label} delivery {index} must be a JSON object")
        if not isinstance(item.get("passed"), bool):
            raise HistoryError(f"{label} delivery {index}.passed must be a boolean")
        actual_passed += int(item["passed"])

        attempts = item.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise HistoryError(f"{label} delivery {index}.attempts must be a non-empty JSON array")
        actual_attempts += len(attempts)
        for attempt_index, attempt in enumerate(attempts):
            attempt_label = f"{label} delivery {index} attempt {attempt_index}"
            if not isinstance(attempt, dict):
                raise HistoryError(f"{attempt_label} must be a JSON object")
            if "latency_ms" in attempt:
                _require_finite_non_negative_number(
                    attempt["latency_ms"], f"{attempt_label}.latency_ms"
                )
            if "elapsed_ms" in attempt:
                _require_finite_non_negative_number(
                    attempt["elapsed_ms"], f"{attempt_label}.elapsed_ms"
                )
            if "attempt" in attempt:
                _require_non_negative_integer(
                    attempt["attempt"], f"{attempt_label}.attempt", maximum=10
                )
                if attempt["attempt"] == 0:
                    raise HistoryError(f"{attempt_label}.attempt must be between 1 and 10")
            if "status_code" in attempt:
                status_code = attempt["status_code"]
                if status_code is not None and (
                    isinstance(status_code, bool)
                    or not isinstance(status_code, int)
                    or not 100 <= status_code <= 599
                ):
                    raise HistoryError(
                        f"{attempt_label}.status_code must be null or an HTTP status code"
                    )
            for field in ("expected_status", "expected_json", "passed"):
                if field in attempt and not isinstance(attempt[field], bool):
                    raise HistoryError(f"{attempt_label}.{field} must be a boolean")
            if "error" in attempt and attempt["error"] is not None and not isinstance(
                attempt["error"], str
            ):
                raise HistoryError(f"{attempt_label}.error must be a string or null")
            if "injected_fault" in attempt and (
                not isinstance(attempt["injected_fault"], str)
                or attempt["injected_fault"] not in _INJECTED_FAULTS
            ):
                raise HistoryError(
                    f"{attempt_label}.injected_fault must be one of the supported "
                    "HookStorm fault-injection markers"
                )

        if "schedule_after_ms" in item:
            _require_non_negative_integer(
                item["schedule_after_ms"],
                f"{label} delivery {index}.schedule_after_ms",
                maximum=300_000,
            )

    if summary["passed"] != actual_passed:
        raise HistoryError(f"{label} summary.passed does not match delivery outcomes")
    if summary["failed"] != len(deliveries) - actual_passed:
        raise HistoryError(f"{label} summary.failed does not match delivery outcomes")
    if summary["attempts"] != actual_attempts:
        raise HistoryError(f"{label} summary.attempts does not match recorded attempts")

    verification = result.get("verification")
    if verification is not None:
        if not isinstance(verification, dict):
            raise HistoryError(f"{label} verification must be a JSON object or null")
        if "passed" in verification and not isinstance(verification["passed"], bool):
            raise HistoryError(f"{label} verification.passed must be a boolean")
        if "latency_ms" in verification:
            _require_finite_non_negative_number(
                verification["latency_ms"], f"{label} verification.latency_ms"
            )
        if "status_code" in verification:
            status_code = verification["status_code"]
            if status_code is not None and (
                isinstance(status_code, bool)
                or not isinstance(status_code, int)
                or not 100 <= status_code <= 599
            ):
                raise HistoryError(
                    f"{label} verification.status_code must be null or an HTTP status code"
                )


def save_run(result: dict[str, Any], path: str | Path | None = None) -> str:
    """Persist a sanitized HookStorm result and return its run ID."""
    required = ("run_id", "scenario", "started_at", "target", "summary", "deliveries")
    missing = [key for key in required if key not in result]
    if missing:
        raise HistoryError(f"Cannot save incomplete run result; missing: {', '.join(missing)}")

    _validate_run_result(result, "Run")
    safe_result = _sanitize_result(result)
    metrics = result_metrics(safe_result)
    try:
        encoded = json.dumps(
            safe_result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise HistoryError(f"Cannot serialize run result for history: {exc}") from exc

    connection = _connect(path)
    try:
        _initialize(connection)
        connection.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, scenario, started_at, target, success,
                total_deliveries, passed, failed, attempts, duration_ms, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(result["run_id"]),
                str(result["scenario"]),
                str(result["started_at"]),
                str(result["target"]),
                int(bool(result["summary"].get("success"))),
                metrics["total_deliveries"],
                metrics["passed"],
                metrics["failed"],
                metrics["attempts"],
                metrics["duration_ms"],
                encoded,
            ),
        )
        connection.commit()
    except sqlite3.Error as exc:
        raise HistoryError(f"Cannot save run to HookStorm history: {exc}") from exc
    finally:
        connection.close()
    return str(result["run_id"])


def list_runs(
    path: str | Path | None = None,
    *,
    limit: int = 20,
    scenario: str | None = None,
) -> list[dict[str, Any]]:
    """List recent run summaries without returning stored request data."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise HistoryError("History limit must be an integer between 1 and 500")

    connection = _connect(path)
    try:
        _initialize(connection)
        if scenario:
            rows = connection.execute(
                """
                SELECT run_id, scenario, started_at, target, success,
                       total_deliveries, passed, failed, attempts, duration_ms, result_json
                FROM runs WHERE scenario = ?
                ORDER BY started_at DESC, rowid DESC LIMIT ?
                """,
                (scenario, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT run_id, scenario, started_at, target, success,
                       total_deliveries, passed, failed, attempts, duration_ms, result_json
                FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            result = json.loads(item.pop("result_json"))
            item["success"] = bool(item["success"])
            item.update(result_metrics(result))
            output.append(item)
        return output
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise HistoryError(f"Cannot read HookStorm history: {exc}") from exc
    finally:
        connection.close()


def get_run(run_id: str, path: str | Path | None = None) -> dict[str, Any]:
    """Retrieve one full stored result by run ID."""
    if not run_id.strip():
        raise HistoryError("Run ID cannot be empty")
    connection = _connect(path)
    try:
        _initialize(connection)
        row = connection.execute(
            "SELECT run_id, scenario, started_at, target, success, result_json FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise HistoryError(f"Run '{run_id}' was not found in local history")
        result = json.loads(row["result_json"])
        result["history"] = {
            "success": bool(row["success"]),
            "database_run_id": row["run_id"],
        }
        return result
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise HistoryError(f"Cannot read run '{run_id}': {exc}") from exc
    finally:
        connection.close()


def _delivery_key(delivery: dict[str, Any]) -> tuple[str, str, int]:
    event_type = delivery.get("event_type")
    event_id = delivery.get("event_id")
    duplicate_index = delivery.get("duplicate_index", 0)
    if not isinstance(event_type, str) or not event_type.strip() or len(event_type) > 256:
        raise HistoryError(
            "Delivery event_type must be a non-empty string of at most 256 characters"
        )
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
        raise HistoryError(
            "Delivery event_id must be a non-empty string of at most 256 characters"
        )
    duplicate_index = _require_non_negative_integer(
        duplicate_index, "Delivery duplicate_index", maximum=100
    )
    return (event_type, event_id, duplicate_index)


def _delivery_latency(delivery: dict[str, Any]) -> float:
    return round(
        sum(float(item.get("latency_ms", 0.0)) for item in delivery.get("attempts", [])),
        2,
    )


def compare_results(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    latency_threshold_pct: float = 20.0,
    latency_min_delta_ms: float = 25.0,
    allow_config_change: bool = False,
) -> dict[str, Any]:
    """Compare two runs and identify failure, coverage, and latency regressions."""
    if isinstance(latency_threshold_pct, bool) or not isinstance(latency_threshold_pct, (int, float)):
        raise HistoryError("Latency threshold must be a number")
    if not 0 <= latency_threshold_pct <= 1000 or not math.isfinite(float(latency_threshold_pct)):
        raise HistoryError("Latency threshold must be between 0 and 1000 percent")
    if (
        isinstance(latency_min_delta_ms, bool)
        or not isinstance(latency_min_delta_ms, (int, float))
        or not 0 <= latency_min_delta_ms <= 60_000
    ):
        raise HistoryError("Minimum absolute latency delta must be between 0 and 60000 ms")
    if not isinstance(allow_config_change, bool):
        raise HistoryError("allow_config_change must be a boolean")

    _validate_run_result(current, "Current run")
    _validate_run_result(baseline, "Baseline")
    current_fingerprint = _read_scenario_fingerprint(current, "Current run")
    baseline_fingerprint = _read_scenario_fingerprint(baseline, "Baseline")
    same_configuration = (
        None
        if current_fingerprint is None or baseline_fingerprint is None
        else current_fingerprint == baseline_fingerprint
    )

    current_metrics = result_metrics(current)
    baseline_metrics = result_metrics(baseline)
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    previous = _delivery_index(baseline.get("deliveries", []), "Baseline")
    latest = _delivery_index(current.get("deliveries", []), "Current run")

    if same_configuration is False and not allow_config_change:
        regressions.append({
            "reason": "scenario configuration changed",
            "baseline_fingerprint": baseline_fingerprint,
            "current_fingerprint": current_fingerprint,
        })
    for key in sorted(set(previous) | set(latest)):
        old = previous.get(key)
        new = latest.get(key)
        label = f"{key[0]} / {key[1]} / duplicate {key[2]}"
        if old is None and new is not None:
            coverage_change = {"delivery": label, "change": "added", "passed": bool(new.get("passed"))}
            changes.append(coverage_change)
            if not new.get("passed"):
                regressions.append({**coverage_change, "reason": "new delivery failed"})
            continue
        if new is None and old is not None:
            coverage_change = {"delivery": label, "change": "removed"}
            changes.append(coverage_change)
            regressions.append({**coverage_change, "reason": "delivery coverage decreased"})
            continue

        assert old is not None and new is not None
        old_passed = bool(old.get("passed"))
        new_passed = bool(new.get("passed"))
        old_latency = _delivery_latency(old)
        new_latency = _delivery_latency(new)
        change: dict[str, Any] = {
            "delivery": label,
            "change": "unchanged",
            "baseline_passed": old_passed,
            "current_passed": new_passed,
            "baseline_latency_ms": old_latency,
            "current_latency_ms": new_latency,
        }
        if old_passed and not new_passed:
            change["change"] = "failed"
            regressions.append({**change, "reason": "previously passing delivery now fails"})
        elif not old_passed and new_passed:
            change["change"] = "recovered"
            improvements.append({**change, "reason": "previously failing delivery now passes"})

        if old_latency > 0:
            latency_change_pct = round(((new_latency - old_latency) / old_latency) * 100, 2)
            change["latency_change_pct"] = latency_change_pct
            latency_delta_ms = round(new_latency - old_latency, 2)
            change["latency_delta_ms"] = latency_delta_ms
            if (
                latency_change_pct > latency_threshold_pct
                and latency_delta_ms >= latency_min_delta_ms
            ):
                regressions.append({
                    **change,
                    "reason": f"delivery latency increased by {latency_change_pct}% "
                              f"and {latency_delta_ms} ms "
                              f"(threshold {latency_threshold_pct}%; "
                              f"minimum absolute delta {latency_min_delta_ms} ms)",
                })
            elif (
                latency_change_pct < -latency_threshold_pct
                and -latency_delta_ms >= latency_min_delta_ms
            ):
                improvements.append({
                    **change,
                    "reason": f"delivery latency improved by {abs(latency_change_pct)}% "
                              f"and {abs(latency_delta_ms)} ms",
                })
        changes.append(change)

    baseline_verification = baseline.get("verification")
    current_verification = current.get("verification")
    if baseline_verification is not None or current_verification is not None:
        if current_verification is None:
            regressions.append({
                "reason": "business-state verification was removed",
                "verification": "missing",
            })
        elif not current_verification.get("passed"):
            regressions.append({
                "reason": "business-state verification failed",
                "verification": current_verification,
            })
        elif baseline_verification is not None and not baseline_verification.get("passed"):
            improvements.append({
                "reason": "business-state verification recovered",
                "verification": current_verification,
            })

    failed_delta = current_metrics["failed"] - baseline_metrics["failed"]
    pass_rate_delta = round(
        current_metrics["pass_rate_pct"] - baseline_metrics["pass_rate_pct"], 2
    )
    p95_delta_pct: float | None = None
    baseline_p95 = baseline_metrics["p95_latency_ms"]
    current_p95 = current_metrics["p95_latency_ms"]
    if baseline_p95 > 0:
        p95_delta_pct = round(((current_p95 - baseline_p95) / baseline_p95) * 100, 2)
        p95_delta_ms = round(current_p95 - baseline_p95, 2)
        if (
            p95_delta_pct > latency_threshold_pct
            and p95_delta_ms >= latency_min_delta_ms
        ):
            regressions.append({
                "reason": f"p95 delivery latency increased by {p95_delta_pct}% "
                          f"and {p95_delta_ms} ms "
                          f"(threshold {latency_threshold_pct}%; "
                          f"minimum absolute delta {latency_min_delta_ms} ms)",
                "baseline_p95_ms": baseline_p95,
                "current_p95_ms": current_p95,
                "p95_delta_ms": p95_delta_ms,
            })
        elif (
            p95_delta_pct < -latency_threshold_pct
            and -p95_delta_ms >= latency_min_delta_ms
        ):
            improvements.append({
                "reason": f"p95 delivery latency improved by {abs(p95_delta_pct)}% "
                          f"and {abs(p95_delta_ms)} ms",
                "baseline_p95_ms": baseline_p95,
                "current_p95_ms": current_p95,
                "p95_delta_ms": p95_delta_ms,
            })

    if failed_delta > 0 and not any(
        item.get("reason", "").startswith("new delivery failed")
        or item.get("reason", "").startswith("previously passing")
        for item in regressions
    ):
        regressions.append({
            "reason": f"failed deliveries increased by {failed_delta}",
            "baseline_failed": baseline_metrics["failed"],
            "current_failed": current_metrics["failed"],
        })

    return {
        "baseline_run_id": baseline.get("run_id"),
        "current_run_id": current.get("run_id"),
        "baseline_scenario": baseline.get("scenario"),
        "current_scenario": current.get("scenario"),
        "same_scenario": baseline.get("scenario") == current.get("scenario"),
        "same_configuration": same_configuration,
        "allow_config_change": allow_config_change,
        "latency_threshold_pct": float(latency_threshold_pct),
        "latency_min_delta_ms": float(latency_min_delta_ms),
        "baseline": baseline_metrics,
        "current": current_metrics,
        "delta": {
            "failed_deliveries": failed_delta,
            "pass_rate_pct": pass_rate_delta,
            "p95_latency_pct": p95_delta_pct,
            "duration_ms": round(
                current_metrics["duration_ms"] - baseline_metrics["duration_ms"], 2
            ),
        },
        "changes": changes,
        "regressions": regressions,
        "improvements": improvements,
        "has_regressions": bool(regressions),
    }


def compare_history_runs(
    baseline_run_id: str,
    *,
    current_run_id: str = "latest",
    path: str | Path | None = None,
    latency_threshold_pct: float = 20.0,
    latency_min_delta_ms: float = 25.0,
    allow_config_change: bool = False,
) -> dict[str, Any]:
    """Load and compare two records from the local history database."""
    baseline = get_run(baseline_run_id, path)
    if current_run_id == "latest":
        recent = list_runs(path, limit=1)
        if not recent:
            raise HistoryError("No runs are stored in local history yet")
        current_run_id = recent[0]["run_id"]
        if current_run_id == baseline_run_id:
            raise HistoryError(
                "The latest run is the selected baseline. Run the scenario again "
                "or provide --current with another run ID."
            )
    current = get_run(current_run_id, path)
    return compare_results(
        current,
        baseline,
        latency_threshold_pct=latency_threshold_pct,
        latency_min_delta_ms=latency_min_delta_ms,
        allow_config_change=allow_config_change,
    )


_EXPORT_FORMAT = "hookstorm-history"
_EXPORT_FORMAT_VERSION = 1


def _resolve_history_run_id(run_id: str, path: str | Path | None = None) -> str:
    if run_id != "latest":
        return run_id
    recent = list_runs(path, limit=1)
    if not recent:
        raise HistoryError("No runs are stored in local history yet")
    return str(recent[0]["run_id"])


def export_history_run(
    run_id: str,
    output: str | Path,
    path: str | Path | None = None,
) -> str:
    """Write one sanitized run as a portable JSON baseline and return its resolved path."""
    resolved_id = _resolve_history_run_id(run_id, path)
    result = get_run(resolved_id, path)
    safe_result = _sanitize_result(result)
    destination = Path(output).expanduser()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "format": _EXPORT_FORMAT,
            "format_version": _EXPORT_FORMAT_VERSION,
            "run": safe_result,
        }
        encoded = json.dumps(
            document, ensure_ascii=False, indent=2, allow_nan=False
        )
        _write_private_text(destination, encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise HistoryError(f"Cannot export run '{resolved_id}': {exc}") from exc
    return str(destination.resolve())


def load_history_export(source: str | Path) -> dict[str, Any]:
    """Read a bounded portable baseline and re-apply the history field whitelist."""
    location = Path(source).expanduser()

    def reject_non_standard_number(value: str) -> None:
        raise HistoryError(
            f"Baseline file '{location}' contains non-standard JSON number {value}"
        )

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise HistoryError(
                    f"Baseline file '{location}' contains duplicate JSON key {key!r}"
                )
            parsed[key] = value
        return parsed

    try:
        with location.open("rb") as stream:
            serialized = stream.read(_MAX_HISTORY_EXPORT_BYTES + 1)
    except OSError as exc:
        raise HistoryError(f"Cannot read baseline file '{location}': {exc}") from exc

    if len(serialized) > _MAX_HISTORY_EXPORT_BYTES:
        raise HistoryError(
            f"Baseline file '{location}' exceeds the "
            f"{_MAX_HISTORY_EXPORT_BYTES}-byte size limit"
        )

    try:
        source_text = serialized.decode("utf-8")
        document = json.loads(
            source_text,
            parse_constant=reject_non_standard_number,
            object_pairs_hook=reject_duplicate_keys,
        )
    except UnicodeDecodeError as exc:
        raise HistoryError(f"Baseline file '{location}' is not valid UTF-8") from exc
    except (ValueError, RecursionError) as exc:
        raise HistoryError(
            f"Baseline file '{location}' is not valid JSON within parser limits: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise HistoryError("Baseline file must contain a JSON object")
    format_version = document.get("format_version")
    if (
        document.get("format") != _EXPORT_FORMAT
        or isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != _EXPORT_FORMAT_VERSION
    ):
        raise HistoryError(
            "Unsupported baseline file format; expected hookstorm-history version 1"
        )
    result = document.get("run")
    if not isinstance(result, dict):
        raise HistoryError("Baseline file is missing its run object")

    required = ("run_id", "scenario", "started_at", "target", "summary", "deliveries")
    missing = [key for key in required if key not in result]
    if missing:
        raise HistoryError(
            f"Baseline run is incomplete; missing: {', '.join(missing)}"
        )
    try:
        _validate_run_result(result, "Baseline")
        safe_result = _sanitize_result(result)
        result_metrics(safe_result)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise HistoryError(f"Baseline run contains invalid metrics: {exc}") from exc
    return safe_result


def compare_history_file(
    baseline_file: str | Path,
    *,
    current_run_id: str = "latest",
    path: str | Path | None = None,
    latency_threshold_pct: float = 20.0,
    latency_min_delta_ms: float = 25.0,
    allow_config_change: bool = False,
) -> dict[str, Any]:
    """Compare the latest local run against a sanitized JSON baseline file."""
    baseline = load_history_export(baseline_file)
    resolved_current_id = _resolve_history_run_id(current_run_id, path)
    if resolved_current_id == baseline.get("run_id"):
        raise HistoryError(
            "The current run is the selected baseline. Run the scenario again "
            "or export a different baseline."
        )
    current = get_run(resolved_current_id, path)
    return compare_results(
        current,
        baseline,
        latency_threshold_pct=latency_threshold_pct,
        latency_min_delta_ms=latency_min_delta_ms,
        allow_config_change=allow_config_change,
    )
