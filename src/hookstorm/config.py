"""Scenario loading and validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from hookstorm.json_safety import ensure_json_nesting_depth


class ScenarioError(ValueError):
    """Raised when a scenario file is invalid."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys before constructing mappings."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        seen: dict[Any, Any] = {}
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                previous = seen.get(key)
            except TypeError as exc:
                raise ScenarioError("YAML mapping keys must be hashable") from exc
            if previous is not None:
                raise ScenarioError(
                    f"Duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}; "
                    f"first defined at line {previous.line + 1}"
                )
            seen[key] = key_node.start_mark

        self.flatten_mapping(node)
        return super().construct_mapping(node, deep=deep)


_DEFAULT_RUN_TIMEOUT_MS = 3_600_000
_MAX_RUN_TIMEOUT_MS = 86_400_000

_ALLOWED_TOP_LEVEL = {
    "version", "name", "description", "delivery", "retry", "events", "verify", "run_timeout_ms"
}
_ALLOWED_DELIVERY = {
    "order", "duplicate_count", "delay_ms", "timeout_ms", "signature", "concurrency",
    "signature_provider", "signature_timestamp_offset_seconds", "fault_injection",
}
_ALLOWED_RETRY = {"max_attempts", "backoff_ms", "retry_on"}
_ALLOWED_EVENT = {
    "id", "event_id", "payload", "headers", "delivery",
    "expect_status", "expect_json", "expect_duplicate_json", "schedule_after_ms",
}


def _unknown_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ScenarioError(f"Unknown key(s) in {where}: {', '.join(sorted(map(str, unknown)))}")


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ScenarioError(f"{name} must be an integer between {minimum} and {maximum}")
    return value



def _estimate_worst_case_duration_ms(
    delivery: dict[str, Any],
    retry: dict[str, Any],
    events: list[dict[str, Any]],
    verify: dict[str, Any] | None,
) -> int:
    """Conservatively estimate request timeouts, waits, scheduling, and verification."""
    concurrency = delivery.get("concurrency", 1)
    attempts = retry["max_attempts"]
    retry_wait_ms = sum(
        min(retry["backoff_ms"] * (2**index), 60_000)
        for index in range(attempts - 1)
    )
    event_work: list[int] = []
    latest_schedule_ms = 0

    for event in events:
        effective = {
            "duplicate_count": 0,
            "delay_ms": 0,
            "timeout_ms": 5_000,
            **delivery,
            **event["delivery"],
        }
        latest_schedule_ms = max(latest_schedule_ms, event["schedule_after_ms"])
        one_delivery_ms = (
            attempts * (effective["timeout_ms"] + effective["delay_ms"])
            + retry_wait_ms
        )
        event_work.append((effective["duplicate_count"] + 1) * one_delivery_ms)

    total_work_ms = sum(event_work)
    event_phase_ms = (total_work_ms + concurrency - 1) // concurrency
    if concurrency > 1:
        event_phase_ms += max(event_work, default=0)

    verification_ms = verify["timeout_ms"] if verify is not None else 0
    return latest_schedule_ms + event_phase_ms + verification_ms


def _is_safe_header_value(value: str, *, allow_horizontal_tab: bool = True) -> bool:
    """Accept printable ASCII HTTP field values and, optionally, horizontal tabs."""
    for character in value:
        codepoint = ord(character)
        if allow_horizontal_tab and codepoint == 0x09:
            continue
        if not 0x20 <= codepoint <= 0x7E:
            return False
    return True


