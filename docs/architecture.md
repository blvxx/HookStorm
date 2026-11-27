# Architecture

HookStorm is intentionally small and local-first.

~~~text
YAML scenario
     |
     v
config.py ---- validate schema, limits, and headers
     |
     +-------> doctor.py ---- scenario + environment + optional DNS/TCP/TLS preflight
     |
     v
safety.py ---- validate target (loopback unless explicitly opted in)
     |
     v
runner.py ---- schedule -> inject selected faults -> serialize -> sign -> send -> retry -> assert
     |                                           |
     +-----------------------> report.py --------+---- JSON / JUnit / HTML cards + waterfall
     |                                           |
     +-----------------------> history.py -------+---- sanitized SQLite / JSON baselines
     |
     v
CLI summaries + diagnostics + exit code (0 passed, 1 assertions failed, 2 invalid input/configuration)
~~~

## Main modules

- `config.py`: loads version 1 YAML scenarios with safe parsing, duplicate-key rejection, defaults, and bounded event, payload, header, and timing limits.
- `safety.py`: validates target URL syntax and the local-only policy. Remote HTTPS requires `--allow-remote`; non-loopback HTTP additionally requires `--allow-insecure-http`.
- `doctor.py`: validates scenarios, endpoint configuration, signing-secret presence, and verification-probe authentication configuration. Optional connection checks perform DNS/TCP and HTTPS TLS diagnostics without sending HTTP requests.
- `runner.py`: sends requests asynchronously with HTTPX, schedules independent events, preserves event IDs, signs the exact serialized request bytes, refreshes timestamped signatures on every retry, and checks HTTP and JSON responses. Deterministic `connect_error` faults fail before sending; `response_loss` faults discard the receiver response after processing. Delivery and verification responses are streamed with a 2 MiB decoded-body cap.
- `report.py`: generates JSON, JUnit XML, or self-contained HTML with attempt cards, a shared-clock waterfall, search, result/fault filters, and sorting.
- `history.py`: persists a strict whitelist of run metadata and assertion outcomes to local SQLite and exports sanitized JSON baselines. SQLite uses `PRAGMA user_version` for schema version 1; existing unversioned databases are migrated in place without rewriting stored runs. Schema creation takes an immediate write transaction, and concurrent processes serialize through SQLite with a 30-second busy timeout. Payloads, headers, response bodies, and secret values remain excluded.
- `cli.py`: exposes run, validate, init, doctor, export-pytest, and history commands.
- `standard_webhooks.py`: validates and decodes `whsec_` keys and builds Standard Webhooks v1 HMAC headers.
- `examples/receiver.py`: standard-library local demo server with provider-signature, idempotency, and commit-then-drop endpoints. Never expose it publicly.
- `pytest_export.py`: generates pytest integration modules with an embedded normalized scenario and runtime-only target / secret environment variables.
- `scaffold.py`: generates a starter YAML scenario and SHA-pinned GitHub Actions staging workflow. It refuses to overwrite existing files unless `--force` is passed.

## Deliberate trade-offs

1. CLI before dashboard. The scenario runner and deterministic reports are the core value.
2. No hosted receiver or webhook relay. Users point HookStorm at their own test endpoint.
3. No implicit remote access. Loopback is the default; remote testing requires explicit opt-in.
4. No raw payloads in built-in reports or history. Reports focus on delivery behavior instead of copying business data.

## Current limitations

- Concurrency applies across independent events; retries and duplicate deliveries within one event remain sequential. With concurrency above 1, request completion order is intentionally nondeterministic.
- A timeout or dropped acknowledgement does not prove the receiver rolled back work. The deterministic `response_loss` fault is a client-side simulation, not a network proxy or proof of an actual packet drop. The `/webhook/commit-then-drop` demo closes the receiver-side TCP connection after committing its business effect.
- Provider signing formats include generic HMAC, Stripe, GitHub, Shopify, and Standard Webhooks v1 HMAC. These request generators are for authorized test endpoints, not replacements for provider SDKs or receiver-side verification libraries. Standard Webhooks asymmetric v1a signing and multi-key rotation are not implemented.
- JSON object assertions support recursive subset matching; arrays are compared exactly and in order.
- A passing status or JSON assertion does not prove the business side effect happened exactly once; verify it independently in your test environment.
- `doctor` checks credential presence and syntax but does not send credentials or confirm that the application accepts them. `doctor --connect` confirms socket reachability and TLS setup only; it does not verify HTTP routing or webhook-handler behavior.

## CI integration

- `--format junit` writes one JUnit testcase per event delivery, including duplicates; assertion failures and transport errors are distinguished.
- `.github/workflows/ci.yml` runs lint and tests across the supported Python / operating-system matrix, builds distributions, and smoke-tests installed artifacts.
- `.github/workflows/webhook-scenarios.yml` runs the local demo receiver and representative YAML scenarios, compares runs with cached main-branch baselines, and uploads per-scenario JUnit reports.
- `examples/ci/webhook-regression.yml` is a consumer-facing staging template. It uses a caller-managed endpoint and HMAC secret, skips fork-originated PRs, and refreshes baselines only from trusted main-branch runs.

## Run history and regression model

Run history is local and opt-out. By default it is stored at `~/.hookstorm/history.sqlite3`; `HOOKSTORM_HISTORY_DB` or a per-command `--db` / `--history-db` option can override the path.

Only run metadata, event identifiers, statuses, attempt timing, and assertion outcomes are persisted. Request payloads, request / response headers, response bodies, and secrets are excluded by an explicit field whitelist. The stored target includes its URL scheme, hostname, explicit port (when present), and path. Query strings and fragments are removed by the runner.

The `history compare` command compares deliveries by event type, event ID, and duplicate index. It reports delivery coverage, pass-rate, per-delivery latency, aggregate p95 latency, and business-state verification regressions. The latency gate requires both a relative increase above 20 percent and an absolute p95 increase of at least 25 ms by default, reducing noise when measurements are small. Both thresholds are configurable. Retry backoff is excluded from the delivery latency metric.

Portable baseline files use the `hookstorm-history` format at version 1. Export and import both apply the same result whitelist; the loader rejects unknown format versions and re-sanitizes loaded JSON before comparison. `history compare-file` compares a portable baseline to a run in the current SQLite database, allowing the database itself to remain ephemeral.
