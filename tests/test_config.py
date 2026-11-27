from __future__ import annotations

import hashlib
import json

import pytest

from hookstorm.config import (
    ScenarioError,
    load_scenario,
    scenario_fingerprint,
    validate_scenario,
)


def valid_scenario() -> dict:
    return {
        "version": 1,
        "name": "basic",
        "events": [{"id": "ping", "event_id": "evt_1", "payload": {"ok": True}}],
    }


def test_scenario_fingerprint_is_stable_and_ignores_display_metadata() -> None:
    first = validate_scenario(valid_scenario())
    second = validate_scenario({**valid_scenario(), "name": "renamed", "description": "new notes"})

    assert scenario_fingerprint(first) == scenario_fingerprint(second)
    assert len(scenario_fingerprint(first)) == 64


def test_scenario_fingerprint_changes_with_behavior_configuration() -> None:
    first = validate_scenario(valid_scenario())
    changed_data = valid_scenario()
    changed_data["events"][0]["payload"] = {"ok": False}
    second = validate_scenario(changed_data)

    assert scenario_fingerprint(first) != scenario_fingerprint(second)


def test_valid_scenario_gets_defaults() -> None:
    scenario = validate_scenario(valid_scenario())
    assert scenario["retry"]["max_attempts"] == 1
    assert scenario["events"][0]["expect_status"] == [200, 201, 202, 204]


@pytest.mark.parametrize("bad_version", [None, 0, 2, "1"])
def test_rejects_unknown_scenario_versions(bad_version: object) -> None:
    data = valid_scenario()
    data["version"] = bad_version
    with pytest.raises(ScenarioError, match="version"):
        validate_scenario(data)


def test_rejects_unknown_keys() -> None:
    data = valid_scenario()
    data["shell"] = "unexpected"
    with pytest.raises(ScenarioError, match="Unknown key"):
        validate_scenario(data)


def test_rejects_invalid_headers() -> None:
    data = valid_scenario()
    data["events"][0]["headers"] = {"Host": "attacker.invalid"}
    with pytest.raises(ScenarioError, match="may not override"):
        validate_scenario(data)


@pytest.mark.parametrize("value", ["snowman \u2603", "bad\x00value", "bad\x1fvalue", "bad\x7fvalue"])
def test_rejects_non_ascii_or_control_header_values(value: str) -> None:
    data = valid_scenario()
    data["events"][0]["headers"] = {"X-Custom": value}
    with pytest.raises(ScenarioError, match="invalid HTTP header"):
        validate_scenario(data)


def test_rejects_case_insensitive_duplicate_custom_header_names() -> None:
    data = valid_scenario()
    data["events"][0]["headers"] = {
        "X-Delivery-Mode": "first",
        "x-delivery-mode": "second",
    }

    with pytest.raises(ScenarioError, match="case-insensitive duplicate"):
        validate_scenario(data)


def test_accepts_horizontal_tab_in_http_header_value() -> None:
    data = valid_scenario()
    data["events"][0]["headers"] = {"X-Custom": "first\tsecond"}
    assert validate_scenario(data)["events"][0]["headers"]["X-Custom"] == "first\tsecond"


@pytest.mark.parametrize("field", ["event_id", "id"])
@pytest.mark.parametrize("value", ["event-\u2603", "event-\x00", "event-\x7f", "event-\tvalue"])
def test_rejects_unsafe_event_header_values(field: str, value: str) -> None:
    data = valid_scenario()
    data["events"][0][field] = value
    with pytest.raises(ScenarioError, match="ASCII header-safe"):
        validate_scenario(data)


def test_rejects_duplicate_event_ids_within_scenario() -> None:
    data = valid_scenario()
    data["events"].append({
        "id": "invoice.created",
        "event_id": "evt_1",
        "payload": {"invoice": 1},
    })
    with pytest.raises(ScenarioError, match="event_id must be unique"):
        validate_scenario(data)


def test_duplicate_delivery_uses_one_unique_event_id() -> None:
    data = valid_scenario()
    data["delivery"] = {"duplicate_count": 2}
    scenario = validate_scenario(data)
    assert scenario["events"][0]["event_id"] == "evt_1"
    assert scenario["delivery"]["duplicate_count"] == 2


