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
| HOOKSTORM_MCP_ENABLE_REPLAY | Disabled | Set to 1 to enable the local loopback replay tools |
| HOOKSTORM_MCP_EVIDENCE_ROOT | HOOKSTORM_MCP_SCENARIO_ROOT | Directory containing trusted receiver-side JSONL/NDJSON evidence exports |
| HOOKSTORM_GITHUB_TOKEN | Unset | Optional GitHub token with repository Webhooks: read permission |
| HOOKSTORM_STRIPE_API_KEY | Unset | Optional restricted/test Stripe key with Events read access |
| HOOKSTORM_SHOPIFY_CLI | shopify | Optional path to Shopify CLI 5.1+ executable |

Scenario paths must resolve inside HOOKSTORM_MCP_SCENARIO_ROOT. The server accepts YAML files in the root or its scenarios directory for listing, and limits individual scenario files to 2 MiB. Symlinks that resolve outside the configured root are rejected.

## Available tools

| Tool | Behavior |
|---|---|
| hookstorm_list_runs | List recent sanitized runs, optionally filtered by scenario |
| hookstorm_find_deliveries | Search a bounded recent history window by event ID, event type, scenario, and pass/fail status |
| hookstorm_inspect_run | Retrieve one run with attempt details and failure categories |
| hookstorm_list_scenarios | List YAML scenarios in the root and scenarios/ |
| hookstorm_inspect_scenario | Validate a scenario and inspect event identities, retry settings, expected statuses, and assertion keys |
| hookstorm_diagnose_run | Explain recorded failures from stored history |
| hookstorm_analyze_run | Combine failed attempts with scenario settings and privacy-preserving context |
| hookstorm_replay_failed_delivery | Replay one failed event after the replay safeguards pass |
| hookstorm_replay_failed_deliveries | Replay up to 50 selected failed event IDs in one confirmed run |
| hookstorm_replay_with_idempotency_guard | Replay one failed event only after matching explicit pre- and post-replay business-state assertions |
| hookstorm_list_github_deliveries | List repository webhook delivery metadata from GitHub's read-only REST API |
| hookstorm_inspect_github_delivery | Inspect one GitHub delivery and optionally correlate it with a local run |
| hookstorm_retrieve_stripe_event | Retrieve one canonical Stripe Event; optionally compare it with a local run |
| hookstorm_list_stripe_events | Search a bounded page of Stripe Events by type, creation time, pagination cursor, and account-wide delivery_success |
| hookstorm_list_shopify_deliveries | Read Shopify webhook delivery logs through an authenticated Shopify CLI session; optionally correlate them with a local run |
| hookstorm_inspect_receiver_evidence | Match trusted structured receiver logs to an exact event and confirm only a consistent allowlisted failure code |

### Search recent deliveries

Use `hookstorm_find_deliveries` to locate delivery attempts without opening run records one by one. It supports exact filters for `event_id`, `event_type`, and `scenario`, plus `status=failed|passed|all`. By default it returns failed deliveries from the newest 100 runs. Set `lookback_runs` from 1 to 500 and `limit` from 1 to 100 to control the search cost and result size.

The response includes run identifiers, event identity, attempt statuses and timings, bounded diagnostic categories, and the sanitized target. It does not return payload values, custom header values, or response bodies. When more matches exist than the requested result limit, `has_more` is true. The search never sends webhook requests.

Scenario inspection never returns payload values. Run inspection uses the existing sanitized history record; it does not retrieve or reconstruct request bodies, headers, or receiver response bodies.

## Provider event and delivery sources

Provider tools are read-only. They never ask a provider to redeliver an event, and they do not persist provider payloads or raw provider responses in HookStorm history.

### GitHub repository webhook deliveries

Set `HOOKSTORM_GITHUB_TOKEN` to a fine-grained personal access token or GitHub App token with **Webhooks: read** access to the repository. Then call:

- `hookstorm_list_github_deliveries(owner, repo, hook_id, limit, status)` to list recent deliveries.
- `hookstorm_inspect_github_delivery(owner, repo, hook_id, delivery_id)` to inspect one delivery's request and response metadata.

The detail tool returns the provider delivery GUID, event name/action, delivery timestamp, HTTP status, duration, a query-free destination URL, payload byte counts and SHA-256 fingerprints. It returns only allowlisted request/response header names and values; signature headers, authorization, arbitrary headers, request payload values, and response bodies are not returned. GitHub API responses are capped at 2 MiB and payload fingerprints at 1 MiB.

