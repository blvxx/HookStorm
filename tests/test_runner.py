from __future__ import annotations

import asyncio
import base64
import time

import pytest

from hookstorm.config import validate_scenario
from hookstorm.runner import run_scenario


def scenario(*, delivery=None, retry=None, event=None):
    return validate_scenario({
        "version": 1,
        "name": "test scenario",
        "delivery": delivery or {},
        "retry": retry or {},
        "events": [{
            "id": "order.created",
            "event_id": "evt_test_1",
            "payload": {"order_id": 1},
            **(event or {}),
        }],
    })


def test_retries_transient_server_error(receiver) -> None:
    base, state = receiver
    result = asyncio.run(run_scenario(
        scenario(retry={"max_attempts": 3, "backoff_ms": 1}), f"{base}/flaky"
    ))
    assert result["summary"]["success"] is True
    assert result["summary"]["attempts"] == 2
    assert [x["status_code"] for x in result["deliveries"][0]["attempts"]] == [503, 200]
    run_id = state["requests"][0]["run_id"]
    assert state["attempts"][(run_id, "evt_test_1")] == 2


def test_detects_duplicate_idempotency_response(receiver) -> None:
    base, state = receiver
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"duplicate_count": 2},
            event={
                "expect_json": {"duplicate": False},
                "expect_duplicate_json": {"duplicate": True},
            },
        ),
        base,
    ))
    assert result["summary"]["success"] is True
    assert result["summary"]["total_deliveries"] == 3
    assert len(state["requests"]) == 3
    assert all(item["event_id"] == "evt_test_1" for item in state["requests"])


def test_reverse_order_is_applied(receiver) -> None:
    base, state = receiver
    data = {
        "version": 1,
        "name": "reverse",
        "delivery": {"order": "reverse"},
        "retry": {"max_attempts": 1},
        "events": [
            {"id": "first", "event_id": "evt_first", "payload": {}},
            {"id": "second", "event_id": "evt_second", "payload": {}},
        ],
    }
    result = asyncio.run(run_scenario(validate_scenario(data), base))
    assert result["summary"]["success"] is True
    assert [request["event_id"] for request in state["requests"]] == ["evt_second", "evt_first"]


def test_generates_and_sends_hmac_signature(receiver) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(delivery={"signature": "valid"}), f"{base}/signature", secret="test-secret"
    ))
    assert result["summary"]["success"] is True


def test_expected_401_can_be_a_pass(receiver) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(delivery={"signature": "invalid"}, event={"expect_status": [401]}),
        f"{base}/signature",
        secret="test-secret",
    ))
    assert result["summary"]["success"] is True
    assert result["deliveries"][0]["attempts"][0]["status_code"] == 401


def test_repeated_runs_get_distinct_run_ids_and_clean_idempotency_state(receiver) -> None:
    base, state = receiver
    test_scenario = scenario(event={"expect_json": {"duplicate": False}})
    first = asyncio.run(run_scenario(test_scenario, base))
    second = asyncio.run(run_scenario(test_scenario, base))
    assert first["summary"]["success"] is True
    assert second["summary"]["success"] is True
    assert first["run_id"] != second["run_id"]
    assert len({request["run_id"] for request in state["requests"]}) == 2


def test_missing_signature_is_sent_as_absence_and_can_be_expected(receiver) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(delivery={"signature": "missing"}, event={"expect_status": [401]}),
        f"{base}/signature",
    ))
    assert result["summary"]["success"] is True


def test_query_parameters_are_not_written_to_report_target(receiver) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(),
        f"{base}/webhook?api_key=do-not-store-this",
    ))
    assert result["summary"]["success"] is True
    assert result["target"] == f"{base}/webhook"
    assert "do-not-store-this" not in result["target"]



def test_nested_json_expectations_match_subset(receiver) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(event={"expect_json": {"metadata": {"delivery": {"accepted": True}}}}),
        f"{base}/nested",
    ))
    assert result["summary"]["success"] is True


def test_json_boolean_does_not_match_integer(receiver) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(event={"expect_json": {"ok": 1}}),
        f"{base}/nested",
    ))
    assert result["summary"]["success"] is False
    assert result["deliveries"][0]["attempts"][0]["expected_json"] is False


