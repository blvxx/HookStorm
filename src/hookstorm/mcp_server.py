"""Local MCP tools for inspecting and safely replaying HookStorm deliveries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hookstorm import __version__
from hookstorm.config import ScenarioError, load_scenario, scenario_fingerprint
from hookstorm.history import HistoryError, get_run, list_runs, save_run
from hookstorm.runner import run_scenario
from hookstorm.safety import validate_target

_MAX_SCENARIO_FILE_BYTES = 2 * 1024 * 1024
_MAX_SCENARIO_LIST_ITEMS = 100


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _scenario_root(value: str | Path | None) -> Path:
    raw = value if value is not None else os.getenv(
        "HOOKSTORM_MCP_SCENARIO_ROOT", os.getcwd()
    )
    root = Path(raw).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Scenario root is not an existing directory: {root}")
    return root


def _resolve_scenario_path(value: str, root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("scenario_path must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Scenario file does not exist: {value}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot resolve scenario file: {exc}") from exc

    if not resolved.is_relative_to(root):
        raise ValueError("Scenario path must stay inside HOOKSTORM_MCP_SCENARIO_ROOT")
    if not resolved.is_file() or resolved.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("Scenario path must reference a YAML file")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ValueError(f"Cannot inspect scenario file: {exc}") from exc
    if size > _MAX_SCENARIO_FILE_BYTES:
        raise ValueError(
            f"Scenario file exceeds the MCP safety limit of "
            f"{_MAX_SCENARIO_FILE_BYTES // (1024 * 1024)} MiB"
        )
    return resolved


def _load_scenario(value: str, root: Path) -> tuple[Path, dict[str, Any]]:
    path = _resolve_scenario_path(value, root)
    return path, load_scenario(path)


def _attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: attempt.get(key)
        for key in (
            "attempt",
            "status_code",
            "latency_ms",
            "elapsed_ms",
            "error",
            "expected_status",
            "expected_json",
            "passed",
            "injected_fault",
        )
        if key in attempt
    }


def _diagnose_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
    attempts = delivery.get("attempts", [])
    latest = attempts[-1] if attempts else {}
    status = latest.get("status_code")
    error = str(latest.get("error") or "")
    message = error.lower()

    if status in {401, 403}:
        category = "authentication_or_signature"
        explanation = (
            "The receiver rejected the request. Check the signature provider, "
            "secret value, signing format, and any endpoint authentication."
        )
    elif "timed out" in message or "timeout" in message:
        category = "timeout"
        explanation = (
            "The receiver did not complete the response within the configured "
            "deadline. Check receiver latency, dependencies, and timeout settings."
        )
    elif "request error" in message:
        category = "transport"
        explanation = (
            "The request did not complete at the HTTP transport layer. Check the "
            "target URL, DNS/TCP/TLS reachability, firewall, and staging allowlist."
        )
    elif status == 404:
        category = "endpoint_route"
        explanation = (
            "The target returned 404. Check the path, HTTP method, router, and "
            "whether the selected environment exposes this webhook endpoint."
        )
    elif isinstance(status, int) and status >= 500:
        category = "receiver_server_error"
        explanation = (
            "The receiver or an upstream service returned a server error. Inspect "
            "application and dependency logs around the recorded attempt time."
        )
    elif latest.get("expected_json") is False:
        category = "response_assertion"
        explanation = (
            "The response JSON did not satisfy the scenario assertion. Compare "
            "the receiver response contract with the scenario's expect_json value."
        )
    elif latest.get("expected_status") is False:
        category = "status_assertion"
        explanation = (
            "The final HTTP status is not in the scenario's expect_status list. "
            "Check the receiver response and the expected status codes."
        )
    elif not attempts:
        category = "missing_attempt"
        explanation = "The stored delivery has no attempt details; inspect the run record."
    else:
        category = "unknown"
        explanation = (
            "The stored fields do not identify a common failure category. "
            "Inspect the receiver logs and scenario assertions."
        )

    return {
        "event_type": delivery.get("event_type"),
        "event_id": delivery.get("event_id"),
        "duplicate_index": delivery.get("duplicate_index", 0),
        "category": category,
        "explanation": explanation,
        "attempts": [_attempt_summary(item) for item in attempts],
    }


def _scenario_summary(path: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    events = []
    for event in scenario["events"]:
        effective_delivery = {
            **scenario["delivery"],
            **event["delivery"],
        }
        expected = event.get("expect_json")
        events.append(
            {
                "event_type": event["id"],
                "event_id": event["event_id"],
                "signature": effective_delivery.get("signature", "none"),
                "signature_provider": effective_delivery.get(
                    "signature_provider", "generic"
                ),
                "max_attempts": scenario["retry"]["max_attempts"],
                "duplicate_count": effective_delivery.get("duplicate_count", 0),
                "schedule_after_ms": event.get("schedule_after_ms", 0),
                "expected_statuses": event["expect_status"],
                "expected_json_top_level_keys": sorted(expected) if expected else [],
            }
        )
    return {
        "path": path.name,
        "name": scenario["name"],
        "description": scenario.get("description", ""),
        "fingerprint": scenario_fingerprint(scenario),
        "event_count": len(events),
        "retry": {
            "max_attempts": scenario["retry"]["max_attempts"],
            "backoff_ms": scenario["retry"]["backoff_ms"],
            "retry_on": scenario["retry"]["retry_on"],
        },
        "delivery": {
            "concurrency": scenario["delivery"].get("concurrency", 1),
            "order": scenario["delivery"].get("order", "in_order"),
        },
        "verification_configured": scenario.get("verify") is not None,
        "events": events,
    }


def _validate_replay_target(target: str) -> str:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target must be supplied explicitly for every replay")
    parsed = urlsplit(target.strip())
    if parsed.query:
        raise ValueError(
            "MCP replay targets must not contain query strings; use environment-backed "
            "credentials in the receiver instead"
        )
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(
            "MCP replay targets must not contain embedded credentials or URL fragments"
        )
    # The MCP replay tool intentionally keeps the same loopback-only default as HookStorm.
    return validate_target(target)


def _target_matches_history(target: str, historical_target: str) -> bool:
    return target.rstrip("/") == historical_target.rstrip("/")


def _prepare_replay(
    *,
    run: dict[str, Any],
    scenario: dict[str, Any],
    event_id: str,
    target: str,
    allow_scenario_change: bool,
    allow_target_change: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    failed = [
        item
        for item in run.get("deliveries", [])
        if item.get("event_id") == event_id and not item.get("passed")
    ]
    if not failed:
        raise ValueError(
            f"Run '{run['run_id']}' has no failed delivery for event_id '{event_id}'"
        )

    candidates = [
        event
        for event in scenario["events"]
        if event["event_id"] == event_id
    ]
    if len(candidates) != 1:
        raise ValueError(
            "The scenario must contain exactly one event with the requested event_id"
        )
    event = candidates[0]

    if any(item.get("event_type") != event["id"] for item in failed):
        scenario_changed = True
    else:
        stored_fingerprint = run.get("scenario_fingerprint")
        scenario_changed = (
            not isinstance(stored_fingerprint, str)
            or stored_fingerprint != scenario_fingerprint(scenario)
        )

    if run.get("scenario") != scenario["name"]:
        scenario_changed = True

    target_changed = not _target_matches_history(target, run.get("target", ""))
    warnings = []
    if scenario_changed:
        warnings.append(
            "The current YAML scenario does not match the configuration recorded "
            "for the failed run."
        )
    if target_changed:
        warnings.append(
            "The supplied target differs from the target recorded in the failed run."
        )

    if scenario_changed and not allow_scenario_change:
        raise ValueError(
            "Scenario mismatch: inspect the current YAML before replay, then set "
            "allow_scenario_change=true only when the changed payload/configuration is intended."
        )
    if target_changed and not allow_target_change:
        raise ValueError(
            "Target mismatch: pass the exact historical target or set "
            "allow_target_change=true after checking the destination."
        )

    return event, {
        "failed_deliveries_found": len(failed),
        "scenario_changed": scenario_changed,
        "target_changed": target_changed,
        "warnings": warnings,
    }


def create_server(
    *,
    history_db: str | Path | None = None,
    scenario_root: str | Path | None = None,
    enable_replay: bool | None = None,
):
    """Create the local MCP server; injected paths make its tools testable."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError(
            "The HookStorm MCP server requires the optional dependency. "
            "Install HookStorm with: python -m pip install 'hookstorm[mcp]'"
        ) from exc

    root = _scenario_root(scenario_root)
    replay_allowed = (
        _enabled(os.getenv("HOOKSTORM_MCP_ENABLE_REPLAY"))
        if enable_replay is None
        else enable_replay
    )
    server = MCPServer(
        "HookStorm",
        instructions=(
            "Inspect local HookStorm run history and YAML scenarios. "
            "History contains sanitized metadata, not request payloads or response bodies. "
            "Webhook replay is disabled unless explicitly enabled in the server environment, "
            "requires a failed event, an explicit target, and per-call confirmation. "
            "Replay targets are loopback-only."
        ),
    )

    @server.tool()
    def hookstorm_list_runs(
        limit: int = 20,
        scenario: str | None = None,
    ) -> dict[str, Any]:
        """List recent sanitized HookStorm runs; this tool does not send webhook requests."""
        runs = list_runs(history_db, limit=limit, scenario=scenario)
        return {"count": len(runs), "runs": runs}

    @server.tool()
    def hookstorm_inspect_run(run_id: str) -> dict[str, Any]:
        """Inspect one stored run, including attempt statuses and safe error details."""
        run = get_run(run_id, history_db)
        return {
            "run": run,
            "diagnostics": [
                _diagnose_delivery(item)
                for item in run.get("deliveries", [])
                if not item.get("passed")
            ],
        }

    @server.tool()
    def hookstorm_list_scenarios() -> dict[str, Any]:
        """List YAML scenarios in the configured root and its scenarios directory."""
        candidates = set(root.glob("*.yaml")) | set(root.glob("*.yml"))
        scenario_dir = root / "scenarios"
        if scenario_dir.is_dir():
            candidates.update(scenario_dir.glob("*.yaml"))
            candidates.update(scenario_dir.glob("*.yml"))
        paths = []
        for candidate in sorted(candidates):
            try:
                resolved = candidate.resolve(strict=True)
                if (
                    resolved.is_relative_to(root)
                    and resolved.is_file()
                    and resolved.suffix.lower() in {".yaml", ".yml"}
                ):
                    paths.append(resolved.relative_to(root).as_posix())
            except OSError:
                continue
            if len(paths) >= _MAX_SCENARIO_LIST_ITEMS:
                break
        return {"root": str(root), "count": len(paths), "scenarios": paths}

    @server.tool()
    def hookstorm_inspect_scenario(scenario_path: str) -> dict[str, Any]:
        """Validate a YAML scenario and describe event contracts without exposing payload values."""
        path, scenario = _load_scenario(scenario_path, root)
        return _scenario_summary(path, scenario)

    @server.tool()
    def hookstorm_diagnose_run(run_id: str) -> dict[str, Any]:
        """Explain failed deliveries using stored statuses, timings, and assertion outcomes."""
        run = get_run(run_id, history_db)
        failures = [
            _diagnose_delivery(item)
            for item in run.get("deliveries", [])
            if not item.get("passed")
        ]
        verification = run.get("verification")
        verification_diagnosis = None
        if verification is not None and not verification.get("passed"):
            verification_diagnosis = {
                "category": "business_state_verification",
                "status_code": verification.get("status_code"),
                "error": verification.get("error"),
                "explanation": (
                    "The post-delivery state probe did not satisfy its assertion. "
                    "Check the read-only verification endpoint and the expected business state."
                ),
            }
        return {
            "run_id": run["run_id"],
            "scenario": run["scenario"],
            "target": run["target"],
            "success": run["summary"]["success"],
            "summary": run["summary"],
            "failed_deliveries": failures,
            "verification": verification_diagnosis,
            "limitations": [
                "History does not contain request payloads, request headers, or response bodies.",
                "JSON assertion failures include the mismatch path, not the actual response value.",
                "Use the original YAML scenario to inspect or replay an event.",
            ],
        }

    @server.tool()
    async def hookstorm_replay_failed_delivery(
        run_id: str,
        scenario_path: str,
        event_id: str,
        target: str,
        confirm: bool = False,
        allow_scenario_change: bool = False,
        allow_target_change: bool = False,
    ) -> dict[str, Any]:
        """Replay one failed event from its original YAML; sends a real POST only after explicit opt-ins."""
        run = get_run(run_id, history_db)
        path, scenario = _load_scenario(scenario_path, root)
        safe_target = _validate_replay_target(target)
        event, checks = _prepare_replay(
            run=run,
            scenario=scenario,
            event_id=event_id,
            target=safe_target,
            allow_scenario_change=allow_scenario_change,
            allow_target_change=allow_target_change,
        )
        preview = {
            "executed": False,
            "replay_enabled": replay_allowed,
            "requires_confirmation": True,
            "source_run_id": run_id,
            "scenario": path.name,
            "event_type": event["id"],
            "event_id": event_id,
            "target": safe_target,
            "failed_deliveries_found": checks["failed_deliveries_found"],
            "scenario_changed": checks["scenario_changed"],
            "target_changed": checks["target_changed"],
            "warnings": checks["warnings"],
        }
        if not replay_allowed:
            preview["message"] = (
                "No request was sent. Replay is disabled. Set "
                "HOOKSTORM_MCP_ENABLE_REPLAY=1 in the MCP server environment "
                "after reviewing the local test boundary."
            )
            return preview
        if not confirm:
            preview["message"] = (
                "No request was sent. Set confirm=true only after approving this "
                "specific replay and verifying the target."
            )
            return preview

        selected_event = {**event, "schedule_after_ms": 0}
        selected_scenario = {
            **scenario,
            "events": [selected_event],
            # A full-scenario state assertion may not be valid for a one-event replay.
            "verify": None,
        }
        result = await run_scenario(selected_scenario, safe_target)
        history_saved = True
        history_warning = None
        try:
            save_run(result, history_db)
        except HistoryError as exc:
            history_saved = False
            history_warning = str(exc)

        return {
            "executed": True,
            "replay_enabled": True,
            "source_run_id": run_id,
            "run_id": result["run_id"],
            "scenario": result["scenario"],
            "target": result["target"],
            "summary": result["summary"],
            "deliveries": [
                {
                    "event_type": item["event_type"],
                    "event_id": item["event_id"],
                    "duplicate_index": item["duplicate_index"],
                    "passed": item["passed"],
                    "attempts": [_attempt_summary(attempt) for attempt in item["attempts"]],
                }
                for item in result["deliveries"]
            ],
            "history_saved": history_saved,
            "history_warning": history_warning,
            "verification": {
                "performed": False,
                "reason": "The original scenario's whole-run verification probe was not repeated.",
            },
        }

    return server


def main() -> None:
    """Run HookStorm MCP over the local stdio transport."""
    create_server().run()


if __name__ == "__main__":
    main()