To compare a delivery with a local run, pass both `run_id` and `scenario_path` to `hookstorm_inspect_github_delivery`. A correlation is `confirmed` only if the GitHub GUID/header agree, the local scenario fingerprint still matches the saved run, the canonical JSON payload fingerprint and event type match, and the destination matches. A payload-only match is not enough to authorize replay. Status-code agreement is reported separately; a 4xx/5xx response confirms what the receiver returned, not the internal reason it returned it.

### Stripe Events

Set `HOOKSTORM_STRIPE_API_KEY` to a restricted or test-mode secret key with Events read access.

- `hookstorm_retrieve_stripe_event(event_id)` retrieves one canonical Event using `GET /v1/events/{id}`.
- `hookstorm_list_stripe_events(limit, event_type, event_types, delivery_success, created_after, created_before, starting_after, ending_before)` searches up to 100 Events per page and returns a cursor when more results exist. Optional `run_id` and `scenario_path` let the tool compare each Event with one local run.

Both tools return allowlisted Event metadata and canonical payload fingerprints, not nested object values. The list tool supports Stripe's `delivery_success` filter. This flag describes whether **all webhook endpoints** have successfully received the Event: `false` also includes Events still pending delivery and Events whose attempts have failed. It is account-wide; it does not identify which endpoint failed or return that endpoint's response. The Events API only exposes Events from the last 30 days. Use pagination cursors to inspect additional pages.

This is an **event source**, not a per-endpoint delivery-attempt log source. Stripe's Events API retrieves and filters Event objects, while per-endpoint attempt status, response details, and retry timelines must be inspected through Stripe's Dashboard webhook delivery view. `delivery_attempt_history_available` is therefore always `false`. A matching Event ID and payload can confirm which event a local scenario represents, but it cannot confirm that a particular endpoint attempt failed.

### Shopify webhook delivery logs

Shopify CLI 5.1+ exposes historical app logs, including webhook deliveries, in JSON. Authenticate a user on the machine that runs MCP with `shopify auth login`, then call `hookstorm_list_shopify_deliveries`. The MCP process runs `shopify app logs --type webhook --json --no-input` from `HOOKSTORM_MCP_SCENARIO_ROOT`. Optional `store` and `client_id` filters narrow the app/store. `since` accepts `1h`, `6h`, `12h`, or `24h`; `limit` accepts 1–1,000.

The command needs an existing interactive Shopify account session; Shopify's changelog states that CI automation tokens are not supported for this log command. Set `HOOKSTORM_SHOPIFY_CLI` if the executable is not named `shopify` on `PATH`. The adapter disables optional Shopify CLI usage telemetry, does not pass through `--verbose`, does not invoke a shell, does not expose CLI stderr, and sanitizes JSON records before returning them. The CLI's 2 MiB JSON output limit and command timeout are enforced. Some records may omit or truncate body data; those records cannot be joined by payload fingerprint.

Pass both `run_id` and `scenario_path` to `hookstorm_list_shopify_deliveries` to attach a per-record correlation result. A `confirmed` join requires the saved scenario fingerprint, Shopify event identity, payload fingerprint, topic, and target to agree. Missing or conflicting fields result in `partial` or `unmatched`, never a nearest-run guess.

### Structured receiver-side evidence

Provider delivery records show what the provider observed at the HTTP boundary. They do not expose the receiver's internal signature verifier, handler, database, or queue logs. To make a receiver-side cause confirmable without asking the model to infer it, export structured application logs as UTF-8 JSONL/NDJSON and call `hookstorm_inspect_receiver_evidence(evidence_path, event_id, provider, provider_delivery_id, request_id)`.

The file must be inside `HOOKSTORM_MCP_EVIDENCE_ROOT` (default: the configured scenario root), use a `.jsonl` or `.ndjson` extension, and contain records with this versioned schema:

~~~json
{
  "schema_version": 1,
  "timestamp": "2026-11-28T10:15:00Z",
  "provider": "stripe",
  "provider_delivery_id": "evt_1A2B3C",
  "event_id": "evt_1A2B3C",
  "event_type": "payment_intent.succeeded",
  "request_id": "req_123",
  "trace_id": "trace_123",
  "component": "webhook-handler",
  "severity": "error",
  "outcome": "rejected",
  "error_code": "signature_invalid"
}
~~~

