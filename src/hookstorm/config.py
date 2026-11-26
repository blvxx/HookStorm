"""Scenario loading and validation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    """Raised when a scenario file is invalid."""


_ALLOWED_TOP_LEVEL = {"version", "name", "description", "delivery", "retry", "events"}
_ALLOWED_DELIVERY = {"order", "duplicate_count", "delay_ms", "timeout_ms", "signature"}
_ALLOWED_RETRY = {"max_attempts", "backoff_ms", "retry_on"}
_ALLOWED_EVENT = {
    "id", "event_id", "payload", "headers", "delivery",
    "expect_status", "expect_json", "expect_duplicate_json",
}


def _unknown_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ScenarioError(f"Unknown key(s) in {where}: {', '.join(sorted(unknown))}")


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ScenarioError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _validate_delivery(value: Any, where: str, *, allow_order: bool = True) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioError(f"{where} must be a mapping")
    allowed = _ALLOWED_DELIVERY if allow_order else _ALLOWED_DELIVERY - {"order"}
    _unknown_keys(value, allowed, where)
    result = dict(value)
    if "order" in result and result["order"] not in {"in_order", "reverse"}:
        raise ScenarioError(f"{where}.order must be 'in_order' or 'reverse'")
    if "duplicate_count" in result:
        result["duplicate_count"] = _integer(result["duplicate_count"], f"{where}.duplicate_count", minimum=0, maximum=100)
    if "delay_ms" in result:
        result["delay_ms"] = _integer(result["delay_ms"], f"{where}.delay_ms", minimum=0, maximum=60_000)
    if "timeout_ms" in result:
        result["timeout_ms"] = _integer(result["timeout_ms"], f"{where}.timeout_ms", minimum=1, maximum=300_000)
    if "signature" in result and result["signature"] not in {"none", "valid", "invalid", "missing"}:
        raise ScenarioError(f"{where}.signature must be none, valid, invalid, or missing")
    return result


def _validate_retry(value: Any) -> dict[str, Any]:
    if value is None:
        return {"max_attempts": 1, "backoff_ms": 0, "retry_on": [408, 429, 500, 502, 503, 504]}
    if not isinstance(value, dict):
        raise ScenarioError("retry must be a mapping")
    _unknown_keys(value, _ALLOWED_RETRY, "retry")
    max_attempts = _integer(value.get("max_attempts", 1), "retry.max_attempts", minimum=1, maximum=10)
    backoff_ms = _integer(value.get("backoff_ms", 0), "retry.backoff_ms", minimum=0, maximum=60_000)
    retry_on = value.get("retry_on", [408, 429, 500, 502, 503, 504])
    if not isinstance(retry_on, list) or not retry_on:
        raise ScenarioError("retry.retry_on must be a non-empty list of HTTP status codes")
    if any(isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599 for code in retry_on):
        raise ScenarioError("retry.retry_on entries must be HTTP status codes from 100 to 599")
    return {"max_attempts": max_attempts, "backoff_ms": backoff_ms, "retry_on": sorted(set(retry_on))}


def validate_scenario(data: Any) -> dict[str, Any]:
    """Validate and normalize a parsed scenario mapping."""
    if not isinstance(data, dict):
        raise ScenarioError("Scenario root must be a YAML mapping")
    _unknown_keys(data, _ALLOWED_TOP_LEVEL, "scenario")
    if data.get("version") != 1:
        raise ScenarioError("Scenario version must be 1")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScenarioError("name must be a non-empty string")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ScenarioError("description must be a string")

    delivery = _validate_delivery(data.get("delivery", {}), "delivery")
    retry = _validate_retry(data.get("retry"))
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise ScenarioError("events must be a non-empty list")
    if len(events) > 500:
        raise ScenarioError("A scenario may contain at most 500 events")

    normalized_events: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        where = f"events[{index}]"
        if not isinstance(event, dict):
            raise ScenarioError(f"{where} must be a mapping")
        _unknown_keys(event, _ALLOWED_EVENT, where)
        event_id = event.get("event_id")
        event_type = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ScenarioError(f"{where}.event_id must be a non-empty string")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ScenarioError(f"{where}.id must be a non-empty string")
        payload = event.get("payload", {})
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ScenarioError(f"{where}.payload must be valid JSON-compatible data") from exc
        headers = event.get("headers", {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise ScenarioError(f"{where}.headers must be a mapping of string keys to string values")
        for key, val in headers.items():
            if not re.fullmatch(r"[!#$%&'*+._^|~0-9A-Za-z-]+", key) or any(ch in key + val for ch in "\r\n"):
                raise ScenarioError(f"{where}.headers contains an invalid header")
            if key.lower() in {"host", "content-length", "transfer-encoding"}:
                raise ScenarioError(f"{where}.headers may not override {key}")
        event_delivery = _validate_delivery(event.get("delivery", {}), f"{where}.delivery", allow_order=False)
        expect_status = event.get("expect_status", [200, 201, 202, 204])
        if isinstance(expect_status, int) and not isinstance(expect_status, bool):
            expect_status = [expect_status]
        if not isinstance(expect_status, list) or not expect_status:
            raise ScenarioError(f"{where}.expect_status must be an integer or non-empty list")
        if any(isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599 for code in expect_status):
            raise ScenarioError(f"{where}.expect_status entries must be HTTP status codes")
        for expectation_key in ("expect_json", "expect_duplicate_json"):
            expected = event.get(expectation_key)
            if expected is not None and not isinstance(expected, dict):
                raise ScenarioError(f"{where}.{expectation_key} must be a JSON object")
        normalized_events.append({
            "id": event_type,
            "event_id": event_id,
            "payload": payload,
            "headers": headers,
            "delivery": event_delivery,
            "expect_status": sorted(set(expect_status)),
            "expect_json": event.get("expect_json"),
            "expect_duplicate_json": event.get("expect_duplicate_json"),
        })

    return {
        "version": 1,
        "name": name.strip(),
        "description": description,
        "delivery": delivery,
        "retry": retry,
        "events": normalized_events,
    }


def load_scenario(path: str | Path) -> dict[str, Any]:
    """Load a scenario from a YAML file using the safe loader."""
    scenario_path = Path(path)
    try:
        raw = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"Scenario file not found: {scenario_path}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"Invalid YAML in {scenario_path}: {exc}") from exc
    except OSError as exc:
        raise ScenarioError(f"Cannot read scenario file {scenario_path}: {exc}") from exc
    return validate_scenario(raw)
