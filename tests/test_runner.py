from __future__ import annotations

import asyncio
import base64
import time

import pytest

from hookstorm.config import ScenarioError, validate_scenario
from hookstorm.report import render_html, render_json, render_junit
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
    assert len(first["run_id"]) == 32
    assert len(second["run_id"]) == 32
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


def test_provider_signature_header_cannot_be_overridden() -> None:
    with pytest.raises(ScenarioError, match="may not override"):
        scenario(
            delivery={"signature": "valid", "signature_provider": "github"},
            event={"headers": {"X-Hub-Signature-256": "sha256=attacker-controlled"}},
        )

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



def test_standard_webhooks_signature_matches_the_standard(receiver) -> None:
    base, state = receiver
    key = b"hookstorm-test-standard-webhooks-secret"
    secret = "whsec_" + base64.b64encode(key).decode("ascii")
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "valid", "signature_provider": "standard_webhooks"},
            event={
                "expect_status": [200],
                "expect_json": {"ok": True, "provider": "standard"},
            },
        ),
        f"{base}/signature/standard",
        secret=secret,
    ))

    assert result["summary"]["success"] is True
    headers = {key.lower(): value for key, value in state["requests"][0]["headers"].items()}
    assert headers["webhook-id"] == "evt_test_1"
    assert headers["webhook-timestamp"].isdigit()
    assert headers["webhook-signature"].startswith("v1,")


def test_standard_webhooks_invalid_signature_is_rejected(receiver) -> None:
    base, _ = receiver
    key = b"hookstorm-test-standard-webhooks-secret"
    secret = "whsec_" + base64.b64encode(key).decode("ascii")
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "invalid", "signature_provider": "standard_webhooks"},
            event={"expect_status": [401], "expect_json": {"ok": False}},
        ),
        f"{base}/signature/standard",
        secret=secret,
    ))

    assert result["summary"]["success"] is True
    assert result["deliveries"][0]["attempts"][0]["status_code"] == 401


def test_standard_webhooks_stale_timestamp_is_rejected(receiver) -> None:
    base, _ = receiver
    key = b"hookstorm-test-standard-webhooks-secret"
    secret = "whsec_" + base64.b64encode(key).decode("ascii")
    result = asyncio.run(run_scenario(
        scenario(
            delivery={
                "signature": "valid",
                "signature_provider": "standard_webhooks",
                "signature_timestamp_offset_seconds": -3600,
            },
            event={"expect_status": [401], "expect_json": {"ok": False, "provider": "standard"}},
        ),
        f"{base}/signature/standard",
        secret=secret,
    ))

    assert result["summary"]["success"] is True
    assert result["deliveries"][0]["attempts"][0]["status_code"] == 401


def test_standard_webhooks_retry_reuses_id_and_refreshes_attempt_timestamp(receiver) -> None:
    base, state = receiver
    key = b"hookstorm-test-standard-webhooks-secret"
    secret = "whsec_" + base64.b64encode(key).decode("ascii")
    result = asyncio.run(run_scenario(
        scenario(
            delivery={"signature": "valid", "signature_provider": "standard_webhooks"},
            retry={"max_attempts": 2, "backoff_ms": 1_100},
            event={"expect_json": {"ok": True}},
        ),
        f"{base}/signature/standard-flaky",
        secret=secret,
    ))

    assert result["summary"]["success"] is True
    assert [attempt["status_code"] for attempt in result["deliveries"][0]["attempts"]] == [
        503, 200
    ]
    first, second = [
        {key.lower(): value for key, value in request["headers"].items()}
        for request in state["requests"]
    ]
    assert first["webhook-id"] == second["webhook-id"] == "evt_test_1"
    assert int(second["webhook-timestamp"]) > int(first["webhook-timestamp"])


@pytest.mark.parametrize("secret", ["not-a-whsec-secret", "whsec_!!!!", "whsec_YQ=="])
def test_standard_webhooks_invalid_secret_fails_before_sending(receiver, secret: str) -> None:
    base, state = receiver
    data = scenario(
        delivery={"signature": "valid", "signature_provider": "standard_webhooks"}
    )

    with pytest.raises(ValueError, match="Standard Webhooks"):
        asyncio.run(run_scenario(data, f"{base}/signature/standard", secret=secret))

    assert state["requests"] == []


