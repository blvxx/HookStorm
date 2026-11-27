"""Deterministic JSON payload fuzzing and bounded failure minimization."""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from hookstorm.provider_profiles import provider_headers
from hookstorm.runner import run_scenario

RunScenario = Callable[..., Awaitable[dict[str, Any]]]
PayloadPredicate = Callable[[Any], Awaitable[bool]]
_MAX_PAYLOAD_BYTES = 1_048_576
_MAX_FUZZ_HTTP_ATTEMPTS = 50_000


def _random_json_value(rng: random.Random, nonce: str) -> Any:
    return rng.choice(
        [
            None,
            True,
            False,
            0,
            -1,
            2**31 - 1,
            f"hookstorm-fuzz-{nonce}",
            [],
            {"fuzz": True},
        ]
    )


def _pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def mutate_payload(
    payload: Any, rng: random.Random, *, nonce: str
) -> tuple[Any, dict[str, str]]:
    """Apply one reproducible structure-aware mutation to a JSON payload."""
    candidate = copy.deepcopy(payload)
    nodes: list[tuple[Any, Any, str]] = []

    def visit(value: Any, path: str, parent: Any = None, key: Any = None) -> None:
        nodes.append((parent, key, path))
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(
                    child_value,
                    f"{path}/{_pointer_token(child_key)}",
                    value,
                    child_key,
                )
        elif isinstance(value, list):
            for index, child_value in enumerate(value):
                visit(child_value, f"{path}/{index}", value, index)

    visit(candidate, "$")
    parent, key, path = rng.choice(nodes)
    selected = candidate if parent is None else parent[key]

    if isinstance(selected, dict):
        if selected and rng.random() < 0.4:
            removed = rng.choice(list(selected))
            del selected[removed]
            operation = "remove-object-key"
        else:
            new_key = f"_hookstorm_fuzz_{nonce}"
            suffix = 1
            while new_key in selected:
                new_key = f"_hookstorm_fuzz_{nonce}_{suffix}"
                suffix += 1
            selected[new_key] = _random_json_value(rng, nonce)
            operation = "add-object-key"
    elif isinstance(selected, list):
        if selected and rng.random() < 0.35:
            removed_index = rng.randrange(len(selected))
            del selected[removed_index]
            operation = "remove-array-item"
        else:
            selected.append(_random_json_value(rng, nonce))
            operation = "append-array-item"
    else:
        replacements = [
            None,
            True,
            False,
            0,
            -1,
            "",
            f"hookstorm-fuzz-{nonce}",
            [],
            {},
        ]
        replacement = rng.choice([item for item in replacements if item != selected])
        if parent is None:
            candidate = replacement
        else:
            parent[key] = replacement
        operation = "replace-json-value"

    try:
        encoded = json.dumps(
            candidate, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Payload mutation produced a non-JSON value") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        candidate = {"_hookstorm_fuzz": nonce}
        path = "$"
        operation = "replace-payload-over-limit"

    return candidate, {"path": path, "operation": operation}


def _candidate_reductions(value: Any) -> Iterable[Any]:
    """Yield smaller JSON values in deterministic delta-debugging order."""
    if isinstance(value, dict):
        yield {}
        for key in list(value):
            reduced = copy.deepcopy(value)
            del reduced[key]
            yield reduced
            for child in _candidate_reductions(value[key]):
                reduced_child = copy.deepcopy(value)
                reduced_child[key] = child
                yield reduced_child
    elif isinstance(value, list):
        yield []
        for index in range(len(value)):
            reduced = copy.deepcopy(value)
            del reduced[index]
            yield reduced
            for child in _candidate_reductions(value[index]):
                reduced_child = copy.deepcopy(value)
                reduced_child[index] = child
                yield reduced_child
    elif isinstance(value, str):
        if len(value) > 1:
            yield value[: len(value) // 2]
        yield ""
        yield "x"
    elif isinstance(value, bool):
        yield False
    elif isinstance(value, int):
        yield 0
        yield 1
        yield -1
        if value not in {0, 1, -1}:
            yield value // 2
    elif isinstance(value, float):
        yield 0.0
        yield 1.0
        yield value / 2


def _unique_reductions(value: Any) -> Iterable[Any]:
    seen: set[str] = set()
    for candidate in _candidate_reductions(value):
        try:
            marker = json.dumps(
                candidate, sort_keys=True, ensure_ascii=False, allow_nan=False
            )
        except (TypeError, ValueError):
            continue
        if candidate != value and marker not in seen:
            seen.add(marker)
            yield candidate


async def minimize_json_payload(
    payload: Any, predicate: PayloadPredicate, *, budget: int
) -> tuple[Any, int]:
    """Greedily reduce a payload while a caller-supplied failure remains reproducible."""
    if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= 100:
        raise ValueError("Minimization budget must be an integer between 0 and 100")
    current = copy.deepcopy(payload)
    attempts = 0
    while attempts < budget:
        improved = False
        for candidate in _unique_reductions(current):
            if attempts >= budget:
                break
            attempts += 1
            if await predicate(candidate):
                current = candidate
                improved = True
                break
        if not improved:
            break
    return current, attempts


def _apply_trial_event_ids(
    scenario: dict[str, Any], *, seed: int, iteration: int, trial: int
) -> None:
    seed_token = f"{seed & ((1 << 64) - 1):016x}"
    provider = scenario.get("provider")
    for event_index, event in enumerate(scenario["events"]):
        old_event_id = event["event_id"]
        suffix = f"_hsfuzz_{seed_token}_{iteration:04x}_{trial:03x}_{event_index:02x}"
        event["event_id"] = f"{old_event_id[:256 - len(suffix)]}{suffix}"
        if provider is not None:
            managed = provider_headers(provider, event["id"], old_event_id)
            managed_names = {name.lower() for name in managed}
            headers = event["headers"]
            for name in list(headers):
                if name.lower() in managed_names:
                    del headers[name]
            headers.update(
                provider_headers(provider, event["id"], event["event_id"])
            )
            if (
                provider == "stripe"
                and isinstance(event["payload"], dict)
                and event["payload"].get("id") == old_event_id
            ):
                event["payload"]["id"] = event["event_id"]


def scenario_to_yaml(scenario: dict[str, Any]) -> str:
    """Serialize a normalized scenario as valid, editable version 1 YAML."""
    document = copy.deepcopy(scenario)
    provider = document.get("provider")
    if provider is not None:
        for event in document["events"]:
            managed = provider_headers(provider, event["id"], event["event_id"])
            managed_names = {name.lower() for name in managed}
            event["headers"] = {
                key: value
                for key, value in event["headers"].items()
                if key.lower() not in managed_names
            }
    return yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _write_case(directory: Path, name: str, scenario: dict[str, Any]) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite fuzz case: {path}")
    path.write_text(scenario_to_yaml(scenario), encoding="utf-8")
    return str(path)


def _safe_target(target: str) -> str:
    parts = urlsplit(target)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _estimated_http_attempts_per_run(scenario: dict[str, Any]) -> int:
    retry_attempts = scenario["retry"]["max_attempts"]
    attempts = 0
    for event in scenario["events"]:
        delivery = {**scenario["delivery"], **event["delivery"]}
        attempts += (delivery.get("duplicate_count", 0) + 1) * retry_attempts
    if scenario.get("verify") is not None:
        attempts += 1
    return attempts


async def run_fuzz(
    scenario: dict[str, Any],
    target: str,
    *,
    seed: int = 1,
    iterations: int = 25,
    allow_remote: bool = False,
    allow_insecure_http: bool = False,
    secret: str | None = None,
    minimize_budget: int = 0,
    cases_dir: str | Path | None = None,
    runner: RunScenario | None = None,
) -> dict[str, Any]:
    """Run seeded payload mutations and optionally minimize failing payloads."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Fuzz seed must be an integer")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 500:
        raise ValueError("Fuzz iterations must be an integer between 1 and 500")
    if (
        isinstance(minimize_budget, bool)
        or not isinstance(minimize_budget, int)
        or not 0 <= minimize_budget <= 100
    ):
        raise ValueError("Minimization budget must be an integer between 0 and 100")
    if not scenario.get("events"):
        raise ValueError("Fuzzing requires at least one scenario event")

    attempts_per_run = _estimated_http_attempts_per_run(scenario)
    worst_case_attempts = attempts_per_run * iterations * (minimize_budget + 1)
    if worst_case_attempts > _MAX_FUZZ_HTTP_ATTEMPTS:
        raise ValueError(
            f"Fuzz run could issue {worst_case_attempts:,} HTTP attempts, exceeding "
            f"the {_MAX_FUZZ_HTTP_ATTEMPTS:,} attempt safety limit; reduce iterations, "
            "retries, duplicates, or minimization budget"
        )

    execute = runner or run_scenario
    failure_directory = Path(cases_dir) if cases_dir is not None else None
    cases: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    executions = 0

    for iteration in range(1, iterations + 1):
        rng = random.Random(f"{seed}:{iteration}")
        event_index = rng.randrange(len(scenario["events"]))
        candidate_scenario = copy.deepcopy(scenario)
        event = candidate_scenario["events"][event_index]
        mutated_payload, mutation = mutate_payload(
            event["payload"], rng, nonce=f"{seed:x}_{iteration:04x}"
        )
        event["payload"] = mutated_payload
        _apply_trial_event_ids(
            candidate_scenario, seed=seed, iteration=iteration, trial=0
        )

        result = await execute(
            candidate_scenario,
            target,
            allow_remote=allow_remote,
            allow_insecure_http=allow_insecure_http,
            secret=secret,
        )
        executions += 1
        success = bool(result["summary"]["success"])
        case: dict[str, Any] = {
            "iteration": iteration,
            "event_index": event_index,
            "event_type": event["id"],
            "event_id": event["event_id"],
            "mutation": mutation,
            "success": success,
            "run_id": result["run_id"],
            "scenario_fingerprint": result["scenario_fingerprint"],
            "summary": result["summary"],
        }

        if success:
            passed += 1
        else:
            failed += 1
            if failure_directory is not None:
                case["case_file"] = _write_case(
                    failure_directory,
                    f"fuzz-seed-{seed}-case-{iteration:04d}.yaml",
                    candidate_scenario,
                )

            if minimize_budget:
                trial_number = 0

                async def reproduces(candidate_payload: Any) -> bool:
                    nonlocal trial_number, executions
                    trial_number += 1
                    trial_scenario = copy.deepcopy(candidate_scenario)
                    trial_scenario["events"][event_index]["payload"] = copy.deepcopy(
                        candidate_payload
                    )
                    _apply_trial_event_ids(
                        trial_scenario,
                        seed=seed,
                        iteration=iteration,
                        trial=trial_number,
                    )
                    trial_result = await execute(
                        trial_scenario,
                        target,
                        allow_remote=allow_remote,
                        allow_insecure_http=allow_insecure_http,
                        secret=secret,
                    )
                    executions += 1
                    return not bool(trial_result["summary"]["success"])

                minimized_payload, minimize_attempts = await minimize_json_payload(
                    mutated_payload, reproduces, budget=minimize_budget
                )
                case["minimization"] = {
                    "attempts": minimize_attempts,
                    "changed": minimized_payload != mutated_payload,
                    "budget": minimize_budget,
                }
                if failure_directory is not None:
                    minimized_scenario = copy.deepcopy(candidate_scenario)
                    minimized_scenario["events"][event_index]["payload"] = minimized_payload
                    _apply_trial_event_ids(
                        minimized_scenario,
                        seed=seed,
                        iteration=iteration,
                        trial=minimize_budget + 1,
                    )
                    case["minimized_case_file"] = _write_case(
                        failure_directory,
                        f"fuzz-seed-{seed}-case-{iteration:04d}-minimized.yaml",
                        minimized_scenario,
                    )

        cases.append(case)

    return {
        "schema_version": 1,
        "tool": "HookStorm",
        "mode": "seeded-payload-fuzz",
        "scenario": scenario["name"],
        "target": _safe_target(target),
        "seed": seed,
        "iterations": iterations,
        "worst_case_http_attempts": worst_case_attempts,
        "summary": {
            "passed": passed,
            "failed": failed,
            "executions": executions,
            "success": failed == 0,
        },
        "cases": cases,
    }
