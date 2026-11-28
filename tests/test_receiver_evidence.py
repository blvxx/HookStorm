from __future__ import annotations

import json
from pathlib import Path

import pytest

from hookstorm.receiver_evidence import (
    ReceiverEvidenceError,
    inspect_receiver_evidence,
)


def _record(
    *,
    event_id: str = "evt_1A2B3C",
    provider: str = "stripe",
    provider_delivery_id: str = "evt_1A2B3C",
    outcome: str = "rejected",
    severity: str = "error",
    error_code: str = "signature_invalid",
) -> dict:
    return {
        "schema_version": 1,
        "timestamp": "2026-11-28T10:15:00Z",
        "provider": provider,
        "provider_delivery_id": provider_delivery_id,
        "event_id": event_id,
        "event_type": "payment_intent.succeeded",
        "request_id": "req_123",
        "trace_id": "trace_123",
        "component": "webhook-handler",
        "severity": severity,
        "outcome": outcome,
        "error_code": error_code,
        "message": "raw payload and customer secret must never be returned",
        "stacktrace": "secret stack frame",
    }


def _write(root: Path, records: list[dict] | list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "receiver.jsonl"
    path.write_text(
        "\n".join(
            item if isinstance(item, str) else json.dumps(item)
            for item in records
        ),
        encoding="utf-8",
    )
    return path


def test_receiver_evidence_confirms_exact_consistent_failure_without_raw_logs(
    tmp_path: Path,
) -> None:
    _write(tmp_path, [_record()])
    result = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
        provider="stripe",
        provider_delivery_id="evt_1A2B3C",
        request_id="req_123",
    )

    assert result["status"] == "confirmed"
    assert result["root_cause"]["confirmed"] is True
    assert result["root_cause"]["code"] == "signature_invalid"
    assert result["root_cause"]["stage"] == "authentication"
    assert result["matching_record_count"] == 1
    serialized = json.dumps(result)
    for secret in ("customer secret", "secret stack", "raw payload", "stacktrace"):
        assert secret not in serialized


def test_receiver_evidence_requires_exact_provider_delivery_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        [
            _record(provider_delivery_id="delivery-A"),
            _record(
                provider_delivery_id="delivery-A",
                error_code="database_unavailable",
            ),
        ],
    )
    wrong_delivery = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
        provider="stripe",
        provider_delivery_id="delivery-B",
    )
    assert wrong_delivery["status"] == "unmatched"
    assert wrong_delivery["root_cause"]["confirmed"] is False

    conflict = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
        provider="stripe",
        provider_delivery_id="delivery-A",
    )
    assert conflict["status"] == "conflicting"
    assert conflict["root_cause"]["confirmed"] is False


def test_receiver_evidence_does_not_confirm_unknown_or_mixed_outcomes(
    tmp_path: Path,
) -> None:
    unknown_path = _write(tmp_path, [_record(error_code="free-form-model-guess")])
    unknown = inspect_receiver_evidence(
        unknown_path.name,
        root=tmp_path,
        event_id="evt_1A2B3C",
    )
    assert unknown["status"] == "unconfirmed"
    assert unknown["root_cause"]["confirmed"] is False

    _write(
        tmp_path,
        [
            _record(),
            _record(outcome="processed", severity="info", error_code=""),
        ],
    )
    mixed = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
    )
    assert mixed["status"] == "mixed_outcomes"
    assert mixed["root_cause"]["confirmed"] is False


def test_receiver_evidence_never_confirms_incomplete_or_truncated_logs(
    tmp_path: Path,
) -> None:
    _write(tmp_path, [_record(), "{malformed-json"])
    incomplete = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
    )
    assert incomplete["status"] == "incomplete"
    assert incomplete["root_cause"]["confirmed"] is False

    _write(tmp_path, [_record() for _ in range(101)])
    truncated = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
    )
    assert truncated["matches_truncated"] is True
    assert truncated["root_cause"]["confirmed"] is False


def test_receiver_evidence_rejects_paths_outside_root_and_oversized_files(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(_record()), encoding="utf-8")
    root = tmp_path / "evidence"
    root.mkdir()

    with pytest.raises(ReceiverEvidenceError, match="inside"):
        inspect_receiver_evidence(
            str(outside),
            root=root,
            event_id="evt_1A2B3C",
        )

    large = root / "large.jsonl"
    large.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ReceiverEvidenceError, match="4 MiB"):
        inspect_receiver_evidence(
            large.name,
            root=root,
            event_id="evt_1A2B3C",
        )


def test_receiver_evidence_rejects_boolean_schema_version(tmp_path: Path) -> None:
    record = _record()
    record["schema_version"] = True
    _write(tmp_path, [record])
    result = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
    )
    assert result["status"] == "unmatched"
    assert result["evidence_integrity"]["ignored_records"] == 1
    assert result["root_cause"]["confirmed"] is False


def test_receiver_evidence_requires_provider_or_request_scope_to_confirm(
    tmp_path: Path,
) -> None:
    _write(tmp_path, [_record()])
    unscoped = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
    )
    assert unscoped["status"] == "unconfirmed"
    assert unscoped["identity_scope_specific"] is False
    assert unscoped["root_cause"]["confirmed"] is False

    scoped = inspect_receiver_evidence(
        "receiver.jsonl",
        root=tmp_path,
        event_id="evt_1A2B3C",
        provider="generic",
    )
    assert scoped["root_cause"]["confirmed"] is False
    assert scoped["status"] == "unmatched"