def test_rejects_excessive_duplicate_count() -> None:
    data = valid_scenario()
    data["delivery"] = {"duplicate_count": 1000}
    with pytest.raises(ScenarioError, match="duplicate_count"):
        validate_scenario(data)


def test_rejects_non_json_payload() -> None:
    from datetime import date

    data = valid_scenario()
    data["events"][0]["payload"] = {"when": date.today()}
    with pytest.raises(ScenarioError, match="JSON-compatible"):
        validate_scenario(data)



def test_rejects_non_integer_scenario_version() -> None:
    data = valid_scenario()
    data["version"] = 1.0
    with pytest.raises(ScenarioError, match="version"):
        validate_scenario(data)


def test_rejects_event_id_with_newline() -> None:
    data = valid_scenario()
    data["events"][0]["event_id"] = "evt_1\r\nX-Injected: yes"
    with pytest.raises(ScenarioError, match="header-safe"):
        validate_scenario(data)


def test_rejects_payload_over_one_megabyte() -> None:
    data = valid_scenario()
    data["events"][0]["payload"] = {"blob": "x" * (1_048_576 + 1)}
    with pytest.raises(ScenarioError, match="1 MiB"):
        validate_scenario(data)


def test_rejects_scenarios_with_excessive_worst_case_attempts() -> None:
    data = valid_scenario()
    data["delivery"] = {"duplicate_count": 100}
    data["retry"] = {"max_attempts": 10}
    data["events"] = [
        {"id": f"event.{index}", "event_id": f"evt_{index}", "payload": {}}
        for index in range(200)
    ]
    with pytest.raises(ScenarioError, match="20,000"):
        validate_scenario(data)


def test_rejects_non_string_order_and_signature_modes() -> None:
    data = valid_scenario()
    data["delivery"] = {"order": ["reverse"]}
    with pytest.raises(ScenarioError, match="order"):
        validate_scenario(data)



def test_accepts_bounded_global_concurrency() -> None:
    data = valid_scenario()
    data["delivery"] = {"concurrency": 4}
    assert validate_scenario(data)["delivery"]["concurrency"] == 4


@pytest.mark.parametrize("value", [0, 51, True, 1.5, "2"])
def test_rejects_invalid_concurrency(value: object) -> None:
    data = valid_scenario()
    data["delivery"] = {"concurrency": value}
    with pytest.raises(ScenarioError, match="concurrency"):
        validate_scenario(data)


def test_concurrency_is_global_not_per_event() -> None:
    data = valid_scenario()
    data["events"][0]["delivery"] = {"concurrency": 2}
    with pytest.raises(ScenarioError, match="Unknown key"):
        validate_scenario(data)



def test_normalizes_expected_json_using_json_semantics() -> None:
    data = valid_scenario()
    data["events"][0]["expect_json"] = {1: {"accepted": True}}
    normalized = validate_scenario(data)
    assert normalized["events"][0]["expect_json"] == {"1": {"accepted": True}}


def test_rejects_non_json_expected_values() -> None:
    from datetime import date

    data = valid_scenario()
    data["events"][0]["expect_json"] = {"created_at": date.today()}
    with pytest.raises(ScenarioError, match="JSON-compatible"):
        validate_scenario(data)



@pytest.mark.parametrize("provider", ["generic", "stripe", "github", "shopify", "standard_webhooks"])
def test_accepts_supported_signature_providers(provider: str) -> None:
    data = valid_scenario()
    data["delivery"] = {"signature_provider": provider, "signature": "valid"}
    assert validate_scenario(data)["delivery"]["signature_provider"] == provider


@pytest.mark.parametrize("provider", ["Stripe", "github_v1", "", None, 1])
def test_rejects_unknown_signature_providers(provider: object) -> None:
    data = valid_scenario()
    data["delivery"] = {"signature_provider": provider}
    with pytest.raises(ScenarioError, match="signature_provider"):
        validate_scenario(data)


@pytest.mark.parametrize("offset", [-86_401, 86_401, True, 1.5, "30"])
def test_rejects_invalid_stripe_timestamp_offsets(offset: object) -> None:
    data = valid_scenario()
    data["delivery"] = {
        "signature_provider": "stripe",
        "signature_timestamp_offset_seconds": offset,
    }
    with pytest.raises(ScenarioError, match="signature_timestamp_offset_seconds"):
        validate_scenario(data)


