from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from hookstorm.capture import (
    MAX_CAPTURE_BODY_BYTES,
    CaptureError,
    CaptureStore,
    _parse_content_length,
    create_capture_server,
)
from hookstorm.config import load_scenario, scenario_fingerprint, validate_scenario


def _send_json(
    port: int,
    payload: object,
    headers: dict[str, str] | None = None,
    *,
    path: str = "/webhook",
) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    connection.request("POST", path, body=body, headers=request_headers)
    response = connection.getresponse()
    result = json.loads(response.read())
    connection.close()
    return response.status, result


def _start_server(tmp_path: Path, **kwargs):
    output = tmp_path / "captured" / "webhooks.yaml"
    server, store = create_capture_server(output, port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store, thread, output


def _stop_server(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_content_length_parser_rejects_ambiguous_and_oversized_values() -> None:
    assert _parse_content_length(["0000000000000000000001"]) == 1

    with pytest.raises(CaptureError) as duplicate:
        _parse_content_length(["1", "1"])
    assert duplicate.value.status_code == 411

    with pytest.raises(CaptureError) as non_ascii:
        _parse_content_length(["１"])
    assert non_ascii.value.status_code == 411

    with pytest.raises(CaptureError) as oversized:
        _parse_content_length(["9" * 5_000])
    assert oversized.value.status_code == 413


def test_capture_server_is_loopback_only_and_redacts_sensitive_headers(tmp_path) -> None:
    server, store, thread, output = _start_server(tmp_path)
    try:
        assert server.server_address[0] == "127.0.0.1"
        status, response = _send_json(
            server.server_address[1],
            {"type": "invoice.paid", "data": {"object": {"id": "in_123"}}},
            {
                "Authorization": "Bearer do-not-save",
                "Stripe-Signature": "t=123,v1=signature",
                "X-Shopify-Hmac-Sha256": "shopify-hmac-secret",
                "webhook-id": "message_123",
                "webhook-timestamp": "1700000000",
                "webhook-signature": "v1,standard-signature",
                "X-API-Key": "private-key",
                "X-Auth-Key": "auth-key-value",
                "X-Private-Key": "private-key-value",
                "X-Client-Key": "client-key-value",
                "X-Trace-ID": "trace-123",
                "X-Event-ID": "provider-event-1",
            },
            path="/hooks/stripe?token=redact-me",
        )
        assert status == 202
        assert response["message"] == "captured evt_capture_0001"
    finally:
        _stop_server(server, thread)

    scenario = load_scenario(output)
    event = scenario["events"][0]
    assert store.total_body_bytes > 0
    assert event["id"] == "invoice.paid"
    assert event["payload"]["data"]["object"]["id"] == "in_123"
    assert event["headers"] == {
        "Content-Type": "application/json",
        "X-Trace-ID": "trace-123",
    }
    metadata = scenario["capture"]["events"][0]
    assert metadata["path"] == "/hooks/stripe"
    assert metadata["original_event_id"] == "provider-event-1"
    assert metadata["sensitive_headers"] == {}
    saved = output.read_text(encoding="utf-8")
    assert "do-not-save" not in saved
    assert "t=123,v1=signature" not in saved
    assert "shopify-hmac-secret" not in saved
    assert "standard-signature" not in saved
    assert "private-key" not in saved
    assert "auth-key-value" not in saved
    assert "private-key-value" not in saved
    assert "client-key-value" not in saved
    assert "redact-me" not in saved
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600


def test_sensitive_headers_are_saved_only_as_non_replayed_capture_metadata(tmp_path) -> None:
    server, _, thread, output = _start_server(
        tmp_path, include_sensitive_headers=True
    )
    try:
        status, _ = _send_json(
            server.server_address[1],
            {"type": "payment_intent.succeeded", "id": "pi_1"},
            {
                "Authorization": "Bearer forensic-token",
                "Stripe-Signature": "t=123,v1=forensic-signature",
                "X-Shopify-Hmac-Sha256": "forensic-hmac",
                "Content-Type": "application/json",
            },
        )
        assert status == 202
    finally:
        _stop_server(server, thread)

    scenario = load_scenario(output)
    event = scenario["events"][0]
    sensitive = scenario["capture"]["events"][0]["sensitive_headers"]

    assert "Authorization" not in event["headers"]
    assert "Stripe-Signature" not in event["headers"]
    assert sensitive == {
        "Authorization": "Bearer forensic-token",
        "Stripe-Signature": "t=123,v1=forensic-signature",
        "X-Shopify-Hmac-Sha256": "forensic-hmac",
    }
    scenario_without_capture_metadata = dict(scenario)
    scenario_without_capture_metadata.pop("capture")
    assert scenario_fingerprint(scenario) == scenario_fingerprint(
        scenario_without_capture_metadata
    )


def test_capture_http_statuses_distinguish_quota_size_and_storage_errors(
    tmp_path, monkeypatch
) -> None:
    server, store, thread, output = _start_server(tmp_path, max_events=1)
    try:
        port = server.server_address[1]
        status, _ = _send_json(port, {"first": True})
        assert status == 202

        status, body = _send_json(port, {"second": True})
        assert status == 429
        assert "event limit" in body["message"]
        assert len(store.events) == 1
    finally:
        _stop_server(server, thread)

    import hookstorm.capture as capture_module

    size_server, size_store, size_thread, size_output = _start_server(
        tmp_path / "size", max_events=2
    )
    monkeypatch.setattr(capture_module, "MAX_CAPTURE_SCENARIO_BYTES", 1)
    try:
        status, body = _send_json(size_server.server_address[1], {"payload": "too large"})
        assert status == 413
        assert "total body limit" in body["message"]
        assert size_store.events == []
        assert not size_output.exists()
    finally:
        _stop_server(size_server, size_thread)

    monkeypatch.undo()
    storage_root = tmp_path / "storage"
    storage_output_before = storage_root / "captured" / "webhooks.yaml"
    storage_output_before.parent.mkdir(parents=True)
    storage_output_before.write_text("previous valid capture", encoding="utf-8")
    storage_server, storage_store, storage_thread, storage_output = _start_server(
        storage_root
    )

    def fail_replace(_source, _destination):
        raise OSError("simulated disk error")

    monkeypatch.setattr(capture_module.os, "replace", fail_replace)
    try:
        status, body = _send_json(storage_server.server_address[1], {"stored": False})
        assert status == 500
        assert body["message"] == "capture could not be persisted"
        assert storage_store.events == []
        assert storage_store.capture_events == []
        assert storage_store.total_body_bytes == 0
        assert storage_output.read_text(encoding="utf-8") == "previous valid capture"
    finally:
        _stop_server(storage_server, storage_thread)


def test_capture_infers_github_event_action_and_preserves_source_id(tmp_path) -> None:
    server, _, thread, output = _start_server(tmp_path)
    try:
        status, _ = _send_json(
            server.server_address[1],
            {"action": "opened", "issue": {"number": 42}},
            {
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "provider-delivery-42",
                "Content-Type": "application/json",
            },
        )
        assert status == 202
    finally:
        _stop_server(server, thread)

    scenario = load_scenario(output)
    assert scenario["events"][0]["id"] == "issues.opened"
    assert scenario["events"][0]["payload"]["action"] == "opened"
    assert scenario["capture"]["events"][0]["original_event_id"] == "provider-delivery-42"


def test_capture_rejects_non_json_body_and_enforces_body_limit(tmp_path) -> None:
    store = CaptureStore(tmp_path / "capture.yaml")

    with pytest.raises(CaptureError, match="UTF-8 JSON"):
        store.record({"Content-Type": "application/json"}, b"not-json")
    with pytest.raises(CaptureError, match="1 MiB"):
        store.record(
            {"Content-Type": "application/json"},
            b" " * (MAX_CAPTURE_BODY_BYTES + 1),
        )
    assert store.events == []
    assert not store.output.exists()


def test_capture_converts_unsupported_json_and_scenario_data_to_client_errors(
    tmp_path,
) -> None:
    store = CaptureStore(tmp_path / "bounded-errors.yaml")
    headers = {"Content-Type": "application/json"}

    too_deep = ("[" * 1_100 + "null" + "]" * 1_100).encode("ascii")
    with pytest.raises(CaptureError, match="nesting limits"):
        store.record(headers, too_deep)

    oversized_integer = b'{"value":' + (b"9" * 5_000) + b"}"
    with pytest.raises(CaptureError, match="UTF-8 JSON"):
        store.record(headers, oversized_integer)

    with pytest.raises(CaptureError, match="HookStorm scenario"):
        store.record(headers, b'{"value": NaN}')

    with pytest.raises(CaptureError, match="HookStorm scenario"):
        store.record(
            {**headers, "X-Event-Type": "x" * 257},
            b'{"ok":true}',
        )

    assert store.events == []
    assert store.capture_events == []
    assert store.total_body_bytes == 0
    assert not store.output.exists()


def test_capture_enforces_event_limit_and_validates_port(tmp_path) -> None:
    store = CaptureStore(tmp_path / "capture.yaml", max_events=1)
    store.record({}, b'{"first": true}')

    with pytest.raises(CaptureError, match="event limit"):
        store.record({}, b'{"second": true}')
    with pytest.raises(CaptureError, match="port"):
        create_capture_server(tmp_path / "bad.yaml", port=65536)
    with pytest.raises(CaptureError, match="max_events"):
        CaptureStore(tmp_path / "bad.yaml", max_events=0)


def test_capture_metadata_must_match_scenario_events() -> None:
    with pytest.raises(ValueError, match="capture.events must contain"):
        validate_scenario(
            {
                "version": 1,
                "name": "capture metadata validation",
                "capture": {"source": "hookstorm.capture", "events": []},
                "events": [
                    {"id": "ping", "event_id": "evt_1", "payload": {}},
                ],
            }
        )



def test_concurrent_capture_requests_receive_unique_ordered_event_ids(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = CaptureStore(tmp_path / "concurrent.yaml", max_events=12)

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(
            pool.map(
                lambda index: store.record(
                    {"Content-Type": "application/json"},
                    json.dumps({"sequence": index}).encode("utf-8"),
                ),
                range(12),
            )
        )

    event_ids = [event["event_id"] for event in events]
    assert len(set(event_ids)) == 12
    scenario = load_scenario(tmp_path / "concurrent.yaml")
    assert len(scenario["events"]) == 12
    assert [event["event_id"] for event in scenario["events"]] == [
        f"evt_capture_{index:04d}" for index in range(1, 13)
    ]
    assert [
        item["event_id"] for item in scenario["capture"]["events"]
    ] == [event["event_id"] for event in scenario["events"]]



def test_standard_webhooks_headers_are_metadata_not_replayed_headers(tmp_path) -> None:
    store = CaptureStore(
        tmp_path / "standard-capture.yaml",
        include_sensitive_headers=True,
    )
    event = store.record(
        {
            "webhook-id": "msg_123",
            "webhook-timestamp": "1700000000",
            "webhook-signature": "v1,signature-value",
            "Content-Type": "application/json",
        },
        b'{"type": "order.created", "data": {"id": "ord_1"}}',
    )

    scenario = load_scenario(tmp_path / "standard-capture.yaml")
    assert event["headers"] == {"Content-Type": "application/json"}
    metadata = scenario["capture"]["events"][0]
    assert metadata["original_event_id"] == "msg_123"
    assert metadata["sensitive_headers"] == {
        "webhook-signature": "v1,signature-value",
    }


def test_captured_scenario_can_later_select_provider_profile(tmp_path) -> None:
    store = CaptureStore(tmp_path / "provider-capture.yaml")
    store.record(
        {"Content-Type": "application/json"},
        b'{"id": "evt_source", "type": "payment_intent.succeeded"}',
    )

    raw_scenario = yaml.safe_load(
        (tmp_path / "provider-capture.yaml").read_text(encoding="utf-8")
    )
    assert raw_scenario["delivery"] == {}
    raw_scenario["provider"] = "stripe"

    profiled = validate_scenario(raw_scenario)

    assert profiled["provider"] == "stripe"
    assert profiled["delivery"]["signature"] == "valid"
    assert profiled["delivery"]["signature_provider"] == "stripe"

def test_capture_rejects_malformed_request_path_without_mutating_store(tmp_path) -> None:
    store = CaptureStore(tmp_path / "invalid-path.yaml")

    with pytest.raises(CaptureError, match="request target path is invalid"):
        store.record(
            {"Content-Type": "application/json"},
            b'{"ok":true}',
            request_path="//[",
        )

    assert store.events == []
    assert store.capture_events == []
    assert store.total_body_bytes == 0
    assert not store.output.exists()


def test_capture_rejects_duplicate_json_keys_at_any_depth(tmp_path) -> None:
    store = CaptureStore(tmp_path / "duplicate-keys.yaml")

    with pytest.raises(CaptureError, match="duplicate key"):
        store.record(
            {"Content-Type": "application/json"},
            b'{"type":"invoice.paid","type":"invoice.refunded"}',
        )

    with pytest.raises(CaptureError, match="duplicate key"):
        store.record(
            {"Content-Type": "application/json"},
            b'{"data":{"id":"evt_1","id":"evt_2"}}',
        )

    assert store.events == []
    assert store.capture_events == []
    assert store.total_body_bytes == 0
    assert not store.output.exists()
