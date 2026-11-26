# Architecture

HookStorm is intentionally small and local-first.

~~~text
YAML scenario
     |
     v
config.py ---- validate schema, limits, and headers
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
- runner.py: sends requests asynchronously with HTTPX, preserves event IDs, supports bounded concurrency across independent events, generates generic / Stripe / GitHub / Shopify HMAC headers from the exact request bytes, and checks nested JSON responses.
- report.py: generates JSON, JUnit XML, or self-contained HTML with elapsed-time attempt tracks, search, result filters, and expandable attempt details.
- history.py: persists a strict whitelist of run metadata and assertion outcomes to local SQLite, exports versioned sanitized JSON baselines, and compares local or portable baselines for pass-rate, coverage, and latency regressions.
- examples/receiver.py: standard-library local demo server; never use it as a public endpoint.
- pytest_export.py: generates pytest integration modules with an embedded, normalized scenario and runtime-only target / secret environment variables.
- scaffold.py: generates an application-specific starter YAML scenario and a pinned GitHub Actions staging workflow; preserves existing files unless the caller explicitly passes `--force`, and never embeds target URLs or signing secrets.

## Deliberate trade-offs

1. CLI before dashboard. The scenario runner and deterministic reports are the core value.
2. No hosted receiver. Users point HookStorm at their own test endpoint.
3. No implicit remote access. Loopback is the default; remote testing requires an explicit opt-in.
4. No raw payloads in reports. Reports focus on delivery behavior instead of copying business data.

## Current limitations

- Concurrency applies across independent events; retries and duplicate deliveries within one event remain sequential. With concurrency above 1, request completion order is intentionally nondeterministic.
- A timeout does not prove the receiver rolled back work. The handler may finish after the sender stops waiting.
- Provider signing formats match Stripe, GitHub, and Shopify webhook headers. These are request generators for authorized test endpoints, not a replacement for each provider's SDK or a general receiver-side verification library.
- JSON object assertions support recursive subset matching; arrays are compared exactly and in order.
- A passing status or JSON assertion does not prove the business side effect happened exactly once; verify it independently in your test environment.


## CI integration

- `--format junit` writes one JUnit testcase per event delivery, including duplicates; assertion failures and transport errors are distinguished.
- `.github/workflows/ci.yml` publishes the unit-test JUnit report from each OS / Python matrix job.
- `.github/workflows/webhook-scenarios.yml` runs the local demo receiver and representative YAML scenarios, compares each run with a cached main-branch baseline, and uploads the per-scenario JUnit reports.
- Baselines are exported as versioned JSON files and carried between ephemeral workflow runs using the GitHub Actions cache. Pull requests read the main-branch baseline but never update it; successful pushes to main refresh the cache.
- `examples/ci/webhook-regression.yml` is a consumer-facing staging template. It uses a caller-managed test endpoint and HMAC secret, skips fork-originated PRs, restores baselines read-only, and refreshes them only from trusted main-branch runs.


## Run history and regression model

Run history is local and opt-out. By default it is stored at `~/.hookstorm/history.sqlite3`; `HOOKSTORM_HISTORY_DB` or a per-command `--db` / `--history-db` option can override the path.

Only run metadata, event identifiers, statuses, attempt timing, and assertion outcomes are persisted. Request payloads, request / response headers, response bodies, and secrets are excluded by an explicit field whitelist. The sanitized result still retains the target URL path for identifying the tested endpoint; query strings are removed by the runner.

The `history compare` command compares deliveries by event type, event ID, and duplicate index. It reports newly failing deliveries, recovered deliveries, removed coverage, per-delivery latency changes, aggregate p95 latency changes, pass-rate changes, and total run duration. The default latency regression threshold is 20 percent. A detected regression returns exit code 1; command and database errors return exit code 2. The p95 metric uses per-delivery sums of HTTP attempt latency and excludes configured retry backoff.

Portable baseline files use the `hookstorm-history` format at version 1. Export and import both apply the same result whitelist; the loader rejects unknown format versions and re-sanitizes loaded JSON before comparison. `history compare-file` compares a portable baseline to a run in the current SQLite database, allowing the database itself to remain ephemeral.