def test_rejects_timestamp_offset_for_non_stripe_provider() -> None:
    data = valid_scenario()
    data["delivery"] = {
        "signature_provider": "github",
        "signature_timestamp_offset_seconds": 30,
    }
    with pytest.raises(ScenarioError, match="only be used with signature_provider: stripe"):
        validate_scenario(data)


def test_standard_webhooks_timestamp_offset_is_supported() -> None:
    data = valid_scenario()
    data["delivery"] = {
        "signature_provider": "standard_webhooks",
        "signature": "valid",
        "signature_timestamp_offset_seconds": -60,
    }
    normalized = validate_scenario(data)
    assert normalized["delivery"]["signature_timestamp_offset_seconds"] == -60


def test_standard_webhooks_event_id_rejects_period_delimiter() -> None:
    data = valid_scenario()
    data["delivery"] = {"signature_provider": "standard_webhooks", "signature": "valid"}
    data["events"][0]["event_id"] = "evt.with.periods"

    with pytest.raises(ScenarioError, match="must not contain"):
        validate_scenario(data)


def test_rejects_standard_webhooks_managed_headers() -> None:
    data = valid_scenario()
    data["events"][0]["headers"] = {"webhook-signature": "attacker"}
    with pytest.raises(ScenarioError, match="may not override"):
        validate_scenario(data)


def test_rejects_timestamp_offset_without_explicit_stripe_provider() -> None:
    data = valid_scenario()
    data["delivery"] = {"signature_timestamp_offset_seconds": 30}
    with pytest.raises(ScenarioError, match="only be used with signature_provider: stripe"):
        validate_scenario(data)



def test_signature_provider_can_be_overridden_per_event() -> None:
    data = valid_scenario()
    data["delivery"] = {"signature_provider": "generic", "signature": "valid"}
    data["events"][0]["delivery"] = {"signature_provider": "shopify"}
    normalized = validate_scenario(data)
    assert normalized["events"][0]["delivery"]["signature_provider"] == "shopify"



def test_event_stripe_timestamp_offset_inherits_global_provider() -> None:
    data = valid_scenario()
    data["delivery"] = {"signature_provider": "stripe", "signature": "valid"}
    data["events"][0]["delivery"] = {"signature_timestamp_offset_seconds": -60}
    normalized = validate_scenario(data)
    assert normalized["events"][0]["delivery"]["signature_timestamp_offset_seconds"] == -60



def test_validates_business_state_verification_defaults() -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1:8000/__hookstorm/state",
        "expect_json": {"processed_events": 1},
    }
    verify = validate_scenario(data)["verify"]
    assert verify["expect_status"] == [200]
    assert verify["timeout_ms"] == 5_000
    assert verify["expect_json"] == {"processed_events": 1}


def test_verify_is_optional_and_defaults_to_none() -> None:
    assert validate_scenario(valid_scenario())["verify"] is None


@pytest.mark.parametrize("url", ["", "/state", "ftp://localhost/state", "http://user:pass@localhost/state", "http://localhost/state#fragment"])
def test_rejects_invalid_verification_urls(url: str) -> None:
    data = valid_scenario()
    data["verify"] = {"url": url, "expect_json": {"processed_events": 1}}
    with pytest.raises(ScenarioError, match="verify.url"):
        validate_scenario(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1/state",
        "http://bad host/state",
        "http://localhost:0/state",
        "http://localhost:not-a-port/state",
    ],
)
def test_rejects_malformed_verification_host_and_port(url: str) -> None:
    data = valid_scenario()
    data["verify"] = {"url": url, "expect_json": {}}
    with pytest.raises(ScenarioError, match="verify.url"):
        validate_scenario(data)


def test_rejects_unknown_verification_keys() -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1/state",
        "expect_json": {},
        "shell": "echo unsafe",
    }
    with pytest.raises(ScenarioError, match="Unknown key"):
        validate_scenario(data)


@pytest.mark.parametrize("status", [[], [0], [600], [True], "200"])
def test_rejects_invalid_verification_status(status: object) -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1/state",
        "expect_status": status,
        "expect_json": {},
    }
    with pytest.raises(ScenarioError, match="verify.expect_status"):
        validate_scenario(data)


