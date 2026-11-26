# HookStorm

**Break your webhook delivery flow on purpose — before production does.**

A local-first webhook reliability CLI for retries, duplicate events, out-of-order delivery, timeouts, signature validation, and ambiguous acknowledgements.

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
| Provider-compatible signatures | delivery.signature + signature_provider: generic / stripe / github / shopify / standard_webhooks |
| Expected HTTP status | expect_status |
| Nested JSON response assertions | expect_json, expect_duplicate_json |
| Business-state / idempotency verification after delivery | verify.url, verify.expect_json |

The same event_id is preserved across retries and duplicate deliveries, so the receiver can recognize repeated deliveries. HookStorm also includes a commit-then-drop example: the receiver commits the business effect, closes the connection before acknowledging it, and then receives the same event again. The retry response and run-scoped verify probe prove whether the effect was applied only once. Set delivery.concurrency from 1 to 50 to run independent events concurrently; retries and duplicate deliveries for each event remain ordered, and reports retain scenario order.

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

### Bootstrap a webhook CI integration in your own project

From the root of the application repository (after installing HookStorm), run:

~~~bash
hookstorm init
~~~

The wizard creates `scenarios/webhook.yaml` and `.github/workflows/webhook-regression.yml`. In an interactive terminal it asks which signature format your staging receiver uses (`generic`, `stripe`, `github`, `shopify`, or `standard_webhooks`); non-interactive runs default to `generic`.

You can also choose the provider and event contract explicitly:

~~~bash
hookstorm init --signature-provider github \\
  --event-type issues.opened --event-id evt_issues_opened_001
~~~

Edit the generated scenario to match your actual payload and response. Then configure the GitHub `staging` Environment with `HOOKSTORM_TARGET_URL` as a variable and `HOOKSTORM_SECRET` as a secret. The wizard never writes endpoint URLs or signing secrets into generated files. Existing files are preserved; use `--force` only when you intentionally want to replace both generated files. Use `--no-workflow` to generate only the scenario.

The generated workflow targets a dedicated staging endpoint with the explicit `--allow-remote` opt-in, skips fork-originated PRs, and updates the baseline only from trusted successful runs on `main`. Before sending any webhook event, its preflight step validates the scenario, checks that the required environment variables are present, and verifies staging DNS/TCP/TLS connectivity. Protect the staging Environment with required reviewers before enabling it for same-repository pull requests. See [CI integration for application repositories](docs/ci-integration.md) for the complete setup.

Run the same checks locally before the first integration run. Set the endpoint and, for signed scenarios, the HMAC secret:

~~~bash
export HOOKSTORM_TARGET_URL='https://staging.example.com/webhooks/test'
export HOOKSTORM_SECRET='your-staging-secret'
hookstorm doctor scenarios/webhook.yaml
~~~

Add `--connect` for a compact socket check, or `--verbose` for detailed diagnostics:

~~~bash
hookstorm doctor scenarios/webhook.yaml --connect
hookstorm doctor scenarios/webhook.yaml --verbose
~~~

The default `doctor` command does not contact the endpoint. `--connect` resolves the host and opens a socket (plus a TLS handshake for HTTPS). `--verbose` implies `--connect` and reports each resolved IPv4 / IPv6 address, DNS lookup time, per-address TCP connection time, negotiated TLS version and cipher, certificate subject / issuer, and expiry date. It warns when an address fails while another succeeds, or when a certificate is nearing expiry. No HTTP request or webhook event is sent, so these checks cannot verify the application route, HTTP authentication, or the receiver's response.

For scripts and CI integrations, use the versioned JSON output:

~~~bash
hookstorm doctor scenarios/webhook.yaml --format json
hookstorm doctor scenarios/webhook.yaml --verbose --format json
~~~

The JSON document has `schema_version: 1`, an overall `status` / `ready` flag, a stable `summary`, a `network` section describing whether a connection check was requested and performed, and a `checks` array. Each check contains `status`, `label`, `message`, and `details`. The command still returns exit code `0` when ready and `1` when a required check fails; argument/configuration errors use exit code `2`. JSON is written to stdout without text banners, and secret values or the full target URL are not included.

