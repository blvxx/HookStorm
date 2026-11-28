from __future__ import annotations

import asyncio
import json

import httpx
from importlib import import_module
from pathlib import Path

import pytest

from hookstorm.config import load_scenario, scenario_fingerprint
from hookstorm.history import get_run, save_run
from hookstorm.mcp_server import create_server

try:
    Client = import_module("mcp").Client
except ImportError:
    Client = None

pytestmark = pytest.mark.skipif(
    Client is None,
    reason="the optional MCP extra is not installed",
)


def _scenario_file(root: Path) -> Path:
    path = root / "scenarios" / "replay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1
name: Replay test
retry:
  max_attempts: 1
events:
  - id: ping
    event_id: evt_replay
    payload:
      private_value: payload-must-not-leak
    expect_status: [200]
    headers:
      X-Internal-Debug-Token: header-value-must-not-leak
    expect_json:
      ok: true
""",
        encoding="utf-8",
    )
    return path


def _multi_scenario_file(root: Path) -> Path:
    path = _scenario_file(root)
    path.write_text(
        """version: 1
name: Replay test
retry:
  max_attempts: 1
events:
  - id: ping
    event_id: evt_replay
    payload:
      private_value: first-payload
    expect_status: [200]
  - id: pong
    event_id: evt_replay_2
    payload:
      private_value: second-payload
    expect_status: [200]
