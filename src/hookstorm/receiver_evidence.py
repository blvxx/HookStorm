"""Read-only inspection of versioned, structured receiver evidence logs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

_MAX_EVIDENCE_FILE_BYTES = 4 * 1024 * 1024
_MAX_EVIDENCE_LINE_BYTES = 16 * 1024
_MAX_EVIDENCE_LINES = 10_000
_MAX_RETURNED_MATCHES = 100

_ALLOWED_PROVIDERS = {"github", "stripe", "shopify", "generic"}
_ALLOWED_SEVERITIES = {"debug", "info", "warning", "error", "critical"}
_ALLOWED_OUTCOMES = {"accepted", "processed", "duplicate", "failed", "rejected"}

# These are explicit receiver-reported error codes, not model-generated diagnoses.
_CAUSES: dict[str, tuple[str, str]] = {
    "signature_invalid": (
        "authentication",
        "The receiver's structured log records a failed webhook signature check.",
    ),
    "signature_missing": (
        "authentication",
        "The receiver's structured log records a missing webhook signature.",
    ),
    "timestamp_expired": (
        "authentication",
        "The receiver's structured log records a webhook timestamp outside its accepted tolerance.",
    ),
    "payload_validation_failed": (
        "validation",
        "The receiver's structured log records payload validation failure.",
    ),
    "unsupported_event_type": (
        "routing",
        "The receiver's structured log records an unsupported event type.",
    ),
    "route_not_found": (
        "routing",
        "The receiver's structured log records that no handler route matched the request.",
    ),
    "database_unavailable": (
        "database",
        "The receiver's structured log records an unavailable database dependency.",
    ),
    "database_constraint_violation": (
        "database",
        "The receiver's structured log records a database constraint violation.",
    ),
    "queue_unavailable": (
        "queue",
        "The receiver's structured log records an unavailable queue dependency.",
    ),
    "downstream_timeout": (
        "downstream",
        "The receiver's structured log records a downstream dependency timeout.",
    ),
    "downstream_rejected": (
        "downstream",
        "The receiver's structured log records a downstream dependency rejection.",
    ),
    "idempotency_conflict": (
        "idempotency",
        "The receiver's structured log records an idempotency-key conflict.",
    ),
    "rate_limited": (
        "throttling",
        "The receiver's structured log records application-level rate limiting.",
    ),
    "internal_error": (
        "processing",
        "The receiver's structured log records an internal processing error.",
    ),
}


class ReceiverEvidenceError(ValueError):
    """Raised when a receiver evidence source cannot be inspected safely."""


def _safe_text(value: Any, maximum: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        return None
    return value


def _required_text(value: Any, name: str, maximum: int = 256) -> str:
    text = _safe_text(value, maximum)
    if text is None:
        raise ReceiverEvidenceError(f"{name} must be a non-empty string of at most {maximum} characters")
    return text


def _timestamp(value: Any) -> str | None:
    text = _safe_text(value, 64)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return text


def _resolve_evidence_path(value: str, root: str | Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ReceiverEvidenceError("evidence_path must be a non-empty path")
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ReceiverEvidenceError("Receiver evidence root is not an existing directory")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ReceiverEvidenceError("Receiver evidence file could not be resolved") from exc
    if not resolved.is_relative_to(root_path):
        raise ReceiverEvidenceError("Evidence path must stay inside HOOKSTORM_MCP_EVIDENCE_ROOT")
    if not resolved.is_file() or resolved.suffix.lower() not in {".jsonl", ".ndjson"}:
        raise ReceiverEvidenceError("Evidence path must reference a JSONL or NDJSON file")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ReceiverEvidenceError("Receiver evidence file could not be inspected") from exc
    if size > _MAX_EVIDENCE_FILE_BYTES:
        raise ReceiverEvidenceError("Receiver evidence file exceeds the 4 MiB inspection limit")
    return resolved


def _parse_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    timestamp = _timestamp(value.get("timestamp"))
    event_id = _safe_text(value.get("event_id"), 256)
    severity = _safe_text(value.get("severity"), 16)
    outcome = _safe_text(value.get("outcome"), 16)
    if timestamp is None or event_id is None or severity is None or outcome is None:
        return None
    provider = value.get("provider")
    if provider is not None and provider not in _ALLOWED_PROVIDERS:
        return None
    provider_delivery_id = _safe_text(value.get("provider_delivery_id"), 256)
    event_type = _safe_text(value.get("event_type"), 256)
    request_id = _safe_text(value.get("request_id"), 256)
    trace_id = _safe_text(value.get("trace_id"), 256)
    error_code = _safe_text(value.get("error_code"), 100)
    component = _safe_text(value.get("component"), 100)
    severity = severity.lower()
    outcome = outcome.lower()
    if severity not in _ALLOWED_SEVERITIES or outcome not in _ALLOWED_OUTCOMES:
        return None
    return {
        "timestamp": timestamp,
        "provider": provider,
        "provider_delivery_id": provider_delivery_id,
        "event_id": event_id,
        "event_type": event_type,
        "request_id": request_id,
        "trace_id": trace_id,
        "component": component,
        "severity": severity,
        "outcome": outcome,
        "error_code": error_code,
    }


def inspect_receiver_evidence(
    evidence_path: str,
    *,
    root: str | Path,
    event_id: str,
    provider: str | None = None,
    provider_delivery_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Find exact event records and confirm only an explicit, consistent receiver error code."""
    event_id = _required_text(event_id, "event_id")
    if provider is not None:
        provider = _required_text(provider, "provider", 16).lower()
        if provider not in _ALLOWED_PROVIDERS - {"generic"}:
            raise ReceiverEvidenceError("provider must be github, stripe, or shopify")
    if provider_delivery_id is not None:
        provider_delivery_id = _required_text(provider_delivery_id, "provider_delivery_id")
    if request_id is not None:
        request_id = _required_text(request_id, "request_id")

    path = _resolve_evidence_path(evidence_path, root)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReceiverEvidenceError("Receiver evidence file could not be read") from exc
    if len(raw) > _MAX_EVIDENCE_FILE_BYTES:
        raise ReceiverEvidenceError("Receiver evidence file exceeds the 4 MiB inspection limit")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiverEvidenceError("Receiver evidence file must be UTF-8 JSONL") from exc

    lines = content.splitlines()
    if len(lines) > _MAX_EVIDENCE_LINES:
        raise ReceiverEvidenceError("Receiver evidence file exceeds the 10,000-line inspection limit")

    matches: list[dict[str, Any]] = []
    ignored_records = 0
    for line in lines:
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > _MAX_EVIDENCE_LINE_BYTES:
            ignored_records += 1
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            ignored_records += 1
            continue
        record = _parse_record(parsed)
        if record is None:
            ignored_records += 1
            continue
        if record["event_id"] != event_id:
            continue
        if provider is not None and record["provider"] != provider:
            continue
        if (
            provider_delivery_id is not None
            and record["provider_delivery_id"] != provider_delivery_id
        ):
            continue
        if request_id is not None and record["request_id"] != request_id:
            continue
        matches.append(record)

    matches.sort(key=lambda item: item["timestamp"])
    truncated = len(matches) > _MAX_RETURNED_MATCHES
    returned = matches[-_MAX_RETURNED_MATCHES:]

    codes = {
        record["error_code"]
        for record in matches
        if record["error_code"] in _CAUSES
        and record["outcome"] in {"failed", "rejected"}
        and record["severity"] in {"error", "critical"}
    }
    all_failures = bool(matches) and all(
        record["outcome"] in {"failed", "rejected"}
        and record["severity"] in {"error", "critical"}
        and record["error_code"] in _CAUSES
        for record in matches
    )
    confirmed = (
        all_failures
        and len(codes) == 1
        and ignored_records == 0
        and not truncated
    )
    if confirmed:
        code = next(iter(codes))
        stage, explanation = _CAUSES[code]
        status = "confirmed"
        reason = None
    else:
        code = None
        stage = None
        explanation = None
        if not matches:
            status = "unmatched"
            reason = "No structured receiver log record matched the exact event and supplied filters."
        elif ignored_records or truncated:
            status = "incomplete"
            reason = "Some records were skipped or the result was truncated; a root cause cannot be confirmed safely."
        elif len(codes) > 1:
            status = "conflicting"
            reason = "Matching receiver records report different failure codes."
        elif any(record["outcome"] not in {"failed", "rejected"} for record in matches):
            status = "mixed_outcomes"
            reason = "Matching records include a non-failure outcome, so the event may have been processed on another attempt."
        else:
            status = "unconfirmed"
            reason = "Matching records do not contain one consistent, allowlisted receiver failure code."

    return {
        "provider": provider,
        "source": "local_receiver_structured_logs",
        "evidence_file": path.name,
        "event_id": event_id,
        "provider_delivery_id_filter": provider_delivery_id,
        "request_id_filter": request_id,
        "status": status,
        "matching_record_count": len(matches),
        "returned_record_count": len(returned),
        "matches_truncated": truncated,
        "evidence_integrity": {
            "complete": ignored_records == 0 and not truncated,
            "ignored_records": ignored_records,
            "schema": "hookstorm.receiver-evidence.v1",
        },
        "matches": [
            {
                **record,
                "cause": _CAUSES.get(record["error_code"], (None, None))[1],
            }
            for record in returned
        ],
        "root_cause": {
            "confirmed": confirmed,
            "code": code,
            "stage": stage,
            "explanation": explanation,
            "source": "receiver_structured_log" if confirmed else None,
            "scope": "receiver_handler",
            "confirmation_basis": (
                "Exact event identity, matching optional provider delivery/request identifiers, "
                "explicit failed/rejected outcome, error severity, one allowlisted error code, "
                "and a complete, untruncated evidence file."
                if confirmed
                else None
            ),
            "reason_unconfirmed": reason,
        },
        "read_only": True,
        "limitations": [
            "Confirmation means the receiver's own structured log explicitly recorded this failure code for the matched event; HookStorm does not independently prove the log producer is correct.",
            "Raw log messages, stack traces, request/response bodies, headers, and arbitrary fields are never returned.",
            "The JSONL source must be exported from trusted receiver-side logging; provider delivery metadata alone cannot establish an internal receiver root cause.",
        ],
    }