When a scenario defines `verify.auth`, doctor checks that the named Bearer / API-key environment variable is present and follows the accepted credential format. It also requires HTTPS for authenticated non-loopback verification URLs. These are local checks only: doctor does not send the credential or test whether the application accepts it.

The demo receiver verifies the generic format and provider-compatible Stripe, GitHub, Shopify, and Standard Webhooks signatures. Try the provider scenarios:

~~~bash
hookstorm run examples/stripe-signature.yaml --target http://127.0.0.1:8000/webhook/signature/stripe
hookstorm run examples/github-signature.yaml --target http://127.0.0.1:8000/webhook/signature/github
hookstorm run examples/shopify-signature.yaml --target http://127.0.0.1:8000/webhook/signature/shopify
hookstorm run examples/standard-webhooks.yaml --target http://127.0.0.1:8000/webhook/signature/standard
~~~

- **Stripe:** `Stripe-Signature: t=...,v1=...`; signs `timestamp.raw_body`.
- **GitHub:** `X-Hub-Signature-256: sha256=...`; HMAC-SHA256 hex over the raw body.
- **Shopify:** `X-Shopify-Hmac-Sha256`; Base64-encoded HMAC-SHA256 over the raw body.
- **Standard Webhooks:** `webhook-id`, `webhook-timestamp`, and `webhook-signature: v1,<base64>`; signs `webhook-id.timestamp.raw_body` using an HMAC key decoded from a `whsec_<base64>` secret.

For the Standard Webhooks example, the local demo receiver prints a public, demo-only `whsec_` value at startup. Copy that value to `HOOKSTORM_SECRET` in the HookStorm terminal. Never reuse the demo key for a real endpoint. Standard Webhooks keeps `webhook-id` stable across retries and refreshes the timestamp and signature for each attempt.

Set `signature: invalid` to send a deliberately incorrect digest, or `signature: missing` to omit the provider header. The optional `signature_timestamp_offset_seconds` works with Stripe and Standard Webhooks so you can test stale or future timestamps in your own receiver.

~~~bash
# Linux / macOS
export HOOKSTORM_SECRET='local-demo-secret'
# Windows PowerShell
$env:HOOKSTORM_SECRET = 'local-demo-secret'
hookstorm run examples/invalid-signature.yaml --target http://127.0.0.1:8000/webhook/signature
~~~

The invalid-signature scenario expects 401, so a correct rejection is reported as a passing test.

For Standard Webhooks, copy the `whsec_` demo secret printed by `examples/receiver.py` into `HOOKSTORM_SECRET` in the HookStorm terminal. This key is intentionally public and for local demonstration only. Real integrations must use the secret issued for their endpoint.

## Reproduce a committed event with a lost acknowledgement

One of the most dangerous webhook edge cases is an ambiguous acknowledgement: the handler commits the order or payment, but the connection closes before the sender receives the HTTP response. The sender cannot know whether the operation committed, so it retries.

The included receiver has a dedicated endpoint that reproduces this exact sequence:

~~~bash
hookstorm run examples/commit-then-drop.yaml \
  --target http://127.0.0.1:8000/webhook/commit-then-drop
~~~

The first request is committed to SQLite and then its connection is closed without a response. HookStorm retries with the same event ID and body. The receiver reports that the retry was a duplicate and did not apply a second effect; the final `verify` GET confirms that only one row exists for the run.

This is a local demonstration of at-least-once delivery and idempotent processing. It does not claim that every receiver is transactional: use your own database-backed test endpoint to verify your application's transaction boundary.

For the failure model, receiver transaction boundary, and application-specific adaptation notes, see [Ambiguous webhook acknowledgements](docs/ambiguous-acknowledgements.md).

## Verify business state after delivery

A successful webhook response does not prove that an order, payment, or other business operation was written exactly once. Add a read-only verification endpoint to your test receiver and configure a verify block:

~~~yaml
verify:
  url: http://127.0.0.1:8000/__hookstorm/state
  expect_status: [200]
  expect_json:
    processed_events: 1
~~~

After all webhook deliveries finish, HookStorm performs one **GET** request. It sends the generated X-HookStorm-Run header so the receiver can return state scoped to that test run. The expected JSON is checked with the same recursive subset rules as delivery responses. A failed probe makes the overall run fail and is included in JSON, HTML, JUnit, and run history.

