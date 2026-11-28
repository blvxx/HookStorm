"""Local MCP tools for inspecting and safely replaying HookStorm deliveries."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hookstorm import __version__
from hookstorm.config import load_scenario, scenario_fingerprint
from hookstorm.history import HistoryError, get_run, list_runs, save_run
from hookstorm.runner import run_scenario
from hookstorm.safety import validate_target
from hookstorm.providers import (
    GitHubWebhookAdapter,
    canonical_json_fingerprint,
)

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

    if latest.get("injected_fault") == "connect_error":
        category = "injected_fault"
        explanation = (
            "HookStorm intentionally failed before sending this attempt. This is a configured "
            "client-side test fault, not evidence that the receiver rejected the request."
        )
    elif latest.get("injected_fault") == "response_loss":
        category = "injected_fault"
        explanation = (
            "HookStorm intentionally discarded the receiver acknowledgement after the "
            "request was processed. This is a configured client-side simulation."
        )
    elif status in {401, 403}:
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
    elif status is None and error:
        category = "transport"
        explanation = (
            "The request did not complete at the HTTP transport layer. Check the "
            "target URL, DNS/TCP/TLS reachability, firewall, and staging allowlist."
        )
    elif status in {408, 429}:
        category = "transient_http_status"
        explanation = (
            "The receiver or an upstream service returned a retryable HTTP status. "
            "Check rate limits, queue saturation, and whether the retry policy is appropriate."
        )
    elif status in {400, 422}:
        category = "receiver_validation"
        explanation = (
            "The receiver rejected the request as invalid. Compare its expected payload "
            "schema and required headers with the selected YAML scenario."
        )
    elif status == 404:
        category = "endpoint_route"
        explanation = (
            "The target returned 404. Check the path, HTTP method, router, and "
            "whether the selected environment exposes this webhook endpoint."
        )
    elif status == 409:
        category = "receiver_conflict"
        explanation = (
            "The receiver returned 409 Conflict. This may be an idempotency or state conflict, "
            "but the status alone does not prove whether a business effect was committed."
        )
    elif status == 413:
        category = "payload_too_large"
        explanation = (
            "The receiver or an upstream proxy rejected the request size. Check request-body "
            "limits and reduce the synthetic payload if the receiver contract permits it."
        )
    elif status == 415:
        category = "unsupported_media_type"
        explanation = (
            "The receiver rejected the Content-Type. Confirm the endpoint expects JSON and "
            "whether it requires a specific content type or vendor media type."
        )
    elif isinstance(status, int) and 300 <= status < 400:
        category = "redirect_not_followed"
        explanation = (
            "The endpoint returned a redirect. HookStorm intentionally does not follow redirects; "
            "configure the final webhook URL directly and verify the destination."
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


def _scenario_event_context(
    event: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Return useful event configuration without revealing payload or header values."""
    delivery = {
        **scenario["delivery"],
        **event["delivery"],
    }
    payload_bytes = len(
        json.dumps(
            event["payload"],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    signature = delivery.get("signature", "none")
    provider = delivery.get("signature_provider", "generic")
    generated_signature_header = {
        "generic": "X-HookStorm-Signature",
        "stripe": "Stripe-Signature",
        "github": "X-Hub-Signature-256",
        "shopify": "X-Shopify-Hmac-Sha256",
        "standard_webhooks": "webhook-signature",
    }.get(provider)
    return {
        "event_type": event["id"],
        "event_id": event["event_id"],
        "payload_size_bytes": payload_bytes,
        "custom_header_names": sorted(event["headers"]),
        "signature": {
            "mode": signature,
            "provider": provider,
            "generated_header": generated_signature_header if signature in {"valid", "invalid"} else None,
            "requires_secret": signature in {"valid", "invalid"},
        },
        "delivery": {
            "max_attempts": scenario["retry"]["max_attempts"],
            "retry_on": scenario["retry"]["retry_on"],
            "backoff_ms": scenario["retry"]["backoff_ms"],
            "duplicate_count": delivery.get("duplicate_count", 0),
            "timeout_ms": delivery.get("timeout_ms", scenario["delivery"].get("timeout_ms", 5_000)),
            "delay_ms": delivery.get("delay_ms", scenario["delivery"].get("delay_ms", 0)),
            "concurrency": scenario["delivery"].get("concurrency", 1),
            "schedule_after_ms": event.get("schedule_after_ms", 0),
        },
        "expect_status": event["expect_status"],
        "expect_json_top_level_keys": sorted(event["expect_json"]) if event.get("expect_json") else [],
        "expect_duplicate_json_top_level_keys": (
            sorted(event["expect_duplicate_json"])
            if event.get("expect_duplicate_json")
            else []
        ),
        "fault_injection": delivery.get(
            "fault_injection",
            {"connect_error_attempts": [], "response_loss_attempts": []},
        ),
    }


def _failure_recommendations(diagnosis: dict[str, Any]) -> list[str]:
    """Suggest bounded next checks; never claim a cause that history cannot prove."""
    category = diagnosis["category"]
    recommendations = {
        "authentication_or_signature": [
            "Compare the scenario's signature provider with the receiver's expected signing format.",
            "Confirm the secret is configured in the receiver and HookStorm environment; do not share secret values.",
            "Check timestamp tolerance and raw-body handling where the provider uses timestamped signatures.",
        ],
        "receiver_validation": [
            "Compare the event payload schema and required fields with the receiver contract.",
            "Check validation logs for the event ID and attempt timestamp.",
        ],
        "endpoint_route": [
            "Check the HTTP method and path mounted by the receiver.",
            "Confirm the request reached the intended application and environment.",
        ],
        "receiver_server_error": [
            "Inspect application and upstream dependency logs for the event ID and attempt time.",
            "Check database, queue, and downstream service health before replaying.",
        ],
        "timeout": [
            "Compare receiver processing time with the configured request timeout.",
            "Inspect downstream calls and queue wait time for the event ID.",
        ],
        "transport": [
            "Check DNS, TCP/TLS connectivity, firewall rules, and the configured target path.",
            "Correlate the attempt time with reverse-proxy and receiver access logs.",
        ],
        "transient_http_status": [
            "Check rate limits, queue saturation, and upstream retry guidance.",
            "Confirm that retry_on and exponential backoff match the receiver's throttling policy.",
        ],
        "response_assertion": [
            "Compare the receiver's documented JSON response with the scenario's expected key paths.",
            "Treat the mismatch path as evidence; HookStorm history does not retain the actual response body.",
        ],
        "status_assertion": [
            "Compare the final HTTP status with the scenario's expect_status list.",
            "Check whether the receiver intentionally returns a different status for duplicate deliveries.",
        ],
        "injected_fault": [
            "This attempt was deliberately altered by HookStorm; do not attribute it to a receiver outage.",
            "Use the next real attempt and an independent state probe to evaluate idempotency.",
        ],
        "receiver_conflict": [
            "Check whether the receiver treats this event ID as already processed or rejects a state transition.",
            "Verify the business effect independently before retrying or replaying the event.",
        ],
        "payload_too_large": [
            "Check receiver, reverse-proxy, and API gateway body-size limits.",
            "Compare the scenario payload size with the integration's documented maximum.",
        ],
        "unsupported_media_type": [
            "Check the endpoint's accepted Content-Type values.",
            "Inspect whether a vendor-specific JSON media type is required.",
        ],
        "redirect_not_followed": [
            "Update the scenario target to the final webhook endpoint rather than relying on redirects.",
            "Check the intended scheme, host, and path before sending another test request.",
        ],
    }.get(category, [
        "Inspect the receiver logs using the event ID and recorded attempt time.",
        "Compare the original scenario with the receiver's current webhook contract.",
    ])
    if category in {"transport", "timeout"} or any(
        attempt.get("injected_fault") == "response_loss"
        for attempt in diagnosis.get("attempts", [])
    ):
        recommendations.append(
            "The business operation may have committed even though HookStorm did not observe an acknowledgement; "
            "verify receiver state before replaying to avoid repeating a side effect."
        )
    return recommendations


def _prepare_replay_batch(
    *,
    run: dict[str, Any],
    scenario: dict[str, Any],
    event_ids: list[str],
    target: str,
    allow_scenario_change: bool,
    allow_target_change: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate a bounded batch of failed event IDs against one stored run."""
    if not isinstance(event_ids, list) or not event_ids:
        raise ValueError("event_ids must be a non-empty list")
    if len(event_ids) > 50:
        raise ValueError("A replay batch may contain at most 50 event IDs")
    if any(not isinstance(item, str) or not item.strip() for item in event_ids):
        raise ValueError("Every event_id must be a non-empty string")
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("event_ids must not contain duplicates")

    failed_by_id: dict[str, list[dict[str, Any]]] = {}
    for delivery in run.get("deliveries", []):
        if not delivery.get("passed"):
            failed_by_id.setdefault(delivery.get("event_id", ""), []).append(delivery)

    scenario_events: dict[str, dict[str, Any]] = {}
    for event in scenario["events"]:
        event_id = event["event_id"]
        if event_id in scenario_events:
            raise ValueError("The scenario contains duplicate event IDs")
        scenario_events[event_id] = event

    for event_id in event_ids:
        if not failed_by_id.get(event_id):
            raise ValueError(
                f"Run '{run['run_id']}' has no failed delivery for event_id '{event_id}'"
            )
        event = scenario_events.get(event_id)
        if event is None:
            raise ValueError(
                f"Scenario does not contain an event with event_id '{event_id}'"
            )
        if any(item.get("event_type") != event["id"] for item in failed_by_id[event_id]):
            raise ValueError(
                f"Event type for '{event_id}' differs from the failed run; replay is blocked"
            )

    selected_ids = set(event_ids)
    selected = [
        event
        for event in scenario["events"]
        if event["event_id"] in selected_ids
    ]
    scenario_changed = (
        run.get("scenario") != scenario["name"]
        or not isinstance(run.get("scenario_fingerprint"), str)
        or run["scenario_fingerprint"] != scenario_fingerprint(scenario)
    )
    target_changed = not _target_matches_history(target, run.get("target", ""))
    warnings = []
    if scenario_changed:
        warnings.append(
            "The current YAML scenario differs from the configuration recorded for the failed run."
        )
    if target_changed:
        warnings.append(
            "The supplied target differs from the exact target recorded in the failed run."
        )
    if scenario_changed and not allow_scenario_change:
        raise ValueError(
            "Scenario mismatch: review the current YAML, then set "
            "allow_scenario_change=true only when the changed scenario is intentional."
        )
    if target_changed and not allow_target_change:
        raise ValueError(
            "Target mismatch: use the exact historical target or explicitly set "
            "allow_target_change=true after checking the destination."
        )
    return selected, {
        "scenario_changed": scenario_changed,
        "target_changed": target_changed,
        "failed_deliveries_found": sum(len(failed_by_id[item]) for item in event_ids),
        "selected_event_count": len(selected),
        "warnings": warnings,
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
    return target == historical_target


def _normalized_target(value: str | None) -> str | None:
    """Normalize a target for comparison without retaining query strings."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _correlate_github_delivery(
    external: dict[str, Any],
    run: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Join provider evidence to one local run only when stable evidence agrees."""
    request = external.get("request", {})
    request_headers = request.get("headers", {})
    provider_guid = external.get("guid")
    header_guid = request_headers.get("x-github-delivery")
    provider_identifiers_match = (
        provider_guid is None
        or header_guid is None
        or provider_guid == header_guid
    )
    payload_fingerprint = request.get("payload_sha256")
    provider_event = request_headers.get("x-github-event")
    provider_action = external.get("action")
    provider_event_type = (
        f"{provider_event}.{provider_action}"
        if provider_event and provider_action
        else provider_event
    )

    exact_id_events = [
        event
        for event in scenario.get("events", [])
        if provider_identifiers_match
        and provider_guid is not None
        and event.get("event_id") == provider_guid
    ]
    payload_events = []
    if isinstance(payload_fingerprint, str):
        for event in scenario.get("events", []):
            try:
                event_fingerprint, _ = canonical_json_fingerprint(event.get("payload", {}))
            except ValueError:
                continue
            type_matches = (
                provider_event_type is None
                or event.get("id") == provider_event_type
                or event.get("id") == provider_event
            )
            if event_fingerprint == payload_fingerprint and type_matches:
                payload_events.append(event)

    candidates = exact_id_events if exact_id_events else payload_events
    event = candidates[0] if len(candidates) == 1 else None
    scenario_matches = (
        run.get("scenario") == scenario.get("name")
        and isinstance(run.get("scenario_fingerprint"), str)
        and run.get("scenario_fingerprint") == scenario_fingerprint(scenario)
    )
    event_id_matches_provider = bool(
        event is not None
        and provider_identifiers_match
        and provider_guid is not None
        and event.get("event_id") == provider_guid
    )
    payload_matches = None
    event_type_matches = None
    local_delivery = None
    if event is not None:
        try:
            event_fingerprint, _ = canonical_json_fingerprint(event.get("payload", {}))
            payload_matches = (
                isinstance(payload_fingerprint, str)
                and event_fingerprint == payload_fingerprint
            )
        except ValueError:
            payload_matches = False
        event_type_matches = (
            provider_event_type is None
            or event.get("id") == provider_event_type
            or event.get("id") == provider_event
        )
        local_matches = [
            item
            for item in run.get("deliveries", [])
            if item.get("event_id") == event.get("event_id")
            and item.get("event_type") == event.get("id")
        ]
        if len(local_matches) == 1:
            local_delivery = local_matches[0]

    provider_target = _normalized_target(request.get("target"))
    local_target = _normalized_target(run.get("target"))
    target_matches = (
        provider_target == local_target
        if provider_target is not None and local_target is not None
        else None
    )
    local_attempts = local_delivery.get("attempts", []) if local_delivery else []
    latest_attempt = local_attempts[-1] if local_attempts else {}
    local_status = latest_attempt.get("status_code")
    provider_status = external.get("status_code")
    status_codes_match = (
        local_status == provider_status
        if isinstance(local_status, int) and isinstance(provider_status, int)
        else None
    )

    confirmed_correlation = bool(
        event is not None
        and local_delivery is not None
        and scenario_matches
        and payload_matches is True
        and event_type_matches is True
        and target_matches is True
        and provider_identifiers_match
    )
    if confirmed_correlation:
        correlation_status = "confirmed"
    elif event is not None or local_delivery is not None or payload_events:
        correlation_status = "partial"
    else:
        correlation_status = "unmatched"

    confirmed_facts = []
    if isinstance(provider_status, int):
        confirmed_facts.append(
            {
                "source": "github_webhook_delivery_api",
                "fact": "GitHub recorded an HTTP response status for this delivery",
                "status_code": provider_status,
                "delivered_at": external.get("delivered_at"),
            }
        )
    if isinstance(local_status, int):
        confirmed_facts.append(
            {
                "source": "hookstorm_local_history",
                "fact": "HookStorm recorded this HTTP status for the matched local event",
                "status_code": local_status,
                "run_id": run.get("run_id"),
                "event_id": event.get("event_id") if event else None,
            }
        )

    injected_fault = latest_attempt.get("injected_fault")
    if injected_fault:
        root_cause_confirmed = True
        root_cause = (
            "HookStorm recorded an explicitly configured client-side fault: "
            f"{injected_fault}."
        )
        root_cause_source = "hookstorm_local_history"
    else:
        root_cause_confirmed = False
        root_cause = None
        root_cause_source = None

    replay_target = run.get("target", "")
    try:
        _validate_replay_target(replay_target)
        loopback_target = True
    except (TypeError, ValueError):
        loopback_target = False

    replay_candidate = bool(
        confirmed_correlation
        and event_id_matches_provider
        and local_delivery is not None
        and not local_delivery.get("passed")
        and loopback_target
    )
    return {
        "status": correlation_status,
        "match_basis": (
            "provider_delivery_guid"
            if event_id_matches_provider
            else "canonical_payload_sha256_and_event_type"
            if event is not None and payload_matches is True
            else None
        ),
        "scenario_matches_recorded_fingerprint": scenario_matches,
        "provider_event_guid_matches_local_event_id": event_id_matches_provider,
        "provider_guid_matches_request_header": provider_identifiers_match,
        "payload_fingerprint_matches": payload_matches,
        "event_type_matches": event_type_matches,
        "provider_event_type": provider_event_type,
        "local_event": (
            {
                "event_id": event.get("event_id"),
                "event_type": event.get("id"),
                "passed": bool(local_delivery.get("passed")),
                "attempt_count": len(local_attempts),
                "last_status_code": local_status,
                "diagnosis": (
                    _diagnose_delivery(local_delivery)
                    if not local_delivery.get("passed")
                    else None
                ),
            }
            if event is not None and local_delivery is not None
            else None
        ),
        "target_matches": target_matches,
        "status_codes_match": status_codes_match,
        "confirmed_facts": confirmed_facts,
        "root_cause": {
            "confirmed": root_cause_confirmed,
            "value": root_cause,
            "source": root_cause_source,
            "boundary": (
                "Provider and HTTP status records establish what response was observed, "
                "not the internal application failure that produced it. A status code alone "
                "does not prove a signature, database, queue, or business-logic defect."
            ),
        },
        "replay_readiness": {
            "eligible_for_existing_loopback_replay": replay_candidate,
            "target_is_loopback": loopback_target,
            "requires_server_opt_in": "HOOKSTORM_MCP_ENABLE_REPLAY=1",
            "requires_per_call_confirmation": True,
            "preserves_event_id_and_run_correlation": True,
            "idempotency_status": "not_proven_by_correlation",
            "note": (
                "Correlation does not prove receiver-side idempotency. Before replay, verify "
                "the receiver's persisted deduplication state or a suitable read-only business-state "
                "probe. Provider redelivery is not triggered by this read-only adapter."
            ),
        },
        "limitations": [
            "A confirmed match requires the original scenario fingerprint and canonical payload fingerprint to match.",
            "Payload fingerprints are used for comparison; payload values are never returned.",
            "Provider delivery metadata does not expose the receiver's internal logs or prove its root cause.",
        ],
    }


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
    github_http_client: Any | None = None,
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
        version=__version__,
        instructions=(
            "Inspect local HookStorm run history and YAML scenarios, analyze failed runs with privacy-preserving scenario context, "
            "History contains sanitized metadata, not request payloads or response bodies. "
            "Webhook replay is disabled unless explicitly enabled in the server environment, "
            "requires failed events, an explicit target, and per-call confirmation. "
            "Replay targets are loopback-only. If HOOKSTORM_GITHUB_TOKEN is configured, "
            "read-only tools can inspect repository webhook delivery records from GitHub. "
            "Provider HTTP statuses are evidence of observed responses, not automatic proof "
            "of an internal root cause."
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
    def hookstorm_analyze_run(
        run_id: str,
        scenario_path: str | None = None,
    ) -> dict[str, Any]:
        """Combine sanitized run outcomes with scenario settings and actionable failure checks."""
        run = get_run(run_id, history_db)
        scenario = None
        scenario_match = None
        scenario_file = None
        if scenario_path is not None:
            path, scenario = _load_scenario(scenario_path, root)
            scenario_file = path.name
            scenario_match = (
                run.get("scenario") == scenario["name"]
                and run.get("scenario_fingerprint") == scenario_fingerprint(scenario)
            )

        event_index = (
            {event["event_id"]: event for event in scenario["events"]}
            if scenario is not None
            else {}
        )
        failures = []
        for delivery in run.get("deliveries", []):
            if delivery.get("passed"):
                continue
            diagnosis = _diagnose_delivery(delivery)
            event = event_index.get(delivery.get("event_id"))
            attempts = delivery.get("attempts", [])
            diagnosis["attempt_count"] = len(attempts)
            if event is not None and scenario is not None:
                context = _scenario_event_context(event, scenario)
                diagnosis["scenario_context"] = context
                if scenario_match is False:
                    diagnosis["remaining_configured_attempts"] = None
                    diagnosis["attempts_exhausted"] = None
                    diagnosis["scenario_warning"] = (
                        "The supplied scenario fingerprint differs from the stored run; "
                        "scenario settings below are current-file context, not proof of the original request."
                    )
                else:
                    last_attempt = attempts[-1] if attempts else {}
                    last_status = last_attempt.get("status_code")
                    retryable = (
                        last_status is None
                        or last_status in context["delivery"]["retry_on"]
                    )
                    remaining = (
                        max(0, context["delivery"]["max_attempts"] - len(attempts))
                        if retryable
                        else 0
                    )
                    diagnosis["remaining_configured_attempts"] = remaining
                    diagnosis["attempts_exhausted"] = remaining == 0
            elif scenario is not None:
                diagnosis["scenario_context"] = None
                diagnosis["scenario_warning"] = (
                    "No event with this ID was found in the supplied scenario."
                )
            diagnosis["recommendations"] = _failure_recommendations(diagnosis)
            failures.append(diagnosis)

        verification = run.get("verification")
        verification_analysis = None
        if verification is not None:
            verification_analysis = {
                "passed": verification.get("passed"),
                "status_code": verification.get("status_code"),
                "latency_ms": verification.get("latency_ms"),
                "error": verification.get("error"),
                "recommendation": (
                    "Check the test-only state endpoint and its run-scoped query. "
                    "A failed state probe does not by itself identify which delivery caused the mismatch."
                    if not verification.get("passed")
                    else "The configured post-delivery state assertion passed."
                ),
            }

        return {
            "run_id": run["run_id"],
            "scenario": run["scenario"],
            "scenario_file": scenario_file,
            "scenario_matches_recorded_fingerprint": scenario_match,
            "target": run["target"],
            "success": run["summary"]["success"],
            "summary": run["summary"],
            "failed_deliveries": failures,
            "verification": verification_analysis,
            "privacy": (
                "Payload values, header values, credentials, and response bodies are not returned. "
                "Scenario context contains only payload byte counts, header names, and assertion key names."
            ),
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
        result = await run_scenario(selected_scenario, safe_target, correlation_run_id=run_id)
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
            "correlation_run_id": result["correlation_run_id"],
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

    @server.tool()
    async def hookstorm_replay_failed_deliveries(
        run_id: str,
        scenario_path: str,
        event_ids: list[str],
        target: str,
        confirm: bool = False,
        allow_scenario_change: bool = False,
        allow_target_change: bool = False,
    ) -> dict[str, Any]:
        """Preview or replay up to 50 failed events from one run after explicit confirmation."""
        run = get_run(run_id, history_db)
        path, scenario = _load_scenario(scenario_path, root)
        safe_target = _validate_replay_target(target)
        selected_events, checks = _prepare_replay_batch(
            run=run,
            scenario=scenario,
            event_ids=event_ids,
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
            "target": safe_target,
            "event_ids": [event["event_id"] for event in selected_events],
            **checks,
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
                "No request was sent. Set confirm=true only after reviewing all selected "
                "event IDs, the destination, and the warnings."
            )
            return preview

        selected_scenario = {
            **scenario,
            "events": [
                {**event, "schedule_after_ms": 0}
                for event in scenario["events"]
                if event["event_id"] in set(event_ids)
            ],
            "verify": None,
        }
        result = await run_scenario(selected_scenario, safe_target, correlation_run_id=run_id)
        try:
            save_run(result, history_db)
            history_saved = True
            history_warning = None
        except HistoryError as exc:
            history_saved = False
            history_warning = str(exc)
        return {
            "executed": True,
            "replay_enabled": True,
            "source_run_id": run_id,
            "run_id": result["run_id"],
            "correlation_run_id": result["correlation_run_id"],
            "scenario": result["scenario"],
            "target": result["target"],
            "selected_event_ids": [event["event_id"] for event in selected_events],
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
                "reason": "The original whole-scenario verification probe was not repeated.",
            },
        }

    @server.tool()
    def hookstorm_find_deliveries(
        event_id: str | None = None,
        event_type: str | None = None,
        scenario: str | None = None,
        status: str = "failed",
        limit: int = 20,
        lookback_runs: int = 100,
    ) -> dict[str, Any]:
        """Search a bounded number of recent sanitized runs for matching deliveries."""
        if status not in {"failed", "passed", "all"}:
            raise ValueError("status must be one of: failed, passed, all")
        normalized_filters: dict[str, str | None] = {}
        for field, value in (
            ("event_id", event_id),
            ("event_type", event_type),
            ("scenario", scenario),
        ):
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
                    raise ValueError(f"{field} must be a non-empty string of at most 256 characters")
                normalized_filters[field] = value.strip()
            else:
                normalized_filters[field] = None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if (
            isinstance(lookback_runs, bool)
            or not isinstance(lookback_runs, int)
            or not 1 <= lookback_runs <= 500
        ):
            raise ValueError("lookback_runs must be an integer between 1 and 500")

        run_summaries = list_runs(
            history_db,
            limit=lookback_runs,
            scenario=normalized_filters["scenario"],
        )
        matches: list[dict[str, Any]] = []
        searched_runs = 0
        has_more = False
        for run_summary in run_summaries:
            run = get_run(run_summary["run_id"], history_db)
            searched_runs += 1
            for delivery in run.get("deliveries", []):
                if (
                    normalized_filters["event_id"] is not None
                    and delivery.get("event_id") != normalized_filters["event_id"]
                ):
                    continue
                if (
                    normalized_filters["event_type"] is not None
                    and delivery.get("event_type") != normalized_filters["event_type"]
                ):
                    continue
                passed = bool(delivery.get("passed"))
                if status == "failed" and passed:
                    continue
                if status == "passed" and not passed:
                    continue
                if len(matches) >= limit:
                    has_more = True
                    break
                matches.append(
                    {
                        "run_id": run["run_id"],
                        "scenario": run["scenario"],
                        "started_at": run["started_at"],
                        "target": run["target"],
                        "run_success": run["summary"]["success"],
                        "delivery": {
                            "event_type": delivery.get("event_type"),
                            "event_id": delivery.get("event_id"),
                            "duplicate_index": delivery.get("duplicate_index", 0),
                            "passed": passed,
                            "attempts": [
                                _attempt_summary(attempt)
                                for attempt in delivery.get("attempts", [])
                            ],
                            "diagnosis": (
                                _diagnose_delivery(delivery) if not passed else None
                            ),
                        },
                    }
                )
            if has_more:
                break

        return {
            "count": len(matches),
            "has_more": has_more,
            "searched_runs": searched_runs,
            "lookback_runs": lookback_runs,
            "status_filter": status,
            "filters": normalized_filters,
            "deliveries": matches,
            "limitations": [
                "Search is limited to the newest lookback_runs records in local HookStorm history.",
                "History contains sanitized attempt metadata, not request payloads, header values, or response bodies.",
            ],
        }


    @server.tool()
    def hookstorm_list_github_deliveries(
        owner: str,
        repo: str,
        hook_id: int,
        limit: int = 20,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List external GitHub repository webhook deliveries; performs read-only API calls."""
        with GitHubWebhookAdapter(client=github_http_client) as adapter:
            deliveries = adapter.list_deliveries(
                owner,
                repo,
                hook_id,
                limit=limit,
                status=status,
            )
        return {
            "provider": "github",
            "source": "github_repository_webhook_delivery_api",
            "count": len(deliveries),
            "deliveries": deliveries,
            "read_only": True,
            "limitations": [
                "The tool reads delivery metadata only; it does not redeliver provider events.",
                "Use hookstorm_inspect_github_delivery for one delivery's request/response metadata.",
            ],
        }

    @server.tool()
    def hookstorm_inspect_github_delivery(
        owner: str,
        repo: str,
        hook_id: int,
        delivery_id: int,
        run_id: str | None = None,
        scenario_path: str | None = None,
    ) -> dict[str, Any]:
        """Inspect one GitHub delivery and optionally correlate it with a local HookStorm run."""
        if (run_id is None) != (scenario_path is None):
            raise ValueError("run_id and scenario_path must be supplied together")

        with GitHubWebhookAdapter(client=github_http_client) as adapter:
            external = adapter.get_delivery(
                owner,
                repo,
                hook_id,
                delivery_id,
            )

        result: dict[str, Any] = {
            "provider": "github",
            "source": "github_repository_webhook_delivery_api",
            "delivery": external,
            "read_only": True,
        }
        if run_id is not None and scenario_path is not None:
            run = get_run(run_id, history_db)
            _, scenario = _load_scenario(scenario_path, root)
            result["correlation"] = _correlate_github_delivery(
                external,
                run,
                scenario,
            )
        else:
            result["correlation"] = {
                "status": "not_requested",
                "instruction": "Pass both run_id and scenario_path to attempt evidence-based correlation.",
            }
        return result

    return server


try:
    # Expose a module-level server object for the MCP Inspector and MCP CLI.
    mcp = create_server()
except RuntimeError:
    # Keep the base HookStorm install importable without the optional MCP extra.
    mcp = None


def main() -> None:
    """Run HookStorm MCP over the local stdio transport."""
    if mcp is None:
        create_server()  # raises the actionable optional-dependency error
        return
    mcp.run()


if __name__ == "__main__":
    main()