The tool accepts only exact event and optional provider/delivery/request ID matches. It confirms a cause only when every matching record is an ERROR/CRITICAL failed or rejected outcome, all records report the same explicit allowlisted `error_code`, and the source file is complete and valid. Supported codes include signature failures, timestamp expiry, payload validation, unsupported event types/routes, database and queue failures, downstream timeouts/rejections, idempotency conflicts, rate limiting, and internal processing errors.

Malformed or skipped records, conflicting failure codes, successful/duplicate outcomes mixed with failures, or unknown error codes suppress confirmation. The tool returns structured metadata and a deterministic explanation; raw messages, stack traces, payloads, arbitrary fields, and headers are not returned. It caps files at 4 MiB, lines at 16 KiB, records at 10,000, and returned matches at 100. When optional `run_id` and `scenario_path` are supplied, the tool separately confirms the link to the local HookStorm run using the saved scenario fingerprint and exact event ID/type. A receiver log match and a local-run match are distinct claims.

A confirmed cause means the trusted receiver log explicitly recorded the allowlisted failure code for that event. HookStorm does not independently attest that the log producer is correct; export logs from a trusted source and preserve the event/request identifiers across your provider and receiver instrumentation.

### What a confirmed cause means

A provider record can confirm an observed delivery time, endpoint, attempt number, and HTTP status. It cannot by itself prove a database failure, signature bug, queue problem, or business-logic exception inside the receiver. The correlation result therefore keeps `root_cause.confirmed=false` for the provider receiver unless receiver-side evidence is available. An explicit deterministic client-side fault marker (for example, a configured connection error or acknowledgement loss) is exposed separately as `local_fault_injection.confirmed`, with scope `local_hookstorm_reproduction`; it proves only what HookStorm injected into its own test request, not why the provider's real delivery failed. Otherwise the tool returns observed facts, a bounded diagnosis, and the remaining evidence needed.

### Replay and idempotency boundary

Provider adapters are intentionally read-only. They do not invoke GitHub redelivery, Stripe retries, or Shopify redelivery. A confirmed provider-to-local correlation may mark a failed event as eligible for the existing local loopback replay tool, but `idempotency_status` remains `not_proven_by_correlation`. Correlation is not an idempotency check.

Before any replay, inspect a read-only business-state endpoint or the receiver's persisted idempotency record. Existing replay remains disabled unless `HOOKSTORM_MCP_ENABLE_REPLAY=1`, requires `confirm=true`, uses the same event ID and source-run correlation ID, and is restricted to loopback targets. It is a local reproduction flow, not a production provider-redelivery action. Never replay a payment/order event against production merely because the provider log shows a 4xx/5xx response.

Official provider references:

