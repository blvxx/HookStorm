from __future__ import annotations

import asyncio
import json
import os
import random
import stat

import pytest

from hookstorm.config import load_scenario, scenario_fingerprint, validate_scenario
from hookstorm.fuzz import (
    _write_case,
    minimize_json_payload,
    mutate_payload,
    run_fuzz,
    scenario_to_yaml,
)


def _base_scenario() -> dict:
    return validate_scenario(
        {
            "version": 1,
            "name": "fuzz test",
            "events": [
                {
                    "id": "test.ping",
                    "event_id": "evt_fuzz_test",
                    "payload": {"account": {"active": True}, "items": [1, 2]},
                }
            ],
        }
    )


def test_payload_mutation_is_reproducible_and_stays_json_compatible() -> None:
    payload = {"account": {"active": True}, "items": [1, 2]}
    first = mutate_payload(payload, random.Random("seed-123"), nonce="seed123_0001")
    second = mutate_payload(payload, random.Random("seed-123"), nonce="seed123_0001")

    assert first == second
    assert first[0] != payload
    json.dumps(first[0], allow_nan=False)


def test_minimizer_keeps_a_failure_predicate_and_respects_budget() -> None:
    async def predicate(candidate: object) -> bool:
        return isinstance(candidate, dict) and candidate.get("required") == "keep"

    minimized, attempts = asyncio.run(
        minimize_json_payload(
            {"required": "keep", "unused": {"large": [1, 2, 3]}},
            predicate,
            budget=20,
        )
    )

    assert minimized == {"required": "keep"}
    assert 1 <= attempts <= 20


def test_provider_profile_case_yaml_round_trips_without_managed_header_overrides(
    tmp_path,
) -> None:
    scenario = validate_scenario(
        {
            "version": 1,
            "name": "github fuzz profile",
            "provider": "github",
            "events": [{}],
        }
    )
    case_path = tmp_path / "github-profile.yaml"
    case_path.write_text(scenario_to_yaml(scenario), encoding="utf-8")

    replay = load_scenario(case_path)

    assert replay["provider"] == "github"
    assert replay["events"][0]["headers"]["X-GitHub-Event"] == "issues"
    assert replay["events"][0]["headers"]["X-GitHub-Delivery"]
    assert scenario_fingerprint(replay) == scenario_fingerprint(scenario)


def test_fuzz_runner_uses_unique_ids_and_is_reproducible() -> None:
    scenario = _base_scenario()

    async def fake_runner(candidate, target, **kwargs):
        return {
            "summary": {"success": True, "total_deliveries": 1, "passed": 1, "failed": 0},
            "run_id": "run-fixed",
            "scenario_fingerprint": scenario_fingerprint(candidate),
        }

    first = asyncio.run(
        run_fuzz(scenario, "http://127.0.0.1:8000/webhook", seed=17, iterations=5, runner=fake_runner)
    )
    second = asyncio.run(
        run_fuzz(scenario, "http://127.0.0.1:8000/webhook", seed=17, iterations=5, runner=fake_runner)
    )

    assert first["summary"]["success"] is True
    assert first["summary"]["executions"] == 5
    assert [case["event_id"] for case in first["cases"]] == [
        case["event_id"] for case in second["cases"]
    ]
    assert len({case["event_id"] for case in first["cases"]}) == 5
    assert [case["mutation"] for case in first["cases"]] == [
        case["mutation"] for case in second["cases"]
    ]


def test_fuzz_runner_saves_replayable_and_minimized_failure_cases(tmp_path) -> None:
    scenario = _base_scenario()

    async def failing_runner(candidate, target, **kwargs):
        return {
            "summary": {"success": False, "total_deliveries": 1, "passed": 0, "failed": 1},
            "run_id": "run-failure",
            "scenario_fingerprint": scenario_fingerprint(candidate),
        }

    cases_dir = tmp_path / "fuzz-cases"
    result = asyncio.run(
        run_fuzz(
            scenario,
            "http://127.0.0.1:8000/webhook",
            seed=5,
            iterations=1,
            minimize_budget=10,
            cases_dir=cases_dir,
            runner=failing_runner,
        )
    )

    case = result["cases"][0]
    assert result["summary"]["failed"] == 1
    assert case["case_file"]
    assert case["minimized_case_file"]
    replayable = load_scenario(case["case_file"])
    minimized = load_scenario(case["minimized_case_file"])
    assert replayable["events"][0]["event_id"]
    assert minimized["events"][0]["event_id"]
    assert case["minimization"]["attempts"] >= 1