def _validate_fault_injection(value: Any, where: str) -> dict[str, list[int]]:
    """Validate deterministic per-attempt client-side transport fault plans."""
    if not isinstance(value, dict):
        raise ScenarioError(f"{where} must be a mapping")
    allowed = {"connect_error_attempts", "response_loss_attempts"}
    _unknown_keys(value, allowed, where)

    normalized: dict[str, list[int]] = {}
    for field in sorted(allowed):
        attempts = value.get(field, [])
        if not isinstance(attempts, list):
            raise ScenarioError(f"{where}.{field} must be a list of attempt numbers")
        parsed = [
            _integer(attempt, f"{where}.{field} entry", minimum=1, maximum=10)
            for attempt in attempts
        ]
        if len(parsed) != len(set(parsed)):
            raise ScenarioError(f"{where}.{field} must not contain duplicate attempt numbers")
        normalized[field] = sorted(parsed)

    overlap = set(normalized["connect_error_attempts"]) & set(
        normalized["response_loss_attempts"]
    )
    if overlap:
        numbers = ", ".join(str(number) for number in sorted(overlap))
        raise ScenarioError(
            f"{where} cannot inject more than one fault on the same attempt ({numbers})"
        )
    return normalized


def _validate_delivery(value: Any, where: str, *, allow_order: bool = True) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioError(f"{where} must be a mapping")
    allowed = _ALLOWED_DELIVERY if allow_order else _ALLOWED_DELIVERY - {"order", "concurrency"}
    _unknown_keys(value, allowed, where)
    result = dict(value)
    if "order" in result and (not isinstance(result["order"], str) or result["order"] not in {"in_order", "reverse"}):
        raise ScenarioError(f"{where}.order must be 'in_order' or 'reverse'")
    if "duplicate_count" in result:
        result["duplicate_count"] = _integer(result["duplicate_count"], f"{where}.duplicate_count", minimum=0, maximum=100)
    if "delay_ms" in result:
        result["delay_ms"] = _integer(result["delay_ms"], f"{where}.delay_ms", minimum=0, maximum=60_000)
    if "concurrency" in result:
        result["concurrency"] = _integer(result["concurrency"], f"{where}.concurrency", minimum=1, maximum=50)
    if "signature_provider" in result and (
        not isinstance(result["signature_provider"], str)
        or result["signature_provider"] not in {"generic", "stripe", "github", "shopify", "standard_webhooks"}
    ):
        raise ScenarioError(
            f"{where}.signature_provider must be generic, stripe, github, shopify, or standard_webhooks"
        )
    if "signature_timestamp_offset_seconds" in result:
        result["signature_timestamp_offset_seconds"] = _integer(
            result["signature_timestamp_offset_seconds"],
            f"{where}.signature_timestamp_offset_seconds",
            minimum=-86_400,
            maximum=86_400,
        )
    if "timeout_ms" in result:
        result["timeout_ms"] = _integer(result["timeout_ms"], f"{where}.timeout_ms", minimum=1, maximum=300_000)
    if "signature" in result and (
        not isinstance(result["signature"], str)
        or result["signature"] not in {"none", "valid", "invalid", "missing"}
    ):
        raise ScenarioError(f"{where}.signature must be none, valid, invalid, or missing")
    if "fault_injection" in result:
        result["fault_injection"] = _validate_fault_injection(
            result["fault_injection"], f"{where}.fault_injection"
        )
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