def test_requires_json_object_for_verification() -> None:
    data = valid_scenario()
    data["verify"] = {"url": "http://127.0.0.1/state", "expect_json": [1]}
    with pytest.raises(ScenarioError, match="verify.expect_json"):
        validate_scenario(data)


@pytest.mark.parametrize("timeout", [0, 300_001, True, 1.5, "100"])
def test_rejects_invalid_verification_timeout(timeout: object) -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1/state",
        "expect_json": {},
        "timeout_ms": timeout,
    }
    with pytest.raises(ScenarioError, match="verify.timeout_ms"):
        validate_scenario(data)


def test_validates_bearer_verification_auth_by_environment_name() -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1:8000/state",
        "expect_json": {},
        "auth": {"type": "bearer", "token_env": "HOOKSTORM_VERIFY_TOKEN"},
    }
    assert validate_scenario(data)["verify"]["auth"] == {
        "type": "bearer",
        "token_env": "HOOKSTORM_VERIFY_TOKEN",
    }


def test_validates_api_key_verification_auth_defaults() -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1:8000/state",
        "expect_json": {},
        "auth": {"type": "api_key", "key_env": "HOOKSTORM_VERIFY_API_KEY"},
    }
    assert validate_scenario(data)["verify"]["auth"] == {
        "type": "api_key",
        "key_env": "HOOKSTORM_VERIFY_API_KEY",
        "header": "X-API-Key",
    }


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "basic", "token_env": "HOOKSTORM_TOKEN"},
        {"type": "bearer", "token_env": "BAD-NAME"},
        {"type": "bearer", "token_env": ""},
        {"type": "api_key", "key_env": "HOOKSTORM_KEY", "header": "bad header"},
        {"type": "api_key", "key_env": "HOOKSTORM_KEY", "header": "X" * 257},
        {"type": "api_key", "key_env": "HOOKSTORM_KEY", "header": "Authorization"},
        {"type": "api_key", "key_env": "HOOKSTORM_KEY", "header": "X-HookStorm-Run"},
        {"type": "api_key", "key_env": "HOOKSTORM_KEY", "unexpected": "value"},
    ],
)
def test_rejects_invalid_verification_auth(auth: dict) -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1:8000/state",
        "expect_json": {},
        "auth": auth,
    }
    with pytest.raises(ScenarioError, match="verify.auth"):
        validate_scenario(data)


def test_rejects_non_mapping_verification_auth() -> None:
    data = valid_scenario()
    data["verify"] = {
        "url": "http://127.0.0.1:8000/state",
        "expect_json": {},
        "auth": "HOOKSTORM_TOKEN",
    }
    with pytest.raises(ScenarioError, match="verify.auth"):
        validate_scenario(data)




def test_per_event_schedule_defaults_to_zero_and_accepts_bounded_offset() -> None:
    data = valid_scenario()
    normalized = validate_scenario(data)
    assert normalized["events"][0]["schedule_after_ms"] == 0

    data["events"][0]["schedule_after_ms"] = 300_000
    normalized = validate_scenario(data)
    assert normalized["events"][0]["schedule_after_ms"] == 300_000


@pytest.mark.parametrize("offset", [-1, 300_001, True, 1.5, "100"])
def test_rejects_invalid_per_event_schedule_offset(offset: object) -> None:
    data = valid_scenario()
    data["events"][0]["schedule_after_ms"] = offset
    with pytest.raises(ScenarioError, match="schedule_after_ms"):
        validate_scenario(data)