""",
        encoding="utf-8",
    )
    return path


def _failed_run(
    path: Path,
    target: str,
    db: Path,
    event_ids: list[str] | None = None,
) -> str:
    scenario = load_scenario(path)
    run_id = "run_failed_replay_test"
    selected_ids = event_ids or ["evt_replay"]
    event_index = {event["event_id"]: event for event in scenario["events"]}
    deliveries = []
    for event_id in selected_ids:
        event = event_index[event_id]
        deliveries.append(
            {
                "event_type": event["id"],
                "event_id": event_id,
                "duplicate_index": 0,
                "passed": False,
                "attempts": [
                    {
                        "attempt": 1,
                        "status_code": 503,
                        "latency_ms": 5.0,
                        "error": "HTTP 503",
                        "expected_status": False,
                        "expected_json": True,
                        "passed": False,
                    }
                ],
            }
        )
    count = len(deliveries)
    result = {
        "run_id": run_id,
        "scenario": scenario["name"],
        "scenario_fingerprint": scenario_fingerprint(scenario),
        "started_at": "2026-11-28T10:00:00+00:00",
        "target": target,
        "duration_ms": 5.0 * count,
        "summary": {
            "success": False,
            "total_deliveries": count,
            "passed": 0,
            "failed": count,
            "attempts": count,
        },
        "deliveries": deliveries,
        "verification": None,
    }
    save_run(result, db)
    return run_id


def _call_tool(server, name: str, arguments: dict) -> dict:
    async def call() -> dict:
        async with Client(server) as client:
            response = await client.call_tool(name, arguments)
            assert not response.is_error
            assert response.structured_content is not None
            return response.structured_content

    return asyncio.run(call())


def test_inspect_scenario_does_not_expose_payload_values(tmp_path: Path) -> None:
    scenario_path = _scenario_file(tmp_path)
    server = create_server(scenario_root=tmp_path, enable_replay=False)

    result = _call_tool(
        server,
        "hookstorm_inspect_scenario",
        {"scenario_path": "scenarios/replay.yaml"},
    )

    assert result["name"] == "Replay test"
    assert result["event_count"] == 1
    assert result["events"][0]["event_id"] == "evt_replay"
    assert result["events"][0]["expected_json_top_level_keys"] == ["ok"]
    assert "payload-must-not-leak" not in json.dumps(result)
    assert "header-value-must-not-leak" not in json.dumps(result)
    assert scenario_path.is_file()


def test_scenario_paths_cannot_escape_configured_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
    outside.write_text("version: 1\nname: Outside\nevents:\n  - id: ping\n    event_id: outside\n", encoding="utf-8")
    server = create_server(scenario_root=tmp_path, enable_replay=False)

    async def call() -> None:
        async with Client(server) as client:
            response = await client.call_tool(
                "hookstorm_inspect_scenario",
                {"scenario_path": str(outside)},
            )
            assert response.is_error

    asyncio.run(call())


def test_diagnose_run_uses_sanitized_history(tmp_path: Path) -> None:
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    run_id = _failed_run(scenario_path, "http://127.0.0.1:8000/webhook", db)
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
    )

    result = _call_tool(server, "hookstorm_diagnose_run", {"run_id": run_id})

    assert result["success"] is False
    assert result["failed_deliveries"][0]["category"] == "receiver_server_error"
    assert "request payloads" in " ".join(result["limitations"])
    assert "payload-must-not-leak" not in json.dumps(result)


def test_replay_is_disabled_without_server_opt_in(receiver, tmp_path: Path) -> None:
    base, state = receiver
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    target = f"{base}/webhook"
    run_id = _failed_run(scenario_path, target, db)
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
    )

    result = _call_tool(
        server,
        "hookstorm_replay_failed_delivery",
        {
            "run_id": run_id,
            "scenario_path": "scenarios/replay.yaml",
            "event_id": "evt_replay",
            "target": target,
            "confirm": True,
        },
    )

    assert result["executed"] is False
    assert result["replay_enabled"] is False
    assert state["requests"] == []


def test_replay_requires_confirmation_and_sends_only_selected_event(
    receiver, tmp_path: Path
) -> None:
    base, state = receiver
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    target = f"{base}/webhook"
    run_id = _failed_run(scenario_path, target, db)
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=True,
    )
    arguments = {
        "run_id": run_id,
        "scenario_path": "scenarios/replay.yaml",
        "event_id": "evt_replay",
        "target": target,
    }

    preview = _call_tool(
        server,
        "hookstorm_replay_failed_delivery",
        {**arguments, "confirm": False},
    )
    assert preview["executed"] is False
    assert state["requests"] == []

    result = _call_tool(
        server,
        "hookstorm_replay_failed_delivery",
        {**arguments, "confirm": True},
    )

    assert result["executed"] is True
    assert result["summary"]["success"] is True
    assert result["history_saved"] is True
    assert result["correlation_run_id"] == run_id
    assert len(state["requests"]) == 1
    assert state["requests"][0]["event_id"] == "evt_replay"


def test_replay_rejects_changed_scenario_without_explicit_override(
    receiver, tmp_path: Path
) -> None:
    base, state = receiver
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    target = f"{base}/webhook"
    run_id = _failed_run(scenario_path, target, db)
    scenario_path.write_text(
        scenario_path.read_text(encoding="utf-8").replace(
            "payload-must-not-leak", "changed-payload"
        ),
        encoding="utf-8",
    )
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=True,
    )

    async def call() -> None:
        async with Client(server) as client:
            response = await client.call_tool(
                "hookstorm_replay_failed_delivery",
                {
                    "run_id": run_id,
                    "scenario_path": "scenarios/replay.yaml",
                    "event_id": "evt_replay",
                    "target": target,
                    "confirm": True,
                },
            )
            assert response.is_error

    asyncio.run(call())
    assert state["requests"] == []


def test_analyze_run_joins_scenario_context_without_disclosing_payload_values(
    tmp_path: Path,
) -> None:
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    run_id = _failed_run(
        scenario_path,
        "http://127.0.0.1:8000/webhook",
        db,
    )
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
    )

    result = _call_tool(
        server,
        "hookstorm_analyze_run",
        {
            "run_id": run_id,
            "scenario_path": "scenarios/replay.yaml",
        },
    )

    failure = result["failed_deliveries"][0]
    assert result["scenario_matches_recorded_fingerprint"] is True
    assert failure["category"] == "receiver_server_error"
    assert failure["scenario_context"]["event_id"] == "evt_replay"
    assert failure["scenario_context"]["payload_size_bytes"] > 0
    assert failure["scenario_context"]["custom_header_names"] == ["X-Internal-Debug-Token"]
    assert failure["scenario_context"]["expect_json_top_level_keys"] == ["ok"]
    assert failure["attempts_exhausted"] is True
    assert "payload-must-not-leak" not in json.dumps(result)
    assert "header-value-must-not-leak" not in json.dumps(result)


def test_batch_replay_requires_confirmation_and_replays_only_selected_failed_events(
    receiver,
    tmp_path: Path,
) -> None:
    base, state = receiver
    scenario_path = _multi_scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    target = f"{base}/webhook"
    event_ids = ["evt_replay_2", "evt_replay"]
    run_id = _failed_run(scenario_path, target, db, event_ids)
    # Simulate the first attempts committing their business effects before the ACK was lost.
    for event_id in event_ids:
        state["seen"].add((run_id, event_id))
        state["effects"].add((run_id, event_id))
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=True,
    )
    arguments = {
        "run_id": run_id,
        "scenario_path": "scenarios/replay.yaml",
        "event_ids": event_ids,
        "target": target,
    }

    preview = _call_tool(
        server,
        "hookstorm_replay_failed_deliveries",
        {**arguments, "confirm": False},
    )
    assert preview["executed"] is False
    assert preview["selected_event_count"] == 2
    assert state["requests"] == []

    result = _call_tool(
        server,
        "hookstorm_replay_failed_deliveries",
        {**arguments, "confirm": True},
    )

    assert result["executed"] is True
    assert result["summary"]["success"] is True
    assert result["selected_event_ids"] == ["evt_replay", "evt_replay_2"]
    assert result["history_saved"] is True
    assert result["correlation_run_id"] == run_id
    assert get_run(result["run_id"], db)["correlation_run_id"] == run_id
    assert sorted(request["event_id"] for request in state["requests"]) == sorted(event_ids)
    assert all(
        request["headers"]["X-HookStorm-Run"] == run_id
        for request in state["requests"]
    )
    # The receiver recognizes both deliveries as duplicates and applies no new effects.
    assert len(state["effects"]) == 2


def test_batch_replay_refuses_any_event_without_a_failed_delivery(
    receiver,
    tmp_path: Path,
) -> None:
    base, state = receiver
    scenario_path = _multi_scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    target = f"{base}/webhook"
    run_id = _failed_run(scenario_path, target, db, ["evt_replay"])
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=True,
    )

    async def call() -> None:
        async with Client(server) as client:
            response = await client.call_tool(
                "hookstorm_replay_failed_deliveries",
                {
                    "run_id": run_id,
                    "scenario_path": "scenarios/replay.yaml",
                    "event_ids": ["evt_replay", "evt_replay_2"],
                    "target": target,
                    "confirm": True,
                },
            )
            assert response.is_error

    asyncio.run(call())
    assert state["requests"] == []


def test_replay_refuses_remote_and_query_string_targets(receiver, tmp_path: Path) -> None:
    base, state = receiver
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    run_id = _failed_run(scenario_path, f"{base}/webhook", db)
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=True,
    )

    async def call(target: str) -> None:
        async with Client(server) as client:
            response = await client.call_tool(
                "hookstorm_replay_failed_delivery",
                {
                    "run_id": run_id,
                    "scenario_path": "scenarios/replay.yaml",
                    "event_id": "evt_replay",
                    "target": target,
                    "confirm": True,
                },
            )
            assert response.is_error

    asyncio.run(call("https://example.com/webhook"))
    asyncio.run(call(f"{base}/webhook?token=must-not-be-in-url"))
    assert state["requests"] == []


def test_find_deliveries_searches_sanitized_history_by_event_id(tmp_path: Path) -> None:
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    run_id = _failed_run(
        scenario_path,
        "http://127.0.0.1:8000/webhook",
        db,
    )
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
    )

    result = _call_tool(
        server,
        "hookstorm_find_deliveries",
        {"event_id": "evt_replay"},
    )

    assert result["count"] == 1
    assert result["has_more"] is False
    assert result["searched_runs"] == 1
    delivery = result["deliveries"][0]
    assert delivery["run_id"] == run_id
    assert delivery["delivery"]["event_type"] == "ping"
    assert delivery["delivery"]["event_id"] == "evt_replay"
    assert delivery["delivery"]["passed"] is False
    assert delivery["delivery"]["diagnosis"]["category"] == "receiver_server_error"
    assert "payload-must-not-leak" not in json.dumps(result)
    assert "header-value-must-not-leak" not in json.dumps(result)


def test_find_deliveries_applies_status_and_lookback_limits(tmp_path: Path) -> None:
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    _failed_run(scenario_path, "http://127.0.0.1:8000/webhook", db)
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
    )

    passed = _call_tool(
        server,
        "hookstorm_find_deliveries",
        {"event_type": "ping", "status": "passed", "lookback_runs": 1},
    )
    assert passed["count"] == 0
    assert passed["status_filter"] == "passed"

    async def invalid_limit() -> None:
        async with Client(server) as client:
            response = await client.call_tool(
                "hookstorm_find_deliveries",
                {"event_id": "evt_replay", "lookback_runs": 501},
            )
            assert response.is_error

    asyncio.run(invalid_limit())

def _github_delivery_detail(payload: dict) -> dict:
    return {
        "id": 12345678,
        "guid": "evt_replay",
        "delivered_at": "2026-11-28T10:00:00Z",
        "redelivery": False,
        "duration": 0.27,
        "status": "Failed",
        "status_code": 503,
        "event": "ping",
        "request": {
            "method": "POST",
            "url": "http://127.0.0.1:8000/webhook?private-query=must-not-leak",
            "headers": {
                "X-GitHub-Delivery": "evt_replay",
                "X-GitHub-Event": "ping",
                "Authorization": "api-header-secret",
                "X-Hub-Signature-256": "signature-secret",
            },
            "payload": payload,
        },
        "response": {
            "status_code": 503,
            "headers": {
                "Content-Type": "application/json",
                "Set-Cookie": "response-cookie-secret",
            },
            "payload": {
                "message": "internal-response-must-not-leak",
            },
        },
    }


def test_inspect_github_delivery_correlates_exact_local_event_without_leaking_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    target = "http://127.0.0.1:8000/webhook"
    run_id = _failed_run(scenario_path, target, db)
    payload = {"private_value": "payload-must-not-leak"}
    detail = _github_delivery_detail(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer github-token-secret"
        return httpx.Response(200, json=detail)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("HOOKSTORM_GITHUB_TOKEN", "github-token-secret")
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
        github_http_client=client,
    )
    result = _call_tool(
        server,
        "hookstorm_inspect_github_delivery",
        {
            "owner": "octocat",
            "repo": "hello-world",
            "hook_id": 42,
            "delivery_id": 12345678,
            "run_id": run_id,
            "scenario_path": "scenarios/replay.yaml",
        },
    )
    client.close()

    correlation = result["correlation"]
    assert correlation["status"] == "confirmed"
    assert correlation["match_basis"] == "provider_delivery_guid"
    assert correlation["scenario_matches_recorded_fingerprint"] is True
    assert correlation["provider_event_guid_matches_local_event_id"] is True
    assert correlation["payload_fingerprint_matches"] is True
    assert correlation["event_type_matches"] is True
    assert correlation["target_matches"] is True
    assert correlation["status_codes_match"] is True
    assert correlation["confirmed_facts"][0]["status_code"] == 503
    assert correlation["root_cause"]["confirmed"] is False
    assert correlation["replay_readiness"]["eligible_for_existing_loopback_replay"] is True
    assert correlation["replay_readiness"]["idempotency_status"] == "not_proven_by_correlation"

    serialized = json.dumps(result)
    for secret in (
        "github-token-secret",
        "private-query",
        "must-not-leak",
        "api-header-secret",
        "signature-secret",
        "payload-must-not-leak",
        "response-cookie-secret",
        "internal-response-must-not-leak",
    ):
        assert secret not in serialized


def test_github_delivery_correlation_blocks_replay_candidate_when_payload_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_path = _scenario_file(tmp_path)
    db = tmp_path / "history.sqlite3"
    run_id = _failed_run(
        scenario_path,
        "http://127.0.0.1:8000/webhook",
        db,
    )
    detail = _github_delivery_detail({"private_value": "different-payload"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=detail)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("HOOKSTORM_GITHUB_TOKEN", "github-token-secret")
    server = create_server(
        history_db=db,
        scenario_root=tmp_path,
        enable_replay=False,
        github_http_client=client,
    )
    result = _call_tool(
        server,
        "hookstorm_inspect_github_delivery",
        {
            "owner": "octocat",
            "repo": "hello-world",
            "hook_id": 42,
            "delivery_id": 12345678,
            "run_id": run_id,
            "scenario_path": "scenarios/replay.yaml",
        },
    )
    client.close()

    correlation = result["correlation"]
    assert correlation["status"] == "partial"
    assert correlation["payload_fingerprint_matches"] is False
    assert correlation["replay_readiness"]["eligible_for_existing_loopback_replay"] is False


def test_github_delivery_inspection_requires_run_and_scenario_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOOKSTORM_GITHUB_TOKEN", "github-token-secret")
    server = create_server(scenario_root=tmp_path, enable_replay=False)

    async def call() -> None:
        async with Client(server) as client:
            response = await client.call_tool(
                "hookstorm_inspect_github_delivery",
                {
                    "owner": "octocat",
                    "repo": "hello-world",
                    "hook_id": 42,
                    "delivery_id": 12345678,
                    "run_id": "run-without-scenario",
                },
            )
            assert response.is_error

    asyncio.run(call())
