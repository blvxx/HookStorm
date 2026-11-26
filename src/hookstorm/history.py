"""Local run history and regression comparison for HookStorm."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".hookstorm" / "history.sqlite3"


class HistoryError(RuntimeError):
    """Raised when a local run-history operation cannot be completed."""


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
    try:
        if location != ":memory:":
            Path(location).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(location, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise HistoryError(f"Cannot open HookStorm history database: {exc}") from exc


def _initialize(connection: sqlite3.Connection) -> None:
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
    connection.commit()


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
    index = max(0, min(len(ordered) - 1, int((len(ordered) * percentile + 0.999999)) - 1))
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


def save_run(result: dict[str, Any], path: str | Path | None = None) -> str:
    """Persist a sanitized HookStorm result and return its run ID."""
    required = ("run_id", "scenario", "started_at", "target", "summary", "deliveries")
    missing = [key for key in required if key not in result]
    if missing:
        raise HistoryError(f"Cannot save incomplete run result; missing: {', '.join(missing)}")

    metrics = result_metrics(result)
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
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
    return (
        str(delivery.get("event_type", "")),
        str(delivery.get("event_id", "")),
        int(delivery.get("duplicate_index", 0)),
    )


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
) -> dict[str, Any]:
    """Compare two runs and identify failure, coverage, and latency regressions."""
    if isinstance(latency_threshold_pct, bool) or not isinstance(latency_threshold_pct, (int, float)):
        raise HistoryError("Latency threshold must be a number")
    if not 0 <= latency_threshold_pct <= 1000:
        raise HistoryError("Latency threshold must be between 0 and 1000 percent")

    current_metrics = result_metrics(current)
    baseline_metrics = result_metrics(baseline)
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    previous = {_delivery_key(item): item for item in baseline.get("deliveries", [])}
    latest = {_delivery_key(item): item for item in current.get("deliveries", [])}
    for key in sorted(set(previous) | set(latest)):
        old = previous.get(key)
        new = latest.get(key)
        label = f"{key[0]} / {key[1]} / duplicate {key[2]}"
        if old is None and new is not None:
            change = {"delivery": label, "change": "added", "passed": bool(new.get("passed"))}
            changes.append(change)
            if not new.get("passed"):
                regressions.append({**change, "reason": "new delivery failed"})
            continue
        if new is None and old is not None:
            change = {"delivery": label, "change": "removed"}
            changes.append(change)
            regressions.append({**change, "reason": "delivery coverage decreased"})
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
            if latency_change_pct > latency_threshold_pct:
                regressions.append({
                    **change,
                    "reason": f"delivery latency increased by {latency_change_pct}% "
                              f"(threshold {latency_threshold_pct}%)",
                })
            elif latency_change_pct < -latency_threshold_pct:
                improvements.append({
                    **change,
                    "reason": f"delivery latency improved by {abs(latency_change_pct)}%",
                })
        changes.append(change)

    failed_delta = current_metrics["failed"] - baseline_metrics["failed"]
    pass_rate_delta = round(
        current_metrics["pass_rate_pct"] - baseline_metrics["pass_rate_pct"], 2
    )
    p95_delta_pct: float | None = None
    baseline_p95 = baseline_metrics["p95_latency_ms"]
    current_p95 = current_metrics["p95_latency_ms"]
    if baseline_p95 > 0:
        p95_delta_pct = round(((current_p95 - baseline_p95) / baseline_p95) * 100, 2)
        if p95_delta_pct > latency_threshold_pct:
            regressions.append({
                "reason": f"p95 delivery latency increased by {p95_delta_pct}% "
                          f"(threshold {latency_threshold_pct}%)",
                "baseline_p95_ms": baseline_p95,
                "current_p95_ms": current_p95,
            })
        elif p95_delta_pct < -latency_threshold_pct:
            improvements.append({
                "reason": f"p95 delivery latency improved by {abs(p95_delta_pct)}%",
                "baseline_p95_ms": baseline_p95,
                "current_p95_ms": current_p95,
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
        "latency_threshold_pct": float(latency_threshold_pct),
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
) -> dict[str, Any]:
    """Load and compare two records from the local history database."""
    if current_run_id == "latest":
        recent = list_runs(path, limit=1)
        if not recent:
            raise HistoryError("No runs are stored in local history yet")
        current_run_id = recent[0]["run_id"]
    current = get_run(current_run_id, path)
    baseline = get_run(baseline_run_id, path)
    return compare_results(
        current,
        baseline,
        latency_threshold_pct=latency_threshold_pct,
    )