Try the database-backed demo:

~~~bash
hookstorm run examples/idempotency-verification.yaml \
  --target http://127.0.0.1:8000/webhook
~~~

The included receiver uses SQLite with a unique (run_id, event_id) key to simulate an idempotent business write. It exposes the run-scoped count at GET /__hookstorm/state. The scenario sends one event three times and verifies that only one row was created.

Verification URLs follow the same local-only default as webhook targets. Use --allow-remote only for an authorized test environment. HookStorm stores only the probe URL path (not query parameters), status, timing, and assertion outcome; it never stores the response body or authentication values.

Private verification endpoints can use environment-backed authentication. The scenario stores only the environment variable name, never the credential itself:

~~~yaml
verify:
  url: https://staging.example.test/__hookstorm/state
  expect_json:
    processed_events: 1
  auth:
    type: bearer
    token_env: HOOKSTORM_VERIFY_TOKEN
~~~

For an API key header, use `type: api_key` and `key_env`; the header defaults to `X-API-Key`, or set a safe custom header:

~~~yaml
  auth:
    type: api_key
    key_env: HOOKSTORM_VERIFY_API_KEY
    header: X-Client-Key
~~~

Set the environment variable in the shell before running HookStorm:

~~~bash
# Linux / macOS
export HOOKSTORM_VERIFY_TOKEN='your-staging-token'

# Windows PowerShell
$env:HOOKSTORM_VERIFY_TOKEN = 'your-staging-token'
~~~

HookStorm resolves and validates the credential before sending any webhook events. A missing, empty, or malformed value stops the run before traffic is sent. Credentials are sent only on the read-only verification GET; they are not included in terminal output, JSON / HTML / JUnit reports, or run history. Bearer credentials use the `Authorization: Bearer` header. API-key header names cannot override HookStorm-managed or hop-by-hop headers. Authenticated remote verification URLs must use HTTPS; HTTP is allowed only for localhost / loopback development endpoints.

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

### Portable baselines for CI

SQLite history is local to a runner. Export a sanitized JSON baseline to carry it between isolated CI jobs, then compare a new run with that file:

~~~bash
# Export the latest saved run
hookstorm history export latest --output .hookstorm-baselines/payment.json

# Run the same scenario in a later job after restoring that file
hookstorm run scenarios/payment.yaml --target http://127.0.0.1:8000/webhook \
  --history-db .hookstorm/history.sqlite3
hookstorm history compare-file .hookstorm-baselines/payment.json \
  --db .hookstorm/history.sqlite3 --latency-threshold-pct 20
~~~

`history export` writes a versioned JSON file using the same explicit field whitelist as SQLite. `history compare-file` compares that baseline with the latest run in the selected local database; use `--current RUN_ID` to select a different current run. The file excludes payloads, request / response headers, response bodies, and secrets, but retains event identifiers and the target URL path.

The repository's `.github/workflows/webhook-scenarios.yml` demonstrates the full GitHub Actions flow. It restores baseline files from the main-branch Actions cache, compares each PR scenario, and saves refreshed baselines only after a successful run on `main`. A new scenario is seeded on its first successful main-branch run. The built-in workflow sets a very high latency threshold to avoid flaky timing gates on shared runners; for a controlled application test environment, choose a stricter threshold such as 20%.

The comparison checks pass rate, failed deliveries, per-delivery latency, p95 delivery latency, and delivery coverage. Latency is calculated as the sum of HTTP attempt times for each delivery (retry backoff is excluded). A regression exits with code `1`, so the command can fail a CI step. Use `--no-history` on `hookstorm run` to skip persistence, `--history-db PATH` to choose a database for a run, or set `HOOKSTORM_HISTORY_DB` for a shared local default.

### Use HookStorm in another repository

A copy-ready GitHub Actions workflow and matching staging scenario are available in `examples/ci/`. Follow [the external CI integration guide](docs/ci-integration.md) to configure a staging endpoint, HMAC secret, cached baseline, and pull-request regression gate.

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
- [x] Staging preflight diagnostics for scenario, secrets, and endpoint connectivity
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