def test_response_lost_after_business_commit_retries_without_duplicate_effect(receiver) -> None:
    base, state = receiver
    data = validate_scenario({
        "version": 1,
        "name": "commit succeeded but acknowledgement was lost",
        "retry": {"max_attempts": 2, "backoff_ms": 1},
        "events": [{
            "id": "order.created",
            "event_id": "evt_commit_ack_lost",
            "payload": {"order_id": 42},
            "expect_json": {"duplicate": True, "effect_applied": False},
        }],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 1},
        },
    })

    result = asyncio.run(run_scenario(data, f"{base}/commit-then-drop"))

    assert result["summary"]["success"] is True
    attempts = result["deliveries"][0]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["status_code"] is None
    assert attempts[0]["error"].startswith("request error:")
    assert attempts[1]["status_code"] == 200
    assert result["verification"]["passed"] is True
    run_id = result["run_id"]
    assert len([key for key in state["effects"] if key[0] == run_id]) == 1
    assert state["attempts"][(run_id, "evt_commit_ack_lost")] == 2


def test_business_state_verification_checks_unique_side_effects(receiver) -> None:
    base, state = receiver
    data = validate_scenario({
        "version": 1,
        "name": "real idempotency check",
        "delivery": {"duplicate_count": 2},
        "events": [{
            "id": "order.created",
            "event_id": "evt_order_42",
            "payload": {"order_id": 42},
            "expect_json": {"duplicate": False},
            "expect_duplicate_json": {"duplicate": True},
        }],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 1},
        },
    })

    result = asyncio.run(run_scenario(data, base))

    assert result["summary"]["success"] is True
    assert result["summary"]["total_deliveries"] == 3
    assert result["verification"]["method"] == "GET"
    assert result["verification"]["passed"] is True
    run_id = result["run_id"]
    assert len([key for key in state["effects"] if key[0] == run_id]) == 1


def test_failed_business_state_verification_fails_the_run(receiver) -> None:
    base, _ = receiver
    data = validate_scenario({
        "version": 1,
        "name": "business state mismatch",
        "events": [{
            "id": "order.created",
            "event_id": "evt_order_43",
            "payload": {"order_id": 43},
        }],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 2},
        },
    })

    result = asyncio.run(run_scenario(data, base))

    assert result["deliveries"][0]["passed"] is True
    assert result["verification"]["passed"] is False
    assert result["verification"]["expected_json"] is False
    assert result["summary"]["success"] is False


def test_verification_url_obeys_local_only_safety_before_delivery(receiver) -> None:
    base, state = receiver
    data = validate_scenario({
        "version": 1,
        "name": "unsafe probe",
        "events": [{"id": "ping", "event_id": "evt_unsafe", "payload": {}}],
        "verify": {
            "url": "http://8.8.8.8/state",
            "expect_json": {},
        },
    })

    with pytest.raises(ValueError, match="Remote IP"):
        asyncio.run(run_scenario(data, base))

    assert state["requests"] == []


def test_verification_query_parameters_are_not_in_results(receiver) -> None:
    base, _ = receiver
    data = validate_scenario({
        "version": 1,
        "name": "redacted probe URL",
        "events": [{"id": "ping", "event_id": "evt_redacted", "payload": {}}],
        "verify": {
            "url": f"{base}/state?api_key=do-not-store-this",
            "expect_json": {"processed_events": 1},
        },
    })
    result = asyncio.run(run_scenario(data, base))

    assert result["verification"]["target"] == f"{base}/state"
    assert "do-not-store-this" not in str(result)


def test_verify_bearer_auth_is_sent_but_never_reported(receiver, monkeypatch) -> None:
    base, state = receiver
    token = "bearer-super-secret-token"
    monkeypatch.setenv("HOOKSTORM_VERIFY_TOKEN", token)
    data = validate_scenario({
        "version": 1,
        "name": "bearer verify auth",
        "events": [{"id": "ping", "event_id": "evt_bearer", "payload": {}}],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 1},
            "auth": {"type": "bearer", "token_env": "HOOKSTORM_VERIFY_TOKEN"},
        },
    })

    result = asyncio.run(run_scenario(data, base))

    assert result["summary"]["success"] is True
    headers = state["verification_requests"][0]["headers"]
    assert headers["Authorization"] == f"Bearer {token}"
    assert token not in render_json(result)
    assert token not in render_html(result)
    assert token not in render_junit(result)


