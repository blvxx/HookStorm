"""Async webhook scenario runner."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from hookstorm.safety import validate_target


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _matches_subset(actual: Any, expected: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(actual, dict):
        return False, "response body is not a JSON object"
    for key, value in expected.items():
        if key not in actual:
            return False, f"response JSON is missing key '{key}'"
        if actual[key] != value:
            return False, f"response JSON key '{key}' did not match the expected value"
    return True, None


def _signature(body: bytes, secret: str, mode: str) -> str | None:
    if mode in {"none", "missing"}:
        return None
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if mode == "invalid":
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    return f"sha256={digest}"


async def run_scenario(
    scenario: dict[str, Any],
    target: str,
    *,
    allow_remote: bool = False,
    secret: str | None = None,
) -> dict[str, Any]:
    """Execute a validated scenario and return a privacy-conscious result document."""
    target = validate_target(target, allow_remote=allow_remote)
    secret = secret if secret is not None else os.getenv("HOOKSTORM_SECRET")
    started = datetime.now(UTC)
    monotonic_started = time.perf_counter()
    run_id = uuid.uuid4().hex[:12]
    all_deliveries: list[dict[str, Any]] = []
    default_delivery = {
        "order": "in_order", "duplicate_count": 0, "delay_ms": 0,
        "timeout_ms": 5_000, "signature": "none",
    }
    retry = scenario["retry"]
    ordered_events = list(scenario["events"])
    if scenario["delivery"].get("order", "in_order") == "reverse":
        ordered_events.reverse()

    async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        for event in ordered_events:
            delivery_config = {**default_delivery, **scenario["delivery"], **event["delivery"]}
            event_body = _json_bytes(event["payload"])
            base_headers = {
                "Content-Type": "application/json",
                "User-Agent": "HookStorm/0.1 (+https://github.com/blvxx/HookStorm)",
                "X-HookStorm-Run": run_id,
                "X-Event-ID": event["event_id"],
                "X-Event-Type": event["id"],
            }
            reserved = {
                "x-hookstorm-run", "x-event-id", "x-event-type", "x-hookstorm-attempt",
                "x-hookstorm-signature", "x-hookstorm-duplicate-index",
            }
            for key, value in event["headers"].items():
                if key.lower() not in reserved:
                    base_headers[key] = value

            signature_mode = delivery_config["signature"]
            if signature_mode in {"valid", "invalid"} and not secret:
                raise ValueError("This scenario requires HOOKSTORM_SECRET or the configured secret environment variable.")
            signature = _signature(event_body, secret or "", signature_mode)
            if signature:
                base_headers["X-HookStorm-Signature"] = signature
            else:
                base_headers.pop("X-HookStorm-Signature", None)

            for duplicate_index in range(delivery_config["duplicate_count"] + 1):
                attempts: list[dict[str, Any]] = []
                expected_json = (
                    event["expect_duplicate_json"]
                    if duplicate_index > 0 and event["expect_duplicate_json"] is not None
                    else event["expect_json"]
                )
                for attempt in range(1, retry["max_attempts"] + 1):
                    if delivery_config["delay_ms"]:
                        await asyncio.sleep(delivery_config["delay_ms"] / 1000)
                    headers = dict(base_headers)
                    headers["X-HookStorm-Attempt"] = str(attempt)
                    headers["X-HookStorm-Duplicate-Index"] = str(duplicate_index)
                    started_attempt = time.perf_counter()
                    outcome = {
                        "attempt": attempt, "status_code": None, "latency_ms": 0.0,
                        "error": None, "expected_status": False,
                        "expected_json": expected_json is None, "passed": False,
                    }
                    try:
                        response = await client.post(
                            target, content=event_body, headers=headers,
                            timeout=delivery_config["timeout_ms"] / 1000,
                        )
                        outcome["status_code"] = response.status_code
                        outcome["latency_ms"] = round((time.perf_counter() - started_attempt) * 1000, 2)
                        outcome["expected_status"] = response.status_code in event["expect_status"]
                        if expected_json is not None:
                            try:
                                matched, mismatch = _matches_subset(response.json(), expected_json)
                            except ValueError:
                                matched, mismatch = False, "response body is not valid JSON"
                            outcome["expected_json"] = matched
                            if mismatch:
                                outcome["error"] = mismatch
                        outcome["passed"] = outcome["expected_status"] and outcome["expected_json"]
                        retryable = response.status_code in retry["retry_on"]
                    except httpx.TimeoutException:
                        outcome["latency_ms"] = round((time.perf_counter() - started_attempt) * 1000, 2)
                        outcome["error"] = "request timed out"
                        retryable = True
                    except httpx.RequestError as exc:
                        outcome["latency_ms"] = round((time.perf_counter() - started_attempt) * 1000, 2)
                        outcome["error"] = f"request error: {type(exc).__name__}"
                        retryable = True
                    attempts.append(outcome)
                    if not retryable or attempt >= retry["max_attempts"]:
                        break
                    if retry["backoff_ms"]:
                        await asyncio.sleep((retry["backoff_ms"] * (2 ** (attempt - 1))) / 1000)

                last = attempts[-1]
                all_deliveries.append({
                    "event_id": event["event_id"],
                    "event_type": event["id"],
                    "duplicate_index": duplicate_index,
                    "attempts": attempts,
                    "passed": bool(last["passed"]),
                })

    passed = sum(1 for delivery in all_deliveries if delivery["passed"])
    return {
        "schema_version": 1, "tool": "HookStorm", "version": "0.1.0", "run_id": run_id,
        "scenario": scenario["name"], "target": target, "started_at": started.isoformat(),
        "duration_ms": round((time.perf_counter() - monotonic_started) * 1000, 2),
        "summary": {
            "total_deliveries": len(all_deliveries), "passed": passed,
            "failed": len(all_deliveries) - passed,
            "attempts": sum(len(delivery["attempts"]) for delivery in all_deliveries),
            "success": passed == len(all_deliveries),
        },
        "deliveries": all_deliveries,
    }
