# HookStorm MCP server

HookStorm includes an optional local Model Context Protocol (MCP) server. It lets an MCP host inspect local webhook test history, validate scenarios, and diagnose failed deliveries. The server is local-first and uses the same HookStorm engine and SQLite history as the CLI.

## Install

Install HookStorm with the optional MCP extra from the repository checkout:

~~~bash
python -m pip install -e ".[mcp]"
~~~

The extra installs the official MCP Python SDK and its development CLI. The server command is:

~~~bash
hookstorm-mcp
~~~

The default transport is stdio. The MCP process does not open a listening network port.

For development, the MCP Inspector can launch the module directly:

~~~bash
mcp dev src/hookstorm/mcp_server.py
~~~

## Configure the local workspace

The server reads these environment variables:

| Variable | Default | Purpose |
|---|---|---|
| HOOKSTORM_MCP_SCENARIO_ROOT | Current working directory | Root directory in which MCP may inspect YAML scenarios |
| HOOKSTORM_HISTORY_DB | HookStorm's normal local history database | Select the same run history used by the CLI |
| HOOKSTORM_MCP_ENABLE_REPLAY | Disabled | Set to 1 to enable the replay tool |

Scenario paths must resolve inside HOOKSTORM_MCP_SCENARIO_ROOT. The server accepts YAML files in the root or its scenarios directory for listing, and limits individual scenario files to 2 MiB. Symlinks that resolve outside the configured root are rejected.

## Available tools

| Tool | Behavior |
|---|---|
| hookstorm_list_runs | List recent sanitized runs, optionally filtered by scenario |
| hookstorm_inspect_run | Retrieve one run with attempt details and failure categories |
| hookstorm_list_scenarios | List YAML scenarios in the root and scenarios/ |
| hookstorm_inspect_scenario | Validate a scenario and inspect event identities, retry settings, expected statuses, and assertion keys |
| hookstorm_diagnose_run | Explain recorded failures from stored history |
| hookstorm_analyze_run | Combine failed attempts with scenario settings and privacy-preserving context |
| hookstorm_replay_failed_delivery | Replay one failed event after the replay safeguards pass |
| hookstorm_replay_failed_deliveries | Replay up to 50 selected failed event IDs in one confirmed run |

Scenario inspection never returns payload values. Run inspection uses the existing sanitized history record; it does not retrieve or reconstruct request bodies, headers, or receiver response bodies.

## Replay safety

The replay tool sends real HTTP requests and is disabled by default. To enable it, set HOOKSTORM_MCP_ENABLE_REPLAY=1 in the MCP server process environment. This is a server-side opt-in, separate from the per-call confirm=true argument.

Every replay call must provide:

- The ID of a stored run that contains a failed delivery for the requested event ID.
- The original YAML scenario, located inside the configured scenario root.
- An explicit target URL.
- confirm=true after checking the preview and destination.

The replay tool only permits loopback targets (localhost, 127.0.0.1, or ::1). Query strings, embedded URL credentials, and fragments are rejected. The target must match the target recorded in the failed run unless allow_target_change=true is explicitly supplied.

The current scenario must match the fingerprint recorded in the failed run. If it does not, the replay is rejected unless allow_scenario_change=true is explicitly supplied. Use that override only after reviewing the changed payload and delivery settings.

A replay selects the requested event and sends its configured retries and duplicates. It resets its scheduled start offset to zero. The original whole-scenario business-state verification probe is not repeated because its expectations may describe more events than the selected one. The replay result is saved to HookStorm history when possible.

The tool is intentionally loopback-only in this first version. Do not use it to test production systems. For staging or remote-target replay, a later iteration must add a stronger target allowlist and DNS-rebinding protection rather than simply exposing --allow-remote to the model.


## AI-assisted diagnosis

Use `hookstorm_analyze_run` when an agent needs more context than the sanitized history contains. Pass the stored run ID and, when available, the original scenario path. The tool joins each failed event to the current YAML by stable event ID and reports:

- The recorded HTTP status, attempt timing, transport error, and assertion outcome.
- Effective retry, timeout, duplicate, concurrency, and signature-provider settings.
- Payload byte size, custom header names, generated signature-header name, and expected JSON key names.
- Failure-specific checks, including authentication/signature rejection, receiver validation, route errors, throttling, server errors, timeouts, transport failures, redirects, oversized payloads, unsupported media types, and possible idempotency conflicts.

The tool never returns payload values, header values, credentials, or response bodies. When the scenario fingerprint differs from the stored run, current-file settings are explicitly marked as context rather than a record of what was sent. A transport timeout or lost acknowledgement is treated as potentially ambiguous: inspect receiver state before replaying because the business operation may already have committed.

The analysis is deterministic and evidence-bounded. It provides structured evidence and next checks for the connected AI agent; it does not claim to have seen receiver logs or to know the exact business outcome.

## Batch replay

`hookstorm_replay_failed_deliveries` accepts one stored run, one YAML scenario, a list of up to 50 event IDs, and an explicit target. Every selected event must have at least one failed delivery in that run and must exist exactly once in the scenario. The tool validates all selected events before sending any request; one invalid ID blocks the entire batch.

The replay uses each selected event's configured retry and duplicate behavior and preserves scenario order. It disables the original whole-scenario verification probe because the original expectation may describe events outside the selected batch. Replay remains disabled by default, requires the server-side opt-in and `confirm=true`, and permits loopback targets only. It does not replay only the failed duplicate index: it replays the selected event using its full configured duplicate policy, so review the preview before confirming.

## Example MCP client configuration

For an MCP client that supports local stdio servers, register the command with the absolute path to the installed executable when needed:

~~~json
{
  "mcpServers": {
    "hookstorm": {
      "command": "hookstorm-mcp",
      "args": [],
      "env": {
        "HOOKSTORM_MCP_SCENARIO_ROOT": "/absolute/path/to/application",
        "HOOKSTORM_HISTORY_DB": "/absolute/path/to/.hookstorm/history.sqlite3"
      }
    }
  }
}
~~~

To enable replay, add HOOKSTORM_MCP_ENABLE_REPLAY=1 to env. Keep it unset for read-only diagnosis.

## Test

Install the development and MCP extras, then run the focused server tests:

~~~bash
python -m pip install -e ".[dev,mcp]"
pytest tests/test_mcp_server.py -q
~~~

The dedicated MCP CI job runs Ruff and the MCP test module on Python 3.12.
