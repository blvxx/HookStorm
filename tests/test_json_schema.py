from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from hookstorm.config import load_scenario
from hookstorm.scenario_schema import read_scenario_schema

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_schema_is_valid_json_schema_2020_12() -> None:
    schema = json.loads(read_scenario_schema())
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "urn:hookstorm:scenario:v1"


def test_schema_accepts_all_shipped_yaml_scenarios() -> None:
    schema = json.loads(read_scenario_schema())
    validator = Draft202012Validator(schema)
    scenario_files = sorted((ROOT / "examples").rglob("*.yaml"))
    assert scenario_files, "Expected repository example scenarios"

    for scenario_file in scenario_files:
        normalized = load_scenario(scenario_file)
        errors = list(validator.iter_errors(normalized))
        assert not errors, (
            f"{scenario_file.relative_to(ROOT)} does not match the published schema:\n"
            + "\n".join(error.message for error in errors)
        )


def test_schema_rejects_unknown_root_properties() -> None:
    schema = json.loads(read_scenario_schema())
    validator = Draft202012Validator(schema)
    invalid = {
        "version": 1,
        "name": "unknown root key",
        "events": [{"id": "ping", "event_id": "evt_schema", "payload": {}}],
        "unrecognized": True,
    }

    errors = list(validator.iter_errors(invalid))
    assert any("Additional properties are not allowed" in error.message for error in errors)


def test_provider_profile_allows_generated_event_defaults() -> None:
    schema = json.loads(read_scenario_schema())
    validator = Draft202012Validator(schema)
    provider_scenario = {
        "version": 1,
        "name": "provider defaults",
        "provider": "stripe",
        "events": [{"payload": {"type": "payment_intent.succeeded"}}],
    }

    assert not list(validator.iter_errors(provider_scenario))

def test_schema_accepts_all_request_fault_injection_options() -> None:
    schema = json.loads(read_scenario_schema())
    validator = Draft202012Validator(schema)
    request_faults = [
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
    scenario = {
        "version": 1,
        "name": "request fault schema",
        "retry": {"max_attempts": 10},
        "events": [
            {
                "id": "request.test",
                "event_id": "evt_request_schema",
                "payload": {},
                "delivery": {
                    "fault_injection": {
                        name: [index + 1] for index, name in enumerate(request_faults)
                    }
                },
            }
        ],
    }

    assert not list(validator.iter_errors(scenario))
