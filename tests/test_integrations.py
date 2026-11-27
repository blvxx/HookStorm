from __future__ import annotations

import gzip

import pytest

from hookstorm.integrations import WebhookRecorder, WebhookRequestError


def _headers(**overrides: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-HookStorm-Run": "run_001",
        "X-Event-ID": "evt_001",
    }
    headers.update(overrides)
    return headers


def test_recorder_preserves_wire_body_and_detects_duplicate_deliveries() -> None:
    recorder = WebhookRecorder()
    raw_body = b'{"type":"invoice.paid","amount":1250}'

    first = recorder.record(_headers(), raw_body)
    second = recorder.record(_headers(), raw_body)

    assert first.raw_body == raw_body
    assert first.payload == {"type": "invoice.paid", "amount": 1250}
    assert first.duplicate is False
    assert second.duplicate is True
    assert first.response()["ok"] is True
    assert recorder.snapshot() == {
        "total_events": 2,
        "unique_deliveries": 1,
        "duplicate_deliveries": 1,
        "event_ids": ["evt_001", "evt_001"],
    }


def test_recorder_scopes_duplicate_identity_to_run_id() -> None:
    recorder = WebhookRecorder()
    body = b'{"ok":true}'

    first = recorder.record(_headers(**{"X-HookStorm-Run": "run_a"}), body)
    second = recorder.record(_headers(**{"X-HookStorm-Run": "run_b"}), body)

    assert first.duplicate is False
    assert second.duplicate is False
    assert recorder.snapshot()["unique_deliveries"] == 2


def test_recorder_redacts_sensitive_headers_by_default() -> None:
    recorder = WebhookRecorder()
    event = recorder.record(
        _headers(
            Authorization="Bearer not-for-reports",
            **{"Stripe-Signature": "t=1,v1=not-for-reports"},
        ),
        b'{"ok":true}',
    )

    assert event.headers["Authorization"] == "[REDACTED]"
    assert event.headers["Stripe-Signature"] == "[REDACTED]"
    assert event.headers["Content-Type"] == "application/json"


def test_recorder_can_include_sensitive_headers_only_when_explicitly_enabled() -> None:
    recorder = WebhookRecorder(include_sensitive_headers=True)
    event = recorder.record(
        _headers(Authorization="Bearer local-test-token"),
        b'{"ok":true}',
    )

    assert event.headers["Authorization"] == "Bearer local-test-token"


def test_recorder_decodes_gzip_but_preserves_compressed_wire_bytes() -> None:
    recorder = WebhookRecorder()
    compressed = gzip.compress(b'{"kind":"compressed"}', mtime=0)
    event = recorder.record(
        _headers(**{"Content-Encoding": "gzip"}),
        compressed,
    )

    assert event.raw_body == compressed
    assert event.payload == {"kind": "compressed"}


def test_recorder_rejects_unsupported_encoding_and_invalid_compression() -> None:
    recorder = WebhookRecorder()
    with pytest.raises(WebhookRequestError) as unsupported:
        recorder.record(_headers(**{"Content-Encoding": "br"}), b"body")
    assert unsupported.value.status_code == 415

    with pytest.raises(WebhookRequestError) as invalid:
        recorder.record(_headers(**{"Content-Encoding": "gzip"}), b"not-gzip")
    assert invalid.value.status_code == 400


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"Content-Type": "text/plain"}, b'{"ok":true}', 415),
        ({"Content-Type": "application/json"}, b"{", 400),
        ({"Content-Type": "application/json"}, b'{"x":1,"x":2}', 400),
        ({"Content-Type": "application/json"}, b'{"x":{"y":1,"y":2}}', 400),
        ({"Content-Type": "application/json"}, b"\xff", 400),
    ],
)
def test_recorder_rejects_invalid_json_and_content_types(
    headers: dict[str, str],
    body: bytes,
    status: int,
) -> None:
    recorder = WebhookRecorder()
    with pytest.raises(WebhookRequestError) as error:
        recorder.record(headers, body)
    assert error.value.status_code == status


def test_recorder_bounds_wire_and_decompressed_body_sizes() -> None:
    recorder = WebhookRecorder(max_body_bytes=32)
    with pytest.raises(WebhookRequestError) as wire_limit:
        recorder.record(_headers(), b" " * 33)
    assert wire_limit.value.status_code == 413

    compressed = gzip.compress(b'{"payload":"' + (b"x" * 128) + b'"}', mtime=0)
    with pytest.raises(WebhookRequestError) as decoded_limit:
        recorder.record(_headers(**{"Content-Encoding": "gzip"}), compressed)
    assert decoded_limit.value.status_code == 413


def test_recorder_clear_resets_events_and_duplicate_state() -> None:
    recorder = WebhookRecorder()
    recorder.record(_headers(), b'{"ok":true}')
    recorder.clear()
    event = recorder.record(_headers(), b'{"ok":true}')

    assert event.duplicate is False
    assert recorder.snapshot()["total_events"] == 1


def test_fastapi_router_records_requests_and_rejects_bad_payloads() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hookstorm.integrations.fastapi import create_router

    integration = create_router()
    app = FastAPI()
    app.include_router(integration.router)

    with TestClient(app) as client:
        first = client.post(
            "/__hookstorm/webhook",
            content=b'{"type":"order.created"}',
            headers=_headers(Authorization="Bearer test-secret"),
        )
        second = client.post(
            "/__hookstorm/webhook",
            content=b'{"type":"order.created"}',
            headers=_headers(Authorization="Bearer test-secret"),
        )
        bad = client.post(
            "/__hookstorm/webhook",
            content=b'{"duplicate":1,"duplicate":2}',
            headers=_headers(),
        )
        oversized_integration = create_router(max_body_bytes=8)
        small_app = FastAPI()
        small_app.include_router(oversized_integration.router)
        with TestClient(small_app) as small_client:
            oversized = small_client.post(
                "/__hookstorm/webhook",
                content=b'{"long":"payload"}',
                headers=_headers(),
            )

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert bad.status_code == 400
    assert oversized.status_code == 413
    assert integration.recorder.events[0].headers["Authorization"] == "[REDACTED]"


def test_flask_endpoint_records_requests_and_returns_http_errors() -> None:
    from flask import Flask

    from hookstorm.integrations.flask import register_endpoint

    app = Flask(__name__)
    recorder = register_endpoint(app)
    client = app.test_client()

    first = client.post(
        "/__hookstorm/webhook",
        data=b'{"type":"order.created"}',
        headers=_headers(),
    )
    second = client.post(
        "/__hookstorm/webhook",
        data=b'{"type":"order.created"}',
        headers=_headers(),
    )
    bad = client.post(
        "/__hookstorm/webhook",
        data=b'{"duplicate":1,"duplicate":2}',
        headers=_headers(),
    )

    assert first.status_code == 200
    assert first.json["duplicate"] is False
    assert second.json["duplicate"] is True
    assert bad.status_code == 400
    assert recorder.snapshot()["total_events"] == 2


def test_framework_adapters_validate_configuration() -> None:
    from fastapi import FastAPI

    from hookstorm.integrations.fastapi import create_router
    from hookstorm.integrations.flask import register_endpoint

    with pytest.raises(ValueError, match="absolute route path"):
        create_router(path="relative")
    with pytest.raises(ValueError, match="absolute route path"):
        register_endpoint(FastAPI(), path="relative")