@pytest.mark.parametrize("iterations,budget", [(0, 0), (501, 0), (1, 101), (True, 0), (500, 100)])
def test_fuzz_runner_rejects_unbounded_parameters(iterations: object, budget: int) -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            run_fuzz(
                _base_scenario(),
                "http://127.0.0.1:8000/webhook",
                iterations=iterations,
                minimize_budget=budget,
            )
        )


def test_standard_webhooks_fuzz_trial_keeps_event_id_envelope_in_sync() -> None:
    from hookstorm.fuzz import _apply_trial_event_ids

    scenario = validate_scenario(
        {
            "version": 1,
            "name": "standard webhooks fuzz",
            "provider": "standard_webhooks",
            "events": [{}],
        }
    )
    event = scenario["events"][0]
    old_event_id = event["event_id"]

    _apply_trial_event_ids(scenario, seed=23, iteration=1, trial=0)

    event = scenario["events"][0]
    assert event["event_id"] != old_event_id
    assert event["payload"]["id"] == event["event_id"]
    from hookstorm.runner import _signature_headers

    signed_headers = _signature_headers(
        b"{}",
        "whsec_aG9va3N0b3JtLXRlc3Qtc3RhbmRhcmQtd2ViaG9va3Mtc2VjcmV0",
        "valid",
        "standard_webhooks",
        event["event_id"],
        0,
    )
    assert signed_headers["webhook-id"] == event["event_id"]

def test_delivery_mutations_are_seeded_valid_and_replayable() -> None:
    base = validate_scenario(
        {
            "version": 1,
            "name": "delivery mutation fuzz",
            "retry": {"max_attempts": 3, "backoff_ms": 1},
            "events": [
                {"id": "first", "event_id": "evt_delivery_1", "payload": {"value": 1}},
                {"id": "second", "event_id": "evt_delivery_2", "payload": {"value": 2}},
            ],
        }
    )

    observed: list[dict] = []

    async def fake_runner(candidate, target, **kwargs):
        normalized = validate_scenario(candidate)
        observed.append(normalized)
        return {
            "summary": {"success": True, "total_deliveries": 2, "passed": 2, "failed": 0},
            "run_id": "run-delivery-fuzz",
            "scenario_fingerprint": scenario_fingerprint(normalized),
        }

    first = asyncio.run(
        run_fuzz(
            base,
            "http://127.0.0.1:8000/webhook",
            seed=42,
            iterations=4,
            vary_order=True,
            vary_duplicates=True,
            max_duplicates=2,
            vary_delay=True,
            max_delay_ms=50,
            vary_faults=True,
            runner=fake_runner,
        )
    )
    first_observed = list(observed)
    observed.clear()
    second = asyncio.run(
        run_fuzz(
            base,
            "http://127.0.0.1:8000/webhook",
            seed=42,
            iterations=4,
            vary_order=True,
            vary_duplicates=True,
            max_duplicates=2,
            vary_delay=True,
            max_delay_ms=50,
            vary_faults=True,
            runner=fake_runner,
        )
    )

    assert first["mode"] == "seeded-scenario-fuzz"
    assert first["dimensions"] == [
        "payload", "order", "duplicates", "delay", "transport_faults"
    ]
    assert first["summary"]["success"] is True
    assert first["worst_case_http_attempts"] == 48
    assert [case["delivery_mutations"] for case in first["cases"]] == [
        case["delivery_mutations"] for case in second["cases"]
    ]
    assert [scenario_fingerprint(item) for item in first_observed] == [
        scenario_fingerprint(item) for item in observed
    ]

    for candidate, case in zip(first_observed, first["cases"], strict=True):
        assert candidate["delivery"]["order"] in {"in_order", "reverse"}
        assert {item["dimension"] for item in case["delivery_mutations"]} == {
            "order", "duplicate_count", "delay_ms", "fault_injection"
        }
        for event in candidate["events"]:
            event_delivery = {**candidate["delivery"], **event["delivery"]}
            assert 0 <= event_delivery.get("duplicate_count", 0) <= 2
            assert 0 <= event_delivery.get("delay_ms", 0) <= 50
            fault_plan = event_delivery.get("fault_injection", {})
            attempts = [
                attempt
                for values in fault_plan.values()
                for attempt in values
            ]
            assert len(attempts) == len(set(attempts))
            assert all(1 <= attempt <= 3 for attempt in attempts)


