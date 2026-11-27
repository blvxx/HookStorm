from __future__ import annotations

import asyncio
import json
import random

import pytest

from hookstorm.config import load_scenario, scenario_fingerprint, validate_scenario
from hookstorm.fuzz import mutate_payload, minimize_json_payload, run_fuzz, scenario_to_yaml


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
