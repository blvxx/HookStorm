from __future__ import annotations

import pytest

from hookstorm.config import ScenarioError, validate_scenario


def valid_scenario() -> dict:
    return {
        "version": 1,
        "name": "basic",
        "events": [{"id": "ping", "event_id": "evt_1", "payload": {"ok": True}}],
    }


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



@pytest.mark.parametrize("provider", ["generic", "stripe", "github", "shopify"])
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
