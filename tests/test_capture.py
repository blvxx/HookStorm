from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path

import pytest

from hookstorm.capture import (
    MAX_CAPTURE_BODY_BYTES,
    CaptureError,
    CaptureStore,
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
                "X-API-Key": "private-key",
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
    assert "private-key" not in saved
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
    }
    assert scenario_fingerprint(scenario) == scenario_fingerprint(
        validate_scenario(
            {
                "version": 1,
                "name": "Captured webhook scenario",
                "description": (
                    "Captured locally by HookStorm. Review payloads and headers before sharing."
                ),
                "delivery": {"signature": "none"},
                "events": [
                    {
                        "id": "payment_intent.succeeded",
                        "event_id": "evt_capture_0001",
                        "payload": {"type": "payment_intent.succeeded", "id": "pi_1"},
                        "headers": {"Content-Type": "application/json"},
                    }
                ],
            }
        )
    )


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