def test_concurrency_limit_runs_events_in_parallel_and_keeps_report_order(receiver) -> None:
    base, state = receiver
    data = {
        "version": 1,
        "name": "concurrent delivery",
        "delivery": {"concurrency": 2},
        "retry": {"max_attempts": 1},
        "events": [
            {"id": f"event.{index}", "event_id": f"evt_{index}", "payload": {}}
            for index in range(4)
        ],
    }
    result = asyncio.run(run_scenario(validate_scenario(data), f"{base}/slow"))
    assert result["summary"]["success"] is True
    assert state["max_active"] == 2
    assert [item["event_id"] for item in result["deliveries"]] == [
        "evt_0", "evt_1", "evt_2", "evt_3"
    ]



def test_missing_hmac_secret_fails_before_sending_any_requests(receiver, monkeypatch) -> None:
    base, state = receiver
    monkeypatch.delenv("HOOKSTORM_SECRET", raising=False)
    data = {
        "version": 1,
        "name": "signature preflight",
        "events": [
            {"id": "first", "event_id": "evt_first", "payload": {}},
            {
                "id": "signed",
                "event_id": "evt_signed",
                "payload": {},
                "delivery": {"signature": "valid"},
            },
        ],
    }
    with pytest.raises(ValueError, match="requires HOOKSTORM_SECRET"):
        asyncio.run(run_scenario(validate_scenario(data), base, secret=None))
    assert state["requests"] == []



@pytest.mark.parametrize("provider", ["stripe", "github", "shopify"])
def test_provider_signature_headers_are_accepted(receiver, provider: str) -> None:
    base, state = receiver
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "valid", "signature_provider": provider},
            event={
                "expect_status": [200],
                "expect_json": {"ok": True, "provider": provider},
            },
        ),
        f"{base}/signature/{provider}",
        secret="test-secret",
    ))
    assert result["summary"]["success"] is True
    headers = {key.lower(): value for key, value in state["requests"][0]["headers"].items()}
    if provider == "stripe":
        signature = headers["stripe-signature"]
        assert signature.startswith("t=")
        assert ",v1=" in signature
    elif provider == "github":
        assert headers["x-hub-signature-256"].startswith("sha256=")
    else:
        decoded = base64.b64decode(headers["x-shopify-hmac-sha256"], validate=True)
        assert len(decoded) == 32


@pytest.mark.parametrize("provider", ["stripe", "github", "shopify"])
def test_provider_invalid_signatures_are_rejected(receiver, provider: str) -> None:
    base, _ = receiver
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "invalid", "signature_provider": provider},
            event={"expect_status": [401], "expect_json": {"ok": False}},
        ),
        f"{base}/signature/{provider}",
        secret="test-secret",
    ))
    assert result["summary"]["success"] is True
    assert result["deliveries"][0]["attempts"][0]["status_code"] == 401


def test_stripe_timestamp_offset_can_exercise_receiver_tolerance(receiver) -> None:
    base, state = receiver
    result = asyncio.run(run_scenario(
        scenario(
            delivery={
                "signature": "valid",
                "signature_provider": "stripe",
                "signature_timestamp_offset_seconds": -3600,
            },
            event={"expect_status": [401]},
        ),
        f"{base}/signature/stripe",
        secret="test-secret",
    ))
    assert result["summary"]["success"] is True
    header = {
        key.lower(): value for key, value in state["requests"][0]["headers"].items()
    }["stripe-signature"]
    timestamp = int(header.split(",", 1)[0].split("=", 1)[1])
    assert abs(time.time() - timestamp) >= 3599


def test_provider_signature_header_cannot_be_overridden(receiver) -> None:
    base, state = receiver
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "valid", "signature_provider": "github"},
            event={
                "headers": {"X-Hub-Signature-256": "sha256=attacker-controlled"},
                "expect_status": [200],
            },
        ),
        f"{base}/signature/github",
        secret="test-secret",
    ))
    assert result["summary"]["success"] is True
    headers = {key.lower(): value for key, value in state["requests"][0]["headers"].items()}
    assert headers["x-hub-signature-256"] != "sha256=attacker-controlled"


def test_missing_provider_signature_omits_provider_header(receiver) -> None:
    base, state = receiver
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "missing", "signature_provider": "shopify"},
            event={"expect_status": [401]},
        ),
        f"{base}/signature/shopify",
    ))
    assert result["summary"]["success"] is True
    headers = {key.lower(): value for key, value in state["requests"][0]["headers"].items()}
    assert "x-shopify-hmac-sha256" not in headers