def test_default_run_timeout_preserves_legacy_fingerprint_shape() -> None:
    scenario = validate_scenario(valid_scenario())
    legacy_behavior = {
        key: scenario.get(key)
        for key in ("version", "delivery", "retry", "events", "verify")
    }
    canonical = json.dumps(
        legacy_behavior,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    assert scenario_fingerprint(scenario) == hashlib.sha256(canonical).hexdigest()


def test_run_timeout_defaults_and_is_included_in_fingerprint() -> None:
    default = validate_scenario(valid_scenario())
    extended = validate_scenario({**valid_scenario(), "run_timeout_ms": 900_000})

    assert default["run_timeout_ms"] == 3_600_000
    assert scenario_fingerprint(default) != scenario_fingerprint(extended)


@pytest.mark.parametrize("value", [0, 99, 86_400_001, True, 1.5, "100"])
def test_rejects_invalid_run_timeout(value: object) -> None:
    data = valid_scenario()
    data["run_timeout_ms"] = value
    with pytest.raises(ScenarioError, match="run_timeout_ms"):
        validate_scenario(data)


def test_rejects_scenarios_whose_worst_case_exceeds_run_timeout() -> None:
    data = valid_scenario()
    data["run_timeout_ms"] = 1_000
    data["delivery"] = {"timeout_ms": 600}
    data["retry"] = {"max_attempts": 2}

    with pytest.raises(ScenarioError, match="worst-case request-time budget"):
        validate_scenario(data)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "version: 1\nversion: 1\nname: duplicate\nevents: [{id: ping, event_id: evt_1}]\n",
        "version: 1\nname: duplicate\ndelivery:\n  timeout_ms: 100\n  timeout_ms: 200\nevents: [{id: ping, event_id: evt_1}]\n",
    ],
)
def test_load_scenario_rejects_duplicate_yaml_keys(tmp_path, yaml_text: str) -> None:
    scenario_file = tmp_path / "duplicate.yaml"
    scenario_file.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ScenarioError, match="Duplicate YAML key"):
        load_scenario(scenario_file)



def test_validates_deterministic_fault_injection_attempts() -> None:
    data = valid_scenario()
    data["retry"] = {"max_attempts": 3}
    data["delivery"] = {
        "fault_injection": {
            "connect_error_attempts": [1],
            "response_loss_attempts": [2],
            "response_body_error_attempts": [3],
        }
    }

    normalized = validate_scenario(data)

    assert normalized["delivery"]["fault_injection"] == {
        "connect_error_attempts": [1],
        "response_loss_attempts": [2],
        "response_body_error_attempts": [3],
    }


def test_fault_injection_can_be_overridden_per_event() -> None:
    data = valid_scenario()
    data["retry"] = {"max_attempts": 3}
    data["delivery"] = {
        "fault_injection": {"connect_error_attempts": [1]}
    }
    data["events"][0]["delivery"] = {
        "fault_injection": {"response_loss_attempts": [2]}
    }

    normalized = validate_scenario(data)

    assert normalized["events"][0]["delivery"]["fault_injection"] == {
        "connect_error_attempts": [],
        "response_loss_attempts": [2],
    }


@pytest.mark.parametrize(
    "fault_plan",
    [
        [],
        {"unknown": [1]},
        {"connect_error_attempts": 1},
        {"connect_error_attempts": [True]},
        {"connect_error_attempts": [0]},
        {"response_loss_attempts": [11]},
        {"response_body_error_attempts": [True]},
        {"response_body_error_attempts": [1, 1]},
        {"connect_error_attempts": [1, 1]},
        {"connect_error_attempts": [2], "response_loss_attempts": [2]},
        {"connect_error_attempts": [2], "response_body_error_attempts": [2]},
    ],
)
def test_rejects_invalid_fault_injection_plans(fault_plan: object) -> None:
    data = valid_scenario()
    data["retry"] = {"max_attempts": 3}
    data["delivery"] = {"fault_injection": fault_plan}

    with pytest.raises(ScenarioError, match="fault_injection"):
        validate_scenario(data)


@pytest.mark.parametrize(
    "fault_field",
    [
        "connect_error_attempts",
        "response_loss_attempts",
        "response_body_error_attempts",
        "malformed_json_attempts",
        "wrong_content_type_attempts",
        "gzip_body_attempts",
        "deflate_body_attempts",
        "invalid_gzip_attempts",
        "invalid_deflate_attempts",
        "chunked_transfer_attempts",
        "malformed_chunked_attempts",
        "partial_upload_attempts",
        "content_length_mismatch_attempts",
    ],
)
def test_fault_injection_attempts_cannot_exceed_retry_budget(fault_field: str) -> None:
    data = valid_scenario()
    data["retry"] = {"max_attempts": 2}
    data["delivery"] = {"fault_injection": {fault_field: [3]}}

    with pytest.raises(ScenarioError, match="must not exceed retry.max_attempts=2"):
        validate_scenario(data)



