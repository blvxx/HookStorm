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
    data["events"][0]["event_id"] = "evt_1\\r\\nX-Injected: yes"
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