def test_verify_api_key_auth_supports_custom_header(receiver, monkeypatch) -> None:
    base, state = receiver
    key = "api-key-super-secret"
    monkeypatch.setenv("HOOKSTORM_VERIFY_API_KEY", key)
    data = validate_scenario({
        "version": 1,
        "name": "api key verify auth",
        "events": [{"id": "ping", "event_id": "evt_api_key", "payload": {}}],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 1},
            "auth": {
                "type": "api_key",
                "key_env": "HOOKSTORM_VERIFY_API_KEY",
                "header": "X-Client-Key",
            },
        },
    })

    result = asyncio.run(run_scenario(data, base))

    assert result["summary"]["success"] is True
    headers = state["verification_requests"][0]["headers"]
    assert headers["X-Client-Key"] == key
    assert key not in str(result)


def test_missing_verify_auth_fails_before_sending_webhooks(receiver, monkeypatch) -> None:
    base, state = receiver
    monkeypatch.delenv("HOOKSTORM_VERIFY_TOKEN", raising=False)
    data = validate_scenario({
        "version": 1,
        "name": "missing verify auth",
        "events": [{"id": "ping", "event_id": "evt_missing_auth", "payload": {}}],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 1},
            "auth": {"type": "bearer", "token_env": "HOOKSTORM_VERIFY_TOKEN"},
        },
    })

    with pytest.raises(ValueError, match="HOOKSTORM_VERIFY_TOKEN"):
        asyncio.run(run_scenario(data, base))

    assert state["requests"] == []
    assert state["verification_requests"] == []


@pytest.mark.parametrize("credential", ["contains whitespace", "invalid\r\nX-Injected: yes"])
def test_invalid_verify_auth_value_fails_before_sending_webhooks(
    receiver, monkeypatch, credential: str
) -> None:
    base, state = receiver
    monkeypatch.setenv("HOOKSTORM_VERIFY_TOKEN", credential)
    data = validate_scenario({
        "version": 1,
        "name": "invalid verify auth",
        "events": [{"id": "ping", "event_id": "evt_invalid_auth", "payload": {}}],
        "verify": {
            "url": f"{base}/state",
            "expect_json": {"processed_events": 1},
            "auth": {"type": "bearer", "token_env": "HOOKSTORM_VERIFY_TOKEN"},
        },
    })

    with pytest.raises(ValueError, match="printable ASCII"):
        asyncio.run(run_scenario(data, base))

    assert state["requests"] == []
    assert state["verification_requests"] == []


def test_authenticated_remote_verification_requires_https(receiver, monkeypatch) -> None:
    base, state = receiver
    monkeypatch.setenv("HOOKSTORM_VERIFY_TOKEN", "test-token")
    data = validate_scenario({
        "version": 1,
        "name": "remote http verify auth",
        "events": [{"id": "ping", "event_id": "evt_remote_http", "payload": {}}],
        "verify": {
            "url": "http://example.com/state",
            "expect_json": {},
            "auth": {"type": "bearer", "token_env": "HOOKSTORM_VERIFY_TOKEN"},
        },
    })

    with pytest.raises(ValueError, match="require HTTPS"):
        asyncio.run(run_scenario(data, base, allow_remote=True))

    assert state["requests"] == []
    assert state["verification_requests"] == []




def test_scheduled_event_waits_before_acquiring_concurrency_slot(receiver) -> None:
    base, state = receiver
    data = validate_scenario({
        "version": 1,
        "name": "independent event schedule",
        "delivery": {"concurrency": 1},
        "events": [
            {
                "id": "delayed.event",
                "event_id": "evt_delayed",
                "schedule_after_ms": 150,
                "payload": {},
            },
            {
                "id": "immediate.event",
                "event_id": "evt_immediate",
                "schedule_after_ms": 0,
                "payload": {},
            },
        ],
    })

    result = asyncio.run(run_scenario(data, base))

    assert result["summary"]["success"] is True
    assert [item["event_id"] for item in state["requests"]] == [
        "evt_immediate",
        "evt_delayed",
    ]
    assert result["deliveries"][0]["schedule_after_ms"] == 150
    assert result["deliveries"][1]["schedule_after_ms"] == 0
    assert result["deliveries"][0]["attempts"][0]["elapsed_ms"] >= 0
    assert "scheduled +150 ms" in render_html(result)
