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
runner.py ---- serialize -> sign -> send -> retry -> assert response
     |
     +-------> report.py ---- JSON / self-contained HTML
     |
     v
CLI exit code (0 passed, 1 assertions failed, 2 invalid input/configuration)
~~~

## Main modules

- config.py: uses yaml.safe_load, rejects unknown keys, applies defaults, and bounds scenario sizes and timings.
- safety.py: accepts only loopback IPs and localhost by default. --allow-remote is an explicit opt-in.
- doctor.py: validates the scenario, configured HTTP(S) target, required signing-secret presence, and verification-probe authentication environment configuration. It checks Bearer / API-key variable presence and format, plus HTTPS for authenticated non-loopback probes, without transmitting credentials or sending HTTP requests. Optional --connect performs DNS/TCP and HTTPS TLS checks for the webhook target; --verbose reports each resolved IPv4 / IPv6 address, connection timings, negotiated TLS details, and certificate metadata. Its versioned JSON output (schema version 1) exposes readiness, check summaries, network-check state, and per-check details for scripts and CI consumers.
- runner.py: sends requests asynchronously with HTTPX, supports per-event start offsets and bounded concurrency across independent events, preserves event IDs, generates generic / Stripe / GitHub / Shopify / Standard Webhooks signatures from the exact request bytes, refreshes timestamped signatures on every retry, and checks nested JSON responses.
- report.py: generates JSON, JUnit XML, or self-contained HTML with elapsed-time attempt tracks, search, result filters, and expandable attempt details.
- history.py: persists a strict whitelist of run metadata and assertion outcomes to local SQLite, exports versioned sanitized JSON baselines, and compares local or portable baselines for pass-rate, coverage, and latency regressions.
- standard_webhooks.py: validates and decodes `whsec_` keys and builds Standard Webhooks v1 HMAC headers.
- examples/receiver.py: standard-library local demo server with provider-signature, idempotency, and commit-then-drop endpoints; never use it as a public endpoint.
- pytest_export.py: generates pytest integration modules with an embedded, normalized scenario and runtime-only target / secret environment variables.
- scaffold.py: generates an application-specific starter YAML scenario and a pinned GitHub Actions staging workflow; preserves existing files unless the caller explicitly passes `--force`, and never embeds target URLs or signing secrets.

## Deliberate trade-offs

1. CLI before dashboard. The scenario runner and deterministic reports are the core value.
2. No hosted receiver. Users point HookStorm at their own test endpoint.
3. No implicit remote access. Loopback is the default; remote testing requires an explicit opt-in.
4. No raw payloads in reports. Reports focus on delivery behavior instead of copying business data.

## Current limitations

- Concurrency applies across independent events; retries and duplicate deliveries within one event remain sequential. With concurrency above 1, request completion order is intentionally nondeterministic.
- A timeout or dropped acknowledgement does not prove the receiver rolled back work. The included commit-then-drop example demonstrates one ambiguous outcome, but cannot model every proxy, crash, database, or queue failure mode.
- Provider signing formats include generic HMAC, Stripe, GitHub, Shopify, and Standard Webhooks v1 HMAC. These are request generators for authorized test endpoints, not replacements for provider SDKs or general receiver-side verification libraries. Standard Webhooks asymmetric v1a signing and multi-key rotation are not implemented.
- JSON object assertions support recursive subset matching; arrays are compared exactly and in order.
- A passing status or JSON assertion does not prove the business side effect happened exactly once; verify it independently in your test environment.
- doctor checks configured verification credential presence and syntax, and enforces HTTPS for authenticated remote probes. It does not send credentials or confirm that the application accepts them. doctor --connect confirms socket reachability and TLS setup only; it does not verify HTTP routing or webhook handler behavior. Verbose diagnostics check a bounded number of resolved addresses and warn about partial address failures or certificates nearing expiry.


## CI integration

- `--format junit` writes one JUnit testcase per event delivery, including duplicates; assertion failures and transport errors are distinguished.
- `.github/workflows/ci.yml` publishes the unit-test JUnit report from each OS / Python matrix job.
- `.github/workflows/webhook-scenarios.yml` runs the local demo receiver and representative YAML scenarios, including Standard Webhooks signatures and a committed-event/lost-ack retry, compares each run with a cached main-branch baseline, and uploads the per-scenario JUnit reports.
- Baselines are exported as versioned JSON files and carried between ephemeral workflow runs using the GitHub Actions cache. Pull requests read the main-branch baseline but never update it; successful pushes to main refresh the cache.
- `examples/ci/webhook-regression.yml` is a consumer-facing staging template. It runs doctor preflight before delivery, uses a caller-managed test endpoint and HMAC secret, skips fork-originated PRs, restores baselines read-only, and refreshes them only from trusted main-branch runs.


## Run history and regression model

Run history is local and opt-out. By default it is stored at `~/.hookstorm/history.sqlite3`; `HOOKSTORM_HISTORY_DB` or a per-command `--db` / `--history-db` option can override the path.

Only run metadata, event identifiers, statuses, attempt timing, and assertion outcomes are persisted. Request payloads, request / response headers, response bodies, and secrets are excluded by an explicit field whitelist. The sanitized result still retains the target URL path for identifying the tested endpoint; query strings are removed by the runner.

The `history compare` command compares deliveries by event type, event ID, and duplicate index. It reports newly failing deliveries, recovered deliveries, removed coverage, per-delivery latency changes, aggregate p95 latency changes, pass-rate changes, and total run duration. The default latency regression threshold is 20 percent. A detected regression returns exit code 1; command and database errors return exit code 2. The p95 metric uses per-delivery sums of HTTP attempt latency and excludes configured retry backoff.

Portable baseline files use the `hookstorm-history` format at version 1. Export and import both apply the same result whitelist; the loader rejects unknown format versions and re-sanitizes loaded JSON before comparison. `history compare-file` compares a portable baseline to a run in the current SQLite database, allowing the database itself to remain ephemeral.