@pytest.mark.parametrize(
    ("provider", "event_type", "prefix"),
    [
        ("stripe", "payment_intent.succeeded", "evt_hookstorm_stripe_"),
        ("github", "issues.opened", "evt_hookstorm_github_"),
        ("shopify", "orders/create", "evt_hookstorm_shopify_"),
        ("standard_webhooks", "order.created", "evt_hookstorm_standard_"),
    ],
)
def test_provider_profile_fills_event_defaults_and_signed_delivery(
    provider: str, event_type: str, prefix: str
) -> None:
    data = valid_scenario()
    data.pop("events")
    data["provider"] = provider
    data["events"] = [{}]

    normalized = validate_scenario(data)
    event = normalized["events"][0]

    assert normalized["provider"] == provider
    assert normalized["delivery"]["signature"] == "valid"
    assert normalized["delivery"]["signature_provider"] == provider
    assert event["id"] == event_type
    assert event["event_id"].startswith(prefix)
    assert event["payload"]
    assert normalized["events"][0]["delivery"]["signature"] == "valid"
    assert normalized["events"][0]["delivery"]["signature_provider"] == provider


def test_provider_profile_respects_explicit_event_overrides() -> None:
    data = valid_scenario()
    data["provider"] = "github"
    data["events"][0]["id"] = "pull_request.opened"
    data["events"][0]["event_id"] = "evt_custom_github"
    data["events"][0]["payload"] = {"action": "opened", "pull_request": {"number": 7}}

    normalized = validate_scenario(data)
    event = normalized["events"][0]

    assert event["id"] == "pull_request.opened"
    assert event["event_id"] == "evt_custom_github"
    assert event["payload"] == {"action": "opened", "pull_request": {"number": 7}}
    assert event["headers"]["X-GitHub-Event"] == "pull_request"
    assert "X-GitHub-Delivery" in event["headers"]


def test_provider_profile_requires_valid_known_provider() -> None:
    data = valid_scenario()
    data["provider"] = "unknown"
    with pytest.raises(ScenarioError, match="provider must be"):
        validate_scenario(data)


def test_provider_profile_fingerprint_is_behavior_defining_but_legacy_fingerprint_is_stable() -> None:
    legacy = validate_scenario(valid_scenario())
    profiled_data = valid_scenario()
    profiled_data["provider"] = "stripe"
    profiled = validate_scenario(profiled_data)

    assert scenario_fingerprint(legacy) != scenario_fingerprint(profiled)
    assert "provider" not in legacy



def test_github_profile_keeps_payload_action_and_headers_in_sync() -> None:
    data = valid_scenario()
    data["provider"] = "github"
    data["events"] = [{}]

    normalized = validate_scenario(data)
    event = normalized["events"][0]

    assert event["id"] == "issues.opened"
    assert event["payload"]["action"] == "opened"
    assert event["headers"]["X-GitHub-Event"] == "issues"
    assert event["headers"]["X-GitHub-Delivery"]


def test_github_profile_updates_action_when_event_type_is_overridden() -> None:
    data = valid_scenario()
    data["provider"] = "github"
    data["events"] = [{"id": "issues.closed"}]

    event = validate_scenario(data)["events"][0]

    assert event["payload"]["action"] == "closed"
    assert event["headers"]["X-GitHub-Event"] == "issues"


def test_standard_webhooks_profile_has_standard_event_envelope() -> None:
    data = valid_scenario()
    data["provider"] = "standard_webhooks"
    data["events"] = [{}]

    event = validate_scenario(data)["events"][0]

    assert event["payload"]["id"] == event["event_id"]
    assert event["payload"]["type"] == "order.created"
    assert event["payload"]["created_at"].endswith("Z")
    assert "data" in event["payload"]


def test_provider_managed_headers_cannot_be_overridden() -> None:
    data = valid_scenario()
    data["provider"] = "shopify"
    data["events"][0]["headers"] = {"X-Shopify-Webhook-Id": "forged"}

    with pytest.raises(ScenarioError, match="may not override"):
        validate_scenario(data)



