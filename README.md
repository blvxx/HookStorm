# HookStorm

**Break your webhook delivery flow on purpose — before production does.**

A local-first chaos testing CLI for retries, duplicate events, out-of-order delivery, timeouts, and HMAC signature failures.

[![CI](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml/badge.svg)](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## Why HookStorm?

Webhook integrations fail in ways that are easy to miss in happy-path tests:

- A provider retries after a 500, but your handler processes the same payment twice.
- Events arrive out of order and overwrite newer state with stale data.
- A signature is missing, invalid, or calculated from the wrong request bytes.
- A slow handler exceeds the sender's timeout, even though it eventually finishes.
- A receiver returns 429 or 503, and retry behavior is not what you expected.

HookStorm sends controlled webhook traffic to **your own endpoint** and checks responses against expectations. It is a test harness, not a hosted webhook inbox or production delivery service.

## Capabilities

| Failure mode | Scenario setting |
|---|---|
| Retry after transient HTTP errors | retry.max_attempts, retry.retry_on, retry.backoff_ms |
| Duplicate event delivery | delivery.duplicate_count |
| Out-of-order delivery | delivery.order: reverse |
| Concurrent event delivery | delivery.concurrency: 1–50 (default: 1) |
| Slow receiver / timeout | delivery.timeout_ms |
| Provider HMAC signature | delivery.signature + signature_provider: generic / stripe / github / shopify |
| Expected HTTP status | expect_status |
| Nested JSON response assertions | expect_json, expect_duplicate_json |

The same event_id is preserved across retries and duplicate deliveries, allowing an idempotency implementation to be checked rather than merely sending unrelated repeated requests. Set `delivery.concurrency` from 1 to 50 to run independent events concurrently; retries and duplicate deliveries for each event remain ordered, and reports retain scenario order.

## Quick start

Requires Python 3.11 or newer.

~~~bash
git clone https://github.com/blvxx/HookStorm.git
cd HookStorm
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
~~~

**Windows PowerShell:**

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
~~~

If PowerShell blocks activation, allow it for the current terminal only:

~~~powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
~~~

Start the included demo receiver in a second terminal:

~~~bash
python examples/receiver.py
~~~

Run a retry scenario against its flaky endpoint:

~~~bash
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky
~~~

HookStorm writes hookstorm-report.html by default. Open it in a browser to inspect delivery outcomes and attempt counts.

Validate a scenario without sending requests:

~~~bash
hookstorm validate examples/duplicate-delivery.yaml
~~~

Export a scenario as a pytest integration test:

~~~bash
hookstorm export-pytest examples/payment-retry.yaml --output tests/test_payment_webhook.py
~~~

The generated test reads its target from `HOOKSTORM_TARGET` and the optional HMAC secret from `HOOKSTORM_SECRET`. It does not embed an endpoint or secret. Remote targets remain blocked unless `HOOKSTORM_ALLOW_REMOTE=1` is explicitly set.

~~~bash
# Linux / macOS
HOOKSTORM_TARGET=http://127.0.0.1:8000/webhook/flaky pytest tests/test_payment_webhook.py

# Windows PowerShell
$env:HOOKSTORM_TARGET = "http://127.0.0.1:8000/webhook/flaky"
pytest tests/test_payment_webhook.py
~~~

## Try the scenarios

~~~bash
hookstorm run examples/duplicate-delivery.yaml --target http://127.0.0.1:8000/webhook
hookstorm run examples/out-of-order.yaml --target http://127.0.0.1:8000/webhook
hookstorm run examples/timeout.yaml --target http://127.0.0.1:8000/webhook/slow
~~~

The timeout example is intentionally expected to fail: it proves that HookStorm detects a receiver that takes longer than the configured timeout.

For signature examples, start the demo receiver with the same development secret in its terminal, then set it in the HookStorm terminal:

~~~bash
# Linux / macOS — set in both terminals
export HOOKSTORM_SECRET='local-demo-secret'
python examples/receiver.py
~~~

~~~powershell
# Windows PowerShell — set in both terminals
$env:HOOKSTORM_SECRET = "local-demo-secret"
python examples/receiver.py
~~~

The demo receiver verifies the generic format and provider-compatible Stripe, GitHub, and Shopify signatures. Try the provider scenarios:

~~~bash
hookstorm run examples/stripe-signature.yaml --target http://127.0.0.1:8000/webhook/signature/stripe
hookstorm run examples/github-signature.yaml --target http://127.0.0.1:8000/webhook/signature/github
hookstorm run examples/shopify-signature.yaml --target http://127.0.0.1:8000/webhook/signature/shopify
~~~

- **Stripe:** `Stripe-Signature: t=...,v1=...`; signs `timestamp.raw_body`.
- **GitHub:** `X-Hub-Signature-256: sha256=...`; HMAC-SHA256 hex over the raw body.
- **Shopify:** `X-Shopify-Hmac-Sha256`; Base64-encoded HMAC-SHA256 over the raw body.

Set `signature: invalid` to send a deliberately incorrect digest, or `signature: missing` to omit the provider header. Stripe's optional `signature_timestamp_offset_seconds` lets you test stale or future timestamp handling in your own receiver.

~~~bash
# Linux / macOS
export HOOKSTORM_SECRET='local-demo-secret'
# Windows PowerShell
$env:HOOKSTORM_SECRET = 'local-demo-secret'
hookstorm run examples/invalid-signature.yaml --target http://127.0.0.1:8000/webhook/signature
~~~

The invalid-signature scenario expects 401, so a correct rejection is reported as a passing test.

## Write your own scenario

~~~yaml
version: 1
name: Order webhook idempotency
delivery:
  concurrency: 4
  duplicate_count: 2
  order: in_order
  timeout_ms: 1500
  signature: valid
retry:
  max_attempts: 3
  backoff_ms: 200
  retry_on: [408, 429, 500, 502, 503, 504]
events:
  - id: order.created
    event_id: evt_order_1001
    payload:
      order_id: order_1001
      total: 12500
    expect_status: [200]
    expect_json: { duplicate: false }
    expect_duplicate_json: { duplicate: true }
~~~

See docs/scenario-format.md for the complete schema.

## Reports

~~~bash
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky --format html --output artifacts/payment-retry.html
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky --format json
~~~

Export delivery checks as JUnit XML for CI systems:

~~~bash
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky --format junit --output test-results/payment-retry.xml
~~~

JUnit output creates one testcase per event delivery (including duplicates), records assertion failures separately from transport errors, and omits payloads, secrets, and URL query strings. GitHub Actions runs the built-in webhook scenarios and uploads JUnit XML reports as workflow artifacts.

Reports include event IDs, delivery/attempt counts, elapsed attempt timing, status codes, latency, and pass/fail checks. The self-contained HTML report adds a searchable and filterable delivery timeline, attempt expansion, and pass/fail filters. Reports intentionally omit request payloads, request headers, response bodies, and secrets.

## Run history and regression detection

Every `hookstorm run` is saved to a local SQLite database by default:

- Windows: `%USERPROFILE%\\.hookstorm\\history.sqlite3`
- Linux / macOS: `~/.hookstorm/history.sqlite3`

The history stores run metadata and assertion outcomes only. Payloads, request headers, response bodies, and secrets are not persisted. Event IDs and the target URL path are retained for comparison; query strings are stripped by the runner. Keep the database local and review identifiers / URL paths before sharing it.

List recent runs:

~~~bash
hookstorm history list --limit 20
hookstorm history list --scenario "Payment webhook retry"
~~~

Inspect one run:

~~~bash
hookstorm history show RUN_ID
~~~

Compare a new run with a saved baseline:

~~~bash
hookstorm history compare BASELINE_RUN_ID
~~~

The current run defaults to the latest recorded run. You can select it explicitly and adjust the latency threshold:

~~~bash
hookstorm history compare BASELINE_RUN_ID --current CURRENT_RUN_ID --latency-threshold-pct 20
~~~

The comparison checks pass rate, failed deliveries, per-delivery latency, p95 delivery latency, and delivery coverage. Latency is calculated as the sum of HTTP attempt times for each delivery (retry backoff is excluded). A regression exits with code `1`, so the command can fail a CI step. Use `--no-history` on `hookstorm run` to skip persistence, `--history-db PATH` to choose a database for a run, or set `HOOKSTORM_HISTORY_DB` for a shared local default.

## Safety and privacy

- **Local-only by default.** Targets must resolve to loopback. A remote URL requires the explicit --allow-remote switch.
- Only send tests to systems you own or are authorized to test. Do not point chaos scenarios at a production payment endpoint.
- Redirects are not followed. Credentials embedded in target URLs are rejected.
- HMAC secrets are read from HOOKSTORM_SECRET (or a custom --secret-env variable), not from the scenario file.
- No cloud relay, tunnel, telemetry, account, or hosted service is involved.
- Reports omit payloads, headers, response bodies, secrets, and URL query strings. Event IDs and endpoint paths remain; review reports before sharing them.
- The demo receiver is for development only, not public exposure.

See SECURITY.md and docs/architecture.md for additional details.

## Development

~~~bash
python -m pip install -e '.[dev]'
ruff check .
pytest
~~~

CI tests Python 3.11, 3.12, and 3.13 on Linux, plus Python 3.12 on Windows and macOS. Every job runs lint, tests, and a package build.

## Roadmap

- [x] YAML scenario format and validation
- [x] Local-first CLI with safety guard
- [x] Retries, duplicates, reverse ordering, delays and timeouts
- [x] HMAC signature modes and response assertions
- [x] HTML / JSON reports
- [x] Local demo receiver and CI test suite
- [x] Bounded concurrent event delivery groups
- [x] JUnit XML reports for CI integrations
- [x] GitHub Actions webhook scenario workflow
- [x] Local SQLite run history and baseline comparison
- [x] Pass-rate, delivery-coverage, and latency regression detection
- [ ] Per-event scheduling
- [x] Export scenarios as pytest tests
- [ ] Interactive run timeline / optional local dashboard
- [x] Stripe, GitHub, and Shopify provider-style signature adapters

## Contributing

Bug reports, reproducible scenarios, documentation improvements, and focused pull requests are welcome. Read CONTRIBUTING.md before opening a PR.

## License

MIT. See LICENSE.