def _validate_verify_auth(value: Any) -> dict[str, str] | None:
    """Validate environment-backed authentication for a verification probe."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ScenarioError("verify.auth must be a mapping")

    auth_type = value.get("type")
    if auth_type == "bearer":
        _unknown_keys(value, {"type", "token_env"}, "verify.auth")
        env_key = "token_env"
    elif auth_type == "api_key":
        _unknown_keys(value, {"type", "key_env", "header"}, "verify.auth")
        env_key = "key_env"
    else:
        raise ScenarioError("verify.auth.type must be 'bearer' or 'api_key'")

    env_name = value.get(env_key)
    if (
        not isinstance(env_name, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_name)
    ):
        raise ScenarioError(f"verify.auth.{env_key} must be a valid environment variable name")

    if auth_type == "bearer":
        return {"type": "bearer", "token_env": env_name}

    header = value.get("header", "X-API-Key")
    if not isinstance(header, str) or not re.fullmatch(
        r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header
    ):
        raise ScenarioError("verify.auth.header must be a valid HTTP header name")
    reserved_headers = {
        "accept-encoding",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "user-agent",
        "x-event-id",
        "x-event-type",
        "x-hookstorm-attempt",
        "x-hookstorm-duplicate-index",
        "x-hookstorm-run",
    }
    if header.lower() in reserved_headers:
        raise ScenarioError(
            f"verify.auth.header may not override transport-managed header '{header}'"
        )
    return {"type": "api_key", "key_env": env_name, "header": header}


def _validate_verify(value: Any) -> dict[str, Any] | None:
    """Validate an optional read-only business-state probe."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ScenarioError("verify must be a mapping")
    _unknown_keys(
        value, {"url", "expect_status", "expect_json", "timeout_ms", "auth"}, "verify"
    )

    url = value.get("url")
    if (
        not isinstance(url, str)
        or not url.strip()
        or len(url) > 2_048
        or any(char in url for char in "\r\n")
    ):
        raise ScenarioError("verify.url must be a non-empty absolute HTTP(S) URL of at most 2048 characters")
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ScenarioError("verify.url must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ScenarioError("verify.url must not contain embedded credentials or a fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ScenarioError("verify.url contains an invalid port") from exc

    expect_status = value.get("expect_status", [200])
    if isinstance(expect_status, int) and not isinstance(expect_status, bool):
        expect_status = [expect_status]
    if not isinstance(expect_status, list) or not expect_status:
        raise ScenarioError("verify.expect_status must be an integer or non-empty list")
    if any(
        isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599
        for code in expect_status
    ):
        raise ScenarioError("verify.expect_status entries must be HTTP status codes")

    expected_json = value.get("expect_json")
    if not isinstance(expected_json, dict):
        raise ScenarioError("verify.expect_json must be a JSON object")
    try:
        encoded_expected = json.dumps(
            expected_json, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        ensure_json_nesting_depth(encoded_expected, label="verify.expect_json")
        expected_json = json.loads(encoded_expected)
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ScenarioError("verify.expect_json must contain JSON-compatible values") from exc

    timeout_ms = _integer(
        value.get("timeout_ms", 5_000), "verify.timeout_ms", minimum=1, maximum=300_000
    )
    return {
        "url": url.strip(),
        "expect_status": sorted(set(expect_status)),
        "expect_json": expected_json,
        "timeout_ms": timeout_ms,
        "auth": _validate_verify_auth(value.get("auth")),
    }


def validate_scenario(data: Any) -> dict[str, Any]:
    """Validate and normalize a parsed scenario mapping."""
    if not isinstance(data, dict):
        raise ScenarioError("Scenario root must be a YAML mapping")
    _unknown_keys(data, _ALLOWED_TOP_LEVEL, "scenario")
    run_timeout_ms = _integer(
        data.get("run_timeout_ms", _DEFAULT_RUN_TIMEOUT_MS),
        "run_timeout_ms",
        minimum=100,
        maximum=_MAX_RUN_TIMEOUT_MS,
    )
    if isinstance(data.get("version"), bool) or not isinstance(data.get("version"), int) or data.get("version") != 1:
        raise ScenarioError("Scenario version must be 1")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScenarioError("name must be a non-empty string")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ScenarioError("description must be a string")

    delivery = _validate_delivery(data.get("delivery", {}), "delivery")
    retry = _validate_retry(data.get("retry"))
    verify = _validate_verify(data.get("verify"))
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise ScenarioError("events must be a non-empty list")
    if len(events) > 500:
        raise ScenarioError("A scenario may contain at most 500 events")

    normalized_events: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for index, event in enumerate(events):
        where = f"events[{index}]"
        if not isinstance(event, dict):
            raise ScenarioError(f"{where} must be a mapping")
        _unknown_keys(event, _ALLOWED_EVENT, where)
        event_id = event.get("event_id")
        event_type = event.get("id")
        if (
            not isinstance(event_id, str)
            or not event_id.strip()
            or len(event_id) > 256
            or not _is_safe_header_value(event_id, allow_horizontal_tab=False)
        ):
            raise ScenarioError(f"{where}.event_id must be a non-empty ASCII header-safe string of at most 256 characters")
        if (
            not isinstance(event_type, str)
            or not event_type.strip()
            or len(event_type) > 256
            or not _is_safe_header_value(event_type, allow_horizontal_tab=False)
        ):
            raise ScenarioError(f"{where}.id must be a non-empty ASCII header-safe string of at most 256 characters")
        if event_id in seen_event_ids:
            raise ScenarioError(f"{where}.event_id must be unique within a scenario")
        seen_event_ids.add(event_id)
        payload = event.get("payload", {})
        try:
            encoded_payload_text = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            ensure_json_nesting_depth(encoded_payload_text, label=f"{where}.payload")
            encoded_payload = encoded_payload_text.encode("utf-8")
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ScenarioError(
                f"{where}.payload must be valid JSON-compatible data with no more than "
                f"128 nesting levels"
            ) from exc
        if len(encoded_payload) > 1_048_576:
            raise ScenarioError(f"{where}.payload exceeds the 1 MiB limit")
        headers = event.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ScenarioError(f"{where}.headers must be a mapping of string keys to string values")
        seen_header_names: set[str] = set()
        for key, value in headers.items():
            normalized_header = key.lower()
            if normalized_header in seen_header_names:
                raise ScenarioError(
                    f"{where}.headers contains duplicate header names that differ only by case: {key}"
                )
            seen_header_names.add(normalized_header)
            if (
                not re.fullmatch(r"[!#$%&'*+._^|~0-9A-Za-z-]+", key)
                or not _is_safe_header_value(value)
            ):
                raise ScenarioError(
                    f"{where}.headers contains an invalid HTTP header name or non-ASCII/control value"
                )
            if normalized_header in {
                "host", "content-length", "transfer-encoding", "accept-encoding",
                "x-hookstorm-run", "x-event-id", "x-event-type",
                "x-hookstorm-attempt", "x-hookstorm-duplicate-index",
                "x-hookstorm-signature", "stripe-signature",
                "x-hub-signature-256", "x-shopify-hmac-sha256",
                "webhook-id", "webhook-timestamp", "webhook-signature",
            }:
                raise ScenarioError(f"{where}.headers may not override {key}")
        header_bytes = sum(len(key) + len(value) for key, value in headers.items())
        if len(headers) > 64 or header_bytes > 16_384:
            raise ScenarioError(f"{where}.headers exceed the 64-header / 16 KiB limit")
        event_delivery = _validate_delivery(event.get("delivery", {}), f"{where}.delivery", allow_order=False)
        expect_status = event.get("expect_status", [200, 201, 202, 204])
        if isinstance(expect_status, int) and not isinstance(expect_status, bool):
            expect_status = [expect_status]
        if not isinstance(expect_status, list) or not expect_status:
            raise ScenarioError(f"{where}.expect_status must be an integer or non-empty list")
        if any(isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599 for code in expect_status):
            raise ScenarioError(f"{where}.expect_status entries must be HTTP status codes")
        expectations: dict[str, Any] = {}
        for expectation_key in ("expect_json", "expect_duplicate_json"):
            expected = event.get(expectation_key)
            if expected is None:
                expectations[expectation_key] = None
                continue
            if not isinstance(expected, dict):
                raise ScenarioError(f"{where}.{expectation_key} must be a JSON object")
            try:
                encoded_expected = json.dumps(
                    expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                ensure_json_nesting_depth(
                    encoded_expected, label=f"{where}.{expectation_key}"
                )
                expectations[expectation_key] = json.loads(encoded_expected)
            except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
                raise ScenarioError(
                    f"{where}.{expectation_key} must contain JSON-compatible values "
                    "with no more than 128 nesting levels"
                ) from exc
        schedule_after_ms = _integer(
            event.get("schedule_after_ms", 0),
            f"{where}.schedule_after_ms",
            minimum=0,
            maximum=300_000,
        )
        normalized_events.append({
            "id": event_type,
            "event_id": event_id,
            "payload": payload,
            "headers": headers,
            "delivery": event_delivery,
            "schedule_after_ms": schedule_after_ms,
            "expect_status": sorted(set(expect_status)),
            "expect_json": expectations["expect_json"],
            "expect_duplicate_json": expectations["expect_duplicate_json"],
        })

    for event in normalized_events:
        effective_delivery = {**delivery, **event["delivery"]}
        fault_plan = effective_delivery.get("fault_injection", {})
        for field in ("connect_error_attempts", "response_loss_attempts"):
            invalid_attempts = [
                number
                for number in fault_plan.get(field, [])
                if number > retry["max_attempts"]
            ]
            if invalid_attempts:
                raise ScenarioError(
                    f"events[].delivery.fault_injection.{field} entries must not exceed "
                    f"retry.max_attempts={retry['max_attempts']}"
                )
        signature_provider = effective_delivery.get("signature_provider", "generic")
        timestamp_offset = effective_delivery.get("signature_timestamp_offset_seconds", 0)
        if timestamp_offset and signature_provider not in {"stripe", "standard_webhooks"}:
            raise ScenarioError(
                "signature_timestamp_offset_seconds can only be used with signature_provider: "
                "stripe or standard_webhooks"
            )
        if signature_provider == "standard_webhooks" and "." in event["event_id"]:
            raise ScenarioError(
                "events[].event_id must not contain '.' when signature_provider is "
                "standard_webhooks"
            )

    estimated_requests = 0
    for event in normalized_events:
        effective_delivery = {**delivery, **event["delivery"]}
        estimated_requests += (effective_delivery.get("duplicate_count", 0) + 1) * retry["max_attempts"]
    if estimated_requests > 20_000:
        raise ScenarioError(
            "Scenario could require more than 20,000 HTTP attempts; reduce events, duplicates, or retries"
        )

    estimated_duration_ms = _estimate_worst_case_duration_ms(
        delivery, retry, normalized_events, verify
    )
    if estimated_duration_ms > run_timeout_ms:
        raise ScenarioError(
            "Scenario worst-case request-time budget is estimated at "
            f"{estimated_duration_ms} ms, exceeding run_timeout_ms={run_timeout_ms}; "
            "increase run_timeout_ms or reduce timeouts, retries, delays, duplicates, or events"
        )

    return {
        "version": 1,
        "run_timeout_ms": run_timeout_ms,
        "name": name.strip(),
        "description": description,
        "delivery": delivery,
        "retry": retry,
        "events": normalized_events,
        "verify": verify,
    }


def scenario_fingerprint(scenario: dict[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint for the behavior-defining scenario config.

    Human-facing name and description are deliberately excluded. The digest includes
    delivery behavior, retry policy, events, and verification expectations. A non-default
    run timeout is included; the default retains the pre-timeout fingerprint for baseline
    compatibility. The digest does not expose the underlying values in history or baselines.
    """
    behavior = {
        key: scenario.get(key)
        for key in ("version", "delivery", "retry", "events", "verify")
    }
    # Preserve fingerprints created before run_timeout_ms was introduced when the
    # effective budget is unchanged; explicit non-default budgets remain behavior-defining.
    run_timeout_ms = scenario.get("run_timeout_ms", _DEFAULT_RUN_TIMEOUT_MS)
    if run_timeout_ms != _DEFAULT_RUN_TIMEOUT_MS:
        behavior["run_timeout_ms"] = run_timeout_ms
    try:
        canonical = json.dumps(
            behavior,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ScenarioError("Scenario configuration cannot be fingerprinted as JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def load_scenario(path: str | Path) -> dict[str, Any]:
    """Load a scenario from a YAML file using the safe loader."""
    scenario_path = Path(path)
    try:
        raw = yaml.load(
            scenario_path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except FileNotFoundError as exc:
        raise ScenarioError(f"Scenario file not found: {scenario_path}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"Invalid YAML in {scenario_path}: {exc}") from exc
    except RecursionError as exc:
        raise ScenarioError(
            f"Scenario in {scenario_path} is nested too deeply to parse safely"
        ) from exc
    except OSError as exc:
        raise ScenarioError(f"Cannot read scenario file {scenario_path}: {exc}") from exc
    return validate_scenario(raw)