def test_empty_payload_is_a_valid_json_object() -> None:
    data = valid_scenario()
    data["events"][0]["payload"] = {}

    normalized = validate_scenario(data)

    assert normalized["events"][0]["payload"] == {}


def test_payload_at_exact_one_megabyte_boundary_is_accepted() -> None:
    data = valid_scenario()
    data["events"][0]["payload"] = {"blob": "x" * (1_048_576 - 11)}

    normalized = validate_scenario(data)

    assert len(json.dumps(
        normalized["events"][0]["payload"],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")) == 1_048_576


def test_scheduled_event_and_delivery_timeout_must_fit_run_timeout() -> None:
    data = valid_scenario()
    data["run_timeout_ms"] = 300
    data["delivery"] = {"timeout_ms": 200}
    data["events"][0]["schedule_after_ms"] = 150

    with pytest.raises(ScenarioError, match="worst-case request-time budget"):
        validate_scenario(data)

def test_request_fault_plans_are_validated_and_cannot_overlap() -> None:
    data = valid_scenario()
    data["retry"] = {"max_attempts": 10}
    field_names = [
        "malformed_json_attempts",
        "wrong_content_type_attempts",
        "gzip_body_attempts",
        "deflate_body_attempts",
        "invalid_gzip_attempts",
        "invalid_deflate_attempts",
        "chunked_transfer_attempts",
        "malformed_chunked_attempts",
        "partial_upload_attempts",
        "content_length_mismatch_attempts",
    ]
    data["events"][0]["delivery"] = {
        "fault_injection": {
            field: [index + 1] for index, field in enumerate(field_names)
        }
    }

    normalized = validate_scenario(data)
    assert all(
        normalized["events"][0]["delivery"]["fault_injection"][field] == [index + 1]
        for index, field in enumerate(field_names)
    )

    conflict = valid_scenario()
    conflict["events"][0]["delivery"] = {
        "fault_injection": {
            "malformed_json_attempts": [1],
            "partial_upload_attempts": [1],
        }
    }
    with pytest.raises(ScenarioError, match="cannot inject more than one fault"):
        validate_scenario(conflict)


def test_validate_scenario_rejects_excessive_json_nesting() -> None:
    data = valid_scenario()
    payload: object = None
    for _ in range(1_200):
        payload = [payload]
    data["events"][0]["payload"] = payload

    with pytest.raises(ScenarioError, match="nesting exceeds supported validation depth"):
        validate_scenario(data)


def test_load_scenario_rejects_excessive_yaml_nesting(tmp_path) -> None:
    scenario_file = tmp_path / "deep.yaml"
    nested = "[" * 1_200 + "null" + "]" * 1_200
    scenario_file.write_text(
        "version: 1\nname: deep\nevents:\n  - id: ping\n    event_id: evt_deep\n    payload: "
        + nested,
        encoding="utf-8",
    )

    with pytest.raises(ScenarioError, match="nesting exceeds parser limits"):
        load_scenario(scenario_file)


def test_load_scenario_rejects_excessive_yaml_aliases(tmp_path) -> None:
    lines = ["base: &base [one, two]"]
    lines.extend(f"alias_{index}: *base" for index in range(17))
    scenario_file = tmp_path / "aliases.yaml"
    scenario_file.write_text("\n".join(lines), encoding="utf-8")

    with pytest.raises(ScenarioError, match="more than 16 aliases"):
        load_scenario(scenario_file)


def test_rejects_scenario_with_excessive_aggregate_payload_bytes() -> None:
    data = valid_scenario()
    data["events"] = [
        {
            "id": f"event.{index}",
            "event_id": f"evt_{index}",
            "payload": {"blob": "x" * 1_000_000},
        }
        for index in range(17)
    ]

    with pytest.raises(ScenarioError, match="aggregate limit"):
        validate_scenario(data)


def test_load_scenario_rejects_oversized_source_file(tmp_path) -> None:
    from hookstorm.config import _MAX_SCENARIO_FILE_BYTES

    scenario_file = tmp_path / "oversized.yaml"
    scenario_file.write_bytes(b"x" * (_MAX_SCENARIO_FILE_BYTES + 1))

    with pytest.raises(ScenarioError, match="file exceeds"):
        load_scenario(scenario_file)