def test_payload_only_fuzz_keeps_legacy_mode_and_no_delivery_changes() -> None:
    async def fake_runner(candidate, target, **kwargs):
        return {
            "summary": {"success": True, "total_deliveries": 1, "passed": 1, "failed": 0},
            "run_id": "run-legacy-fuzz",
            "scenario_fingerprint": scenario_fingerprint(candidate),
        }

    result = asyncio.run(
        run_fuzz(
            _base_scenario(),
            "http://127.0.0.1:8000/webhook",
            iterations=1,
            runner=fake_runner,
        )
    )

    assert result["mode"] == "seeded-payload-fuzz"
    assert result["dimensions"] == ["payload"]
    assert result["cases"][0]["delivery_mutations"] == []


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("max_duplicates", -1),
        ("max_duplicates", 101),
        ("max_delay_ms", -1),
        ("max_delay_ms", 60_001),
        ("max_duplicates", True),
        ("max_delay_ms", True),
    ],
)
def test_fuzz_rejects_invalid_delivery_mutation_bounds(keyword: str, value: object) -> None:
    kwargs = {keyword: value}
    with pytest.raises(ValueError):
        asyncio.run(
            run_fuzz(
                _base_scenario(),
                "http://127.0.0.1:8000/webhook",
                iterations=1,
                **kwargs,
            )
        )

def test_invalid_delivery_mutation_falls_back_to_valid_payload_trial(monkeypatch) -> None:
    import hookstorm.fuzz as fuzz_module

    base = validate_scenario(
        {
            "version": 1,
            "run_timeout_ms": 100,
            "name": "tight budget",
            "delivery": {"timeout_ms": 50},
            "events": [{"id": "ping", "event_id": "evt_tight", "payload": {}}],
        }
    )
    observed: list[dict] = []

    def force_over_budget(candidate, rng, **kwargs):
        candidate["events"][0].setdefault("delivery", {})["delay_ms"] = 60
        return [{
            "dimension": "delay_ms",
            "event_index": 0,
            "value": 60,
        }]

    async def fake_runner(candidate, target, **kwargs):
        observed.append(validate_scenario(candidate))
        return {
            "summary": {"success": True, "total_deliveries": 1, "passed": 1, "failed": 0},
            "run_id": "run-budget-fallback",
            "scenario_fingerprint": scenario_fingerprint(candidate),
        }

    monkeypatch.setattr(fuzz_module, "_mutate_delivery", force_over_budget)
    result = asyncio.run(
        run_fuzz(
            base,
            "http://127.0.0.1:8000/webhook",
            iterations=1,
            vary_delay=True,
            max_delay_ms=60,
            runner=fake_runner,
        )
    )

    assert result["summary"]["success"] is True
    assert observed[0]["events"][0]["delivery"].get("delay_ms", 0) == 0
    skipped = result["cases"][0]["delivery_mutations"][0]
    assert skipped["dimension"] == "delivery_mutations"
    assert "exceeding run_timeout_ms=100" in skipped["skipped"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not portable to Windows")
def test_fuzz_replay_case_is_created_with_owner_only_permissions(tmp_path) -> None:
    path = _write_case(
        tmp_path / "private-cases",
        "replay.yaml",
        _base_scenario(),
    )

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_fuzz_replay_case_is_removed_when_serialization_fails(
    tmp_path, monkeypatch
) -> None:
    directory = tmp_path / "failed-cases"

    def fail_serialization(_scenario):
        raise ValueError("synthetic serialization failure")

    monkeypatch.setattr("hookstorm.fuzz.scenario_to_yaml", fail_serialization)

    with pytest.raises(ValueError, match="synthetic serialization failure"):
        _write_case(directory, "failed.yaml", _base_scenario())

    assert not (directory / "failed.yaml").exists()
    assert list(directory.iterdir()) == []


def test_fuzz_replay_case_refuses_to_overwrite_existing_file(tmp_path) -> None:
    directory = tmp_path / "cases"
    directory.mkdir()
    existing = directory / "case.yaml"
    existing.write_text("keep-me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        _write_case(directory, "case.yaml", _base_scenario())

    assert existing.read_text(encoding="utf-8") == "keep-me"