- [GitHub repository webhook delivery REST API](https://docs.github.com/en/rest/repos/webhooks)
- [Stripe retrieve an Event](https://docs.stripe.com/api/events/retrieve)
- [Shopify CLI app logs, added in Shopify CLI 5.1](https://shopify.dev/changelog/posts/query-your-app-s-logs-in-shopify-app-logs)
- [Shopify app logs command reference](https://shopify.dev/docs/api/shopify-cli/app/app-logs)

## Replay safety

The replay tool sends real HTTP requests and is disabled by default. To enable it, set HOOKSTORM_MCP_ENABLE_REPLAY=1 in the MCP server process environment. This is a server-side opt-in, separate from the per-call confirm=true argument.

Every replay call must provide:

- The ID of a stored run that contains a failed delivery for the requested event ID.
- The original YAML scenario, located inside the configured scenario root.
- An explicit target URL.
- confirm=true after checking the preview and destination.

The replay tool only permits loopback targets (localhost, 127.0.0.1, or ::1). Query strings, embedded URL credentials, and fragments are rejected. The target must match the target recorded in the failed run unless allow_target_change=true is explicitly supplied.

The current scenario must match the fingerprint recorded in the failed run. If it does not, the replay is rejected unless allow_scenario_change=true is explicitly supplied. Use that override only after reviewing the changed payload and delivery settings.

A replay selects the requested event and sends its configured retries and duplicates. It resets its scheduled start offset to zero. The original whole-scenario business-state verification probe is not repeated because its expectations may describe more events than the selected one. The replay result is saved to HookStorm history when possible. A replay receives a new `run_id` for its own history record but preserves the source run's `X-HookStorm-Run` correlation value on the HTTP request; the sanitized history stores this separately as `correlation_run_id`. This lets test receivers that scope idempotency by `(X-HookStorm-Run, event_id)` recognize the replay as the same original delivery without overwriting the original HookStorm run record.

The tool is intentionally loopback-only in this first version. Do not use it to test production systems. For staging or remote-target replay, a later iteration must add a stronger target allowlist and DNS-rebinding protection rather than simply exposing --allow-remote to the model.


## AI-assisted diagnosis

Use `hookstorm_analyze_run` when an agent needs more context than the sanitized history contains. Pass the stored run ID and, when available, the original scenario path. The tool joins each failed event to the current YAML by stable event ID and reports:

- The recorded HTTP status, attempt timing, transport error, and assertion outcome.
- Effective retry, timeout, duplicate, concurrency, and signature-provider settings.
- Payload byte size, custom header names, generated signature-header name, and expected JSON key names.
- Failure-specific checks, including authentication/signature rejection, receiver validation, route errors, throttling, server errors, timeouts, transport failures, redirects, oversized payloads, unsupported media types, and possible idempotency conflicts.

The tool never returns payload values, header values, credentials, or response bodies. When the scenario fingerprint differs from the stored run, current-file settings are explicitly marked as context rather than a record of what was sent. A transport timeout or lost acknowledgement is treated as potentially ambiguous: inspect receiver state before replaying because the business operation may already have committed.

The analysis is deterministic and evidence-bounded. It provides structured evidence and next checks for the connected AI agent; it does not claim to have seen receiver logs or to know the exact business outcome.

### Guarded replay with idempotency assertions

For an event with a possible business side effect, prefer `hookstorm_replay_with_idempotency_guard` over the basic local replay tools. The source scenario must include a read-only `verify.url` endpoint, and the tool requires two distinct JSON subset assertions:

- `expected_unprocessed_json`: the state that must be observed before the request is sent (for example, `{"processed_events": 0}`).
- `expected_processed_json`: the state that must be observed after the request (for example, `{"processed_events": 1}`).

The tool sends a bounded GET to the scenario's verify URL before replay. It includes `X-HookStorm-Run`, `X-Event-ID`, and `X-Event-Type`, uses the scenario's environment-backed verification authentication, pins the resolved loopback address, rejects redirects, and applies the existing 2 MiB response limit. If the precondition does not match, the tool blocks the POST even when `confirm=true`.

The first call should use `confirm=false` to review the read-only preflight result. A second call with `confirm=true` sends exactly the selected event to the same loopback target while preserving the source event ID and correlation run ID. It then runs the same read-only state probe against `expected_processed_json`. The result reports `idempotency_verified=true` only when the postcondition matches.

These assertions are supplied by the operator and must reflect a receiver endpoint that checks the actual idempotency key/effect for this event. Prefer a postcondition that proves one business effect, not merely an HTTP acknowledgement. The preflight and POST are not an atomic transaction: the receiver must still enforce atomic deduplication by event ID to protect against concurrent requests between the GET and POST. A timeout or failed postcondition is inconclusive; inspect the business state before another attempt.

Example scenario probe:

~~~yaml
verify:
  url: http://127.0.0.1:8000/__hookstorm/state
  expect_status: [200]
  expect_json:
    processed_events: 1
~~~

Example guarded tool arguments:

~~~json
{
  "run_id": "the-failed-run-id",
  "scenario_path": "scenarios/webhook.yaml",
  "event_id": "evt_order_1001",
  "target": "http://127.0.0.1:8000/webhook",
  "expected_unprocessed_json": {"processed_events": 0},
  "expected_processed_json": {"processed_events": 1},
  "confirm": false
}
~~~

Change `confirm` to `true` only after reviewing the target and assertions. This tool is local-loopback-only and does not ask GitHub, Stripe, or Shopify to redeliver the provider event. The other replay tools remain available for isolated fixtures, but they do not perform this explicit pre/post-state guard.

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
        "HOOKSTORM_HISTORY_DB": "/absolute/path/to/.hookstorm/history.sqlite3",
        "HOOKSTORM_GITHUB_TOKEN": "read-only-github-token",
        "HOOKSTORM_STRIPE_API_KEY": "restricted-stripe-test-key"
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
pytest tests/test_providers.py tests/test_mcp_server.py -q
~~~

The dedicated MCP CI job runs Ruff and both provider-adapter and MCP tool tests on Python 3.12. Provider integration tests use mocked HTTP/CLI transports; they do not contact GitHub, Stripe, or Shopify.
